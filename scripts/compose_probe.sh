#!/usr/bin/env bash
# Probe a docker compose install on SQLite end to end, on a host with Docker.
#
# It works in a temporary copy of this repo under its own compose project name, so it
# never touches a running install: its containers, image tags and volume carry the
# project name, and its ui is published on a free host port instead of 8766.
#
# The copy's cyris.toml names no backend on purpose. The compose environment, not the
# file, must pick sqlite, so do not add a `[store] backend` line here.
#
# One line per check, `ok` or `FAIL`; the first failure stops the probe with a non-zero
# exit, after the project's containers, volume and the copy are removed. It makes no
# LLM, mail or Cloudflare call, and needs no network beyond the image build.
#
# Usage: scripts/compose_probe.sh
set -euo pipefail

repo=$(cd "$(dirname "$0")/.." && pwd)
project="cyris-probe-$$"
work=""
# What the wizard's Language step saves through /api/settings/values, as
# tests/test_e2e_local_install.py sends it (`browser.ok("POST", "/api/settings/values",
# {"values": LANGUAGE})`) and as `static/onboarding.js` posts it.
setting_key="digest.output_language"
setting_value="de"

ok() { printf 'ok   %s\n' "$1"; }
fail() {
  printf 'FAIL %s\n' "$1" >&2
  exit 1
}

# The same compose project every time, from inside the copy.
dc() { docker compose -p "$project" "$@"; }

cleanup() {
  local status=$?
  trap - EXIT
  if [ -n "$work" ] && [ -d "$work" ]; then
    if [ "$status" -ne 0 ] && [ -f "$work/docker-compose.yml" ]; then
      (cd "$work" && dc logs --tail 40 >&2) || true
    fi
    (cd "$work" && dc down -v --rmi local --remove-orphans >/dev/null 2>&1) || true
    rm -rf "$work"
  fi
  exit "$status"
}
trap cleanup EXIT

for tool in docker curl git tar; do
  command -v "$tool" >/dev/null 2>&1 || fail "needs $tool on PATH"
done
docker compose version >/dev/null 2>&1 || fail "needs the docker compose plugin"

# The compose environment must pick the backend, so nothing here may name one.
unset CYRIS_STORE_BACKEND CYRIS_STORE_SQLITE_PATH COMPOSE_PROJECT_NAME

work=$(mktemp -d "${TMPDIR:-/tmp}/cyris-compose-probe.XXXXXX")
# Tracked and untracked-but-not-ignored files: what a clone plus your edits would build.
# Ignored files (.env, cyris.toml, agent-vault/, node_modules/) stay out of the copy.
git -C "$repo" ls-files -z --cached --others --exclude-standard |
  tar -C "$repo" --null -T - -cf - |
  tar -C "$work" -xf -
ok "copied the repo to a temporary directory"

# A free loopback port for the ui, so a running install keeps 8766.
port=""
for _ in $(seq 1 50); do
  candidate=$((20000 + RANDOM % 20000))
  if ! (exec 3<>"/dev/tcp/127.0.0.1/$candidate") 2>/dev/null; then
    port=$candidate
    break
  fi
done
[ -n "$port" ] || fail "no free host port found"
export CYRIS_UI_PORT="$port"
base="http://127.0.0.1:$port"

cd "$work"
# No `[store] backend` line, and the provider none: no key, no LLM call.
cat >cyris.toml <<'EOF'
[llm_provider]
provider = "none"
model = ""

[html_output]
enabled = true
EOF
: >.env
cp sources.example.yaml sources.yaml
mkdir -p agent-vault

dc build --quiet >/dev/null || fail "image build"
ok "built the image"
dc up -d >/dev/null || fail "docker compose up -d"
ok "started both services as project $project, ui on host port $port"

wait_for_ui() {
  local _
  for _ in $(seq 1 90); do
    if curl -fsS -o /dev/null --max-time 2 "$base/api/build" 2>/dev/null; then
      return 0
    fi
    sleep 1
  done
  return 1
}

# The cyris service loads its config as the scheduler's runs do: from /app, under the
# environment compose gave it. Prints the backend it resolved and the saved value.
readback='
from pathlib import Path
from cyris.bootstrap import load_effective_config

cfg = load_effective_config(Path("/app/cyris.toml"), Path("/app/sources.yaml"))
print(cfg.app.store.backend, cfg.present_settings().get("'"$setting_key"'"))
'

read_back() {
  local expected=$1 label=$2 out
  out=$(dc exec -T cyris python -c "$readback" 2>"$work/readback.err") ||
    fail "$label: docker compose exec cyris could not load the config: $(tail -n 3 "$work/readback.err")"
  [ "$out" = "sqlite $expected" ] ||
    fail "$label: cyris service read '$out', expected 'sqlite $expected'"
  ok "$label: the cyris service reads sqlite and $setting_key=$expected from the volume's file"
}

wait_for_ui || fail "ui did not answer $base/api/build within 90s"
ok "ui answers on $base"

# The wizard's request: a JSON POST. The Host is the loopback address and port the reader
# typed, which the server's check accepts (tests/e2e/harness.py sends it alone). Origin is
# what a browser adds to a write from the page the server itself served: the same
# address, which `_same_origin` in triage_server.py accepts.
http_status=$(curl -sS -o "$work/save.json" -w '%{http_code}' --max-time 30 -X POST \
  -H "Host: 127.0.0.1:$port" \
  -H "Origin: $base" \
  -H 'Content-Type: application/json' \
  --data "{\"values\": {\"$setting_key\": \"$setting_value\"}}" \
  "$base/api/settings/values") || fail "the save request did not complete"
[ "$http_status" = "200" ] ||
  fail "saving $setting_key answered $http_status: $(cat "$work/save.json")"
grep -q '"ok": *true' "$work/save.json" || fail "the save was not ok: $(cat "$work/save.json")"
ok "saved $setting_key=$setting_value through POST /api/settings/values"

read_back "$setting_value" "after the first start"

volumes=$(docker volume ls -q --filter "label=com.docker.compose.project=$project")
case "$volumes" in
  *"${project}_cyris-state"*) ok "the state lives in the named volume ${project}_cyris-state" ;;
  *) fail "no named volume ${project}_cyris-state; found: ${volumes:-none}" ;;
esac
dc exec -T cyris test -f /app/state/cyris.sqlite3 ||
  fail "no /app/state/cyris.sqlite3 in the cyris container"
ok "the file is /app/state/cyris.sqlite3"

before=$(dc ps -q ui cyris | sort | tr '\n' ' ')
dc up -d --force-recreate >/dev/null || fail "docker compose up -d --force-recreate"
after=$(dc ps -q ui cyris | sort | tr '\n' ' ')
[ -n "$after" ] && [ "$before" != "$after" ] || fail "--force-recreate kept the same containers"
ok "recreated both services"
wait_for_ui || fail "ui did not answer after the recreate"
read_back "$setting_value" "after --force-recreate"

dc down >/dev/null || fail "docker compose down"
dc up -d >/dev/null || fail "docker compose up -d after down"
ok "took the project down and up again, keeping the volume"
wait_for_ui || fail "ui did not answer after down and up"
read_back "$setting_value" "after down and up"

dc down -v --rmi local --remove-orphans >/dev/null || fail "docker compose down -v"
if docker volume inspect "${project}_cyris-state" >/dev/null 2>&1; then
  fail "down -v left the volume ${project}_cyris-state"
fi
ok "down -v removed the containers, images and volume"

cd /
rm -rf "$work"
work=""
ok "removed the temporary copy"
