import os
import json
import logging
import time
import threading
import asyncio
from collections import deque
from datetime import datetime

from flask import Flask, request, jsonify, send_from_directory, Response, stream_with_context
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from dotenv import load_dotenv
import requests
import anthropic
import websocket
try:
    from websockets.server import serve as _ws_serve
    _WS_AVAILABLE = True
except ImportError:
    _WS_AVAILABLE = False

from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

load_dotenv()

logging.basicConfig(level=os.environ.get('LOG_LEVEL', 'INFO'))
log = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app, origins=os.environ.get('CORS_ORIGINS', '*').split(','))

limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["200 per minute"] if os.environ.get('RATE_LIMIT_ENABLED', 'true') == 'true' else [],
    storage_uri=os.environ.get('REDIS_URL', 'memory://'),
)

anthropic_client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY', ''))

MIST_API_URL = os.environ.get('MIST_API_URL', 'https://api.mist.com/api/v1')
MIST_API_TOKEN = os.environ.get('MIST_API_TOKEN', '')
MIST_ORG_ID = os.environ.get('MIST_ORG_ID', '')

MIST_ALLOWED_HOSTS = {
    'api.mist.com', 'api.gc1.mist.com', 'api.ac2.mist.com', 'api.gc2.mist.com',
    'api.gc4.mist.com', 'api.eu.mist.com', 'api.gc3.mist.com', 'api.ac6.mist.com',
    'api.gc6.mist.com', 'api.ac5.mist.com', 'api.gc5.mist.com', 'api.gc7.mist.com',
}
CLAUDE_MODEL = os.environ.get('CLAUDE_MODEL', 'claude-sonnet-4-6')
WEBHOOK_SECRET = os.environ.get('WEBHOOK_SECRET', '')

N8N_URL = os.environ.get('N8N_URL', 'https://thewifijedi.app.n8n.cloud')

REQUEST_COUNT = Counter('mist_requests_total', 'Total requests', ['method', 'endpoint', 'status'])
REQUEST_LATENCY = Histogram('mist_request_latency_seconds', 'Request latency')

# In-memory automation store (use DB in production)
automations = {}
_automation_counter = [1]

# Ring buffers for n8n event/analysis feed (survives only while container runs)
_event_buffer    = deque(maxlen=50)
_analysis_buffer = deque(maxlen=20)


# ============================================================================
# WEBSOCKET SERVER  (real-time push to dashboard clients on :8765)
# ============================================================================

_ws_clients: set = set()
_ws_loop: asyncio.AbstractEventLoop | None = None


async def _ws_handler(websocket):
    _ws_clients.add(websocket)
    log.info('WS client connected (%d total)', len(_ws_clients))
    try:
        await websocket.send(json.dumps({
            'type': 'snapshot',
            'events': list(_event_buffer)[:10],
            'analyses': list(_analysis_buffer)[:5],
        }))
        async for message in websocket:
            if message == 'ping':
                await websocket.send(json.dumps({'type': 'pong'}))
    except Exception:
        pass
    finally:
        _ws_clients.discard(websocket)
        log.info('WS client disconnected (%d remaining)', len(_ws_clients))


async def _ws_broadcast_coro(payload: dict):
    if not _ws_clients:
        return
    msg = json.dumps(payload)
    dead: set = set()
    for client in list(_ws_clients):
        try:
            await client.send(msg)
        except Exception:
            dead.add(client)
    _ws_clients -= dead


def ws_broadcast(payload: dict):
    """Thread-safe broadcast from synchronous Flask handlers."""
    if _ws_loop is not None and _ws_loop.is_running():
        asyncio.run_coroutine_threadsafe(_ws_broadcast_coro(payload), _ws_loop)


def _start_ws_server():
    global _ws_loop
    if not _WS_AVAILABLE:
        log.warning('websockets not installed — skipping WS server')
        return
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _serve():
        try:
            async with _ws_serve(_ws_handler, '0.0.0.0', 8765):
                log.info('WebSocket server listening on :8765')
                global _ws_loop
                _ws_loop = loop
                await asyncio.Future()
        except OSError as e:
            if e.errno == 98:  # EADDRINUSE — another worker already owns the port
                log.info('WebSocket port 8765 already bound by another worker — WS broadcast disabled in this worker')
            else:
                log.error('WebSocket server failed: %s', e)

    loop.run_until_complete(_serve())


threading.Thread(target=_start_ws_server, daemon=True, name='ws-server').start()


# ============================================================================
# MIST API HELPERS
# ============================================================================

def get_token():
    """Prefer X-Mist-Token from the current request, fall back to env var."""
    return request.headers.get('X-Mist-Token') or MIST_API_TOKEN


_mist_api_counter = {'total': 0, 'session_start': __import__('time').time(), 'total_seconds': 0.0}
_mist_api_lock = __import__('threading').Lock()

def _mist_track(t0: float):
    elapsed = __import__('time').time() - t0
    with _mist_api_lock:
        _mist_api_counter['total'] += 1
        _mist_api_counter['total_seconds'] += elapsed

def get_mist_base_url():
    host = request.headers.get('X-Mist-Host', '').strip()
    if host and host in MIST_ALLOWED_HOSTS:
        return f'https://{host}/api/v1'
    return MIST_API_URL


def mist_request(method, path, token=None, **kwargs):
    tok = token or get_token()
    headers = {
        'Authorization': f'Token {tok}',
        'Content-Type': 'application/json',
    }
    headers.update(kwargs.pop('headers', {}))
    base_url = get_mist_base_url()
    t0 = __import__('time').time()
    try:
        return requests.request(method, f'{base_url}{path}', headers=headers, timeout=30, **kwargs)
    except requests.RequestException as e:
        log.error('Mist API request failed: %s', e)
        raise
    finally:
        _mist_track(t0)


# ============================================================================
# MCP TOOL FUNCTIONS
# ============================================================================

def find_mist_entity(entity_type, query='', org_id=None, site_id=None):
    org = org_id or MIST_ORG_ID
    try:
        if entity_type == 'sites':
            r = mist_request('GET', f'/orgs/{org}/sites')
        elif entity_type == 'devices':
            path = f'/sites/{site_id}/devices' if site_id else f'/orgs/{org}/devices'
            r = mist_request('GET', path)
        elif entity_type == 'wlans':
            path = f'/sites/{site_id}/wlans' if site_id else f'/orgs/{org}/wlans'
            r = mist_request('GET', path)
        elif entity_type == 'clients':
            if not site_id:
                return {'error': 'site_id required for clients'}
            r = mist_request('GET', f'/sites/{site_id}/stats/clients')
        else:
            return {'error': f'Unknown entity type: {entity_type}'}

        if r.status_code == 200:
            data = r.json()
            if query and isinstance(data, list):
                data = [d for d in data if query.lower() in json.dumps(d).lower()]
            return {'results': data, 'count': len(data) if isinstance(data, list) else 1}
        return {'error': f'API {r.status_code}', 'detail': r.text[:200]}
    except Exception as e:
        return {'error': str(e)}


