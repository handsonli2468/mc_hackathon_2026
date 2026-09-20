#!/usr/bin/env bash
# Deploy the VLM server on this host from a git ref.
#
#   deploy/deploy.sh            # latest origin/main
#   deploy/deploy.sh <commit>   # roll back / pin to a commit
#
# Order: checkout -> build -> unit tests in the new image -> restart -> wait for model_ready.
# A failed build or test leaves the running container untouched.
set -euo pipefail

# Run from a temporary copy: 'git checkout' below rewrites (or, for older commits,
# deletes) this file while bash is still reading it.
if [[ -z "${DEPLOY_SH_COPY:-}" ]]; then
  copy="$(mktemp --suffix=-deploy.sh)"
  cp "$0" "$copy"
  DEPLOY_SH_COPY="$copy" DEPLOY_REPO="$(cd "$(dirname "$0")/.." && pwd)" exec bash "$copy" "$@"
fi
trap 'rm -f "$DEPLOY_SH_COPY"' EXIT

cd "$DEPLOY_REPO"
REF="${1:-origin/main}"
PORT="${VLM_HTTP_PORT:-8080}"
READY_TIMEOUT_S="${READY_TIMEOUT_S:-120}"
COMPOSE=(docker compose)

step() { printf '\n==> %s\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

[[ -f .env ]] || die ".env missing; copy .env.example and set LOCATE_MODELS_DIR"
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  git status --short --untracked-files=no >&2
  die "tracked files modified on this host; commit them elsewhere or 'git checkout -- .' first"
fi

step "fetch and check out $REF"
git fetch --quiet origin
git checkout --quiet --detach "$REF"
COMMIT="$(git rev-parse HEAD)"
echo "$(git log -1 --format='%h %s (%an, %cr)')"

step "build image"
"${COMPOSE[@]}" build server

step "unit tests in the new image"
"${COMPOSE[@]}" run --rm --no-deps server python -m unittest tests.test_vlm_server \
  || die "tests failed; server NOT restarted (still running the previous image)"

step "restart server"
"${COMPOSE[@]}" up -d --remove-orphans server

step "wait for model_ready (up to ${READY_TIMEOUT_S}s)"
deadline=$((SECONDS + READY_TIMEOUT_S))
until curl -sf "http://localhost:${PORT}/api/status" | grep -q '"model_ready": *true'; do
  if (( SECONDS >= deadline )); then
    docker logs --tail 40 vlm-server >&2 || true
    die "model not ready after ${READY_TIMEOUT_S}s"
  fi
  sleep 2
done

mkdir -p output
echo "${COMMIT} $(date -Is)" > output/DEPLOYED

step "deployed"
curl -s "http://localhost:${PORT}/api/status" | python3 -c '
import json, sys
d = json.load(sys.stdin)
print("commit :", sys.argv[1][:12])
print("model  :", d["model_info"])
print("query  :", d["query"])' "${COMMIT}"
cat <<'EOF'

Note: a restart clears the target description. Set it again unless VLM_INITIAL_QUERY is in .env:
  curl -X POST http://localhost:8080/api/query -H 'Content-Type: application/json' -d '{"text":"..."}'
EOF
