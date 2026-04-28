// Tatooine — Self-Driving Network Intelligence
// Pure Cloudflare stack: Worker + Durable Objects + KV + Workflows
// Replaces: Flask, Docker, Redis, PostgreSQL, nginx, n8n cloud

import { DurableObject, WorkflowEntrypoint, WorkflowStep, WorkflowEvent } from 'cloudflare:workers';
import Anthropic from '@anthropic-ai/sdk';

// ─── Types ────────────────────────────────────────────────────────────────────

export interface Env {
  ANTHROPIC_API_KEY: string;
  CLAUDE_MODEL: string;
  WEBHOOK_SECRET: string;
  MIST_API_TOKEN: string;
  MIST_API_URL: string;
  BROADCASTER: DurableObjectNamespace;
  AUTOMATIONS: KVNamespace;
  MIST_EVENT_WORKFLOW: Workflow;
  ASSETS: Fetcher;
}

interface MistEventParams {
  payload: Record<string, unknown>;
  token: string;
  base: string;
}

interface MistCtx {
  token: string;
  base: string;
}

interface Issue {
  id: string;
  level: string;
  type: string;
  severity: string;
  site_id: string | null;
  site_name: string;
  device_id?: string;
  device_name?: string;
  device_type?: string;
  model?: string;
  detail: string;
  raw?: unknown;
}

interface Diagnosis {
  issue_id: string;
  root_cause: string;
  confidence: number;
  explanation: string;
  recommended_action: string;
  auto_remediable: boolean;
}

// ─── Constants ────────────────────────────────────────────────────────────────

const MIST_ALLOWED_HOSTS = new Set([
  'api.mist.com', 'api.gc1.mist.com', 'api.ac2.mist.com', 'api.gc2.mist.com',
  'api.gc4.mist.com', 'api.eu.mist.com', 'api.gc3.mist.com', 'api.ac6.mist.com',
  'api.gc6.mist.com', 'api.ac5.mist.com', 'api.gc5.mist.com', 'api.gc7.mist.com',
]);

// ─── Response Helpers ─────────────────────────────────────────────────────────

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' },
  });
}

function handleCors(): Response {
  return new Response(null, {
    headers: {
      'Access-Control-Allow-Origin': '*',
      'Access-Control-Allow-Methods': 'GET, POST, PUT, DELETE, OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type, X-Mist-Token, X-Mist-Host, X-Tatooine-Confirm, Authorization',
      'Access-Control-Max-Age': '86400',
    },
  });
}

// ─── Mist API Proxy ───────────────────────────────────────────────────────────

function getMistBase(request: Request, env: Env): string {
  const host = (request.headers.get('X-Mist-Host') || '').trim();
  if (host && MIST_ALLOWED_HOSTS.has(host)) return `https://${host}/api/v1`;
  return env.MIST_API_URL || 'https://api.mist.com/api/v1';
}

function getToken(request: Request, env: Env): string {
  const auth = request.headers.get('Authorization') || '';
  if (auth.startsWith('Token ')) return auth.slice(6);
  return request.headers.get('X-Mist-Token') || env.MIST_API_TOKEN || '';
}

// Blocks live-write endpoints unless the caller explicitly acknowledges
// the action is intentional. Prevents accidental or CSRF-driven mutations.
function requireLiveConfirm(request: Request): Response | null {
  if (request.headers.get('X-Tatooine-Confirm') !== 'live') {
    return json({
      error: 'live_confirmation_required',
      message: 'This action modifies live Mist infrastructure. The request must include X-Tatooine-Confirm: live.',
    }, 403);
  }
  return null;
}

async function mistFetch(
  mctx: MistCtx,
  method: string,
  path: string,
  opts: { params?: Record<string, string>; body?: unknown } = {}
): Promise<Response> {
  const u = new URL(`${mctx.base}${path}`);
  if (opts.params) {
    for (const [k, v] of Object.entries(opts.params)) {
      if (v !== undefined && v !== '') u.searchParams.set(k, v);
    }
  }
  return fetch(u.toString(), {
    method,
    headers: { Authorization: `Token ${mctx.token}`, 'Content-Type': 'application/json' },
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
  });
}