def get_mist_config(resource_type, resource_id, org_id=None, site_id=None):
    org = org_id or MIST_ORG_ID
    try:
        if resource_type == 'site':
            r = mist_request('GET', f'/sites/{resource_id}')
        elif resource_type == 'device':
            path = f'/sites/{site_id}/devices/{resource_id}' if site_id else f'/orgs/{org}/devices/{resource_id}'
            r = mist_request('GET', path)
        elif resource_type == 'wlan':
            path = f'/sites/{site_id}/wlans/{resource_id}' if site_id else f'/orgs/{org}/wlans/{resource_id}'
            r = mist_request('GET', path)
        elif resource_type == 'org':
            r = mist_request('GET', f'/orgs/{org or resource_id}')
        else:
            return {'error': f'Unknown resource type: {resource_type}'}

        return {'config': r.json()} if r.status_code == 200 else {'error': f'API {r.status_code}'}
    except Exception as e:
        return {'error': str(e)}


def get_mist_stats(stat_type, site_id=None, device_id=None, org_id=None):
    org = org_id or MIST_ORG_ID
    try:
        if stat_type == 'site_summary':
            path = f'/sites/{site_id}/stats' if site_id else f'/orgs/{org}/stats/sites'
        elif stat_type == 'device':
            if device_id and site_id:
                path = f'/sites/{site_id}/devices/{device_id}/stats'
            else:
                path = f'/sites/{site_id}/stats/devices' if site_id else f'/orgs/{org}/stats/devices'
        elif stat_type == 'clients':
            path = f'/sites/{site_id}/stats/clients' if site_id else f'/orgs/{org}/stats/clients'
        elif stat_type == 'wlans':
            path = f'/sites/{site_id}/stats/wlans' if site_id else f'/orgs/{org}/stats/wlans'
        else:
            return {'error': f'Unknown stat type: {stat_type}'}

        r = mist_request('GET', path)
        return {'stats': r.json()} if r.status_code == 200 else {'error': f'API {r.status_code}'}
    except Exception as e:
        return {'error': str(e)}


def get_mist_insights(topic, context=''):
    return {'topic': topic, 'context': context, 'note': 'Analyze with available data tools'}


def search_mist_data(search_type, query='', site_id=None, start=None, end=None, org_id=None):
    org = org_id or MIST_ORG_ID
    params = {k: v for k, v in {'q': query, 'start': start, 'end': end}.items() if v}
    try:
        if search_type == 'events':
            path = f'/sites/{site_id}/events/device' if site_id else f'/orgs/{org}/logs'
        elif search_type == 'alarms':
            path = f'/sites/{site_id}/alarms' if site_id else f'/orgs/{org}/alarms'
        elif search_type == 'audit_logs':
            path = f'/orgs/{org}/logs'
        elif search_type == 'client_events':
            path = f'/sites/{site_id}/events/client' if site_id else f'/orgs/{org}/events/client'
        else:
            return {'error': f'Unknown search type: {search_type}'}

        r = mist_request('GET', path, params=params)
        return {'results': r.json()} if r.status_code == 200 else {'error': f'API {r.status_code}'}
    except Exception as e:
        return {'error': str(e)}


TOOL_MAP = {
    'find_mist_entity': find_mist_entity,
    'get_mist_config': get_mist_config,
    'get_mist_stats': get_mist_stats,
    'get_mist_insights': get_mist_insights,
    'search_mist_data': search_mist_data,
}

CLAUDE_TOOLS = [
    {
        "name": "find_mist_entity",
        "description": "Search for Mist entities: sites, devices (APs/switches/gateways), WLANs, or clients.",
        "input_schema": {
            "type": "object",
            "properties": {
                "entity_type": {"type": "string", "enum": ["sites", "devices", "wlans", "clients"]},
                "query": {"type": "string", "description": "Optional filter"},
                "org_id": {"type": "string"},
                "site_id": {"type": "string"},
            },
            "required": ["entity_type"],
        },
    },
    {
        "name": "get_mist_config",
        "description": "Get configuration for a Mist site, device, WLAN, or org.",
        "input_schema": {
            "type": "object",
            "properties": {
                "resource_type": {"type": "string", "enum": ["site", "device", "wlan", "org"]},
                "resource_id": {"type": "string"},
                "org_id": {"type": "string"},
                "site_id": {"type": "string"},
            },
            "required": ["resource_type", "resource_id"],
        },
    },
    {
        "name": "get_mist_stats",
        "description": "Get real-time stats: site_summary, device, clients, or wlans.",
        "input_schema": {
            "type": "object",
            "properties": {
                "stat_type": {"type": "string", "enum": ["site_summary", "device", "clients", "wlans"]},
                "site_id": {"type": "string"},
                "device_id": {"type": "string"},
                "org_id": {"type": "string"},
            },
            "required": ["stat_type"],
        },
    },
    {
        "name": "get_mist_insights",
        "description": "Get AI-powered insights about network performance, issues, or configurations.",
        "input_schema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "e.g. offline_devices, performance, security"},
                "context": {"type": "string"},
            },
            "required": ["topic"],
        },
    },
    {
        "name": "search_mist_data",
        "description": "Search event logs, alarms, audit_logs, or client_events.",
        "input_schema": {
            "type": "object",
            "properties": {
                "search_type": {"type": "string", "enum": ["events", "alarms", "audit_logs", "client_events"]},
                "query": {"type": "string"},
                "site_id": {"type": "string"},
                "start": {"type": "integer"},
                "end": {"type": "integer"},
                "org_id": {"type": "string"},
            },
            "required": ["search_type"],
        },
    },
]


# ============================================================================
# ROUTES
# ============================================================================

@app.route('/')
def index():
    return send_from_directory('static', 'index.html')


@app.route('/static/<path:filename>')
def static_files(filename):
    return send_from_directory('static', filename)


@app.route('/health')
def health():
    return jsonify({'status': 'healthy', 'timestamp': datetime.utcnow().isoformat()})


@app.route('/ready')
def ready():
    checks = {
        'api_key': bool(os.environ.get('ANTHROPIC_API_KEY')),
        'mist_token': bool(os.environ.get('MIST_API_TOKEN')),
    }
    ok = all(checks.values())
    return jsonify({'ready': ok, 'checks': checks}), 200 if ok else 503


@app.route('/metrics')
def metrics():
    return generate_latest(), 200, {'Content-Type': CONTENT_TYPE_LATEST}


@app.route('/api/v1/usage', methods=['GET'])
def api_usage():
    with _mist_api_lock:
        total   = _mist_api_counter['total']
        secs    = _mist_api_counter['total_seconds']
        uptime  = int(time.time() - _mist_api_counter['session_start'])
    return jsonify({'mist_api_calls': total, 'total_seconds': round(secs, 1), 'uptime_seconds': uptime})


