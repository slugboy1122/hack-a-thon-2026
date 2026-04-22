#!/usr/bin/env bash
# Tatooine CF Worker — one-shot setup script
# Run from the project folder: bash setup.sh
set -euo pipefail

# ─── Colors ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'
info()    { echo -e "${BLUE}▸${NC} $*"; }
success() { echo -e "${GREEN}✔${NC} $*"; }
warn()    { echo -e "${YELLOW}⚠${NC} $*"; }
die()     { echo -e "${RED}✘ ERROR:${NC} $*" >&2; exit 1; }
header()  { echo -e "\n${BOLD}$*${NC}"; }

# ─── Guard: must run from project root ───────────────────────────────────────
[[ -f wrangler.jsonc ]] || die "Run this script from the tatooine-cf-worker project folder (where wrangler.jsonc lives)."

echo -e "${BOLD}"
echo "  ████████╗ █████╗ ████████╗ ██████╗  ██████╗ ██╗███╗   ██╗███████╗"
echo "     ██╔══╝██╔══██╗╚══██╔══╝██╔═══██╗██╔═══██╗██║████╗  ██║██╔════╝"
echo "     ██║   ███████║   ██║   ██║   ██║██║   ██║██║██╔██╗ ██║█████╗  "
echo "     ██║   ██╔══██║   ██║   ██║   ██║██║   ██║██║██║╚██╗██║██╔══╝  "
echo "     ██║   ██║  ██║   ██║   ╚██████╔╝╚██████╔╝██║██║ ╚████║███████╗"
echo "     ╚═╝   ╚═╝  ╚═╝   ╚═╝    ╚═════╝  ╚═════╝ ╚═╝╚═╝  ╚═══╝╚══════╝"
echo -e "  Cloudflare Worker Setup${NC}\n"

# ─── Step 1: Node.js ──────────────────────────────────────────────────────────
header "Step 1/6 — Node.js"

# Look for node in common locations that might not be on PATH
for candidate in \
    "$HOME/.local/share/fnm/node-versions/"*/installation/bin/node \
    "$HOME/.nvm/versions/node/"*/bin/node \
    /usr/local/bin/node \
    /usr/bin/node; do
  if [[ -x "$candidate" ]]; then
    export PATH="$(dirname "$candidate"):$PATH"
    break
  fi
done

if command -v node &>/dev/null; then
  NODE_VER=$(node --version)
  success "Node.js already installed: $NODE_VER"
else
  info "Node.js not found — installing via fnm..."
  curl -fsSL https://fnm.vercel.app/install | bash

  # Source fnm for this session
  export PATH="$HOME/.local/share/fnm:$PATH"
  eval "$(fnm env 2>/dev/null)" || true

  fnm install 22
  fnm use 22
  success "Node.js $(node --version) installed via fnm"
  info "Open a new terminal after setup to have node on your PATH permanently."
fi

# ─── Step 2: npm install ──────────────────────────────────────────────────────
header "Step 2/6 — Install npm packages"
npm install --silent
success "Dependencies installed"

# ─── Step 3: Cloudflare authentication ───────────────────────────────────────
header "Step 3/6 — Cloudflare authentication"

# Check if already authenticated via env var or existing OAuth token
if npx wrangler whoami &>/dev/null 2>&1; then
  CF_USER=$(npx wrangler whoami 2>/dev/null | grep -oP 'You are logged in with an \S+.*' | head -1 || echo "authenticated")
  success "Already authenticated ($CF_USER)"
else
  # Headless server — API token is the reliable path
  echo ""
  echo "  This server has no browser. Create a Cloudflare API token:"
  echo "  1. Open on your laptop: https://dash.cloudflare.com/profile/api-tokens"
  echo "  2. Create Token → 'Edit Cloudflare Workers' template → Create Token"
  echo "  3. Paste the token below"
  echo ""
  read -rsp "  Cloudflare API Token: " CF_API_TOKEN; echo
  [[ -n "$CF_API_TOKEN" ]] || die "Cloudflare API token is required."
  export CLOUDFLARE_API_TOKEN="$CF_API_TOKEN"
  # Persist for this session and future wrangler calls
  echo "export CLOUDFLARE_API_TOKEN=$CF_API_TOKEN" >> ~/.bashrc
  npx wrangler whoami &>/dev/null || die "Token invalid — check it and try again."
  success "Cloudflare API token accepted"
fi

