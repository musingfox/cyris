#!/bin/sh
# One image, three roles. The Container runtime picks one with CYRIS_ROLE; a
# local docker compose install picks none and gets the default.
set -eu

CONF="--config /app/cyris.toml --sources /app/sources.yaml"

case "${CYRIS_ROLE:-cron}" in
  # Cloudflare Workers Cron fires the hourly tick, so the container's own job is
  # one pass and exit — the instance stops and stops billing.
  run)
    export CYRIS_STORE_BACKEND=${CYRIS_STORE_BACKEND:-d1}
    export CYRIS_HTML_OUTPUT_ENABLED=${CYRIS_HTML_OUTPUT_ENABLED:-true}
    export CYRIS_PROMOTE_PUBLISH_ENABLED=${CYRIS_PROMOTE_PUBLISH_ENABLED:-true}
    # LLM providers refuse by egress location, and placement can move between
    # runs, so each run logs where it left from. Never fatal.
    python - <<'PY' || true
import json
from urllib.request import urlopen

try:
    body = urlopen("https://cloudflare.com/cdn-cgi/trace", timeout=5).read().decode()
    trace = dict(line.split("=", 1) for line in body.splitlines() if "=" in line)
    print(json.dumps({"event": "egress_probe", "colo": trace.get("colo"), "loc": trace.get("loc")}))
except Exception as e:
    print(json.dumps({"event": "egress_probe", "error": str(e)[:200]}))
PY
    # This shell is PID 1, and Container.stop() is one SIGTERM to it. A shell
    # runs a trap only after its foreground command returns, so each step runs
    # in the background and the shell waits on it, which a trapped signal ends.
    # The signal goes on to the running step, and the pass ends when that step
    # does, so `cyris run` gets to finish its own shutdown instead of being
    # orphaned.
    child=
    on_term() {
      trap '' TERM
      if [ -n "$child" ]; then
        kill -TERM "$child" 2>/dev/null || true
        wait "$child" || true
      fi
      exit 143
    }
    trap on_term TERM
    step() {
      "$@" &
      child=$!
      rc=0
      wait "$child" || rc=$?
      child=
      return "$rc"
    }
    # Votes sync even when the run stops (say, on incomplete settings); the
    # pass still exits with the run's failure.
    status=0
    if [ -n "${CYRIS_RUN_PERIOD:-}" ]; then
      step cyris run --period "$CYRIS_RUN_PERIOD" $CONF || status=$?
    else
      step cyris run --if-due $CONF || status=$?
    fi
    step cyris promote-sync $CONF
    exit "$status"
    ;;
  ui)
    export CYRIS_STORE_BACKEND=${CYRIS_STORE_BACKEND:-d1}
    exec cyris triage-ui --host 0.0.0.0 --port 8766 $CONF
    ;;
  # The local scheduler for a docker compose install, which has no Workers Cron
  # to fire the tick; the default so compose needs no CYRIS_ROLE.
  cron)
    exec supercronic /app/crontab
    ;;
  *)
    echo "unknown CYRIS_ROLE: $CYRIS_ROLE" >&2
    exit 2
    ;;
esac