@app.route('/api/v1/chat', methods=['POST'])
@limiter.limit("60 per minute")
def chat():
    data = request.get_json()
    if not data or 'query' not in data:
        return jsonify({'error': 'query required'}), 400

    messages = data.get('history', []) + [{'role': 'user', 'content': data['query']}]
    system = (
        f"You are an expert Mist Network assistant with access to the Mist Cloud API. "
        f"Org ID: {MIST_ORG_ID or 'not configured'}. "
        f"Mist API: {'connected' if MIST_API_TOKEN else 'not configured'}. "
        "Use the available tools to fetch real-time data. Be concise and actionable."
    )

    try:
        for _ in range(10):
            resp = anthropic_client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=4096,
                system=system,
                tools=CLAUDE_TOOLS,
                messages=messages,
            )

            if resp.stop_reason == 'end_turn':
                text = ''.join(b.text for b in resp.content if hasattr(b, 'text'))
                return jsonify({
                    'response': text,
                    'usage': {'input': resp.usage.input_tokens, 'output': resp.usage.output_tokens},
                })

            if resp.stop_reason == 'tool_use':
                messages.append({'role': 'assistant', 'content': resp.content})
                results = []
                for block in resp.content:
                    if block.type == 'tool_use':
                        fn = TOOL_MAP.get(block.name)
                        result = fn(**block.input) if fn else {'error': f'unknown tool {block.name}'}
                        results.append({'type': 'tool_result', 'tool_use_id': block.id, 'content': json.dumps(result)})
                messages.append({'role': 'user', 'content': results})
            else:
                break

        return jsonify({'error': 'max iterations reached'}), 500

    except anthropic.AuthenticationError:
        return jsonify({'error': 'Invalid Anthropic API key'}), 401
    except Exception as e:
        log.exception('Chat error')
        return jsonify({'error': str(e)}), 500


def mist_json(r):
    """Return (json_body, status_code), tolerating empty or non-JSON Mist responses."""
    if not r.content:
        return jsonify({'status': 'ok'}), r.status_code
    try:
        return jsonify(r.json()), r.status_code
    except Exception:
        return jsonify({'error': f'Mist API {r.status_code}', 'detail': r.text[:500]}), r.status_code


# Mist proxy endpoints
@app.route('/api/v1/sites', methods=['GET'])
def list_sites():
    return mist_json(mist_request('GET', f'/orgs/{MIST_ORG_ID}/sites'))


@app.route('/api/v1/sites/<site_id>/devices', methods=['GET'])
def list_devices(site_id):
    return mist_json(mist_request('GET', f'/sites/{site_id}/devices'))


@app.route('/api/v1/sites/<site_id>/devices/<device_id>', methods=['GET'])
def get_device(site_id, device_id):
    return mist_json(mist_request('GET', f'/sites/{site_id}/devices/{device_id}'))


@app.route('/api/v1/sites/<site_id>/devices/<device_id>/reboot', methods=['POST'])
def reboot_device(site_id, device_id):
    return mist_json(mist_request('POST', f'/sites/{site_id}/devices/{device_id}/reboot'))


@app.route('/api/v1/sites/<site_id>/stats', methods=['GET'])
def site_stats(site_id):
    return mist_json(mist_request('GET', f'/sites/{site_id}/stats', params=request.args))


@app.route('/api/v1/sites/<site_id>/stats/devices', methods=['GET'])
def device_stats(site_id):
    return mist_json(mist_request('GET', f'/sites/{site_id}/stats/devices', params=request.args))


@app.route('/api/v1/sites/<site_id>/devices/events/search', methods=['GET'])
def device_events_search(site_id):
    return mist_json(mist_request('GET', f'/sites/{site_id}/devices/events/search', params=request.args))


@app.route('/api/v1/sites/<site_id>/stats/clients', methods=['GET'])
def client_stats(site_id):
    return mist_json(mist_request('GET', f'/sites/{site_id}/stats/clients'))


@app.route('/api/v1/sites/<site_id>/wired_clients/search', methods=['GET'])
def wired_client_search(site_id):
    return mist_json(mist_request('GET', f'/sites/{site_id}/wired_clients/search', params=request.args))


@app.route('/api/v1/sites/<site_id>/nac_clients/search', methods=['GET'])
def nac_client_search(site_id):
    return mist_json(mist_request('GET', f'/sites/{site_id}/nac_clients/search', params=request.args))


@app.route('/api/v1/sites/<site_id>/wlans', methods=['GET', 'POST'])
def wlans(site_id):
    if request.method == 'GET':
        return mist_json(mist_request('GET', f'/sites/{site_id}/wlans'))
    return mist_json(mist_request('POST', f'/sites/{site_id}/wlans', json=request.get_json()))


@app.route('/api/v1/sites/<site_id>/wlans/<wlan_id>', methods=['GET', 'PUT', 'DELETE'])
def wlan(site_id, wlan_id):
    if request.method == 'GET':
        return mist_json(mist_request('GET', f'/sites/{site_id}/wlans/{wlan_id}'))
    if request.method == 'PUT':
        return mist_json(mist_request('PUT', f'/sites/{site_id}/wlans/{wlan_id}', json=request.get_json()))
    return mist_json(mist_request('DELETE', f'/sites/{site_id}/wlans/{wlan_id}'))


@app.route('/api/v1/orgs/<org_id>/alarms', methods=['GET'])
def alarms(org_id):
    return mist_json(mist_request('GET', f'/orgs/{org_id}/alarms'))


@app.route('/api/v1/orgs/<org_id>/events', methods=['GET'])
def events(org_id):
    return mist_json(mist_request('GET', f'/orgs/{org_id}/events'))


@app.route('/api/v1/webhooks/simulate', methods=['POST'])
def simulate_webhook():
    data = request.get_json() or {}
    return jsonify({
        'simulated': True,
        'payload': {
            'topic': data.get('event_type', 'device_down'),
            'events': [{
                'type': data.get('event_type', 'device_down'),
                'timestamp': int(time.time()),
                'org_id': MIST_ORG_ID,
                'site_id': data.get('site_id', 'simulated-site'),
                'device_id': data.get('device_id', 'simulated-device'),
                'data': data.get('data', {}),
            }],
        },
    })


@app.route('/api/v1/webhooks/receive', methods=['POST'])
def receive_webhook():
    payload = request.get_json()
    log.info('Webhook received: %s', json.dumps(payload)[:500])
    return jsonify({'received': True, 'timestamp': datetime.utcnow().isoformat()})


@app.route('/api/v1/automations', methods=['GET', 'POST'])
def automations_list():
    if request.method == 'GET':
        return jsonify(list(automations.values()))

    data = request.get_json()
    if not data or 'name' not in data:
        return jsonify({'error': 'name required'}), 400

    aid = str(_automation_counter[0])
    _automation_counter[0] += 1
    automation = {
        'id': aid,
        'name': data['name'],
        'trigger': data.get('trigger', {}),
        'actions': data.get('actions', []),
        'enabled': data.get('enabled', True),
        'created_at': datetime.utcnow().isoformat(),
        'executions': 0,
    }
    automations[aid] = automation
    return jsonify(automation), 201