async function mistProxy(
  mctx: MistCtx,
  method: string,
  path: string,
  opts: { params?: Record<string, string>; body?: unknown } = {}
): Promise<Response> {
  const r = await mistFetch(mctx, method, path, opts);
  const text = await r.text();
  return new Response(text || '{"status":"ok"}', {
    status: r.status,
    headers: { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' },
  });
}

// ─── Claude Tool Functions ────────────────────────────────────────────────────

async function findMistEntity(
  mctx: MistCtx,
  args: { entity_type: string; query?: string; org_id?: string; site_id?: string }
): Promise<unknown> {
  const { entity_type, query = '', org_id, site_id } = args;
  try {
    let path: string;
    if (entity_type === 'sites') path = `/orgs/${org_id}/sites`;
    else if (entity_type === 'devices') path = site_id ? `/sites/${site_id}/devices` : `/orgs/${org_id}/devices`;
    else if (entity_type === 'wlans') path = site_id ? `/sites/${site_id}/wlans` : `/orgs/${org_id}/wlans`;
    else if (entity_type === 'clients') {
      if (!site_id) return { error: 'site_id required for clients' };
      path = `/sites/${site_id}/stats/clients`;
    } else return { error: `Unknown entity type: ${entity_type}` };

    const r = await mistFetch(mctx, 'GET', path);
    if (r.status === 200) {
      let data = await r.json() as unknown;
      if (query && Array.isArray(data)) {
        data = data.filter((d: unknown) => JSON.stringify(d).toLowerCase().includes(query.toLowerCase()));
      }
      return { results: data, count: Array.isArray(data) ? data.length : 1 };
    }
    return { error: `Mist API ${r.status}` };
  } catch (e) { return { error: String(e) }; }
}

async function getMistConfig(
  mctx: MistCtx,
  args: { resource_type: string; resource_id: string; org_id?: string; site_id?: string }
): Promise<unknown> {
  const { resource_type, resource_id, org_id, site_id } = args;
  try {
    let path: string;
    if (resource_type === 'site') path = `/sites/${resource_id}`;
    else if (resource_type === 'device') path = site_id ? `/sites/${site_id}/devices/${resource_id}` : `/orgs/${org_id}/devices/${resource_id}`;
    else if (resource_type === 'wlan') path = site_id ? `/sites/${site_id}/wlans/${resource_id}` : `/orgs/${org_id}/wlans/${resource_id}`;
    else if (resource_type === 'org') path = `/orgs/${org_id || resource_id}`;
    else return { error: `Unknown resource type: ${resource_type}` };

    const r = await mistFetch(mctx, 'GET', path);
    return r.status === 200 ? { config: await r.json() } : { error: `Mist API ${r.status}` };
  } catch (e) { return { error: String(e) }; }
}

async function getMistStats(
  mctx: MistCtx,
  args: { stat_type: string; site_id?: string; device_id?: string; org_id?: string }
): Promise<unknown> {
  const { stat_type, site_id, device_id, org_id } = args;
  try {
    let path: string;
    if (stat_type === 'site_summary') path = site_id ? `/sites/${site_id}/stats` : `/orgs/${org_id}/stats/sites`;
    else if (stat_type === 'device') {
      if (device_id && site_id) path = `/sites/${site_id}/stats/devices/${device_id}`;
      else if (site_id) path = `/sites/${site_id}/stats/devices`;
      else return { error: 'site_id required for device stats' };
    } else if (stat_type === 'clients') {
      if (!site_id) return { error: 'site_id required for clients' };
      path = `/sites/${site_id}/stats/clients`;
    } else if (stat_type === 'wlans') {
      path = site_id ? `/sites/${site_id}/wlans` : `/orgs/${org_id}/wlans`;
    } else return { error: `Unknown stat type: ${stat_type}` };

    const r = await mistFetch(mctx, 'GET', path);
    return r.status === 200 ? { stats: await r.json() } : { error: `Mist API ${r.status}` };
  } catch (e) { return { error: String(e) }; }
}

async function getMistInsights(
  _mctx: MistCtx,
  args: { topic: string; context?: string }
): Promise<unknown> {
  return {
    topic: args.topic,
    message: 'Use get_mist_stats or find_mist_entity to pull live telemetry for this topic.',
    context: args.context || '',
  };
}

async function searchMistData(
  mctx: MistCtx,
  args: { search_type: string; query?: string; site_id?: string; start?: number; end?: number; org_id?: string }
): Promise<unknown> {
  const { search_type, query, site_id, start, end, org_id } = args;
  const params: Record<string, string> = {};
  if (query) params.q = query;
  if (start) params.start = String(start);
  if (end) params.end = String(end);
  try {
    let path: string;
    if (search_type === 'events') path = site_id ? `/sites/${site_id}/events/device` : `/orgs/${org_id}/logs`;
    else if (search_type === 'alarms') path = site_id ? `/sites/${site_id}/alarms` : `/orgs/${org_id}/alarms`;
    else if (search_type === 'audit_logs') path = `/orgs/${org_id}/logs`;
    else if (search_type === 'client_events') path = site_id ? `/sites/${site_id}/events/client` : `/orgs/${org_id}/events/client`;
    else return { error: `Unknown search type: ${search_type}` };

    const r = await mistFetch(mctx, 'GET', path, { params });
    return r.status === 200 ? { results: await r.json() } : { error: `Mist API ${r.status}` };
  } catch (e) { return { error: String(e) }; }
}

// ─── Claude Chat ──────────────────────────────────────────────────────────────

const CLAUDE_TOOLS: Anthropic.Tool[] = [
  {
    name: 'find_mist_entity',
    description: 'Search for Mist entities: sites, devices (APs/switches/gateways), WLANs, or clients.',
    input_schema: {
      type: 'object',
      properties: {
        entity_type: { type: 'string', enum: ['sites', 'devices', 'wlans', 'clients'] },
        query: { type: 'string', description: 'Optional filter string' },
        org_id: { type: 'string' },
        site_id: { type: 'string' },
      },
      required: ['entity_type'],
    },
  },
  {
    name: 'get_mist_config',
    description: 'Get configuration for a Mist site, device, WLAN, or org.',
    input_schema: {
      type: 'object',
      properties: {
        resource_type: { type: 'string', enum: ['site', 'device', 'wlan', 'org'] },
        resource_id: { type: 'string' },
        org_id: { type: 'string' },
        site_id: { type: 'string' },
      },
      required: ['resource_type', 'resource_id'],
    },
  },
  {
    name: 'get_mist_stats',
    description: 'Get real-time stats: site_summary, device, clients, or wlans.',
    input_schema: {
      type: 'object',
      properties: {
        stat_type: { type: 'string', enum: ['site_summary', 'device', 'clients', 'wlans'] },
        site_id: { type: 'string' },
        device_id: { type: 'string' },
        org_id: { type: 'string' },
      },
      required: ['stat_type'],
    },
  },
  {
    name: 'get_mist_insights',
    description: 'Get AI-powered insights about network performance, issues, or configurations.',
    input_schema: {
      type: 'object',
      properties: {
        topic: { type: 'string', description: 'e.g. offline_devices, performance, security' },
        context: { type: 'string' },
      },
      required: ['topic'],
    },
  },
  {
    name: 'search_mist_data',
    description: 'Search event logs, alarms, audit_logs, or client_events.',
    input_schema: {
      type: 'object',
      properties: {
        search_type: { type: 'string', enum: ['events', 'alarms', 'audit_logs', 'client_events'] },
        query: { type: 'string' },
        site_id: { type: 'string' },
        start: { type: 'integer' },
        end: { type: 'integer' },
        org_id: { type: 'string' },
      },
      required: ['search_type'],
    },
  },
];

async function handleChat(request: Request, env: Env, mctx: MistCtx): Promise<Response> {
  let data: { query?: string; history?: Anthropic.MessageParam[]; org_id?: string };
  try { data = await request.json() as typeof data; }
  catch { return json({ error: 'Invalid JSON body' }, 400); }

  if (!data.query) return json({ error: 'query required' }, 400);

  const client = new Anthropic({ apiKey: env.ANTHROPIC_API_KEY });
  const model = env.CLAUDE_MODEL || 'claude-sonnet-4-6';
  const orgId = data.org_id || 'not configured';

  const system = `You are an expert Mist Network assistant with access to the Mist Cloud API. Org ID: ${orgId}. Mist API: ${mctx.token ? 'connected' : 'not configured'}. Use the available tools to fetch real-time data. Be concise and actionable.`;

  const messages: Anthropic.MessageParam[] = [
    ...(data.history || []),
    { role: 'user', content: data.query },
  ];

  try {
    for (let i = 0; i < 10; i++) {
      const resp = await client.messages.create({ model, max_tokens: 4096, system, tools: CLAUDE_TOOLS, messages });

      if (resp.stop_reason === 'end_turn') {
        const text = resp.content
          .filter((b): b is Anthropic.TextBlock => b.type === 'text')
          .map((b) => b.text)
          .join('');
        return json({ response: text, usage: { input: resp.usage.input_tokens, output: resp.usage.output_tokens } });
      }

      if (resp.stop_reason === 'tool_use') {
        messages.push({ role: 'assistant', content: resp.content });
        const results: Anthropic.ToolResultBlockParam[] = [];
        for (const block of resp.content) {
          if (block.type !== 'tool_use') continue;
          const inp = block.input as Record<string, unknown>;
          let result: unknown;
          switch (block.name) {
            case 'find_mist_entity': result = await findMistEntity(mctx, inp as Parameters<typeof findMistEntity>[1]); break;
            case 'get_mist_config': result = await getMistConfig(mctx, inp as Parameters<typeof getMistConfig>[1]); break;
            case 'get_mist_stats': result = await getMistStats(mctx, inp as Parameters<typeof getMistStats>[1]); break;
            case 'get_mist_insights': result = await getMistInsights(mctx, inp as Parameters<typeof getMistInsights>[1]); break;
            case 'search_mist_data': result = await searchMistData(mctx, inp as Parameters<typeof searchMistData>[1]); break;
            default: result = { error: `unknown tool ${block.name}` };
          }
          results.push({ type: 'tool_result', tool_use_id: block.id, content: JSON.stringify(result) });
        }
        messages.push({ role: 'user', content: results });
      } else break;
    }
    return json({ error: 'max iterations reached' }, 500);
  } catch (e: unknown) {
    const msg = String(e);
    if (msg.includes('authentication') || msg.includes('401')) return json({ error: 'Invalid Anthropic API key' }, 401);
    return json({ error: msg }, 500);
  }
}

// ─── Self-Driving Pipeline ────────────────────────────────────────────────────

async function sdCollectTelemetry(mctx: MistCtx, orgId: string): Promise<Record<string, unknown>> {
  const telemetry: Record<string, unknown> = { org_id: orgId, collected_at: Math.floor(Date.now() / 1000) };

  // Top-level org data
  const [alarmsResp, sitesResp, orgStatsResp] = await Promise.all([
    mistFetch(mctx, 'GET', `/orgs/${orgId}/alarms`, { params: { limit: '100' } }),
    mistFetch(mctx, 'GET', `/orgs/${orgId}/sites`),
    mistFetch(mctx, 'GET', `/orgs/${orgId}/stats`),
  ]);

  const alarmsData = alarmsResp.status === 200 ? await alarmsResp.json() as Record<string, unknown> : {};
  telemetry.alarms = Array.isArray(alarmsData) ? alarmsData : ((alarmsData.results as unknown[]) || []);
  telemetry.org_stats = orgStatsResp.status === 200 ? await orgStatsResp.json() : {};

  const sites = sitesResp.status === 200 ? await sitesResp.json() as Array<Record<string, unknown>> : [];
  telemetry.sites = sites;

  // Per-site device stats — run all in parallel, cap at 20 sites
  const aps: unknown[] = [], switches: unknown[] = [], gateways: unknown[] = [], offline: unknown[] = [];

  await Promise.all(
    sites.slice(0, 20).flatMap((site) => {
      const sid = site.id as string;
      const sname = (site.name || sid) as string;
      const annotate = (devs: Array<Record<string, unknown>>, list: unknown[]) => {
        for (const d of devs) {
          d._site_id = sid;
          d._site_name = sname;
          list.push(d);
          if (d.status !== 'connected') offline.push(d);
        }
      };
      return [
        mistFetch(mctx, 'GET', `/sites/${sid}/stats/devices`, { params: { type: 'ap' } })
          .then(async (r) => r.status === 200 && annotate(await r.json() as Array<Record<string, unknown>>, aps))
          .catch(() => {}),
        mistFetch(mctx, 'GET', `/sites/${sid}/stats/devices`, { params: { type: 'switch' } })
          .then(async (r) => r.status === 200 && annotate(await r.json() as Array<Record<string, unknown>>, switches))
          .catch(() => {}),
        mistFetch(mctx, 'GET', `/sites/${sid}/stats/devices`, { params: { type: 'gateway' } })
          .then(async (r) => r.status === 200 && annotate(await r.json() as Array<Record<string, unknown>>, gateways))
          .catch(() => {}),
      ];
    })
  );

  telemetry.aps = aps;
  telemetry.switches = switches;
  telemetry.gateways = gateways;
  telemetry.offline_devices = offline;
  return telemetry;
}

function sdDetectIssues(telemetry: Record<string, unknown>): Issue[] {
  const issues: Issue[] = [];
  const seen = new Set<string>();
  const add = (issue: Issue) => { if (!seen.has(issue.id)) { seen.add(issue.id); issues.push(issue); } };

  // Offline devices
  for (const dev of (telemetry.offline_devices as Array<Record<string, unknown>>) || []) {
    const devId = String(dev.id || dev.mac || '?');
    const hostname = String(dev.hostname || dev.name || devId);
    add({
      id: `offline-${devId}`,
      level: 'L1', type: 'DEVICE_OFFLINE', severity: 'high',
      site_id: String(dev.site_id || dev._site_id || ''),
      site_name: String(dev._site_name || '—'),
      device_id: devId, device_name: hostname,
      device_type: String(dev.type || 'ap'), model: String(dev.model || '—'),
      detail: `${String(dev.type || 'ap').toUpperCase()} ${hostname} is offline (last seen: ${dev.last_seen || 'unknown'})`,
      raw: dev,
    });
  }

  // RF interference — AP channel utilization > 80%
  for (const ap of (telemetry.aps as Array<Record<string, unknown>>) || []) {
    if (ap.status !== 'connected') continue;
    const rs = (ap.radio_stat || {}) as Record<string, Record<string, unknown>>;
    const devId = String(ap.id || ap.mac || '?');
    for (const [bk, bl] of [['band_6', '6G'], ['band_5', '5G'], ['band_24', '2.4G']]) {
      const b = rs[bk] || {};
      const util = Number(b.util || b.channel_utilization || 0);
      if (util > 80) {
        add({
          id: `rf-util-${devId}-${bk}`,
          level: 'L1', type: 'RF_INTERFERENCE', severity: 'medium',
          site_id: String(ap.site_id || ap._site_id || ''),
          site_name: String(ap._site_name || '—'),
          device_id: devId, device_name: String(ap.hostname || ap.name || devId),
          device_type: 'ap', model: String(ap.model || '—'),
          detail: `Channel utilization ${util}% on ${bl} ch${b.channel || '?'} at AP ${String(ap.hostname || ap.name || devId)} — capacity issue`,
          raw: { band: bk, util, channel: b.channel, noise_floor: b.noise_floor },
        });
      }
    }
  }

  // WAN brownout — gateway interface down
  for (const gw of (telemetry.gateways as Array<Record<string, unknown>>) || []) {
    if (gw.status !== 'connected') continue;
    const devId = String(gw.id || gw.mac || '?');
    const hostname = String(gw.hostname || gw.name || devId);
    const wanStat = (gw.wan_interface_stat || {}) as Record<string, Record<string, unknown>>;
    for (const [iface, wan] of Object.entries(wanStat)) {
      if (!wan.up) {
        add({
          id: `wan-down-${devId}-${iface}`,
          level: 'L1', type: 'WAN_BROWNOUT', severity: 'critical',
          site_id: String(gw.site_id || gw._site_id || ''),
          site_name: String(gw._site_name || '—'),
          device_id: devId, device_name: hostname,
          device_type: 'gateway', model: String(gw.model || '—'),
          detail: `WAN interface ${iface} is DOWN on gateway ${hostname} (site: ${gw._site_name || '?'})`,
          raw: { iface, wan_stat: wan },
        });
      }
    }
  }

  // PoE overload — switch drawing > 90% of budget
  for (const sw of (telemetry.switches as Array<Record<string, unknown>>) || []) {
    if (sw.status !== 'connected') continue;
    const poe = (sw.poe_stat || {}) as Record<string, number>;
    const draw = poe.current_draw || 0;
    const budget = poe.max_power || 0;
    if (budget > 0 && draw / budget > 0.9) {
      const devId = String(sw.id || sw.mac || '?');
      const hostname = String(sw.hostname || sw.name || devId);
      add({
        id: `poe-overload-${devId}`,
        level: 'L1', type: 'POE_OVERLOAD', severity: 'high',
        site_id: String(sw.site_id || sw._site_id || ''),
        site_name: String(sw._site_name || '—'),
        device_id: devId, device_name: hostname,
        device_type: 'switch', model: String(sw.model || '—'),
        detail: `PoE overload on ${hostname}: ${draw.toFixed(1)}W / ${budget.toFixed(1)}W (${Math.round(draw / budget * 100)}% utilized)`,
        raw: poe,
      });
    }
  }

  // Alarm-based issues
  for (const alarm of (telemetry.alarms as Array<Record<string, unknown>>) || []) {
    const atype = String(alarm.type || '');
    const severity = ['critical', 'error'].includes(String(alarm.severity)) ? 'critical' : 'medium';
    add({
      id: `alarm-${alarm.id || '?'}`,
      level: 'L1', type: `ALARM_${atype.toUpperCase().replace(/-/g, '_')}`, severity,
      site_id: (alarm.site_id as string) || null,
      site_name: String(alarm.site_name || '—'),
      detail: String(alarm.message || alarm.reason || atype),
      raw: alarm,
    });
  }

  // Org-level AP offline cluster
  const orgStats = (telemetry.org_stats || {}) as Record<string, number>;
  const numAps = orgStats.num_aps || 0;
  const numApsConn = orgStats.num_aps_connected || 0;
  if (numAps && numApsConn < numAps) {
    const pct = Math.round((numAps - numApsConn) / numAps * 100);
    if (pct >= 5) {
      add({
        id: 'org-ap-offline', level: 'L1', type: 'AP_OFFLINE_CLUSTER',
        severity: pct >= 20 ? 'high' : 'medium',
        site_id: null, site_name: 'Org-wide',
        detail: `${numAps - numApsConn}/${numAps} APs offline (${pct}% of fleet)`,
        raw: orgStats,
      });
    }
  }

  return issues;
}

async function sdClaudeDiagnose(env: Env, issues: Issue[], telemetry: Record<string, unknown>): Promise<Record<string, unknown>> {
  if (!issues.length) return { diagnoses: [], summary: 'No issues detected — network is healthy.', critical_count: 0 };

  const client = new Anthropic({ apiKey: env.ANTHROPIC_API_KEY });
  const model = env.CLAUDE_MODEL || 'claude-sonnet-4-6';

  // Compact device summaries for Claude context
  const apSum = (a: Record<string, unknown>) => {
    const rs = (a.radio_stat || {}) as Record<string, Record<string, unknown>>;
    const radios: Record<string, unknown> = {};
    for (const [bk, bl] of [['band_6', '6G'], ['band_5', '5G'], ['band_24', '2.4G']]) {
      const b = rs[bk] || {};
      if (Object.keys(b).length) radios[bl] = { ch: b.channel, noise: b.noise_floor, clients: b.num_clients };
    }
    return { name: a.hostname || a.name, model: a.model, status: a.status, site: a._site_name, clients: a.num_clients || 0, radio: radios };
  };
  const swSum = (s: Record<string, unknown>) => {
    const poe = (s.poe_stat || {}) as Record<string, unknown>;
    return { name: s.hostname || s.name, model: s.model, status: s.status, site: s._site_name, poe_draw_w: poe.current_draw, poe_budget_w: poe.max_power };
  };
  const gwSum = (g: Record<string, unknown>) => {
    const wan: Record<string, unknown> = {};
    for (const [k, v] of Object.entries((g.wan_interface_stat || {}) as Record<string, Record<string, unknown>>))
      wan[k] = { ip: v.ip, up: v.up, latency_ms: v.latency };
    return { name: g.hostname || g.name, model: g.model, status: g.status, site: g._site_name, wan_interfaces: wan };
  };

  const ctx = {
    aps:      ((telemetry.aps      || []) as Array<Record<string, unknown>>).slice(0, 15).map(apSum),
    switches: ((telemetry.switches || []) as Array<Record<string, unknown>>).slice(0, 10).map(swSum),
    gateways: ((telemetry.gateways || []) as Array<Record<string, unknown>>).slice(0,  5).map(gwSum),
    org_stats: telemetry.org_stats || {},
  };

  const prompt = `You are a Mist Network NOC AI performing automated root-cause analysis.

DETECTED ISSUES (${issues.length} total):
${JSON.stringify(issues.slice(0, 20), null, 2)}

LIVE DEVICE STATS (APs, switches, gateways from /stats/devices):
${JSON.stringify(ctx, null, 2)}

For each issue, classify the root cause into one of:
RF_INTERFERENCE, AP_OFFLINE, AUTH_FAILURE, DHCP_ISSUE, UPSTREAM_SWITCH, WAN_BROWNOUT, FIRMWARE_BUG, CONFIG_ERROR, POE_OVERLOAD, UNKNOWN

Correlate patterns — multiple APs offline on the same switch suggests UPSTREAM_SWITCH; WAN interface down = WAN_BROWNOUT; high noise + high utilization = RF_INTERFERENCE.

Return a JSON object with:
{
  "diagnoses": [
    {
      "issue_id": "<id>",
      "root_cause": "<classification>",
      "confidence": <0-100>,
      "explanation": "<1-2 sentence reason citing specific device stats>",
      "recommended_action": "<specific actionable step>",
      "auto_remediable": <true|false>
    }
  ],
  "summary": "<2-3 sentence overall network health summary citing real device names/sites>",
  "critical_count": <number of critical issues>
}

Respond with valid JSON only.`;

  try {
    const resp = await client.messages.create({ model, max_tokens: 2048, messages: [{ role: 'user', content: prompt }] });
    let text = (resp.content[0] as Anthropic.TextBlock).text.trim();
    if (text.startsWith('```')) text = text.split('\n').slice(1).join('\n').replace(/```$/, '').trim();
    const parsed = JSON.parse(text) as Record<string, unknown>;
    return { ...parsed, claude_usage: { model, input: resp.usage.input_tokens, output: resp.usage.output_tokens } };
  } catch (e) {
    const errStr = String(e);
    const creditsLow = errStr.includes('credit balance') || errStr.includes('insufficient_quota');
    return {
      diagnoses: issues.map((iss) => ({
        issue_id: iss.id,
        root_cause: iss.type === 'DEVICE_OFFLINE' ? 'AP_OFFLINE' : iss.type,
        confidence: 70,
        explanation: iss.detail,
        recommended_action: iss.type === 'DEVICE_OFFLINE' ? 'Reboot AP to restore connectivity' : 'Manual review required',
        auto_remediable: iss.type === 'DEVICE_OFFLINE',
      })),
      summary: creditsLow
        ? `${issues.length} issues detected. ⚠ Anthropic API credits exhausted — rule-based remediations applied.`
        : `${issues.length} issues detected. AI diagnosis unavailable — rule-based fallback applied.`,
      critical_count: issues.filter((i) => i.severity === 'critical').length,
      ai_available: false,
    };
  }
}

async function sdApplyRemediations(
  mctx: MistCtx,
  diagnoses: Diagnosis[],
  dryRun: boolean,
  issuesById: Record<string, Issue>
): Promise<unknown[]> {
  return Promise.all(diagnoses.map(async (diag) => {
    const result: Record<string, unknown> = { issue_id: diag.issue_id, root_cause: diag.root_cause };
    const issue = issuesById[diag.issue_id];
    const devId = issue?.device_id;
    const siteId = issue?.site_id;
    const devName = issue?.device_name || devId || '?';

    if (!diag.auto_remediable) {
      return { ...result, action: 'NO_ACTION', message: `${diag.recommended_action} (manual intervention required)` };
    }

    if (diag.root_cause === 'AP_OFFLINE' || diag.root_cause === 'UPSTREAM_SWITCH') {
      if (dryRun) {
        return { ...result, action: 'DRY_RUN_REBOOT', dry_run: true, message: `[DRY RUN] Would reboot ${devName} (${devId || '?'}) — site ${siteId || '?'}` };
      }
      if (!devId || !siteId) return { ...result, action: 'SKIPPED', reason: `Missing device_id or site_id for ${devName}` };
      try {
        const r = await mistFetch(mctx, 'POST', `/sites/${siteId}/devices/${devId}/reboot`);
        return { ...result, action: 'REBOOT_SENT', http_status: r.status, message: `Reboot command sent to ${devName} (${devId})` };
      } catch (e) {
        return { ...result, action: 'REBOOT_FAILED', error: String(e) };
      }
    }

    if (diag.root_cause === 'RF_INTERFERENCE') {
      return { ...result, action: dryRun ? 'DRY_RUN_RRM_RESET' : 'RRM_RESET_QUEUED', dry_run: dryRun, message: `${dryRun ? '[DRY RUN] ' : ''}Trigger RRM optimization on affected site` };
    }

    if (['WAN_BROWNOUT', 'AP_OFFLINE_CLUSTER'].includes(diag.root_cause)) {
      return { ...result, action: 'NOC_ALERT', dry_run: dryRun, escalate: true, message: `${dryRun ? '[DRY RUN] ' : ''}High-severity alert: ${diag.explanation}` };
    }

    return { ...result, action: 'LOGGED', message: diag.recommended_action };
  }));
}

// ─── Org Insights SSE Stream ──────────────────────────────────────────────────

async function handleInsightsStream(mctx: MistCtx, orgId: string): Promise<Response> {
  const { readable, writable } = new TransformStream<Uint8Array, Uint8Array>();
  const writer = writable.getWriter();
  const enc = new TextEncoder();
  const write = (data: string) => writer.write(enc.encode(`data: ${data}\n\n`));

  // Connect to Mist WebSocket and stream back as SSE
  (async () => {
    try {
      const resp = await fetch('https://api.mist.com/api-ws/v1/stream', {
        headers: { Authorization: `Token ${mctx.token}`, Upgrade: 'websocket' },
      });
      // Cloudflare Workers WebSocket client API
      const ws = (resp as unknown as { webSocket: WebSocket | null }).webSocket;
      if (!ws) {
        await write(JSON.stringify({ error: 'WebSocket upgrade failed' }));
        await writer.close();
        return;
      }
      ws.accept();
      ws.send(JSON.stringify({ subscribe: `/orgs/${orgId}/insights/summary` }));

      let closed = false;
      const heartbeat = setInterval(() => {
        if (closed) { clearInterval(heartbeat); return; }
        write(JSON.stringify({ ping: true })).catch(() => clearInterval(heartbeat));
      }, 25000);

      ws.addEventListener('message', async (evt) => {
        await write(evt.data as string).catch(() => {});
      });
      ws.addEventListener('close', async () => {
        closed = true; clearInterval(heartbeat);
        await write(JSON.stringify({ error: 'stream closed' })).catch(() => {});
        await writer.close().catch(() => {});
      });
      ws.addEventListener('error', async () => {
        closed = true; clearInterval(heartbeat);
        await write(JSON.stringify({ error: 'stream error' })).catch(() => {});
        await writer.close().catch(() => {});
      });
    } catch (e) {
      await write(JSON.stringify({ error: `connection failed: ${String(e)}` })).catch(() => {});
      await writer.close().catch(() => {});
    }
  })();

  return new Response(readable, {
    headers: {
      'Content-Type': 'text/event-stream',
      'Cache-Control': 'no-cache',
      'Connection': 'keep-alive',
      'Access-Control-Allow-Origin': '*',
    },
  });
}

// ─── Mist Event Workflow ──────────────────────────────────────────────────────
// Replaces the n8n "Mist AI Ops — Claude + Webhooks" workflow.
// Receives every Mist webhook, runs durable Claude analysis on high-priority
// alarm events, and broadcasts the result to all dashboard WebSocket clients.
// Steps are individually retried so a transient Claude/network error never
// loses the event.

export class MistEventWorkflow extends WorkflowEntrypoint<Env, MistEventParams> {
  async run(event: WorkflowEvent<MistEventParams>, step: WorkflowStep): Promise<void> {
    const { payload, token, base } = event.payload;
    const topic = String(payload.topic || '');

    // Step 1 — already buffered in the DO by the caller; just log
    await step.do('log-event', async () => {
      console.log(`MistEventWorkflow: topic=${topic} events=${(payload.events as unknown[] || []).length}`);
      return { topic };
    });

    // Low-priority events are done — they're already in the event buffer
    if (topic !== 'alarms') return;

    // Step 2 — Claude alarm analysis (retried up to 3x with backoff)
    const analysis = await step.do('claude-analyze', {
      retries: { limit: 3, delay: '10 seconds', backoff: 'exponential' },
      timeout: '45 seconds',
    }, async () => {
      const client = new Anthropic({ apiKey: this.env.ANTHROPIC_API_KEY });
      const model = this.env.CLAUDE_MODEL || 'claude-sonnet-4-6';

      const events = payload.events as Array<Record<string, unknown>> || [];
      const prompt = [
        'Analyze this Mist network alarm event and provide a concise summary with recommended actions.',
        '',
        `Topic: ${topic}`,
        `Org ID: ${payload.org_id || 'unknown'}`,
        `Site ID: ${payload.site_id || 'unknown'}`,
        `Events (${events.length}):`,
        JSON.stringify(events.slice(0, 10), null, 2),
      ].join('\n');

      const resp = await client.messages.create({
        model,
        max_tokens: 1024,
        messages: [{ role: 'user', content: prompt }],
      });

      return {
        analysis: (resp.content[0] as Anthropic.TextBlock).text,
        topic,
        org_id: payload.org_id || '',
        site_id: payload.site_id || '',
        priority: 'high',
        event_count: events.length,
        analyzed_at: Math.floor(Date.now() / 1000),
      };
    });

    // Step 3 — Broadcast analysis to dashboard via Broadcaster DO
    await step.do('broadcast-analysis', async () => {
      const broadcasterId = this.env.BROADCASTER.idFromName('main');
      const broadcaster = this.env.BROADCASTER.get(broadcasterId);
      await broadcaster.fetch(new Request('http://do/append-analysis', {
        method: 'POST',
        body: JSON.stringify(analysis),
      }));
      console.log(`MistEventWorkflow: analysis broadcast complete for topic=${topic}`);
    });

    // Step 4 — If it's a critical alarm cluster, also trigger self-driving scan
    const alarms = (payload.events as Array<Record<string, unknown>> || [])
      .filter((e) => String(e.severity || '').toLowerCase() === 'critical');

    if (alarms.length >= 3 && payload.org_id && token) {
      await step.do('trigger-self-driving', {
        retries: { limit: 1, delay: '5 seconds', backoff: 'linear' },
        timeout: '90 seconds',
      }, async () => {
        const workerUrl = `${base.replace('/api/v1', '')}/api/v1/orgs/${payload.org_id}/self-driving/pipeline`;
        const r = await fetch(workerUrl, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-Mist-Token': token },
          body: JSON.stringify({ dry_run: true, source: 'workflow-auto' }),
        });
        console.log(`MistEventWorkflow: self-driving triggered, status=${r.status}`);
      });
    }
  }
}

// ─── Broadcaster Durable Object ───────────────────────────────────────────────
// Replaces the in-process WebSocket server (port 8765) and ring buffers.
// Uses the Hibernation API so the DO can be evicted between WS messages without
// dropping connections.

export class Broadcaster extends DurableObject {
  // In-memory ring buffers — survive WebSocket hibernation, reset on eviction
  private events: unknown[] = [];
  private analyses: unknown[] = [];

  async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    const p = url.pathname;

    if (p === '/append-event' && request.method === 'POST') {
      const evt = await request.json();
      this.events.unshift(evt);
      if (this.events.length > 50) this.events.length = 50;
      this.#broadcast({ type: 'event', payload: evt });
      return new Response('ok');
    }

    if (p === '/append-analysis' && request.method === 'POST') {
      const analysis = await request.json();
      this.analyses.unshift(analysis);
      if (this.analyses.length > 20) this.analyses.length = 20;
      this.#broadcast({ type: 'analysis', payload: analysis });
      return new Response('ok');
    }

    if (p === '/broadcast' && request.method === 'POST') {
      const payload = await request.json() as Record<string, unknown>;
      this.#broadcast(payload);
      return new Response('ok');
    }

    if (p === '/events') return Response.json(this.events, { headers: { 'Access-Control-Allow-Origin': '*' } });
    if (p === '/analyses') return Response.json(this.analyses, { headers: { 'Access-Control-Allow-Origin': '*' } });

    if (p === '/status') {
      return Response.json({
        connected_clients: this.ctx.getWebSockets().length,
        events_buffered: this.events.length,
        analyses_buffered: this.analyses.length,
      }, { headers: { 'Access-Control-Allow-Origin': '*' } });
    }

    // WebSocket upgrade — dashboard clients connect here for real-time push
    if (request.headers.get('Upgrade') === 'websocket') {
      const pair = new WebSocketPair();
      const [client, server] = Object.values(pair);
      this.ctx.acceptWebSocket(server);
      // Send buffered snapshot so the client catches up immediately
      server.send(JSON.stringify({ type: 'snapshot', events: this.events.slice(0, 10), analyses: this.analyses.slice(0, 5) }));
      return new Response(null, { status: 101, webSocket: client });
    }

    return new Response('not found', { status: 404 });
  }

  async webSocketMessage(ws: WebSocket, message: string | ArrayBuffer): Promise<void> {
    if (message === 'ping') ws.send(JSON.stringify({ type: 'pong' }));
  }

  async webSocketClose(ws: WebSocket, code: number): Promise<void> {
    ws.close(code, 'Connection closed');
  }

  #broadcast(payload: unknown): void {
    const msg = JSON.stringify(payload);
    for (const ws of this.ctx.getWebSockets()) {
      try { ws.send(msg); } catch { /* client gone */ }
    }
  }
}

// ─── Main Worker ──────────────────────────────────────────────────────────────

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    if (request.method === 'OPTIONS') return handleCors();

    const url = new URL(request.url);
    const path = url.pathname;
    const method = request.method;

    // Static files served directly by ASSETS binding
    if (path === '/' || path.startsWith('/static/') || /\.(svg|webp|jpg|png|ico|txt|html)$/.test(path)) {
      return env.ASSETS.fetch(request);
    }

    const mctx: MistCtx = { token: getToken(request, env), base: getMistBase(request, env) };
    const broadcaster = env.BROADCASTER.get(env.BROADCASTER.idFromName('main'));

    try {
      return await dispatch(path, method, url, request, env, ctx, mctx, broadcaster);
    } catch (e) {
      console.error('Unhandled error:', e);
      return json({ error: String(e) }, 500);
    }
  },
};

// ─── Route Dispatcher ─────────────────────────────────────────────────────────

async function dispatch(
  path: string,
  method: string,
  url: URL,
  request: Request,
  env: Env,
  ctx: ExecutionContext,
  mctx: MistCtx,
  broadcaster: DurableObjectStub
): Promise<Response> {
  const allQP = (): Record<string, string> => {
    const p: Record<string, string> = {};
    url.searchParams.forEach((v, k) => { p[k] = v; });
    return p;
  };
  let m: RegExpMatchArray | null;

  // ── Health ────────────────────────────────────────────────────────────────────
  if (path === '/health') return json({ status: 'ok', timestamp: Date.now() });
  if (path === '/ready') return json({ status: 'ok' });
  if (path === '/api/v1/usage') return json({ message: 'API usage tracking is handled per-session in the browser.' });

  // ── Chat ──────────────────────────────────────────────────────────────────────
  if (path === '/api/v1/chat' && method === 'POST') return handleChat(request, env, mctx);

  // ── Token validation ──────────────────────────────────────────────────────────
  if (path === '/api/validate' && method === 'GET') {
    const tok = (request.headers.get('X-Mist-Token') || '').trim();
    if (!tok) return json({ error: 'No token provided' }, 400);
    const r = await mistFetch({ token: tok, base: mctx.base }, 'GET', '/self');
    if (r.status === 200) {
      const d = await r.json() as Record<string, unknown>;
      const email = String(d.email || '');
      const domain = (email.split('@')[1] ?? '').toLowerCase();
      if (domain !== 'hpe.com' && domain !== 'juniper.net') {
        return json({ error: 'Access restricted to @hpe.com and @juniper.net accounts.' }, 403);
      }
      const privs = (d.privileges as Array<Record<string, unknown>>) || [];
      const orgPriv = privs.find((p) => p.scope === 'org') ?? privs[0] ?? null;
      return json({
        email,
        name: d.name || '',
        org_id: orgPriv?.org_id ?? '',
        role: orgPriv?.role ?? '',
        privileges: privs,
      });
    }
    return json({ error: 'Invalid token' }, 401);
  }

  // ── Logout ────────────────────────────────────────────────────────────────────
  if (path === '/api/v1/logout' && method === 'POST') {
    return json({ ok: true });
  }

  // ── Mist /self ────────────────────────────────────────────────────────────────
  if (path === '/api/v1/self' && method === 'GET') return mistProxy(mctx, 'GET', '/self');

  // ── Site-scoped routes (ordered from most-specific to least-specific) ─────────

  // /api/v1/sites/:sid/stats/devices/:did
  if ((m = path.match(/^\/api\/v1\/sites\/([^/]+)\/stats\/devices\/([^/]+)$/)))
    return mistProxy(mctx, method, `/sites/${m[1]}/stats/devices/${m[2]}`, { params: allQP() });

  // /api/v1/sites/:sid/stats/devices
  if ((m = path.match(/^\/api\/v1\/sites\/([^/]+)\/stats\/devices$/)))
    return mistProxy(mctx, method, `/sites/${m[1]}/stats/devices`, { params: allQP() });

  // /api/v1/sites/:sid/stats/clients
  if ((m = path.match(/^\/api\/v1\/sites\/([^/]+)\/stats\/clients$/)))
    return mistProxy(mctx, method, `/sites/${m[1]}/stats/clients`);

  // /api/v1/sites/:sid/stats
  if ((m = path.match(/^\/api\/v1\/sites\/([^/]+)\/stats$/)))
    return mistProxy(mctx, method, `/sites/${m[1]}/stats`, { params: allQP() });

  // /api/v1/sites/:sid/devices/events/search
  if ((m = path.match(/^\/api\/v1\/sites\/([^/]+)\/devices\/events\/search$/)))
    return mistProxy(mctx, method, `/sites/${m[1]}/devices/events/search`, { params: allQP() });

  // /api/v1/sites/:sid/devices/:did/reboot — live write, requires explicit confirmation
  if ((m = path.match(/^\/api\/v1\/sites\/([^/]+)\/devices\/([^/]+)\/reboot$/)) && method === 'POST') {
    const guard = requireLiveConfirm(request);
    if (guard) return guard;
    return mistProxy(mctx, 'POST', `/sites/${m[1]}/devices/${m[2]}/reboot`);
  }

  // /api/v1/sites/:sid/devices/:did
  if ((m = path.match(/^\/api\/v1\/sites\/([^/]+)\/devices\/([^/]+)$/)))
    return mistProxy(mctx, method, `/sites/${m[1]}/devices/${m[2]}`);

  // /api/v1/sites/:sid/devices
  if ((m = path.match(/^\/api\/v1\/sites\/([^/]+)\/devices$/)))
    return mistProxy(mctx, method, `/sites/${m[1]}/devices`);

  // /api/v1/sites/:sid/wired_clients/search
  if ((m = path.match(/^\/api\/v1\/sites\/([^/]+)\/wired_clients\/search$/)))
    return mistProxy(mctx, method, `/sites/${m[1]}/wired_clients/search`, { params: allQP() });

  // /api/v1/sites/:sid/nac_clients/search
  if ((m = path.match(/^\/api\/v1\/sites\/([^/]+)\/nac_clients\/search$/)))
    return mistProxy(mctx, method, `/sites/${m[1]}/nac_clients/search`, { params: allQP() });

  // /api/v1/sites/:sid/wlans/:wid
  if ((m = path.match(/^\/api\/v1\/sites\/([^/]+)\/wlans\/([^/]+)$/))) {
    const body = ['PUT', 'POST'].includes(method) ? await request.json() : undefined;
    return mistProxy(mctx, method, `/sites/${m[1]}/wlans/${m[2]}`, { body });
  }

  // /api/v1/sites/:sid/wlans
  if ((m = path.match(/^\/api\/v1\/sites\/([^/]+)\/wlans$/))) {
    const body = method === 'POST' ? await request.json() : undefined;
    return mistProxy(mctx, method, `/sites/${m[1]}/wlans`, { body });
  }

  // /api/v1/sites/:sid/sle/**
  if ((m = path.match(/^\/api\/v1\/sites\/([^/]+)\/sle\/(.+)$/)))
    return mistProxy(mctx, method, `/sites/${m[1]}/sle/${m[2]}`, { params: allQP() });

  // ── Self-Driving Pipeline (before generic org handler) ────────────────────────

  if ((m = path.match(/^\/api\/v1\/orgs\/([^/]+)\/self-driving\/scan$/)) && method === 'GET') {
    const orgId = m[1]; const t0 = Date.now();
    const telemetry = await sdCollectTelemetry(mctx, orgId);
    const issues = sdDetectIssues(telemetry);
    return json({
      level: 'L1', org_id: orgId, scanned_at: Math.floor(Date.now() / 1000), duration_ms: Date.now() - t0,
      issues_found: issues.length, issues,
      telemetry_summary: {
        sites: ((telemetry.sites as unknown[]) || []).length,
        alarms: ((telemetry.alarms as unknown[]) || []).length,
        offline_devices: ((telemetry.offline_devices as unknown[]) || []).length,
      },
    });
  }

  if ((m = path.match(/^\/api\/v1\/orgs\/([^/]+)\/self-driving\/diagnose$/)) && method === 'POST') {
    const orgId = m[1];
    let body: { issues?: Issue[]; telemetry?: Record<string, unknown> } = {};
    try { body = await request.json() as typeof body; } catch { /* ok */ }
    let issues = body.issues;
    let telemetry = body.telemetry || {};
    if (!issues) { telemetry = await sdCollectTelemetry(mctx, orgId); issues = sdDetectIssues(telemetry); }
    const t0 = Date.now();
    const result = await sdClaudeDiagnose(env, issues, telemetry);
    return json({ ...result, level: 'L2', org_id: orgId, diagnosed_at: Math.floor(Date.now() / 1000), duration_ms: Date.now() - t0, issues_analyzed: issues.length, claude_usage: result.claude_usage || null });
  }

  if ((m = path.match(/^\/api\/v1\/orgs\/([^/]+)\/self-driving\/remediate$/)) && method === 'POST') {
    const orgId = m[1];
    const body = await request.json() as { diagnoses?: Diagnosis[]; dry_run?: boolean; issues?: Issue[] };
    const diagnoses = body.diagnoses || [];
    if (!diagnoses.length) return json({ error: 'diagnoses required — run /diagnose first' }, 400);
    const dryRun = body.dry_run !== false;
    if (!dryRun) {
      const guard = requireLiveConfirm(request);
      if (guard) return guard;
    }
    const issuesById = Object.fromEntries((body.issues || []).map((i) => [i.id, i]));
    const t0 = Date.now();
    const actions = await sdApplyRemediations(mctx, diagnoses, dryRun, issuesById);
    const taken = actions.filter((a) => (a as Record<string, unknown>).action !== 'SKIPPED');
    return json({
      level: 'L3', org_id: orgId, remediated_at: Math.floor(Date.now() / 1000),
      duration_ms: Date.now() - t0, dry_run: dryRun,
      actions_taken: taken.length, actions_skipped: actions.length - taken.length, actions,
    });
  }

  if ((m = path.match(/^\/api\/v1\/orgs\/([^/]+)\/self-driving\/pipeline$/)) && method === 'POST') {
    const orgId = m[1];
    let body: { dry_run?: boolean } = {};
    try { body = await request.json() as typeof body; } catch { /* ok */ }
    const dryRun = body.dry_run !== false;
    if (!dryRun) {
      const guard = requireLiveConfirm(request);
      if (guard) return guard;
    }
    const t0 = Date.now();

    const telemetry = await sdCollectTelemetry(mctx, orgId);
    const issues = sdDetectIssues(telemetry);
    const issuesById = Object.fromEntries(issues.map((i) => [i.id, i]));
    const diagnosis = await sdClaudeDiagnose(env, issues, telemetry);
    const diagnoses = (diagnosis.diagnoses || []) as Diagnosis[];
    const actions = await sdApplyRemediations(mctx, diagnoses, dryRun, issuesById);

    const result = {
      pipeline: 'L1→L2→L3', org_id: orgId, completed_at: Math.floor(Date.now() / 1000),
      total_duration_ms: Date.now() - t0, dry_run: dryRun, ai_available: diagnosis.ai_available !== false,
      claude_usage: diagnosis.claude_usage || null,
      l1_detection: {
        issues_found: issues.length, issues,
        telemetry_summary: {
          sites: ((telemetry.sites as unknown[]) || []).length,
          alarms: ((telemetry.alarms as unknown[]) || []).length,
          offline_devices: ((telemetry.offline_devices as unknown[]) || []).length,
        },
      },
      l2_diagnosis: { summary: diagnosis.summary || '', critical_count: diagnosis.critical_count || 0, diagnoses },
      l3_remediation: {
        dry_run: dryRun,
        actions_taken: actions.filter((a) => (a as Record<string, unknown>).action !== 'SKIPPED').length,
        actions_skipped: actions.filter((a) => (a as Record<string, unknown>).action === 'SKIPPED').length,
        actions,
      },
    };

    ctx.waitUntil(
      broadcaster.fetch(new Request('http://do/broadcast', { method: 'POST', body: JSON.stringify({ type: 'self_driving_pipeline', payload: result }) }))
    );
    return json(result);
  }

  // /api/v1/orgs/:oid/insights/stream — SSE proxy for Mist org WebSocket
  if ((m = path.match(/^\/api\/v1\/orgs\/([^/]+)\/insights\/stream$/)))
    return handleInsightsStream(mctx, m[1]);

  // /api/v1/orgs/:oid/nacrules/:rid — PUT/DELETE with specific ID
  if ((m = path.match(/^\/api\/v1\/orgs\/([^/]+)\/nacrules\/([^/]+)$/))) {
    const body = method === 'PUT' ? await request.json() : undefined;
    return mistProxy(mctx, method, `/orgs/${m[1]}/nacrules/${m[2]}`, { body });
  }

  // Generic org catch-all — covers sites, stats, wlans, alarms, logs, marvis, nactags, etc.
  if ((m = path.match(/^\/api\/v1\/orgs\/([^/]+)\/(.+)$/))) {
    const body = ['POST', 'PUT'].includes(method) ? await request.json() : undefined;
    return mistProxy(mctx, method, `/orgs/${m[1]}/${m[2]}`, { params: allQP(), body });
  }

  // ── Hover-info (AI context hints with KV cache) ───────────────────────────────

  if (path === '/api/v1/hover-info' && method === 'POST') {
    let body: { action?: string } = {};
    try { body = await request.json() as typeof body; } catch { /* ok */ }
    const action = (body.action || '').trim().slice(0, 100);
    if (!action) return json({ error: 'action required' }, 400);

    const cacheKey = `hover:${action}`;
    const cached = await env.AUTOMATIONS.get(cacheKey);
    if (cached) return json({ text: cached, cached: true });

    const hintMap: Record<string, string> = {
      run_pipeline:      'Runs the full L1→L2→L3 self-driving pipeline: detects issues, asks Claude for root-cause diagnosis, and applies safe remediations.',
      refresh_aps:       'Fetches live AP stats from the Mist API — connection status, client counts, radio channel utilization, and noise floor for all access points.',
      refresh_switches:  'Pulls current switch stats including PoE draw, port status, and uplink health across all sites.',
      refresh_wan:       'Retrieves WAN gateway stats: uplink status, IP assignments, latency, and interface-level health.',
      refresh_wlans:     'Loads the WLAN (SSID) configuration list for the selected site or org — auth type, VLAN, bands, and enabled state.',
      send_chat:         'Sends your message to Claude, which has live access to your Mist org via API tools — it can look up devices, stats, alarms, and configs in real time.',
      get_sites:         'Fetches all sites in your Mist org with location and address details.',
      get_devices:       'Retrieves all devices in the selected site — APs, switches, and gateways with their current status.',
      get_clients:       'Lists active wireless clients on the selected site with RSSI, band, SSID, and IP.',
      get_alarms:        'Pulls open alarms for the org — severity, type, affected site, and timestamp.',
      restart_device:    'Sends a reboot command to the specified device via Mist API. Requires explicit confirmation header.',
      insights_stream:   'Opens a live WebSocket stream from the Broadcaster DO — receives real-time Mist events and Claude alarm analyses as they arrive.',
      load_log:          'Fetches recent audit log entries from Mist showing config changes, user actions, and system events.',
      load_nac:          'Lists NAC (Network Access Control) rules and client authorization policies.',
      troubleshoot:      'Opens the AI troubleshooter — describe a device or client issue and Claude will diagnose it using live Mist telemetry.',
    };

    if (hintMap[action]) {
      await env.AUTOMATIONS.put(cacheKey, hintMap[action], { expirationTtl: 604800 }); // 1 week
      return json({ text: hintMap[action], cached: false });
    }

    if (!env.ANTHROPIC_API_KEY) return json({ text: `${action.replace(/_/g,' ')} — Mist API action.`, cached: false });

    try {
      const client = new Anthropic({ apiKey: env.ANTHROPIC_API_KEY });
      const resp = await client.messages.create({
        model: 'claude-haiku-4-5-20251001',
        max_tokens: 120,
        messages: [{
          role: 'user',
          content: `You are a tooltip generator for the Tatooine Mist Network Intelligence dashboard. Write exactly 1-2 sentences (max 30 words) describing what the "${action.replace(/_/g,' ')}" action does in the context of Mist network management. Be specific and actionable. No markdown.`,
        }],
      });
      const text = (resp.content[0] as Anthropic.TextBlock).text.trim();
      await env.AUTOMATIONS.put(cacheKey, text, { expirationTtl: 604800 });
      return json({ text, cached: false, claude_usage: { model: 'claude-haiku-4-5-20251001', input: resp.usage.input_tokens, output: resp.usage.output_tokens } });
    } catch (e) {
      return json({ text: `${action.replace(/_/g,' ')} — Mist API action.`, cached: false, error: String(e) });
    }
  }

  // ── Automations (KV-backed CRUD) ──────────────────────────────────────────────

  if (path === '/api/v1/automations') {
    if (method === 'GET') {
      const listed = await env.AUTOMATIONS.list();
      const values = await Promise.all(listed.keys.map((k) => env.AUTOMATIONS.get(k.name)));
      return json(values.filter(Boolean).map((v) => JSON.parse(v!)));
    }
    if (method === 'POST') {
      const data = await request.json() as Record<string, unknown>;
      if (!data.name) return json({ error: 'name required' }, 400);
      const id = crypto.randomUUID();
      const automation = { id, name: data.name, trigger: data.trigger || {}, actions: data.actions || [], enabled: data.enabled !== false, created_at: new Date().toISOString(), executions: 0 };
      await env.AUTOMATIONS.put(id, JSON.stringify(automation));
      return json(automation, 201);
    }
  }

  if ((m = path.match(/^\/api\/v1\/automations\/([^/]+)\/execute$/)) && method === 'POST') {
    const val = await env.AUTOMATIONS.get(m[1]);
    if (!val) return json({ error: 'not found' }, 404);
    const auto = JSON.parse(val) as Record<string, unknown>;
    auto.executions = ((auto.executions as number) || 0) + 1;
    await env.AUTOMATIONS.put(m[1], JSON.stringify(auto));
    return json({ executed: true, automation: auto });
  }

  if ((m = path.match(/^\/api\/v1\/automations\/([^/]+)$/))) {
    const val = await env.AUTOMATIONS.get(m[1]);
    if (!val) return json({ error: 'not found' }, 404);
    if (method === 'GET') return json(JSON.parse(val));
    if (method === 'PUT') {
      const updated = { ...JSON.parse(val), ...await request.json() as Record<string, unknown> };
      await env.AUTOMATIONS.put(m[1], JSON.stringify(updated));
      return json(updated);
    }
    if (method === 'DELETE') { await env.AUTOMATIONS.delete(m[1]); return json({ deleted: true }); }
  }

  // ── Webhooks ──────────────────────────────────────────────────────────────────

  if (path === '/api/v1/webhooks/simulate' && method === 'POST') {
    const data = await request.json() as Record<string, unknown>;
    return json({
      simulated: true,
      payload: {
        topic: data.event_type || 'device_down',
        events: [{ type: data.event_type || 'device_down', timestamp: Math.floor(Date.now() / 1000), site_id: data.site_id || 'simulated-site', device_id: data.device_id || 'simulated-device', data: data.data || {} }],
      },
    });
  }

  if (path === '/api/v1/webhooks/receive' && method === 'POST') {
    return json({ received: true, timestamp: new Date().toISOString() });
  }

  if (path === '/webhook/mist' && ['GET', 'POST', 'PUT', 'DELETE'].includes(method)) {
    const secret = request.headers.get('X-Mist-Secret') || url.searchParams.get('secret') || '';
    if (env.WEBHOOK_SECRET && secret !== env.WEBHOOK_SECRET) return json({ error: 'Unauthorized' }, 401);
    const payload = method !== 'GET' ? await request.json() as Record<string, unknown> : {};
    payload._received_at = Math.floor(Date.now() / 1000);

    // Buffer event in Broadcaster DO (feeds WebSocket clients and /api/n8n/events)
    ctx.waitUntil(broadcaster.fetch(new Request('http://do/append-event', { method: 'POST', body: JSON.stringify(payload) })));

    // Dispatch Cloudflare Workflow for durable Claude analysis on high-priority events
    ctx.waitUntil(
      env.MIST_EVENT_WORKFLOW.create({
        id: `mist-${Date.now()}-${crypto.randomUUID().slice(0, 8)}`,
        params: { payload, token: mctx.token, base: mctx.base } satisfies MistEventParams,
      }).catch((e) => console.warn('Workflow dispatch failed:', e))
    );

    return json({ status: 'ok', workflow: 'dispatched' });
  }

  if (path === '/webhook/n8n/analysis' && method === 'POST') {
    const data = await request.json() as Record<string, unknown>;
    data._received_at = Math.floor(Date.now() / 1000);
    ctx.waitUntil(broadcaster.fetch(new Request('http://do/append-analysis', { method: 'POST', body: JSON.stringify(data) })));
    return json({ status: 'ok' });
  }

  // ── n8n & WebSocket status ────────────────────────────────────────────────────

  if (path === '/api/ws/status') {
    const r = await broadcaster.fetch(new Request('http://do/status'));
    return new Response(r.body, { headers: { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' } });
  }

  if (path === '/api/n8n/status') {
    // n8n replaced by native Cloudflare Workflows — report as always-healthy
    return json({ reachable: true, provider: 'cloudflare-workflows', workflow: 'mist-event-pipeline' });
  }

  if (path === '/api/n8n/events') {
    const r = await broadcaster.fetch(new Request('http://do/events'));
    return new Response(r.body, { headers: { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' } });
  }

  if (path === '/api/n8n/analyses') {
    const r = await broadcaster.fetch(new Request('http://do/analyses'));
    return new Response(r.body, { headers: { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' } });
  }

  // /api/n8n/chat — native Claude handler (replaces n8n chat workflow)
  if (path === '/api/n8n/chat' && method === 'POST') {
    return handleChat(request, env, mctx);
  }

  // /api/n8n/action — native Mist API action router (replaces n8n mist-api-proxy workflow)
  if (path === '/api/n8n/action' && method === 'POST') {
    const body = await request.json() as Record<string, string>;
    const { action, org_id, site_id, device_id } = body;
    switch (action) {
      case 'get_sites':      return mistProxy(mctx, 'GET',  `/orgs/${org_id}/sites`);
      case 'get_devices':    return mistProxy(mctx, 'GET',  `/sites/${site_id}/devices`);
      case 'get_clients':    return mistProxy(mctx, 'GET',  `/sites/${site_id}/stats/clients`);
      case 'get_alarms':     return mistProxy(mctx, 'GET',  `/orgs/${org_id}/alarms/search`);
      case 'restart_device': {
        const guard = requireLiveConfirm(request);
        if (guard) return guard;
        return mistProxy(mctx, 'POST', `/sites/${site_id}/devices/${device_id}/restart`);
      }
      default:               return json({ error: 'Unknown action', action }, 400);
    }
  }

  // WebSocket upgrade for dashboard real-time feed
  if (path === '/ws' && request.headers.get('Upgrade') === 'websocket') {
    return broadcaster.fetch(request);
  }

  // Everything else: fall through to ASSETS (SPA catch-all)
  return env.ASSETS.fetch(request);
}
