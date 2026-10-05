# Running a trial deployment

A trial is one tester's own cyris on your Cloudflare account: its own app Worker,
container application, D1 database, KV namespace, Pages project, vote Worker and rss
Worker, behind a login at `<slug>.<domain>`, with the archive private. It is the manual,
non-button half of the clean-account run; its receipt is pending.

Everything here runs from the repo root of the checkout that deploys production. That is
the hazard: one `wrangler` command without the trial's own `--config` redeploys
production's Worker, and one without `--env-file /dev/null` may send a stale `.env` token
instead of your login (see
[the wrangler rule](install-cloudflare.md#run-wrangler-from-the-repo-root-always-with---env-file-devnull)).
Every command below names its config and carries the flag; `tests/test_trial_runbook.py`
holds this file to that. Run the steps in order, in one shell, so the variables persist;
if the shell closes, restore the variables rather than regenerating them: record
`TRIAL_SUFFIX` and each id and token you capture (`TRIAL_D1_ID`, `TRIAL_KV_ID`,
`TRIAL_PROMOTE_TOKEN`, `TRIAL_WORKER_TOKEN`, `TRIAL_RSS_URL`) and `IMAGE_DIGEST` as you go, set them again
with the hand-filled inputs, skip the `git fetch`, `IMAGE_DIGEST` and `openssl rand` lines (a release in between would pick a different image than the first deploy), and re-run
the `names` step. A new suffix would name a Pages project that was never created.

Cost: each trial adds a fixed container cost and an hourly rss poll to your bill, see
[hosting-and-cost.md](hosting-and-cost.md).

## Before you start

- Check Zero Trust → Access for a policy covering `<slug>.<domain>` before choosing the
  domain. A wildcard policy would put Access in front of the trial and give the tester a
  second login.
- Log in to wrangler as the account the trial belongs to, never production's:

  ```sh
  bunx wrangler login --env-file /dev/null
  ```

## 1. Set the inputs

Fill the first four lines by hand. The slug is a short code, 2 to 20 lowercase letters or
digits starting with a letter; it becomes part of every resource name, so it is not a
person's name. `ACCOUNT_ID` is the trial account's id. `IMAGE_DIGEST` is the newest
published image's digest, from the `image/*` git tag the release workflow writes (see
[operations.md](operations.md)). The newest tag is the one `git tag --sort=-creatordate`
lists first ([git-tag](https://git-scm.com/docs/git-tag), `--sort=<key>`).

```sh
SLUG=<code>
DOMAIN=<a domain on the trial account's Cloudflare>
ACCOUNT_ID=<the trial account's id>
TESTER_EMAIL=<the address the tester will receive digests at>
git fetch --tags
IMAGE_DIGEST=$(git tag -l --format='%(contents)' "$(git tag -l 'image/*' --sort=-creatordate | head -1)" | grep -o 'sha256:[0-9a-f]\{64\}')
TRIAL_SUFFIX=$(openssl rand -hex 8)
export CLOUDFLARE_ACCOUNT_ID="$ACCOUNT_ID"
```

Condition for the next step: `echo "$IMAGE_DIGEST"` prints one `sha256:` value.

## 2. Derive the resource names

```sh
eval "$(uv run python scripts/provision_trial.py names --slug "$SLUG" --pages-suffix "$TRIAL_SUFFIX")"
```

It sets `TRIAL_APP_WORKER`, `TRIAL_CONTAINER`, `TRIAL_PROMOTE_WORKER`, `TRIAL_KV_TITLE`,
`TRIAL_D1_NAME`, `TRIAL_PAGES_PROJECT` and `TRIAL_RSS_WORKER`. None equals a production
name.

## 3. Create the D1 database, the KV namespace and the Pages project

Follow [install step 1](install-cloudflare.md#1-create-the-d1-database) for the database.
Put the UUID it prints into `TRIAL_D1_ID`. If it reports that the name exists, stop:
something already uses this slug on the account.

```sh
bunx wrangler d1 create "$TRIAL_D1_NAME" --env-file /dev/null
TRIAL_D1_ID=<the UUID it printed>
```

The vote Worker needs a KV namespace. Put the id it prints into `TRIAL_KV_ID`. If it
reports that the name exists, stop there too.

```sh
bunx wrangler kv namespace create "$TRIAL_KV_TITLE" --env-file /dev/null
TRIAL_KV_ID=<the id it printed>
```

Create the Pages project, as in
[install step 2](install-cloudflare.md#2-choose-a-pages-project-name). Its output must name `$TRIAL_PAGES_PROJECT.pages.dev`; do not
go on if it names another or reports that the name exists. The archive is a Pages 404
until the first publish; step 14 starts one.

```sh
bunx wrangler pages project create "$TRIAL_PAGES_PROJECT" --production-branch main --env-file /dev/null
```

Condition for the next step: the tracked config is untouched, so this prints nothing.

```sh
git status --porcelain wrangler.toml
```

## 4. Create the trial's API token

Create a new API token for this trial, as in
[install step 3](install-cloudflare.md#3-create-an-api-token), so a leaked trial token
reaches only the trial's account and can be revoked alone.

## 5. Render the trial's configs

```sh
uv run python scripts/provision_trial.py render --slug "$SLUG" --domain "$DOMAIN" --account-id "$ACCOUNT_ID" --image-digest "$IMAGE_DIGEST" --kv-id "$TRIAL_KV_ID" --d1-id "$TRIAL_D1_ID"
```

It prints three paths, `wrangler.trial-$SLUG.toml`, `workers/promote/wrangler.trial-$SLUG.toml`
and `workers/rss/wrangler.trial-$SLUG.toml`. Git ignores all three.

## 6. Dry-run all three configs

The rss Worker needs its dependencies first.

```sh
(cd workers/rss && bun install --frozen-lockfile)
```

Each dry run must finish without an error before any deploy.

```sh
bunx wrangler deploy --dry-run --config "wrangler.trial-$SLUG.toml" --env-file /dev/null
bunx wrangler deploy --dry-run --config "workers/promote/wrangler.trial-$SLUG.toml" --env-file /dev/null
bunx wrangler deploy --dry-run --config "workers/rss/wrangler.trial-$SLUG.toml" --env-file /dev/null
```

## 7. Deploy the vote Worker

Its token is its own value, never the one the rss Worker gets.

```sh
TRIAL_PROMOTE_TOKEN=$(openssl rand -hex 32)
printf 'PROMOTE_TOKEN=%s\n' "$TRIAL_PROMOTE_TOKEN" > ".env.trial-$SLUG-promote"
bunx wrangler deploy --config "workers/promote/wrangler.trial-$SLUG.toml" --env-file /dev/null --secrets-file ".env.trial-$SLUG-promote"
```

Note the `workers.dev` URL it prints; the next step needs it.

## 8. Write the app's secrets file

Write `.env.trial-$SLUG` as in
[install step 4](install-cloudflare.md#4-write-the-secrets-file): the five required
names, with `CYRIS_STORE_DATABASE_ID` set to `$TRIAL_D1_ID`,
`CYRIS_PROMOTE_PAGES_PROJECT` to `$TRIAL_PAGES_PROJECT` and `CLOUDFLARE_API_TOKEN` to
the token from step 4, plus `GEMINI_API_KEY`, the only LLM key a trial gets. Add
`CYRIS_PROMOTE_WORKER_URL` (the URL from step 7). Keep `CYRIS_PRIVATE_ARCHIVE` and
`CYRIS_APP_WORKER_NAME` out of this file: the rendered config sets them, and a secret of
the same name would shadow its value. Add `CYRIS_WORKER_TOKEN` and
`CYRIS_RSS_WORKER_URL` only at the rss step below; adding them earlier points the app at
a Worker that does not exist yet.

Append the vote token, so the two copies cannot differ:

```sh
printf '\nCYRIS_PROMOTE_TOKEN=%s\n' "$TRIAL_PROMOTE_TOKEN" >> ".env.trial-$SLUG"
```

Condition for the first app deploy: each receipt below prints its line or `ok`.

```sh
grep -x "CYRIS_STORE_DATABASE_ID=$TRIAL_D1_ID" ".env.trial-$SLUG" || echo MISMATCH
grep -x "CYRIS_PROMOTE_PAGES_PROJECT=$TRIAL_PAGES_PROJECT" ".env.trial-$SLUG" || echo MISMATCH
grep -Eq '^(CYRIS_PRIVATE_ARCHIVE|CYRIS_APP_WORKER_NAME)=' ".env.trial-$SLUG" && echo MISMATCH || echo ok
```

## 9. Deploy the app

```sh
bunx wrangler deploy --config "wrangler.trial-$SLUG.toml" --env-file /dev/null --secrets-file ".env.trial-$SLUG"
```

The first request after a deploy starts the container, and a new custom domain can take
a few minutes to answer; the install guide's
[notes on it](install-cloudflare.md#6-log-in-and-fill-settings) apply. Condition for everything after: an unauthenticated request is
sent to the login (`-D -` dumps the response headers to stdout, see
[curl's `--dump-header`](https://curl.se/docs/manpage.html#-D)), so this prints a `302` status line and `location: /login?next=%2F`.

```sh
curl -s -o /dev/null -D - "https://$SLUG.$DOMAIN/" | grep -iE '^(HTTP/[0-9.]+ 302|location: /login\?next=%2F)'
```

## 10. First boot

The CLI fills the trial's D1 from a scratch directory, with an empty environment, so the
production `.env` of this checkout is never read. See
[running the CLI](install-cloudflare.md#running-the-cli-against-the-deployment) for why
the files are named this way. Agree the article cap with the tester first; the example's
400 is the ceiling a trial must stay under.

```sh
TRIAL_DIR=$(mktemp -d)
MAX_ARTICLES=100
grep -E '^(CLOUDFLARE_ACCOUNT_ID|CLOUDFLARE_API_TOKEN|CYRIS_STORE_DATABASE_ID|CYRIS_PROMOTE_PAGES_PROJECT)=' ".env.trial-$SLUG" > "$TRIAL_DIR/.env"
sed -e '/^\[llm_provider\]/,/^\[digest\]/s/^provider = .*/provider = "gemini"/' -e '/^\[llm_provider\]/,/^\[digest\]/s/^model = .*/model = "gemini-2.5-flash"/' -e "s/^max_articles_per_digest = .*/max_articles_per_digest = $MAX_ARTICLES/" cyris.toml.example > "$TRIAL_DIR/settings.toml"
cat > "$TRIAL_DIR/sources.yaml" <<'EOF'
defaults:
  tier: filter
  language: auto
sources:
  - name: "Stratechery"
    url: "https://stratechery.com/feed/"
    tier: summarize
    tags: [tech, business-strategy]
EOF
```

Replace that source list with the feeds agreed with the tester: RSS only. No entry may be
`type: newsletter`, because a trial has no newsletter Worker and an email-only source
would be silently absent. Agree a limit on the number of sources too.

Push the settings. Its first line names the database it wrote to, and must be the
trial's.

```sh
env -i HOME="$HOME" PATH="$PATH" uv run cyris settings push --config "$TRIAL_DIR/settings.toml" --sources "$TRIAL_DIR/sources.yaml" | tee "$TRIAL_DIR/settings-push.log"
head -1 "$TRIAL_DIR/settings-push.log" | grep -x "D1 database $TRIAL_D1_ID" && echo ok || echo MISMATCH
```

Condition for the sources push: that printed `ok`. Note the count it prints; the rss
step compares against it.

```sh
env -i HOME="$HOME" PATH="$PATH" uv run cyris sources push --config "$TRIAL_DIR/settings.toml" --sources "$TRIAL_DIR/sources.yaml"
rm -r "$TRIAL_DIR"
```

## 11. The rss Worker

Follow [Optional Workers](install-cloudflare.md#optional-workers) for why the rss Worker
comes after the first boot and why app and Worker share one token; this step gives the
trial-safe commands only. The Worker binds the trial's own D1, the one the app uses. The
quiet receipts print `ok` or `MISMATCH`, never the token.

```sh
TRIAL_WORKER_TOKEN=$(openssl rand -hex 32)
printf 'RSS_TOKEN=%s\n' "$TRIAL_WORKER_TOKEN" > ".env.trial-$SLUG-rss"
grep -qx "RSS_TOKEN=$TRIAL_WORKER_TOKEN" ".env.trial-$SLUG-rss" && echo ok || echo MISMATCH
```

Condition for the rss deploy: the receipt printed `ok`.

```sh
bunx wrangler deploy --config "workers/rss/wrangler.trial-$SLUG.toml" --env-file /dev/null --secrets-file ".env.trial-$SLUG-rss"
```

Put the `workers.dev` URL the deploy printed into `TRIAL_RSS_URL`. Without the bearer the
Worker must answer `401`; with it, `POST /poll` must report a `feeds` count equal to the
number of sources the sources push printed. Both before the app is pointed at it.

```sh
TRIAL_RSS_URL=<the URL it printed>
curl -s -o /dev/null -w '%{http_code}\n' "$TRIAL_RSS_URL/stats"
curl -s -X POST -H "Authorization: Bearer $TRIAL_WORKER_TOKEN" "$TRIAL_RSS_URL/poll"
```

If the `/poll` body shows `"feeds":0`, the first boot pushed no RSS source (the Worker's
log says `sources table is empty`): go back to step 10. Point the app at it and check both lines landed:

```sh
printf '\nCYRIS_WORKER_TOKEN=%s\nCYRIS_RSS_WORKER_URL=%s\n' "$TRIAL_WORKER_TOKEN" "$TRIAL_RSS_URL" >> ".env.trial-$SLUG"
grep -qx "CYRIS_WORKER_TOKEN=$TRIAL_WORKER_TOKEN" ".env.trial-$SLUG" && echo ok || echo MISMATCH
grep -x "CYRIS_RSS_WORKER_URL=$TRIAL_RSS_URL" ".env.trial-$SLUG" || echo MISMATCH
```

Condition for the app redeploy: the first receipt printed `ok` and the second printed its
line. Redeploy so the app reads them:

```sh
bunx wrangler deploy --config "wrangler.trial-$SLUG.toml" --env-file /dev/null --secrets-file ".env.trial-$SLUG"
```

## 12. Let the tester receive mail

If the tester gets the digest by email, add `$TESTER_EMAIL` as a destination address and
have them open the verification mail first, as
[step 6](install-cloudflare.md#6-log-in-and-fill-settings) describes; Save on
`/settings` stores the pair only if the test message is delivered.

## 13. Hand over

Give the tester `https://$SLUG.$DOMAIN/login` and the `CYRIS_UI_TOKEN` from
`.env.trial-$SLUG`, through a password manager, never chat or email. They finish
`/settings` themselves (step 6 of the install guide). Keep the three
`.env.trial-$SLUG*` files: secrets cannot be read back from Cloudflare.

## 14. Start the first digest

Until a digest is published the archive is a Pages 404. Start one as in
[install step 7](install-cloudflare.md#7-run-the-first-digest-now).

## Spend controls

- `GEMINI_API_KEY` is the only LLM key in the trial's files, and the key's own budget is
  the hard stop.
- `digest.max_articles_per_digest` is set below the example's 400 in step 10, and a
  source-count limit is agreed with the tester. The tester can change the cap and the
  Gemini model on `/settings`, so both are an agreement, not a lock.
- Once a week, read what the trial spent. The query is read-only:

```sh
bunx wrangler d1 execute "$TRIAL_D1_NAME" --remote --config "workers/rss/wrangler.trial-$SLUG.toml" --env-file /dev/null --command "SELECT date(logged_at) AS day, count(*) AS runs, sum(api_calls) AS calls, round(sum(cost_usd), 4) AS usd FROM usage_log WHERE logged_at >= date('now', '-7 day') GROUP BY day ORDER BY day"
```