@app.route('/api/v1/automations/<automation_id>', methods=['GET', 'PUT', 'DELETE'])
def automation_detail(automation_id):
    if automation_id not in automations:
        return jsonify({'error': 'not found'}), 404
    if request.method == 'GET':
        return jsonify(automations[automation_id])
    if request.method == 'PUT':
        automations[automation_id].update(request.get_json() or {})
        return jsonify(automations[automation_id])
    del automations[automation_id]
    return '', 204


@app.route('/api/v1/automations/<automation_id>/execute', methods=['POST'])
def execute_automation(automation_id):
    if automation_id not in automations:
        return jsonify({'error': 'not found'}), 404
    automations[automation_id]['executions'] += 1
    automations[automation_id]['last_executed'] = datetime.utcnow().isoformat()
    return jsonify({'executed': True, 'automation': automations[automation_id]})


@app.route('/api/v1/self', methods=['GET'])
def self_info():
    return mist_json(mist_request('GET', '/self'))


# Token validation — called by login screen
@app.route('/api/validate', methods=['GET'])
def validate_token():
    tok = request.headers.get('X-Mist-Token', '').strip()
    if not tok:
        return jsonify({'error': 'No token provided'}), 400
    try:
        r = mist_request('GET', '/self', token=tok)
        if r.status_code == 200:
            data = r.json()
            return jsonify({'email': data.get('email', ''), 'name': data.get('name', ''), 'privileges': data.get('privileges', [])})
        return jsonify({'error': 'Invalid token'}), 401
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# Org-level proxy routes (token-forwarding)
@app.route('/api/v1/orgs/<org_id>/sites', methods=['GET'])
def org_sites(org_id):
    return mist_json(mist_request('GET', f'/orgs/{org_id}/sites'))


@app.route('/api/v1/orgs/<org_id>/stats', methods=['GET'])
def org_stats(org_id):
    return mist_json(mist_request('GET', f'/orgs/{org_id}/stats'))


@app.route('/api/v1/orgs/<org_id>/wlans', methods=['GET'])
def org_wlans(org_id):
    return mist_json(mist_request('GET', f'/orgs/{org_id}/wlans'))


@app.route('/api/v1/orgs/<org_id>/devices/search', methods=['GET'])
def org_devices_search(org_id):
    return mist_json(mist_request('GET', f'/orgs/{org_id}/devices/search', params=request.args))


@app.route('/api/v1/orgs/<org_id>/clients/search', methods=['GET'])
def org_clients_search(org_id):
    return mist_json(mist_request('GET', f'/orgs/{org_id}/clients/search', params=request.args))


@app.route('/api/v1/orgs/<org_id>/troubleshoot', methods=['GET'])
def org_troubleshoot(org_id):
    return mist_json(mist_request('GET', f'/orgs/{org_id}/troubleshoot', params=request.args))


@app.route('/api/v1/orgs/<org_id>/marvis', methods=['POST'])
def org_marvis(org_id):
    return mist_json(mist_request('POST', f'/orgs/{org_id}/marvis', json=request.get_json()))


@app.route('/api/v1/orgs/<org_id>/marvis/action', methods=['GET'])
def org_marvis_action(org_id):
    return mist_json(mist_request('GET', f'/orgs/{org_id}/marvis/action', params=request.args))


@app.route('/api/v1/sites/<site_id>/sle/<path:sle_path>', methods=['GET'])
def site_sle(site_id, sle_path):
    return mist_json(mist_request('GET', f'/sites/{site_id}/sle/{sle_path}', params=request.args))


@app.route('/api/v1/sites/<site_id>/stats/devices/<device_id>', methods=['GET'])
def device_stats_by_id(site_id, device_id):
    return mist_json(mist_request('GET', f'/sites/{site_id}/stats/devices/{device_id}', params=request.args))


@app.route('/api/v1/orgs/<org_id>/logs', methods=['GET'])
def org_logs(org_id):
    return mist_json(mist_request('GET', f'/orgs/{org_id}/logs', params=request.args))


@app.route('/api/v1/orgs/<org_id>/nacrules', methods=['GET'])
def org_nacrules(org_id):
    return mist_json(mist_request('GET', f'/orgs/{org_id}/nacrules', params=request.args))


@app.route('/api/v1/orgs/<org_id>/nacrules/<rule_id>', methods=['GET', 'PUT', 'DELETE'])
def org_nacrule(org_id, rule_id):
    return mist_json(mist_request(request.method, f'/orgs/{org_id}/nacrules/{rule_id}',
                                   json=request.get_json() if request.method == 'PUT' else None))


@app.route('/api/v1/orgs/<org_id>/nactags', methods=['GET'])
def org_nactags(org_id):
    return mist_json(mist_request('GET', f'/orgs/{org_id}/nactags', params=request.args))


@app.route('/api/v1/orgs/<org_id>/insights/stream')
def insights_stream(org_id):
    """SSE proxy for Mist Org Insight WebSocket stream."""
    token = get_token()
    mist_ws_url = 'wss://api.mist.com/api-ws/v1/stream'
    subscribe_msg = json.dumps({'subscribe': f'/orgs/{org_id}/insights/summary'})

    def generate():
        ws = None
        try:
            ws = websocket.WebSocket()
            ws.connect(mist_ws_url, header=[f'Authorization: Token {token}'])
            ws.send(subscribe_msg)
            while True:
                try:
                    ws.settimeout(30)
                    data = ws.recv()
                    if data:
                        yield f'data: {data}\n\n'
                except websocket.WebSocketTimeoutException:
                    yield 'data: {"ping":true}\n\n'
                except Exception as e:
                    yield f'data: {{"error":"{str(e)}"}}\n\n'
                    break
        except Exception as e:
            yield f'data: {{"error":"connection failed: {str(e)}"}}\n\n'
        finally:
            if ws:
                try:
                    ws.close()
                except Exception:
                    pass

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive',
        }
    )


# ============================================================================
# N8N INTEGRATION
# ============================================================================

def _n8n_forward(path, payload):
    """Non-blocking fire-and-forget to n8n webhook."""
    try:
        requests.post(
            f'{N8N_URL}/webhook/{path}',
            json=payload,
            timeout=5,
            headers={'Content-Type': 'application/json'},
        )
    except Exception as e:
        log.warning('n8n forward to %s failed: %s', path, e)


@app.route('/webhook/mist', methods=['GET', 'POST', 'PUT', 'DELETE'])
def mist_webhook_receiver():
    """Receive Mist Cloud webhooks, buffer them, and forward to n8n."""
    secret = request.headers.get('X-Mist-Secret', '') or request.args.get('secret', '')
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        return jsonify({'error': 'Unauthorized'}), 401

    payload = request.get_json(silent=True) or {}
    payload['_received_at'] = int(time.time())
    payload['_source_ip'] = request.remote_addr

    _event_buffer.appendleft(payload)
    threading.Thread(target=_n8n_forward, args=('mist-events', payload), daemon=True).start()
    ws_broadcast({'type': 'event', 'payload': payload})
    log.info('Mist webhook received: topic=%s events=%s',
             payload.get('topic', '?'), len(payload.get('events', [])))
    return jsonify({'status': 'ok', 'forwarded_to': f'{N8N_URL}/webhook/mist-events'}), 200


