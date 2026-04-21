# Tatooine — Self-Driving Network Intelligence

**Mist Field PLM Hackathon 2026 · Self-Driving Network Track**

> Closes the loop from telemetry to action — detecting issues automatically, diagnosing root cause with Claude AI, and remediating problems without manual intervention.

**Live demo:** [https://tatooine.thewifijedi.com](https://tatooine.thewifijedi.com)

---

## Self-Driving Levels

| Level | Name | What it does |
|-------|------|-------------|
| **L1** | Detection | Polls live device stats (APs, switches, gateways) across every site in parallel; detects offline devices, RF interference, WAN brownouts, PoE overload |
| **L2** | Diagnosis | Claude AI (`claude-sonnet-4-6`) classifies root cause with confidence scoring, using real radio/WAN/PoE telemetry as context |
| **L3** | Remediation | Executes AP reboots, RRM resets, NOC escalations — dry-run safe |

```bash
# Run the full L1→L2→L3 pipeline
curl -X POST https://tatooine.thewifijedi.com/api/v1/orgs/<ORG_ID>/self-driving/pipeline \
  -H "X-Mist-Token: $MIST_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"dry_run": true}'
```

---

## Dashboard

A single-page app with 13 tabs covering every layer of the network:

| Tab | Description |
|-----|-------------|
| **Dashboard** | Network overview — Sites, APs, Switches, Gateways KPIs; site health table; live event feed; Marvis Actions; SLE scores |
| **AI Assistant** | Claude-powered chat with live Mist org access |
| **Access Points** | Live AP stats — radio channels, noise floor, clients per band, click-through for device detail |
| → Connected Clients | Wireless clients per site (`/stats/clients`) |
| **WLANs** | Org-level WLAN inventory |
| **Switches** | Switch fleet with PoE stats and port details |
| → Connected Clients | Wired clients per site (`/wired_clients/search`) |
| **WAN / Gateways** | Gateway fleet with live WAN interface status |
| **Access Assurance** | NAC rules — 802.1X, MAB, PSK |
| → Connected Clients | NAC-authenticated clients (`/nac_clients/search`) |
| **Self-Driving Pipeline** | L1→L2→L3 UI with progressive rendering and dry-run toggle |
| **n8n AI Ops** | Webhook status, Claude analyses, live event feed |
| **Org Insights** | Live Mist WebSocket stream |
| **Audit Log** | Full org event history |

---

## Self-Driving Pipeline API

| Endpoint | Level | Description |
|----------|-------|-------------|
| `GET /api/v1/orgs/:id/self-driving/scan` | L1 | Detect issues from live Mist telemetry |
| `POST /api/v1/orgs/:id/self-driving/diagnose` | L2 | Claude root-cause analysis |
| `POST /api/v1/orgs/:id/self-driving/remediate` | L3 | Execute automated actions |
| `POST /api/v1/orgs/:id/self-driving/pipeline` | L1→L2→L3 | Full pipeline, single call |

**Detected issue types:** `DEVICE_OFFLINE` · `RF_INTERFERENCE` · `WAN_BROWNOUT` · `POE_OVERLOAD` · `AP_OFFLINE_CLUSTER` · `ALARM_*`

**Root causes (Claude):** `RF_INTERFERENCE` · `AP_OFFLINE` · `UPSTREAM_SWITCH` · `WAN_BROWNOUT` · `FIRMWARE_BUG` · `CONFIG_ERROR` · `POE_OVERLOAD` · `UNKNOWN`

---

## Stack

| Component | Technology |
|-----------|-----------|
| Frontend | Single-file HTML/CSS/JS — KRAYT TERMINAL dark theme + Juniper blue light mode |
| Backend | Python 3.11 + Flask + gunicorn (1 worker, 8 threads) |
| AI | Anthropic Claude `claude-sonnet-4-6` |
| Automation | n8n (self-hosted, Cloudflare tunneled) |
| Real-time | WebSocket server (port 8765) + SSE stream |
| Data | PostgreSQL + Redis |
| Proxy | nginx + Cloudflare Tunnel |
| Fonts | Cinzel · Open Sans · Space Mono |

---

## Mist API Coverage

```
# Device stats (pipeline + dashboard)
GET /sites/:id/stats/devices?type=ap
GET /sites/:id/stats/devices?type=switch
GET /sites/:id/stats/devices?type=gateway
GET /sites/:id/stats/devices/:device_id?type=ap|switch|gateway

# Clients
GET /sites/:id/stats/clients              wireless
GET /sites/:id/wired_clients/search       wired
GET /sites/:id/nac_clients/search         Access Assurance

# Org-level
GET /orgs/:id/sites
GET /orgs/:id/wlans
GET /orgs/:id/alarms
GET /orgs/:id/stats
GET /orgs/:id/logs
GET /orgs/:id/nacrules
GET /orgs/:id/nactags
GET /orgs/:id/marvis/action

# SLE
GET /sites/:id/sle/*

# Org insights stream
GET /orgs/:id/insights/stream             SSE

# Remediation
POST /sites/:id/devices/:id/reboot
```

---

## Deployment

```bash
git clone https://github.com/slugboy1122/hack-a-thon-2026.git
cd hack-a-thon-2026
cp .env.example .env    # add MIST_API_TOKEN and ANTHROPIC_API_KEY
docker compose up -d
# Dashboard: http://localhost:8080
```

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `MIST_API_TOKEN` | Yes | Juniper Mist API token |
| `ANTHROPIC_API_KEY` | Yes | Anthropic Claude API key |
| `MIST_ORG_ID` | No | Default org (overridden by token auth) |
| `REDIS_URL` | No | Redis connection (default: in-memory) |
| `DATABASE_URL` | No | PostgreSQL connection URL |

### Docker Services

| Service | Port | Description |
|---------|------|-------------|
| `nginx` | 8080 | Reverse proxy |
| `mist-api` | 8000 | Flask/gunicorn backend |
| `websocket` | 8765 | Real-time push server |
| `n8n` | 5678 | Workflow automation |
| `postgres` | 5432 | Database |
| `redis` | 6379 | Cache / rate limiting |
| `cloudflared` | — | Cloudflare Tunnel |

---

## Auth

Token-only — paste a Mist API token and org privileges are derived from `/self`. Multi-org tokens show an org picker. No email or org ID required. Token is held in session memory only, never stored server-side.

---

## n8n Automation

A Cloudflare-tunneled n8n instance handles asynchronous AI ops:

- Receives Mist webhooks at `/webhook/mist`
- Calls Claude for event analysis
- Posts results back to the dashboard via `/webhook/n8n/analysis`
- Triggers self-driving pipeline runs on configurable alert conditions

---

## Project Structure

```
├── app.py              Flask backend — 50+ routes, self-driving pipeline, n8n bridge
├── static/
│   └── index.html      Single-page dashboard (all UI, themes, JS)
├── websocket_server.py Real-time WebSocket push server
├── docker-compose.yml  Full stack orchestration
├── nginx.conf          Reverse proxy + WebSocket support
├── Dockerfile          mist-api container
└── n8n.json            n8n workflow export
```
