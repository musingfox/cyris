#!/usr/bin/env bash
# Deploys and drives the hosted Worker `cyris-hosted` (wrangler.hosted.toml) on the trial
# account. docs/hosted-tenants.md says which step runs which subcommand.
#
# Every value only the human has is asked for by name. ACCOUNT_ID is typed for every command
# that reaches Cloudflare through wrangler, and is never stored or read from the environment,
# so neither an exported variable nor an old file can pick the account. deploy, secrets and
# delete also print the account's name and do nothing until you type "yes". The secrets (the
# Anthropic key, the Access service token's secret) are read without echo each time, never
# written down, and reach wrangler and curl on stdin, never in argv, where `ps` would show them.
# HOSTED_URL, which deploy prints, is the one value kept, in .env.hosted (gitignored, mode 600).
set -euo pipefail
cd "$(dirname "$0")/.."

CONFIG=wrangler.hosted.toml
STATE=.env.hosted
# The repo's own wrangler (package.json), so the deploy is the version the tests dry-ran.
WRANGLER=(bun x wrangler)
# .env holds production's secrets; wrangler must not read it into a hosted deploy.
NO_DOTENV=(--env-file /dev/null)

# Every wrangler call goes through here, whoami included: the hosted config, and never .env,
# whose CLOUDFLARE_API_TOKEN would make one call act as another credential than the next, so
# the account a confirmation names could differ from the one deploy acts on.
run_wrangler() {
  "${WRANGLER[@]}" "$@" --config "$CONFIG" "${NO_DOTENV[@]}"
}

unset ACCOUNT_ID CLOUDFLARE_ACCOUNT_ID HOSTED_URL ACCESS_ID ACCESS_SECRET
touch "$STATE"
chmod 600 "$STATE"
# Only this key is read back; anything else in the file is ignored.
while IFS='=' read -r key value; do
  [[ "$key" == HOSTED_URL ]] && HOSTED_URL=$value
done <"$STATE"

TENANTS=$(python3 -c 'import sys, tomllib; print(" ".join(tomllib.load(open(sys.argv[1], "rb"))["vars"]["CYRIS_HOSTED_TENANTS"]))' "$CONFIG")

# ask NAME PROMPT [secret]: read a value typed now; a secret is read without echo.
ask() {
  local name=$1 prompt=$2 kind=${3:-} value
  if [[ "$kind" == secret ]]; then
    read -rsp "$prompt: " value
    echo >&2
  else
    read -rp "$prompt: " value
  fi
  [[ -n "$value" ]] || { echo "$name is required" >&2; exit 1; }
  printf -v "$name" '%s' "$value"
}

ask_account() {
  ask ACCOUNT_ID "ACCOUNT_ID of the trial Cloudflare account (dashboard > Account home > Account ID)"
  [[ "$ACCOUNT_ID" =~ ^[0-9a-f]{32}$ ]] || { echo "ACCOUNT_ID must be 32 hex characters" >&2; exit 1; }
  export CLOUDFLARE_ACCOUNT_ID=$ACCOUNT_ID
}

# confirm_account ACTION: name the account wrangler will act on, then require "yes".
confirm_account() {
  local action=$1 name answer
  # </dev/null: whoami must not take the answer you type next.
  name=$(run_wrangler whoami --json </dev/null 2>/dev/null | python3 -c '
import json, sys
text = sys.stdin.read()
whoami, _ = json.JSONDecoder().raw_decode(text[text.find("{"):])
names = [a.get("name") or "(unnamed)" for a in whoami.get("accounts") or [] if a.get("id") == sys.argv[1]]
print(names[0]) if names else sys.exit(1)
' "$ACCOUNT_ID") || {
    echo "the user logged in to wrangler cannot see account $ACCOUNT_ID; run: bun x wrangler login" >&2
    exit 1
  }
  echo "About to $action cyris-hosted on:"
  echo "  account id:   $ACCOUNT_ID"
  echo "  account name: $name (from wrangler whoami)"
  read -rp "Type yes to go on with this account: " answer
  [[ "$answer" == yes ]] || { echo "not confirmed; nothing was done" >&2; exit 1; }
}

# put_secret NAME VALUE: VALUE reaches wrangler on stdin.
put_secret() {
  printf '%s' "$2" | run_wrangler secret put "$1"
}