@app.route('/webhook/n8n/analysis', methods=['POST'])
def n8n_analysis_callback():
    """n8n posts Claude's completed analysis here after processing a high-priority event."""
    data = request.get_json(silent=True) or {}
    data['_received_at'] = int(time.time())
    _analysis_buffer.appendleft(data)
    ws_broadcast({'type': 'analysis', 'payload': data})
    log.info('n8n analysis received: priority=%s', data.get('priority', '?'))
    return jsonify({'status': 'ok'}), 200


@app.route('/api/ws/status', methods=['GET'])
def ws_status():
    return jsonify({
        'connected_clients': len(_ws_clients),
        'server_running': _ws_loop is not None and _ws_loop.is_running(),
    })


@app.route('/api/n8n/status', methods=['GET'])
def n8n_status():
    """Ping n8n and return reachability."""
    try:
        # n8n cloud health endpoint
        r = requests.get(f'{N8N_URL}/healthz', timeout=5)
        return jsonify({'reachable': r.status_code < 500, 'http_status': r.status_code, 'url': N8N_URL})
    except Exception as e:
        return jsonify({'reachable': False, 'error': str(e), 'url': N8N_URL}), 200


@app.route('/api/n8n/events', methods=['GET'])
def n8n_events():
    """Return buffered raw Mist webhook events."""
    return jsonify(list(_event_buffer))


@app.route('/api/n8n/analyses', methods=['GET'])
def n8n_analyses():
    """Return buffered Claude analysis results from n8n."""
    return jsonify(list(_analysis_buffer))


@app.route('/api/n8n/chat', methods=['POST'])
@limiter.limit('30 per minute')
def n8n_chat():
    """Proxy a chat query through n8n's mist-chat workflow."""
    try:
        body = request.get_json() or {}
        body.setdefault('org_id', MIST_ORG_ID)
        r = requests.post(
            f'{N8N_URL}/webhook/chat',
            json=body,
            timeout=40,
            headers={'Content-Type': 'application/json'},
        )
        return mist_json(r)
    except requests.Timeout:
        return jsonify({'error': 'n8n timeout — workflow did not respond in 40 s'}), 504
    except Exception as e:
        return jsonify({'error': str(e)}), 502


@app.route('/api/n8n/action', methods=['POST'])
@limiter.limit('20 per minute')
def n8n_action():
    """Proxy a Mist API action through n8n's mist-api workflow (GET webhook)."""
    try:
        body = request.get_json() or {}
        body.setdefault('org_id', MIST_ORG_ID)
        r = requests.get(
            f'{N8N_URL}/webhook/mist-api-proxy',
            params=body,
            timeout=40,
        )
        try:
            return jsonify(r.json())
        except ValueError:
            return jsonify({'error': f'n8n returned non-JSON (HTTP {r.status_code})', 'body': r.text[:200]}), 502
    except requests.Timeout:
        return jsonify({'error': 'n8n timeout — workflow did not respond in 40 s'}), 504
    except Exception as e:
        return jsonify({'error': str(e)}), 502


# ============================================================================
# SELF-DRIVING PIPELINE  —  L1 Detection → L2 Diagnosis → L3 Remediation
# ============================================================================

