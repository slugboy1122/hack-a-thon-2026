# Start Here — Tatooine CF Worker Setup

Everything you need to deploy Tatooine on a fresh machine.

---

## Prerequisites

- A Cloudflare account with Workers paid plan (required for Durable Objects + Workflows)
- A Cloudflare API token with **Workers Scripts Edit** and **Workers KV Edit** permissions
- An Anthropic API key (`sk-ant-...`)
- A Juniper Mist API token (users can also supply their own via the UI)
- Node.js 18+ (the setup script installs it via fnm if missing)

---

## Option A — Automated (recommended)

```bash
git clone <repo>
cd tatooine-cf-worker
bash setup.sh
```

The script will:
1. Install Node.js via fnm if not present
2. Run `npm install`
3. Authenticate with Cloudflare (API token — no browser required)
4. Create the `AUTOMATIONS` KV namespace
5. Upload secrets (`ANTHROPIC_API_KEY`, `WEBHOOK_SECRET`, `MIST_API_TOKEN`)
6. Offer to run `wrangler dev` or `wrangler deploy`

---

## Option B — Manual

```bash
# 1. Install dependencies
npm install

# 2. Authenticate (headless server — use API token)
export CLOUDFLARE_API_TOKEN=<your-token>

# 3. Upload secrets
wrangler secret put ANTHROPIC_API_KEY     # required
wrangler secret put WEBHOOK_SECRET        # optional — validates Mist webhooks
wrangler secret put MIST_API_TOKEN        # optional — server-side fallback token

# 4. Deploy
wrangler deploy
```

---

## Custom Domain

The worker is already attached to `tatooine.thewifijedi.com` via the Cloudflare Workers Domains API. If you are deploying to a different domain:

```bash
# Attach a custom domain (replace values)
curl -X PUT "https://api.cloudflare.com/client/v4/accounts/<ACCOUNT_ID>/workers/domains" \
  -H "Authorization: Bearer <CF_API_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{
    "hostname": "your.domain.com",
    "zone_id": "<ZONE_ID>",
    "service": "tatooine",
    "environment": "production"
  }'
```

Do **not** add a `routes` block to `wrangler.jsonc` — the domain is managed via the API.

---

## Mist Webhook Configuration

After deploying, point your Mist org webhook at the worker:

| Field | Value |
|-------|-------|
| URL | `https://tatooine.thewifijedi.com/webhook/mist` |
| Secret | value of `WEBHOOK_SECRET` (used as `X-Mist-Secret` header or `?secret=` param) |
| Topics | `alarms`, `device-events`, `client-events`, `audits` |

API endpoint to create/update:
```
PUT https://api.mist.com/api/v1/orgs/<ORG_ID>/webhooks
```

---

## Local Development

```bash
wrangler dev
# Dashboard: http://localhost:8787
```

Note: Durable Objects and Workflows run locally via miniflare. KV uses a local SQLite store.

---

## Environment Variables (wrangler.jsonc `vars`)

| Variable | Default | Description |
|----------|---------|-------------|
| `CLAUDE_MODEL` | `claude-sonnet-4-6` | Anthropic model for chat and analysis |
| `MIST_API_URL` | `https://api.mist.com/api/v1` | Mist API base URL |

## Secrets (set via `wrangler secret put`)

| Secret | Required | Description |
|--------|----------|-------------|
| `ANTHROPIC_API_KEY` | Yes | Anthropic API key |
| `WEBHOOK_SECRET` | No | Validates `X-Mist-Secret` on incoming webhooks |
| `MIST_API_TOKEN` | No | Server-side Mist token fallback |

---

## Bindings (wrangler.jsonc)

| Binding | Type | Purpose |
|---------|------|---------|
| `BROADCASTER` | Durable Object | WebSocket hub + event/analysis ring buffers |
| `MIST_EVENT_WORKFLOW` | Workflow | Durable alarm analysis pipeline |
| `AUTOMATIONS` | KV Namespace | Automation rule CRUD store |
| `ASSETS` | Static Assets | Serves `static/` as SPA |