ask_access() {
  [[ -n "${HOSTED_URL:-}" ]] || { echo "no HOSTED_URL in $STATE; run: scripts/hosted.sh deploy" >&2; exit 1; }
  ask ACCESS_ID "CF-Access-Client-Id of a service token the cyris-hosted Access application allows"
  ask ACCESS_SECRET "CF-Access-Client-Secret of that service token" secret
}

# call METHOD PATH OUTFILE: one operator route, signed in with the service token. The headers
# go in as curl config on stdin; printf is a builtin, so the secret is in no process's argv.
call() {
  local method=$1 path=$2 out=$3 status
  status=$(
    printf 'header = "CF-Access-Client-Id: %s"\nheader = "CF-Access-Client-Secret: %s"\n' \
      "$ACCESS_ID" "$ACCESS_SECRET" |
      curl -sS --config - --max-time 300 -X "$method" -o "$out" -w '%{http_code}' "$HOSTED_URL$path"
  ) || status=000
  echo "$method $path -> HTTP $status" >&2
  python3 -m json.tool "$out" 2>/dev/null || cat "$out"
  echo
  [[ "$status" == 200 ]]
}

case "${1:-}" in
  deploy)
    ask_account
    confirm_account deploy
    log=$(mktemp "${TMPDIR:-/tmp}/cyris-hosted-deploy.XXXXXX")
    run_wrangler deploy | tee "$log"
    url=$(grep -oE 'https://cyris-hosted\.[A-Za-z0-9.-]+\.workers\.dev' "$log" | head -1 || true)
    if [[ -n "$url" ]]; then
      grep -v '^HOSTED_URL=' "$STATE" >"$STATE.new" || true
      printf 'HOSTED_URL=%s\n' "$url" >>"$STATE.new"
      chmod 600 "$STATE.new"
      mv "$STATE.new" "$STATE"
      echo "HOSTED_URL=$url (kept in $STATE)"
    else
      echo "no workers.dev URL in the deploy output; add HOSTED_URL=https://... to $STATE" >&2
    fi
    ;;
  secrets)
    ask_account
    confirm_account "set the secrets of"
    ask TEAM_DOMAIN "CYRIS_ACCESS_TEAM_DOMAIN, the Access team domain without a scheme (yourteam.cloudflareaccess.com)"
    ask AUD "CYRIS_ACCESS_AUD, the Application Audience (AUD) tag of the cyris-hosted Access application"
    ask ANTHROPIC "ANTHROPIC_API_KEY for the tenants' runs" secret
    put_secret CYRIS_ACCESS_TEAM_DOMAIN "$TEAM_DOMAIN"
    put_secret CYRIS_ACCESS_AUD "$AUD"
    put_secret ANTHROPIC_API_KEY "$ANTHROPIC"
    ;;
  containers)
    ask_account
    run_wrangler containers list
    ;;
  seed)
    ask_access
    out=$(mktemp -d "${TMPDIR:-/tmp}/cyris-hosted-seed.XXXXXX")
    for tenant in $TENANTS; do call POST "/hosted/tenants/$tenant/seed" "$out/$tenant.json"; done
    ;;
  run)
    period=${2:-morning}
    [[ "$period" == morning || "$period" == evening ]] || { echo "period is morning or evening" >&2; exit 2; }
    ask_access
    out=$(mktemp -d "${TMPDIR:-/tmp}/cyris-hosted-run.XXXXXX")
    for tenant in $TENANTS; do call POST "/hosted/tenants/$tenant/run?period=$period" "$out/$tenant.json"; done
    ;;
  check)
    ask_access
    out=$(mktemp -d "${TMPDIR:-/tmp}/cyris-hosted-report.XXXXXX")
    files=()
    for tenant in $TENANTS; do
      call GET "/hosted/tenants/$tenant/report" "$out/$tenant.json"
      files+=("$out/$tenant.json")
    done
    echo "reports kept in $out" >&2
    python3 scripts/hosted_check.py "${files[@]}"
    ;;
  tail)
    ask_account
    run_wrangler tail
    ;;
  delete)
    ask_account
    confirm_account delete
    run_wrangler delete
    ;;
  *)
    echo "usage: $0 deploy|secrets|containers|seed|run [morning|evening]|check|tail|delete" >&2
    exit 2
    ;;
esac