def _sd_collect_telemetry(org_id: str) -> dict:
    """Pull live device stats per site (APs, switches, gateways) plus alarms."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # Capture token and base URL in the main Flask request thread before spawning
    # worker threads — Flask's request proxy is not available in child threads.
    _tok = get_token()
    _base_url = get_mist_base_url()

    def _get(path, **params):
        hdrs = {'Authorization': f'Token {_tok}', 'Content-Type': 'application/json'}
        t0 = time.time()
        try:
            return requests.get(f'{_base_url}{path}', headers=hdrs, params=params or None, timeout=15)
        finally:
            _mist_track(t0)

    telemetry: dict = {'org_id': org_id, 'collected_at': int(time.time())}

    # Alarms
    try:
        r = _get(f'/orgs/{org_id}/alarms', limit=100)
        data = r.json() if r.status_code == 200 else {}
        telemetry['alarms'] = data.get('results', data) if isinstance(data, dict) else data
    except Exception:
        telemetry['alarms'] = []

    # Sites list
    sites = []
    try:
        r = _get(f'/orgs/{org_id}/sites')
        raw = r.json() if r.status_code == 200 else []
        sites = raw if isinstance(raw, list) else raw.get('results', [])
        telemetry['sites'] = sites
    except Exception:
        telemetry['sites'] = []

    # Org aggregate stats (for cluster-level health flags)
    try:
        r = _get(f'/orgs/{org_id}/stats')
        telemetry['org_stats'] = r.json() if r.status_code == 200 else {}
    except Exception:
        telemetry['org_stats'] = {}

    # Per-site live device stats — /sites/:id/stats/devices?type=ap|switch|gateway
    all_aps: list = []
    all_switches: list = []
    all_gateways: list = []

    def _fetch_site(site):
        sid = site.get('id')
        sname = site.get('name', '—')
        if not sid:
            return [], [], []
        aps, switches, gateways = [], [], []
        for dtype, bucket in [('ap', aps), ('switch', switches), ('gateway', gateways)]:
            try:
                r = _get(f'/sites/{sid}/stats/devices', type=dtype)
                devs = r.json() if r.status_code == 200 else []
                if isinstance(devs, list):
                    for d in devs:
                        d['_site_id'] = sid
                        d['_site_name'] = sname
                    bucket.extend(devs)
            except Exception as e:
                log.debug('_fetch_site %s %s: %s', sid, dtype, e)
        return aps, switches, gateways

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_fetch_site, s): s for s in sites}
        for fut in as_completed(futures):
            try:
                aps, switches, gateways = fut.result()
                all_aps.extend(aps)
                all_switches.extend(switches)
                all_gateways.extend(gateways)
            except Exception:
                pass

    telemetry['aps'] = all_aps
    telemetry['switches'] = all_switches
    telemetry['gateways'] = all_gateways

    # Derive offline_devices from real live status (replaces stale search index)
    offline = []
    for dev in all_aps + all_switches + all_gateways:
        if dev.get('status') == 'disconnected':
            ts = dev.get('last_seen')
            dev['_last_seen_str'] = (
                datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M')
                if isinstance(ts, (int, float)) and ts > 0 else 'unknown'
            )
            offline.append(dev)
    telemetry['offline_devices'] = offline

    # Recent audit logs
    try:
        r = _get(f'/orgs/{org_id}/logs', limit=20)
        data = r.json() if r.status_code == 200 else {}
        telemetry['audit_logs'] = data.get('results', [])
    except Exception:
        telemetry['audit_logs'] = []

    return telemetry


def _sd_detect_issues(telemetry: dict) -> list:
    """L1: Detect issues from live per-site device stats, alarms, and org health."""
    issues = []
    seen_ids: set = set()

    def _add(issue):
        if issue['id'] not in seen_ids:
            seen_ids.add(issue['id'])
            issues.append(issue)

    # ── Offline devices (APs, switches, gateways from live stats) ────────────────
    for dev in telemetry.get('offline_devices', []):
        device_id = dev.get('id') or dev.get('mac', '?')
        hostname = dev.get('hostname') or dev.get('name') or device_id
        if isinstance(hostname, list):
            hostname = hostname[0] if hostname else device_id
        dtype = dev.get('type', 'ap')
        _add({
            'id': f"offline-{device_id}",
            'level': 'L1',
            'type': 'DEVICE_OFFLINE',
            'severity': 'high',
            'site_id': dev.get('site_id') or dev.get('_site_id'),
            'site_name': dev.get('_site_name') or dev.get('site_name', '—'),
            'device_id': device_id,
            'device_name': hostname,
            'device_type': dtype,
            'model': dev.get('model', '—'),
            'detail': f"{dtype.upper()} offline: {hostname} ({dev.get('model','?')}) — last seen {dev.get('_last_seen_str','unknown')}",
            'raw': {k: v for k, v in dev.items() if k not in ('wlans', 'port_stat')},
        })

    # ── RF issues from live AP radio stats ───────────────────────────────────────
    for ap in telemetry.get('aps', []):
        if ap.get('status') != 'connected':
            continue
        device_id = ap.get('id') or ap.get('mac', '?')
        hostname = ap.get('hostname') or ap.get('name') or device_id
        site_id = ap.get('site_id') or ap.get('_site_id')
        site_name = ap.get('_site_name', '—')
        rs = ap.get('radio_stat') or {}

        for band_key, band_label in [('band_6', '6 GHz'), ('band_5', '5 GHz'), ('band_24', '2.4 GHz')]:
            band = rs.get(band_key) or {}
            if not band:
                continue
            noise = band.get('noise_floor')
            util = band.get('util') or 0
            ch = band.get('channel', '?')

            if noise is not None and noise > -85:
                _add({
                    'id': f"rf-noise-{device_id}-{band_key}",
                    'level': 'L1', 'type': 'RF_INTERFERENCE', 'severity': 'medium',
                    'site_id': site_id, 'site_name': site_name,
                    'device_id': device_id, 'device_name': hostname,
                    'device_type': 'ap', 'model': ap.get('model', '—'),
                    'detail': f"High noise floor {noise} dBm on {band_label} ch{ch} at AP {hostname} — RF interference likely",
                    'raw': {'band': band_key, 'noise_floor': noise, 'channel': ch, 'util': util},
                })
            elif util > 80:
                _add({
                    'id': f"rf-util-{device_id}-{band_key}",
                    'level': 'L1', 'type': 'RF_INTERFERENCE', 'severity': 'medium',
                    'site_id': site_id, 'site_name': site_name,
                    'device_id': device_id, 'device_name': hostname,
                    'device_type': 'ap', 'model': ap.get('model', '—'),
                    'detail': f"Channel utilization {util}% on {band_label} ch{ch} at AP {hostname} — capacity issue",
                    'raw': {'band': band_key, 'util': util, 'channel': ch, 'noise_floor': noise},
                })

    # ── WAN interface down on connected gateways ─────────────────────────────────
    for gw in telemetry.get('gateways', []):
        if gw.get('status') != 'connected':
            continue
        device_id = gw.get('id') or gw.get('mac', '?')
        hostname = gw.get('hostname') or gw.get('name') or device_id
        site_id = gw.get('site_id') or gw.get('_site_id')
        for iface, wan in (gw.get('wan_interface_stat') or {}).items():
            if not wan.get('up', True):
                _add({
                    'id': f"wan-down-{device_id}-{iface}",
                    'level': 'L1', 'type': 'WAN_BROWNOUT', 'severity': 'critical',
                    'site_id': site_id, 'site_name': gw.get('_site_name', '—'),
                    'device_id': device_id, 'device_name': hostname,
                    'device_type': 'gateway', 'model': gw.get('model', '—'),
                    'detail': f"WAN interface {iface} is DOWN on gateway {hostname} (site: {gw.get('_site_name','?')})",
                    'raw': {'iface': iface, 'wan_stat': wan},
                })

    # ── Switch PoE overload ───────────────────────────────────────────────────────
    for sw in telemetry.get('switches', []):
        if sw.get('status') != 'connected':
            continue
        poe = sw.get('poe_stat') or {}
        draw = poe.get('current_draw', 0) or 0
        budget = poe.get('max_power', 0) or 0
        if budget > 0 and (draw / budget) > 0.90:
            device_id = sw.get('id') or sw.get('mac', '?')
            hostname = sw.get('hostname') or sw.get('name') or device_id
            _add({
                'id': f"poe-overload-{device_id}",
                'level': 'L1', 'type': 'POE_OVERLOAD', 'severity': 'high',
                'site_id': sw.get('site_id') or sw.get('_site_id'),
                'site_name': sw.get('_site_name', '—'),
                'device_id': device_id, 'device_name': hostname,
                'device_type': 'switch', 'model': sw.get('model', '—'),
                'detail': f"PoE overload on {hostname}: {draw:.1f}W / {budget:.1f}W ({draw/budget*100:.0f}% utilized)",
                'raw': poe,
            })

    # ── Alarm-based issues ────────────────────────────────────────────────────────
    for alarm in telemetry.get('alarms', []):
        atype = alarm.get('type', '')
        severity = 'critical' if alarm.get('severity') in ('critical', 'error') else 'medium'
        _add({
            'id': f"alarm-{alarm.get('id','?')}",
            'level': 'L1',
            'type': f"ALARM_{atype.upper().replace('-','_')}",
            'severity': severity,
            'site_id': alarm.get('site_id'),
            'site_name': alarm.get('site_name', '—'),
            'detail': alarm.get('message') or alarm.get('reason') or atype,
            'raw': alarm,
        })

    # ── Org-level AP offline cluster ──────────────────────────────────────────────
    stats = telemetry.get('org_stats', {})
    num_aps = stats.get('num_aps', 0) or 0
    num_aps_connected = stats.get('num_aps_connected', 0) or 0
    if num_aps and num_aps_connected < num_aps:
        offline_count = num_aps - num_aps_connected
        pct = round(offline_count / num_aps * 100)
        if pct >= 5:
            _add({
                'id': 'org-ap-offline',
                'level': 'L1',
                'type': 'AP_OFFLINE_CLUSTER',
                'severity': 'high' if pct >= 20 else 'medium',
                'site_id': None,
                'site_name': 'Org-wide',
                'detail': f"{offline_count}/{num_aps} APs offline ({pct}% of fleet)",
                'raw': stats,
            })

    return issues


def _sd_claude_diagnose(issues: list, telemetry: dict) -> dict:
    """L2: Ask Claude to classify root cause for each detected issue."""
    if not issues:
        return {'diagnoses': [], 'summary': 'No issues detected — network is healthy.'}

    issue_text = json.dumps(issues[:20], indent=2)

    # Build compact live-stats context for Claude
    def _ap_summary(ap):
        rs = ap.get('radio_stat') or {}
        radios = {}
        for bk, bl in [('band_6','6G'), ('band_5','5G'), ('band_24','2.4G')]:
            b = rs.get(bk) or {}
            if b:
                radios[bl] = {'ch': b.get('channel'), 'noise': b.get('noise_floor'), 'clients': b.get('num_clients')}
        return {
            'name': ap.get('hostname') or ap.get('name', '?'),
            'model': ap.get('model'), 'status': ap.get('status'),
            'site': ap.get('_site_name'), 'clients': ap.get('num_clients', 0),
            'radio': radios,
        }

    def _sw_summary(sw):
        poe = sw.get('poe_stat') or {}
        return {
            'name': sw.get('hostname') or sw.get('name', '?'),
            'model': sw.get('model'), 'status': sw.get('status'),
            'site': sw.get('_site_name'), 'uptime': sw.get('uptime'),
            'poe_draw_w': poe.get('current_draw'), 'poe_budget_w': poe.get('max_power'),
        }

    def _gw_summary(gw):
        wan = {k: {'ip': v.get('ip'), 'up': v.get('up'), 'latency_ms': v.get('latency')}
               for k, v in (gw.get('wan_interface_stat') or {}).items()}
        return {
            'name': gw.get('hostname') or gw.get('name', '?'),
            'model': gw.get('model'), 'status': gw.get('status'),
            'site': gw.get('_site_name'), 'wan_interfaces': wan,
        }

    device_context = {
        'aps':      [_ap_summary(a) for a in telemetry.get('aps', [])[:15]],
        'switches': [_sw_summary(s) for s in telemetry.get('switches', [])[:10]],
        'gateways': [_gw_summary(g) for g in telemetry.get('gateways', [])[:5]],
        'org_stats': telemetry.get('org_stats', {}),
    }
    device_context_text = json.dumps(device_context, indent=2)

    prompt = f"""You are a Mist Network NOC AI performing automated root-cause analysis.

