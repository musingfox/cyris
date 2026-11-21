#!/usr/bin/env bash
# Deploys and drives the hosted Worker `cyris-hosted` (wrangler.hosted.toml) on the trial
# account. docs/hosted-tenants.md says which step runs which subcommand.
#
# Every value only the human has is asked for by name. ACCOUNT_ID is typed for every command
# that reaches Cloudflare through wrangler, and is never stored or read from the environment,
# so neither an exported variable nor an old file can pick the account. deploy, secrets and
# delete also print the account's name and do nothing until you type "yes". The secrets (the
# Anthropic key, the operator's webhook, the B2 key id and application key, the Access service
# token's secret) are read without echo each time, never
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

# The runbook's tenants are the ones the seed names sources for. The Worker's own list is its
# registry (ADR-0030), which `register` writes them into before anything else addresses them.
SEED=workers/app/src/hosted_seed.json
TENANTS=$(python3 -c 'import json, sys; print(" ".join(json.load(open(sys.argv[1]))["sources"]))' "$SEED")

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

# ask_optional NAME PROMPT: as ask, but blank (or no input at all) leaves NAME empty.
ask_optional() {
  local name=$1 prompt=$2 value=
  read -rp "$prompt: " value || true
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
  # A token already in the environment, as `aac run` puts it there, is used without asking, so
  # one approval covers every call; these are the names `cyris --tenant` reads too.
  if [[ -n "${CYRIS_ACCESS_CLIENT_ID:-}" && -n "${CYRIS_ACCESS_CLIENT_SECRET:-}" ]]; then
    ACCESS_ID=$CYRIS_ACCESS_CLIENT_ID ACCESS_SECRET=$CYRIS_ACCESS_CLIENT_SECRET
    return
  fi
  ask ACCESS_ID "CF-Access-Client-Id of a service token the cyris-hosted Access application allows"
  ask ACCESS_SECRET "CF-Access-Client-Secret of that service token" secret
}

