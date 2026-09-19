#!/bin/sh
# One image, three roles. The Container runtime picks one with CYRIS_ROLE; the
# Mac mini's compose file picks none and gets the default.
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
    # Votes sync even when the run stops (say, on incomplete settings); the
    # pass still exits with the run's failure.
    status=0
    cyris run --if-due $CONF || status=$?
    cyris promote-sync $CONF
    exit "$status"
    ;;
  ui)
    export CYRIS_STORE_BACKEND=${CYRIS_STORE_BACKEND:-d1}
    exec cyris triage-ui --host 0.0.0.0 --port 8766 $CONF
    ;;
  # ponytail: the Mac mini's role, alive only until M5's cutover. Deleting it
  # takes supercronic and docker/crontab with it.
  cron)
    exec supercronic /app/crontab
    ;;
  *)
    echo "unknown CYRIS_ROLE: $CYRIS_ROLE" >&2
    exit 2
    ;;
esac