DETECTED ISSUES ({len(issues)} total):
{issue_text}

LIVE DEVICE STATS (APs, switches, gateways from /stats/devices):
{device_context_text}

For each issue, classify the root cause into one of:
RF_INTERFERENCE, AP_OFFLINE, AUTH_FAILURE, DHCP_ISSUE, UPSTREAM_SWITCH, WAN_BROWNOUT, FIRMWARE_BUG, CONFIG_ERROR, POE_OVERLOAD, UNKNOWN

Use the live device stats to correlate patterns — e.g. multiple APs offline on the same switch suggests UPSTREAM_SWITCH, WAN interface down means WAN_BROWNOUT, high noise + high utilization = RF_INTERFERENCE.

Return a JSON object with:
{{
  "diagnoses": [
    {{
      "issue_id": "<id from above>",
      "root_cause": "<classification>",
      "confidence": <0-100>,
      "explanation": "<1-2 sentence reason citing specific device stats>",
      "recommended_action": "<specific actionable step>",
      "auto_remediable": <true|false>
    }}
  ],
  "summary": "<2-3 sentence overall network health summary citing real device names/sites>",
  "critical_count": <number of critical issues>
}}

Respond with valid JSON only."""

    try:
        resp = anthropic_client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=2048,
            messages=[{'role': 'user', 'content': prompt}],
        )
        text = resp.content[0].text.strip()
        # strip markdown fences if present
        if text.startswith('```'):
            text = text.split('\n', 1)[1].rsplit('```', 1)[0]
        return json.loads(text)
    except Exception as e:
        log.warning('Claude diagnosis failed: %s', e)
        err_str = str(e)
        credits_low = 'credit balance is too low' in err_str or 'insufficient_quota' in err_str
        fallback_diags = []
        for iss in issues:
            remediable = iss['type'] in ('DEVICE_OFFLINE', 'AP_OFFLINE')
            fallback_diags.append({
                'issue_id': iss['id'],
                'root_cause': 'AP_OFFLINE' if iss['type'] == 'DEVICE_OFFLINE' else iss['type'],
                'confidence': 70,
                'explanation': iss['detail'],
                'recommended_action': 'Reboot AP to restore connectivity' if remediable else 'Manual review required',
                'auto_remediable': remediable,
            })
        if credits_low:
            summary = f'{len(issues)} issues detected. ⚠ Anthropic API credits exhausted — add credits at console.anthropic.com to enable AI diagnosis. Applying rule-based remediations.'
        else:
            summary = f'{len(issues)} issues detected. AI diagnosis unavailable ({type(e).__name__}) — applying rule-based fallback.'
        return {
            'diagnoses': fallback_diags,
            'summary': summary,
            'critical_count': sum(1 for i in issues if i.get('severity') in ('critical', 'high')),
            'ai_available': False,
        }


def _sd_apply_remediations(diagnoses: list, dry_run: bool = True, issues_by_id: dict = None) -> list:
    """L3: Execute remediations for auto_remediable issues."""
    actions = []
    issues_by_id = issues_by_id or {}

    REMEDIATION_MAP = {
        'AP_OFFLINE':       _remediate_ap_offline,
        'DEVICE_OFFLINE':   _remediate_ap_offline,
        'RF_INTERFERENCE':  _remediate_rf_interference,
        'AP_OFFLINE_CLUSTER': _remediate_cluster_offline,
        'WAN_BROWNOUT':     _remediate_cluster_offline,   # escalate to NOC
        'POE_OVERLOAD':     _remediate_cluster_offline,   # escalate to NOC
    }

    for diag in diagnoses:
        if not diag.get('auto_remediable'):
            actions.append({
                'issue_id': diag['issue_id'],
                'action': 'SKIPPED',
                'reason': 'Manual remediation required',
                'root_cause': diag.get('root_cause'),
                'recommendation': diag.get('recommended_action'),
            })
            continue

        # Enrich diagnosis with original issue data so remediators can access device_id/site_id
        orig = issues_by_id.get(diag.get('issue_id'), {})
        enriched = {
            **diag,
            'device_id': orig.get('device_id'),
            'site_id':   orig.get('site_id'),
            'raw':       orig.get('raw', {}),
            'device_name': orig.get('device_name', '?'),
        }

        fn = REMEDIATION_MAP.get(diag.get('root_cause'))
        if fn:
            result = fn(enriched, dry_run=dry_run)
        else:
            result = {
                'action': 'ALERT_SENT',
                'message': f"Alert queued for {diag.get('root_cause')} — {diag.get('recommended_action')}",
                'dry_run': dry_run,
            }
        result['issue_id'] = diag['issue_id']
        result['root_cause'] = diag.get('root_cause')
        actions.append(result)

    return actions


def _remediate_ap_offline(diag: dict, dry_run: bool = True) -> dict:
    # Prefer top-level enriched fields, fall back to raw dict
    device_id = (diag.get('device_id')
                 or diag.get('raw', {}).get('id')
                 or diag.get('raw', {}).get('mac'))
    site_id = (diag.get('site_id')
               or diag.get('raw', {}).get('site_id'))
    device_name = diag.get('device_name', device_id or '?')

    if dry_run:
        return {
            'action': 'DRY_RUN_REBOOT',
            'device_id': device_id,
            'device_name': device_name,
            'site_id': site_id,
            'dry_run': True,
            'message': f"[DRY RUN] Would reboot {device_name} ({device_id or '?'}) — site {site_id or '?'}",
        }

    if not (device_id and site_id):
        return {
            'action': 'SKIPPED',
            'reason': f"Missing device_id or site_id for {device_name}",
            'dry_run': False,
        }

    try:
        r = mist_request('POST', f'/sites/{site_id}/devices/{device_id}/reboot')
        return {
            'action': 'REBOOT_SENT',
            'device_id': device_id,
            'device_name': device_name,
            'site_id': site_id,
            'dry_run': False,
            'http_status': r.status_code,
            'message': f"Reboot command sent to {device_name} ({device_id})",
        }
    except Exception as e:
        return {'action': 'REBOOT_FAILED', 'device_id': device_id, 'error': str(e), 'dry_run': False}


def _remediate_rf_interference(diag: dict, dry_run: bool = True) -> dict:
    return {
        'action': 'DRY_RUN_RRM_RESET' if dry_run else 'RRM_RESET_QUEUED',
        'dry_run': dry_run,
        'message': f"{'[DRY RUN] ' if dry_run else ''}Trigger RRM optimization on affected site to resolve RF interference",
        'detail': diag.get('explanation', ''),
    }


def _remediate_cluster_offline(diag: dict, dry_run: bool = True) -> dict:
    return {
        'action': 'NOC_ALERT',
        'dry_run': dry_run,
        'message': f"{'[DRY RUN] ' if dry_run else ''}High-severity alert: {diag.get('explanation','')}",
        'escalate': True,
    }


@app.route('/api/v1/orgs/<org_id>/self-driving/scan', methods=['GET'])
def self_driving_scan(org_id):
    """L1: Scan org for issues using Mist telemetry."""
    t0 = time.time()
    telemetry = _sd_collect_telemetry(org_id)
    issues = _sd_detect_issues(telemetry)
    return jsonify({
        'level': 'L1',
        'org_id': org_id,
        'scanned_at': int(time.time()),
        'duration_ms': round((time.time() - t0) * 1000),
        'issues_found': len(issues),
        'issues': issues,
        'telemetry_summary': {
            'sites': len(telemetry.get('sites', [])),
            'alarms': len(telemetry.get('alarms', [])),
            'offline_devices': len(telemetry.get('offline_devices', [])),
        },
    })


@app.route('/api/v1/orgs/<org_id>/self-driving/diagnose', methods=['POST'])
def self_driving_diagnose(org_id):
    """L2: Claude root-cause analysis on provided or freshly scanned issues."""
    body = request.get_json(silent=True) or {}
    issues = body.get('issues')
    telemetry = body.get('telemetry', {})

    if issues is None:
        # scan first if caller didn't provide issues
        raw = _sd_collect_telemetry(org_id)
        issues = _sd_detect_issues(raw)
        telemetry = raw

    t0 = time.time()
    result = _sd_claude_diagnose(issues, telemetry)
    result['level'] = 'L2'
    result['org_id'] = org_id
    result['diagnosed_at'] = int(time.time())
    result['duration_ms'] = round((time.time() - t0) * 1000)
    result['issues_analyzed'] = len(issues)
    return jsonify(result)


@app.route('/api/v1/orgs/<org_id>/self-driving/remediate', methods=['POST'])
def self_driving_remediate(org_id):
    """L3: Apply automated remediations based on diagnosis."""
    body = request.get_json(silent=True) or {}
    diagnoses = body.get('diagnoses', [])
    dry_run = body.get('dry_run', True)
    # Caller may pass original issues so remediators can resolve device_id/site_id
    issues_list = body.get('issues', [])
    issues_by_id = {iss['id']: iss for iss in issues_list}

    if not diagnoses:
        return jsonify({'error': 'diagnoses required — run /diagnose first'}), 400

    t0 = time.time()
    actions = _sd_apply_remediations(diagnoses, dry_run=dry_run, issues_by_id=issues_by_id)

    taken = [a for a in actions if a.get('action') not in ('SKIPPED',)]
    skipped = [a for a in actions if a.get('action') == 'SKIPPED']

    return jsonify({
        'level': 'L3',
        'org_id': org_id,
        'remediated_at': int(time.time()),
        'duration_ms': round((time.time() - t0) * 1000),
        'dry_run': dry_run,
        'actions_taken': len(taken),
        'actions_skipped': len(skipped),
        'actions': actions,
    })


@app.route('/api/v1/orgs/<org_id>/self-driving/pipeline', methods=['POST'])
@limiter.limit('10 per minute')
def self_driving_pipeline(org_id):
    """Full self-driving pipeline: L1 scan → L2 diagnose → L3 remediate."""
    body = request.get_json(silent=True) or {}
    dry_run = body.get('dry_run', True)
    t_start = time.time()

    # L1
    telemetry = _sd_collect_telemetry(org_id)
    issues = _sd_detect_issues(telemetry)
    issues_by_id = {iss['id']: iss for iss in issues}

    # L2
    diagnosis = _sd_claude_diagnose(issues, telemetry)
    diagnoses = diagnosis.get('diagnoses', [])

    # L3
    actions = _sd_apply_remediations(diagnoses, dry_run=dry_run, issues_by_id=issues_by_id)

    elapsed = round((time.time() - t_start) * 1000)
    result = {
        'pipeline': 'L1→L2→L3',
        'org_id': org_id,
        'completed_at': int(time.time()),
        'total_duration_ms': elapsed,
        'dry_run': dry_run,
        'ai_available': diagnosis.get('ai_available', True),
        'l1_detection': {
            'issues_found': len(issues),
            'issues': issues,
            'telemetry_summary': {
                'sites': len(telemetry.get('sites', [])),
                'alarms': len(telemetry.get('alarms', [])),
                'offline_devices': len(telemetry.get('offline_devices', [])),
            },
        },
        'l2_diagnosis': {
            'summary': diagnosis.get('summary', ''),
            'critical_count': diagnosis.get('critical_count', 0),
            'diagnoses': diagnoses,
        },
        'l3_remediation': {
            'dry_run': dry_run,
            'actions_taken': sum(1 for a in actions if a.get('action') != 'SKIPPED'),
            'actions_skipped': sum(1 for a in actions if a.get('action') == 'SKIPPED'),
            'actions': actions,
        },
    }

    ws_broadcast({'type': 'self_driving_pipeline', 'payload': result})
    return jsonify(result)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000, debug=False)
