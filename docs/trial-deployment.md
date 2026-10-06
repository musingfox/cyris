# Running a trial deployment

A trial is one tester's own cyris on a Cloudflare account you hold: its own app Worker,
container application, D1 database, KV namespace, Pages project, vote Worker and rss
Worker, behind a login at `<slug>.<domain>`, with the archive private. On an account of
its own, it is also the manual, non-button half of the clean-account run
(`docs/architecture.md` §7, M6).

`scripts/trial-wizard.sh` provisions one, stage by stage. This page says why the stages
are shaped the way they are; the commands themselves live only in the script.

## Running it

From the checkout that deploys production:

```sh
bash scripts/trial-wizard.sh <slug>
```

The slug is a short code, 2 to 20 lowercase letters or digits starting with a letter. It
becomes part of every resource name and of the tester's login address, so it is not a
person's name; `trial1`, `trial2` work. The wizard prints each command that creates or
deploys something and runs it only after you answer `y`. Read-only commands run directly.

Run it again with the same slug to continue after a stop: the stages already done are
skipped, and `<slug> --from N` redoes stage N and every one after it. `<slug> receipts`
runs only the closing checks; use it after the next scheduled publish hour to see the first scheduled
run, and once a week to read what the trial spent.

## Which account

Either works; the first trial (2026-10-06) runs on production's.

- **An account of its own** keeps everything apart, at the price of a second Workers
  Paid plan, since a container needs one.
- **Production's account** needs no second plan. Check three things before you start:
  - No Zero Trust Access policy covers `<slug>.<domain>`, a wildcard such as
    `*.<domain>` included. The wizard opens the page and waits for you in stage 1.
  - No resource name the wizard prints in stage 3 equals a production name.
    `scripts/provision_trial.py` derives them so, and this is the receipt.
  - The trial token carries only the permissions listed below. Cloudflare grants them
    account-wide, so on this account a leaked trial token reaches production's D1 and
    Pages project too.

## Why it is shaped this way

- **Every wrangler deploy names a `wrangler.trial-<slug>.toml`.** The trial runs from the
  same checkout as production, so one `wrangler deploy` without the trial's own config
  redeploys production's Worker. `scripts/provision_trial.py` renders the three trial
  configs, gitignored, and guarantees no trial name equals a production one.
- **Every wrangler call carries `--env-file /dev/null`.** Without it wrangler reads this
  checkout's `.env` and may send production's stale token instead of your login (see
  [the wrangler rule](install-cloudflare.md#run-wrangler-from-the-repo-root-always-with---env-file-devnull)).
  The wizard also unsets a `CLOUDFLARE_API_TOKEN` your shell exported, since that beats
  both. `tests/test_trial_wizard_guard.py` refuses a wizard that drops either flag.
- **Nothing is generated twice.** The slug, the Pages suffix, the image digest and each
  id and token are saved the first time and reused on every later run. A new suffix would
  name a Pages project that was never created; a new digest would deploy a different
  image from the first deploy's.
- **The image is production's.** The wizard takes the newest `image/*` tag the release
  workflow writes (see [operations.md](operations.md)) and asks you to confirm it is the
  one production runs, so the trial tests the code the tester will keep getting.
- **The trial has its own API token**, with the permissions of
  [install step 3](install-cloudflare.md#3-create-an-api-token) and no more, so it can be
  revoked alone. Only on an account of its own does a leak reach nothing but the trial.
  The wizard asks the API what the token may do before saving it: the first trial's
  token lacked Cloudflare Pages → Edit, and its first run got as far as publishing before
  that showed.
- **The secrets file holds what
  [install step 4](install-cloudflare.md#4-write-the-secrets-file) lists, with two
  exceptions.** `GEMINI_API_KEY` is the only LLM key a trial gets. `CYRIS_PRIVATE_ARCHIVE`
  and `CYRIS_APP_WORKER_NAME` stay out of it, because the rendered config sets them and a
  secret of the same name would shadow the value.
- **The vote Worker's token is its own value**, never the one the rss Worker gets.
- **The first boot runs from a scratch directory with an empty environment**, so this
  checkout's production `.env` is never read; see
  [running the CLI](install-cloudflare.md#running-the-cli-against-the-deployment) for why
  the files are named the way they are. Its first line names the database it wrote to,
  and the wizard stops unless that is the trial's.
- **The tester's sources are RSS only.** A trial has no newsletter Worker, so an
  email-only source would be silently absent.
- **The rss Worker comes after the first boot**, and the app is pointed at it only after
  it answers `401` without the bearer and its first poll counts every pushed source. See
  [Optional Workers](install-cloudflare.md#optional-workers) for why the order matters and
  why the app and the Worker share one token.
- **A new custom domain can take minutes to answer**; the install guide's
  [notes on it](install-cloudflare.md#6-log-in-and-fill-settings) apply.
- **The login token goes to the tester through a password manager**, never chat or email.

## Files to keep

The wizard writes these at the repo root, all gitignored:

- `.env.trial-<slug>`, `.env.trial-<slug>-promote` and `.env.trial-<slug>-rss` hold the
  secrets. Cloudflare cannot read them back, so losing one means rotating it.
- `.env.trial-<slug>-wizard` holds the inputs, the ids and the image the trial runs.
- `.env.trial-<slug>-sources.yaml` holds the tester's sources.

## What stays by hand

- Checking Zero Trust → Access for a policy covering `<slug>.<domain>`; a wildcard
  policy would give the tester a second login. The wizard opens the page and waits.
- The tester's own `/settings`: the Discord webhook, mail, anything else.
- The vote round trip: the tester votes, you run `cyris promote-sync` against the trial,
  and the article's `triaged_at` is set in the trial's D1.

## Spend controls

- `GEMINI_API_KEY` is the only LLM key in the trial's files, and the key's own budget is
  the hard stop.
- `digest.max_articles_per_digest` is agreed with the tester below the example's 400, and
  so is a limit on the number of sources. The tester can change the cap and the Gemini
  model on `/settings`, so both are an agreement, not a lock.
- Each trial adds a fixed container cost and an hourly rss poll to your bill; see
  [hosting-and-cost.md](hosting-and-cost.md).
