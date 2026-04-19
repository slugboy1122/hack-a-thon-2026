import os
import json
import logging
import time
from datetime import datetime

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from dotenv import load_dotenv
import requests
import anthropic
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
CLAUDE_MODEL = os.environ.get('CLAUDE_MODEL', 'claude-sonnet-4-6')

REQUEST_COUNT = Counter('mist_requests_total', 'Total requests', ['method', 'endpoint', 'status'])
REQUEST_LATENCY = Histogram('mist_request_latency_seconds', 'Request latency')

# In-memory automation store (use DB in production)
automations = {}
_automation_counter = [1]


# ============================================================================
# MIST API HELPERS
# ============================================================================

def mist_request(method, path, **kwargs):
    headers = {
        'Authorization': f'Token {MIST_API_TOKEN}',
        'Content-Type': 'application/json',
    }
    headers.update(kwargs.pop('headers', {}))
    try:
        return requests.request(method, f'{MIST_API_URL}{path}', headers=headers, timeout=30, **kwargs)
    except requests.RequestException as e:
        log.error('Mist API request failed: %s', e)
        raise


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


# Mist proxy endpoints
@app.route('/api/v1/sites', methods=['GET'])
def list_sites():
    r = mist_request('GET', f'/orgs/{MIST_ORG_ID}/sites')
    return jsonify(r.json()), r.status_code


@app.route('/api/v1/sites/<site_id>/devices', methods=['GET'])
def list_devices(site_id):
    r = mist_request('GET', f'/sites/{site_id}/devices')
    return jsonify(r.json()), r.status_code


@app.route('/api/v1/sites/<site_id>/devices/<device_id>', methods=['GET'])
def get_device(site_id, device_id):
    r = mist_request('GET', f'/sites/{site_id}/devices/{device_id}')
    return jsonify(r.json()), r.status_code


@app.route('/api/v1/sites/<site_id>/devices/<device_id>/reboot', methods=['POST'])
def reboot_device(site_id, device_id):
    r = mist_request('POST', f'/sites/{site_id}/devices/{device_id}/reboot')
    return jsonify(r.json() if r.content else {'status': 'ok'}), r.status_code


@app.route('/api/v1/sites/<site_id>/stats/devices', methods=['GET'])
def device_stats(site_id):
    r = mist_request('GET', f'/sites/{site_id}/stats/devices')
    return jsonify(r.json()), r.status_code


@app.route('/api/v1/sites/<site_id>/stats/clients', methods=['GET'])
def client_stats(site_id):
    r = mist_request('GET', f'/sites/{site_id}/stats/clients')
    return jsonify(r.json()), r.status_code


@app.route('/api/v1/sites/<site_id>/wlans', methods=['GET', 'POST'])
def wlans(site_id):
    if request.method == 'GET':
        r = mist_request('GET', f'/sites/{site_id}/wlans')
    else:
        r = mist_request('POST', f'/sites/{site_id}/wlans', json=request.get_json())
    return jsonify(r.json()), r.status_code


@app.route('/api/v1/sites/<site_id>/wlans/<wlan_id>', methods=['GET', 'PUT', 'DELETE'])
def wlan(site_id, wlan_id):
    if request.method == 'GET':
        r = mist_request('GET', f'/sites/{site_id}/wlans/{wlan_id}')
    elif request.method == 'PUT':
        r = mist_request('PUT', f'/sites/{site_id}/wlans/{wlan_id}', json=request.get_json())
    else:
        r = mist_request('DELETE', f'/sites/{site_id}/wlans/{wlan_id}')
    return jsonify(r.json() if r.content else {}), r.status_code


@app.route('/api/v1/orgs/<org_id>/alarms', methods=['GET'])
def alarms(org_id):
    r = mist_request('GET', f'/orgs/{org_id}/alarms')
    return jsonify(r.json()), r.status_code


@app.route('/api/v1/orgs/<org_id>/events', methods=['GET'])
def events(org_id):
    r = mist_request('GET', f'/orgs/{org_id}/events')
    return jsonify(r.json()), r.status_code


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
    r = mist_request('GET', '/self')
    return jsonify(r.json()), r.status_code


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000, debug=False)
