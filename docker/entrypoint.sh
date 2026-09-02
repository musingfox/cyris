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
    cyris run --if-due $CONF
    cyris promote-sync $CONF
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