# call TENANT METHOD PATH OUTFILE [BODY]: one operator route, signed in with the service token.
# The headers, and BODY when given (JSON), go in as curl config on stdin; printf is a builtin, so
# the secret is in no process's argv, and an address in a body is in no URL. Never ends the
# script: a route that answered neither 200 nor 202 (a run the registry queued), or never
# answered (HTTP 000, no file written), is reported for TENANT and returns non-zero, so the caller
# goes on to the next.
call() {
  local tenant=$1 method=$2 path=$3 out=$4 body=${5:-} status
  status=$(
    {
      printf 'header = "CF-Access-Client-Id: %s"\nheader = "CF-Access-Client-Secret: %s"\n' \
        "$ACCESS_ID" "$ACCESS_SECRET"
      if [[ -n "$body" ]]; then
        # Quoted as curl's config takes a value: a backslash and a double quote are escaped.
        body=${body//\\/\\\\}
        printf 'header = "Content-Type: application/json"\ndata = "%s"\n' "${body//\"/\\\"}"
      fi
    } | curl -sS --config - --max-time 300 -X "$method" -o "$out" -w '%{http_code}' "$HOSTED_URL$path"
  ) || status=000
  echo "$method $path -> HTTP $status" >&2
  if [[ -s "$out" ]]; then
    python3 -m json.tool "$out" 2>/dev/null || cat "$out"
    echo
  fi
  if [[ "$status" != 200 && "$status" != 202 ]]; then
    echo "FAILED $tenant: $method $path answered HTTP $status" >&2
    return 1
  fi
}

# fetch_page TENANT OUTFILE: the tenant's latest page, the one /t/TENANT/ serves its readers, read
# through the operator's GET /hosted/tenants/TENANT/page and signed in as call() is: /t/ takes a
# tenant session, which the operator never holds (ADR-0033). The page is kept for hosted_check.py
# only when it answered 200; it is not printed. Returns non-zero, never ending the script, when it
# did not answer 200.
fetch_page() {
  local tenant=$1 out=$2 status
  status=$(
    printf 'header = "CF-Access-Client-Id: %s"\nheader = "CF-Access-Client-Secret: %s"\n' \
      "$ACCESS_ID" "$ACCESS_SECRET" |
      curl -sS --config - --max-time 60 -o "$out" -w '%{http_code}' "$HOSTED_URL/hosted/tenants/$tenant/page"
  ) || status=000
  echo "GET /hosted/tenants/$tenant/page -> HTTP $status" >&2
  if [[ "$status" != 200 ]]; then
    rm -f "$out"
    echo "FAILED $tenant: its latest page answered HTTP $status" >&2
    return 1
  fi
}

# email_body EMAIL: the JSON body the invite and revoke routes take.
email_body() {
  python3 -c 'import json, sys; print(json.dumps({"email": sys.argv[1]}))' "$1"
}

# finish FAILED_TENANT...: say which tenants failed, if any, and exit 1 when one did.
finish() {
  if (($#)); then
    echo "failed for: $*" >&2
    exit 1
  fi
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
    [[ -n "${HOSTED_URL:-}" ]] || { echo "no HOSTED_URL in $STATE; run: scripts/hosted.sh deploy" >&2; exit 1; }
    ask_account
    confirm_account "set the secrets of"
    ask TEAM_DOMAIN "CYRIS_ACCESS_TEAM_DOMAIN, the Access team domain without a scheme (yourteam.cloudflareaccess.com)"
    ask AUD "CYRIS_ACCESS_AUD, the Application Audience (AUD) tag of the cyris-hosted Access application"
    # Who the operator routes admit, of whoever Access signs in (ADR-0037): the service token's
    # Client ID, which Access signs in as its common_name, and an operator's Access email if one
    # signs in with a browser. Not a secret; set as one so wrangler.hosted.toml names no one.
    ask OPERATORS "CYRIS_HOSTED_OPERATORS, comma separated: the service token's Client ID (xxxx.access), and your Access email if you sign in with a browser"
    ask ANTHROPIC "ANTHROPIC_API_KEY for the tenants' runs" secret
    # Where every tenant's failed run is reported, with its tenant id (ADR-0035). Its URL is its
    # credential, so it is read as a secret, and only the Worker's alert gateway holds it.
    ask OPERATOR_WEBHOOK "CYRIS_HOSTED_OPERATOR_WEBHOOK, the operator's Discord webhook URL" secret
    [[ "$OPERATOR_WEBHOOK" == https://* ]] || { echo "CYRIS_HOSTED_OPERATOR_WEBHOOK must be an https:// URL" >&2; exit 1; }
    ask MAIL_FROM "CYRIS_HOSTED_MAIL_FROM, the sign-in mail's sender, on a domain onboarded to Email Sending (cyris@yourdomain.com)"
    # The daily backup's off-Cloudflare copy in Backblaze B2 (ADR-0041). Blank skips all four: the
    # backup still writes R2, and the Worker alerts once a day that there is no B2 copy. The key id
    # and the application key are the credential, so both are read without echo.
    ask_optional B2_ENDPOINT "CYRIS_HOSTED_B2_ENDPOINT, the B2 bucket's S3 endpoint (https://s3.<region>.backblazeb2.com), blank for no B2 copy"
    if [[ -n "$B2_ENDPOINT" ]]; then
      [[ "$B2_ENDPOINT" =~ ^https://s3\.[a-z0-9-]+\.backblazeb2\.com$ ]] || {
        echo "CYRIS_HOSTED_B2_ENDPOINT must be https://s3.<region>.backblazeb2.com" >&2
        exit 1
      }
      ask B2_BUCKET "CYRIS_HOSTED_B2_BUCKET, the B2 bucket's name"
      ask B2_KEY_ID "CYRIS_HOSTED_B2_KEY_ID, the keyID of an application key limited to that bucket" secret
      ask B2_APPLICATION_KEY "CYRIS_HOSTED_B2_APPLICATION_KEY, that application key" secret
    fi
    put_secret CYRIS_ACCESS_TEAM_DOMAIN "$TEAM_DOMAIN"
    put_secret CYRIS_ACCESS_AUD "$AUD"
    put_secret CYRIS_HOSTED_OPERATORS "$OPERATORS"
    put_secret ANTHROPIC_API_KEY "$ANTHROPIC"
    put_secret CYRIS_HOSTED_OPERATOR_WEBHOOK "$OPERATOR_WEBHOOK"
    # Not a secret either; set the same way, so wrangler.hosted.toml names no deployment's domain.
    put_secret CYRIS_HOSTED_MAIL_FROM "$MAIL_FROM"
    # The host deploy kept, which every tenant's links point at (ADR-0029). Not a secret, but
    # `secret put` is how wrangler sets one value without editing wrangler.hosted.toml.
    put_secret CYRIS_STORE_HOSTED_DOMAIN "${HOSTED_URL#https://}"
    if [[ -n "$B2_ENDPOINT" ]]; then
      # The endpoint and bucket are not secrets; set the same way, so wrangler.hosted.toml names
      # no deployment's bucket.
      put_secret CYRIS_HOSTED_B2_ENDPOINT "$B2_ENDPOINT"
      put_secret CYRIS_HOSTED_B2_BUCKET "$B2_BUCKET"
      put_secret CYRIS_HOSTED_B2_KEY_ID "$B2_KEY_ID"
      put_secret CYRIS_HOSTED_B2_APPLICATION_KEY "$B2_APPLICATION_KEY"
    fi
    ;;
  containers)
    ask_account
    run_wrangler containers list
    ;;
  register)
    # register [TENANT [STATUS]]: the seed's tenants, or only TENANT, with STATUS when named
    # (a restored tenant registered paused is read and checked, and neither polled nor run).
    one=${2:-} status=${3:-}
    if [[ -n "$one" && ! "$one" =~ ^[a-z0-9-]{1,40}$ ]] || [[ -n "$status" && "$status" != active && "$status" != paused ]]; then
      echo "usage: $0 register [TENANT [active|paused]]" >&2
      exit 2
    fi
    ask_access
    out=$(mktemp -d "${TMPDIR:-/tmp}/cyris-hosted-register.XXXXXX")
    failed=()
    for tenant in ${one:-$TENANTS}; do
      call "$tenant" POST "/hosted/tenants/$tenant/register${status:+?status=$status}" "$out/$tenant.json" || failed+=("$tenant")
    done
    finish ${failed[@]+"${failed[@]}"}
    ;;
  schedule)
    # schedule TENANT TIMES TIMEZONE [DAILY_RUN_CAP]: the registry's schedule for one tenant,
    # which is what the hourly tick wakes it by (ADR-0032), e.g. alpha 07:00,19:00 Asia/Taipei.
    tenant=${2:-} times=${3:-} zone=${4:-} cap=${5:-}
    [[ -n "$tenant" && -n "$times" && -n "$zone" ]] || {
      echo "usage: $0 schedule TENANT HH:00,HH:00 TIMEZONE [DAILY_RUN_CAP]" >&2
      exit 2
    }
    # Encoded, so a zone such as Etc/GMT+8 keeps its "+".
    query=$(python3 -c 'import sys, urllib.parse; print(urllib.parse.urlencode([tuple(a.split("=", 1)) for a in sys.argv[1:] if not a.endswith("=")]))' \
      "times=$times" "timezone=$zone" "daily_run_cap=$cap")
    ask_access
    out=$(mktemp -d "${TMPDIR:-/tmp}/cyris-hosted-schedule.XXXXXX")
    call "$tenant" POST "/hosted/tenants/$tenant/schedule?$query" "$out/$tenant.json" || finish "$tenant"
    ;;
  seed)
    ask_access
    out=$(mktemp -d "${TMPDIR:-/tmp}/cyris-hosted-seed.XXXXXX")
    failed=()
    for tenant in $TENANTS; do
      call "$tenant" POST "/hosted/tenants/$tenant/seed" "$out/$tenant.json" || failed+=("$tenant")
    done
    finish ${failed[@]+"${failed[@]}"}
    ;;
  run)
    period=${2:-morning}
    [[ "$period" == morning || "$period" == evening ]] || { echo "period is morning or evening" >&2; exit 2; }
    ask_access
    out=$(mktemp -d "${TMPDIR:-/tmp}/cyris-hosted-run.XXXXXX")
    failed=()
    for tenant in $TENANTS; do
      call "$tenant" POST "/hosted/tenants/$tenant/run?period=$period" "$out/$tenant.json" || failed+=("$tenant")
    done
    finish ${failed[@]+"${failed[@]}"}
    ;;
  poll)
    # The hourly tick's RSS poll of each tenant, now: what it fetched and wrote into the
    # tenant's own buffer, which the tick's "hosted tick" log line sums.
    ask_access
    out=$(mktemp -d "${TMPDIR:-/tmp}/cyris-hosted-poll.XXXXXX")
    failed=()
    for tenant in $TENANTS; do
      call "$tenant" POST "/hosted/tenants/$tenant/poll" "$out/$tenant.json" || failed+=("$tenant")
    done
    finish ${failed[@]+"${failed[@]}"}
    ;;
  address)
    # Each tenant's newsletter address (ADR-0034): read it, or rotate it (a new address, every
    # old one revoked at once), or revoke it. The answers name the addresses, so their files
    # are deleted once printed.
    case "${2:-show}" in
      show) method=GET route=address ;;
      rotate) method=POST route=rotate-address ;;
      revoke) method=POST route=revoke-address ;;
      *) echo "address takes show, rotate or revoke" >&2; exit 2 ;;
    esac
    ask_access
    out=$(mktemp -d "${TMPDIR:-/tmp}/cyris-hosted-address.XXXXXX")
    failed=()
    for tenant in $TENANTS; do
      call "$tenant" "$method" "/hosted/tenants/$tenant/$route" "$out/$tenant.json" || failed+=("$tenant")
    done
    rm -rf "$out"
    finish ${failed[@]+"${failed[@]}"}
    ;;
  newsletter-source)
    # A newsletter source for one tenant, so its runs ingest the issues its address receives from
    # that sender; a run takes an issue only for a source whose email_match names its sender.
    tenant=${2:-} name=${3:-} from=${4:-} homepage=${5:-}
    if [[ -z "$tenant" || -z "$name" || -z "$from" ]]; then
      echo "newsletter-source takes TENANT NAME FROM [HOMEPAGE]" >&2
      exit 2
    fi
    query=$(python3 -c 'import sys, urllib.parse; n, f, h = sys.argv[1:]; print(urllib.parse.urlencode({"name": n, "from": f, **({"homepage": h} if h else {})}))' "$name" "$from" "$homepage")
    ask_access
    out=$(mktemp -d "${TMPDIR:-/tmp}/cyris-hosted-source.XXXXXX")
    failed=()
    call "$tenant" POST "/hosted/tenants/$tenant/newsletter-source?$query" "$out/$tenant.json" || failed+=("$tenant")
    finish ${failed[@]+"${failed[@]}"}
    ;;
  check)
    ask_access
    out=$(mktemp -d "${TMPDIR:-/tmp}/cyris-hosted-report.XXXXXX")
    # The reports and pages are tenants' content, so none is left on this machine: the directory
    # goes when the script ends, however it ends. Each page's SHA-256 is what is kept, printed.
    trap 'rm -rf "$out"' EXIT
    files=()
    failed=()
    for tenant in $TENANTS; do
      if call "$tenant" GET "/hosted/tenants/$tenant/report" "$out/$tenant.json"; then
        files+=("$out/$tenant.json")
        # Beside its report, where hosted_check.py looks for it.
        fetch_page "$tenant" "$out/$tenant.html" || failed+=("$tenant")
      else
        failed+=("$tenant")
      fi
    done
    # The reports that came back are still judged; a tenant without one fails the check.
    verdict=0
    if ((${#files[@]})); then python3 scripts/hosted_check.py "${files[@]}" || verdict=$?; fi
    for page in "$out"/*.html; do
      [[ -e "$page" ]] || continue
      python3 -c 'import hashlib, sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest(), sys.argv[2])' \
        "$page" "$(basename "$page" .html)"
    done
    echo "reports and pages deleted; each page's SHA-256 is above" >&2
    finish ${failed[@]+"${failed[@]}"}
    exit "$verdict"
    ;;
  invite | revoke)
    # invite TENANT EMAIL: put EMAIL on TENANT's invite list and mail it its first sign-in link.
    # revoke TENANT EMAIL: take EMAIL off the list, which ends its sessions at once (ADR-0033).
    action=$1 tenant=${2:-} email=${3:-}
    [[ -n "$tenant" && -n "$email" ]] || { echo "usage: $0 $action TENANT EMAIL" >&2; exit 2; }
    ask_access
    out=$(mktemp -d "${TMPDIR:-/tmp}/cyris-hosted-$action.XXXXXX")
    call "$tenant" POST "/hosted/tenants/$tenant/$action" "$out/$tenant.json" "$(email_body "$email")" ||
      finish "$tenant"
    ;;
  members)
    tenant=${2:-}
    [[ -n "$tenant" ]] || { echo "usage: $0 members TENANT" >&2; exit 2; }
    ask_access
    out=$(mktemp -d "${TMPDIR:-/tmp}/cyris-hosted-members.XXXXXX")
    call "$tenant" GET "/hosted/tenants/$tenant/members" "$out/$tenant.json" || finish "$tenant"
    ;;
  spend)
    # spend [YYYY-MM]: every tenant's AI spend in a UTC month against its allowance, this
    # month unless one is named (ADR-0038).
    month=${2:-}
    [[ -z "$month" || "$month" =~ ^[0-9]{4}-(0[1-9]|1[0-2])$ ]] || { echo "usage: $0 spend [YYYY-MM]" >&2; exit 2; }
    ask_access
    out=$(mktemp -d "${TMPDIR:-/tmp}/cyris-hosted-spend.XXXXXX")
    call spend GET "/hosted/spend${month:+?month=$month}" "$out/spend.json" || finish spend
    ;;
  budget)
    # budget TENANT USD: the tenant's monthly AI allowance, from its next run on (ADR-0038).
    tenant=${2:-} usd=${3:-}
    [[ -n "$tenant" && "$usd" =~ ^[0-9]+(\.[0-9]+)?$ ]] || { echo "usage: $0 budget TENANT USD" >&2; exit 2; }
    ask_access
    out=$(mktemp -d "${TMPDIR:-/tmp}/cyris-hosted-budget.XXXXXX")
    call "$tenant" POST "/hosted/tenants/$tenant/budget?usd=$usd" "$out/$tenant.json" || finish "$tenant"
    ;;
  backup)
    # Today's daily backup now, as the tick runs it at its hour (ADR-0041): what it exported and
    # which copies failed, each of which the operator's webhook was also told.
    ask_access
    out=$(mktemp -d "${TMPDIR:-/tmp}/cyris-hosted-backup.XXXXXX")
    call backup POST /hosted/backup "$out/backup.json" || finish backup
    ;;
  restore)
    # restore DATE TENANT NEW_TENANT: TENANT's export of the UTC day DATE, from the backup's R2
    # bucket, into NEW_TENANT, an id the registry does not hold and whose database holds no table
    # (ADR-0041). Register NEW_TENANT afterwards to read or run it: register NEW_TENANT paused.
    date=${2:-} tenant=${3:-} new=${4:-}
    if [[ ! "$date" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ || ! "$tenant" =~ ^[a-z0-9-]{1,40}$ || ! "$new" =~ ^[a-z0-9-]{1,40}$ ]]; then
      echo "usage: $0 restore DATE TENANT NEW_TENANT (DATE is YYYY-MM-DD, UTC)" >&2
      exit 2
    fi
    ask_access
    out=$(mktemp -d "${TMPDIR:-/tmp}/cyris-hosted-restore.XXXXXX")
    call "$new" POST "/hosted/tenants/$new/restore?date=$date&from=$tenant" "$out/$new.json" || finish "$new"
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
    echo "usage: $0 deploy|secrets|containers|register [TENANT [STATUS]]|schedule TENANT TIMES TIMEZONE [CAP]|seed|run [morning|evening]|poll|address [show|rotate|revoke]|newsletter-source TENANT NAME FROM [HOMEPAGE]|check|invite TENANT EMAIL|revoke TENANT EMAIL|members TENANT|spend [YYYY-MM]|budget TENANT USD|backup|restore DATE TENANT NEW_TENANT|tail|delete" >&2
    exit 2
    ;;
esac
