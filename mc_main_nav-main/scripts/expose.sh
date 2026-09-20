#!/usr/bin/env bash
# Publish the engine API and the obstacle UI to the internet (run ON the mini PC, on the host).
#
#   ./scripts/expose.sh              # Tailscale Funnel: stable URLs (default)
#   ./scripts/expose.sh ensure       # start only what is missing; safe to run from cron
#   ./scripts/expose.sh cloudflare   # Cloudflare quick tunnel: random URLs, no account
#   ./scripts/expose.sh --stop       # stop whichever is running
#
# Anyone with the URL can reach the pages, so the API is protected by a token: start the
# services with BT_ENGINE_TOKEN / TABLE_MAP_TOKEN set (see docs/deploy.md).
set -eo pipefail

ENGINE_PORT="${ENGINE_PORT:-8090}"
MAP_PORT="${MAP_PORT:-8081}"
TS="$HOME/bin/tailscale"
TS_SOCK="$HOME/.tailscale/tailscaled.sock"
TSD="$HOME/bin/tailscaled"
CF="$HOME/bin/cloudflared"
LOGS="$HOME/tunnels"

stop_all() {
  pkill -x cloudflared 2>/dev/null && echo "cloudflare tunnels stopped" || true
  if [ -S "$TS_SOCK" ]; then
    "$TS" --socket="$TS_SOCK" funnel --https=443 off 2>/dev/null || true
    "$TS" --socket="$TS_SOCK" funnel --https=8443 off 2>/dev/null || true
    echo "funnel off (tailscaled left running)"
  fi
}

[ "${1:-}" = "--stop" ] && { stop_all; exit 0; }

# `ensure`: quiet, idempotent, meant for cron. Does nothing when the funnel really works.
#
# Checking the local config is NOT enough. On 2026-09-19 `funnel status` said "on" for
# hours while tailscaled rejected every public connection ("handleIngress: got ingress conn
# for unconfigured ...") because Hostinfo.IngressEnabled had gone false. So probe the real
# public endpoint: --doh-url forces public DNS, which hits Tailscale's ingress servers
# rather than resolving to ourselves over MagicDNS.
if [ "${1:-}" = ensure ]; then
  probe() {
    curl -sf -o /dev/null --doh-url https://1.1.1.1/dns-query -m 20 "https://${1}/health"
  }
  host=$("$TS" --socket="$TS_SOCK" status --json 2>/dev/null |
    python3 -c 'import sys,json;print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' 2>/dev/null || true)

  if pgrep -x tailscaled > /dev/null && [ -n "$host" ] && probe "$host"; then
    exit 0
  fi

  # Reachable config but dead ingress: toggling funnel off and on re-announces it.
  status=$("$TS" --socket="$TS_SOCK" funnel status 2>/dev/null || true)
  if pgrep -x tailscaled > /dev/null && [ -n "$host" ] &&
     [[ "$status" == *"localhost:${ENGINE_PORT}"* && "$status" == *"localhost:${MAP_PORT}"* ]]; then
    echo "$(date -Is) funnel config is fine but the public endpoint is dead; re-announcing" >&2
    "$TS" --socket="$TS_SOCK" funnel --https=443 off > /dev/null 2>&1 || true
    "$TS" --socket="$TS_SOCK" funnel --https=8443 off > /dev/null 2>&1 || true
    sleep 2
  else
    echo "$(date -Is) funnel was down, restarting it" >&2
  fi
  exec "$0"
fi

if [ "${1:-tailscale}" = "cloudflare" ]; then
  if [ ! -x "$CF" ]; then
    mkdir -p "$(dirname "$CF")"
    curl -fsSL -o "$CF" https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64
    chmod +x "$CF"
  fi
  pkill -x cloudflared 2>/dev/null || true
  sleep 1
  mkdir -p "$LOGS"
  setsid "$CF" tunnel --url "http://localhost:${ENGINE_PORT}" > "$LOGS/engine.log" 2>&1 < /dev/null &
  setsid "$CF" tunnel --url "http://localhost:${MAP_PORT}" > "$LOGS/map.log" 2>&1 < /dev/null &
  url_from() {
    for _ in $(seq 1 30); do
      local u; u=$(grep -ho 'https://[a-z0-9-]*\.trycloudflare\.com' "$1" | head -1) || true
      [ -n "$u" ] && { echo "$u"; return; }
      sleep 1
    done
    echo "(no URL yet, see $1)"
  }
  echo "engine : $(url_from "$LOGS/engine.log")"
  echo "map UI : $(url_from "$LOGS/map.log")"
  exit 0
fi

# ---- Tailscale Funnel (stable URLs) ----
# tailscaled runs in userspace mode as this user, so no root is needed.
if [ ! -x "$TS" ]; then
  echo "Tailscale is not installed in ~/bin. See docs/deploy.md." >&2
  exit 1
fi
if ! pgrep -x tailscaled > /dev/null; then
  echo "starting tailscaled (userspace)"
  setsid "$TSD" --tun=userspace-networking --state="$HOME/.tailscale/tailscaled.state" \
    --socket="$TS_SOCK" --statedir="$HOME/.tailscale" > "$HOME/.tailscale/daemon.log" 2>&1 < /dev/null &
  sleep 4
fi
if ! "$TS" --socket="$TS_SOCK" status > /dev/null 2>&1; then
  echo "not logged in; run:  $TS --socket=$TS_SOCK up --hostname=mcpc --accept-dns=false" >&2
  exit 1
fi

"$TS" --socket="$TS_SOCK" funnel --bg --https=443 "http://localhost:${ENGINE_PORT}" > /dev/null
"$TS" --socket="$TS_SOCK" funnel --bg --https=8443 "http://localhost:${MAP_PORT}" > /dev/null
HOST=$("$TS" --socket="$TS_SOCK" status --json | python3 -c 'import sys,json;print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')

# Prove it from the outside before saying it works: on this machine (and any other node on
# the tailnet) the name resolves to ourselves over MagicDNS, which tests nothing.
echo -n "checking the public endpoint "
for _ in $(seq 1 10); do
  if curl -sf -o /dev/null --doh-url https://1.1.1.1/dns-query -m 20 "https://${HOST}/health"; then
    echo "... reachable"
    break
  fi
  echo -n "."
  sleep 6
done

echo "engine : https://${HOST}"
echo "map UI : https://${HOST}:8443"
echo
echo "From the LLM server:"
echo "  BT_ENGINE_URL=https://${HOST} BT_ENGINE_TOKEN=<token> ./scripts/bt_exec.py run tree.xml"