# ─── Step 4: KV namespace ─────────────────────────────────────────────────────
header "Step 4/6 — KV namespace (automations store)"

if grep -q "REPLACE_WITH_YOUR_KV_NAMESPACE_ID" wrangler.jsonc; then
  info "Creating KV namespace AUTOMATIONS..."
  KV_OUTPUT=$(npx wrangler kv namespace create AUTOMATIONS 2>&1)
  echo "$KV_OUTPUT"
  # Parse the ID from wrangler output
  KV_ID=$(echo "$KV_OUTPUT" | grep -oP '"id":\s*"\K[^"]+' | head -1)
  if [[ -z "$KV_ID" ]]; then
    warn "Could not auto-detect KV namespace ID."
    read -rp "Paste the namespace ID from the output above: " KV_ID
    [[ -n "$KV_ID" ]] || die "KV namespace ID is required."
  fi
  # Update wrangler.jsonc in-place (sed handles JSONC fine for this substitution)
  sed -i "s/REPLACE_WITH_YOUR_KV_NAMESPACE_ID/$KV_ID/" wrangler.jsonc
  success "KV namespace configured: $KV_ID"
else
  success "KV namespace already configured — skipping"
fi

# ─── Step 5: Secrets ──────────────────────────────────────────────────────────
header "Step 5/6 — Worker secrets"
info "Secrets are stored encrypted in Cloudflare — never in your code or wrangler.jsonc."
echo ""

# ANTHROPIC_API_KEY (required for Claude chat + self-driving L2)
read -rsp "  ANTHROPIC_API_KEY (sk-ant-...): " ANTHROPIC_KEY; echo
if [[ -n "$ANTHROPIC_KEY" ]]; then
  printf '%s' "$ANTHROPIC_KEY" | npx wrangler secret put ANTHROPIC_API_KEY
  success "ANTHROPIC_API_KEY saved"
else
  warn "Skipped — Claude AI features will not work without this key"
fi

# WEBHOOK_SECRET (optional — validates incoming Mist webhooks)
read -rsp "  WEBHOOK_SECRET for Mist webhooks (Enter to skip): " WEBHOOK_SECRET; echo
if [[ -n "$WEBHOOK_SECRET" ]]; then
  printf '%s' "$WEBHOOK_SECRET" | npx wrangler secret put WEBHOOK_SECRET
  success "WEBHOOK_SECRET saved"
else
  info "Skipped WEBHOOK_SECRET (optional)"
fi

# MIST_API_TOKEN (optional — server-side fallback; users normally enter token in the UI)
read -rsp "  Server-side MIST_API_TOKEN fallback (Enter to skip): " MIST_TOKEN; echo
if [[ -n "$MIST_TOKEN" ]]; then
  printf '%s' "$MIST_TOKEN" | npx wrangler secret put MIST_API_TOKEN
  success "MIST_API_TOKEN saved"
else
  info "Skipped MIST_API_TOKEN (users enter their token in the dashboard UI)"
fi

# ─── Step 6: Dev or Deploy ────────────────────────────────────────────────────
header "Step 6/6 — Launch"
echo ""
echo -e "  ${BOLD}What would you like to do?${NC}"
echo "    1) Local dev  (wrangler dev  — runs at http://localhost:8787)"
echo "    2) Deploy     (wrangler deploy — live on Cloudflare Workers)"
echo "    3) Exit       (run manually later)"
echo ""
read -rp "  Choice [1/2/3]: " CHOICE

case "$CHOICE" in
  1)
    echo ""
    success "Starting local dev server..."
    info "Dashboard: http://localhost:8787"
    info "Press Ctrl+C to stop"
    echo ""
    npx wrangler dev
    ;;
  2)
    echo ""
    info "Deploying to Cloudflare Workers..."
    DEPLOY_OUTPUT=$(npx wrangler deploy 2>&1)
    echo "$DEPLOY_OUTPUT"
    WORKER_URL=$(echo "$DEPLOY_OUTPUT" | grep -oP 'https://[^\s]+workers\.dev' | head -1)
    echo ""
    success "Deployed!"
    [[ -n "$WORKER_URL" ]] && echo -e "  ${BOLD}Dashboard URL:${NC} $WORKER_URL"
    ;;
  *)
    echo ""
    success "Setup complete. Run later with:"
    echo "    npx wrangler dev     # local"
    echo "    npx wrangler deploy  # production"
    ;;
esac
