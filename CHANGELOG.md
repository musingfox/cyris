# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.4.0] — 2026-10-02

The digest now reaches readers by email as well as Discord, and a run that fails
says so on both channels. Publishing to Pages and stopping a run were reworked
after a digest that was published but reported as failed, and a stopped run now
keeps its record. `/settings` can be completed on a first boot, and every page
says what it is and which language it is in. Two new runtime settings are
required, so read *Upgrading from 0.3.0* first.

### Upgrading from 0.3.0

- **Two new runtime settings, `notify.email_to` and `notify.email_from`.** Like
  every runtime setting they are required, and a run without them stops and
  names them. On a `json` install, add `email_to = ""` and `email_from = ""` to
  `[notify]` in `cyris.toml`; an empty `email_to` turns mail off. On a `d1`
  deployment, save the Notifications category on `/settings` (an empty recipient
  stores both), or run `cyris settings push`.
- **`CLOUDFLARE_API_TOKEN` needs Workers Scripts: Read** so a run can find the
  app Worker's custom domain for the digest link. Without it the link falls back
  to pages.dev and the run logs why. Add Email Sending: Edit if you set an email
  recipient.
- **`wrangler.toml` has a `[vars] CYRIS_APP_WORKER_NAME`** that must equal the
  Worker's `name`. A fork that renamed its Worker changes both.
- **The deploy workflow verifies what it deployed.** It needs a `CYRIS_UI_TOKEN`
  repository secret and a `CYRIS_DEPLOYMENT_URL` repository variable.
- **A source list with nothing to fetch stops the run.** An RSS source needs a
  feed URL and a newsletter needs a sender match; a deployment holding only
  sources without them used to run and fetch nothing.

The new D1 `digests` table is created on the first boot, like every other table.

### Added

- **The digest by email**, through Cloudflare Email Service. The mail carries the
  whole issue in a template made for mail clients, with a plain-text part. The
  recipient is a verified Email Routing destination address, and `/settings`
  stores the pair only after a test message is delivered or queued.
- **Failure alerts.** A run that raises, or fetches nothing because a source
  failed, sends a plain alert to Discord and to email, with the error or the
  failed sources and the time the run started. A dry run, a run stopped by
  SIGTERM and a run that fails before it starts send none.
- **Each issue's digest is stored in D1**, in a `digests` table: the final
  content of the issue's last run, which a preview never overwrites. A failed
  write is named in the run summary and never costs the run its result.
- **Sign in from any page.** A signed-out reader on the app Worker sees Sign in
  on the site bar, and `/login` returns to the page that asked.
- **`cyris doctor` checks mail and the digest link**: whether mail is on, and
  which host the digest link names, looked up the way a run looks it up.
- **A favicon and a meta description** on every page, and a project credit in
  every footer, the mail's included. The archive says in one sentence what Cyris is, and the credit links
  the word Cyris to the project's site.
- **Release and deploy checks.** The release workflow imports every `cyris`
  module inside the built image before pushing it, and the deploy workflow fails
  unless production's `/api/build` names the deployed commit.

### Changed

- **The digest link goes to the app Worker's custom domain on its own**, found
  through the Workers domains API, so Discord readers get vote buttons.
  `CYRIS_PROMOTE_CUSTOM_DOMAIN` is now an override.
- **Publishing follows Cloudflare's verdict.** The site's file list records a
  deployment once Cloudflare reports it landed, only a refused or failed
  deployment is redeployed, and one that never lands is named in the error.
  Publishing has a 180-second deadline and Pages requests time out at 20 seconds.
- **The run instance sleeps after 15 minutes**, not 5, so a slow run is no longer
  cut short; the `/settings` instance keeps 5.
- **A multi-article summarize group renders as one card**, on the page and in the
  mail: the group heading, the summary once, then each article with its own link,
  score and vote.
- **Page language.** Each page's `lang` is `en`, the digest content carries
  `[digest] output_language`, and text the model did not write is marked as an
  unknown language.
- **Secondary text uses `--text-dim`** and reaches 4.5:1 contrast on every
  background; `--text-faint` is kept for non-text marks.
- **Newsletter links.** One email is one article: links in the body no longer
  reach `ref_urls`, and Top story and Feature rows no longer list them as
  sources. The canonical link is a page on the sender's own host, accepted only
  when exactly one remains.
- **The README is the pitch**; source tiers and the article lifecycle moved to
  `docs/sources.md`, and the CLI list to the Cloudflare guide. The landing page
  shows a real digest and the delivery channels.
- **For contributors:** every test file carries one level (`unit`,
  `integration`, `e2e`) plus tags, defined in `CLAUDE.md` and enforced by a test.
  `scripts/check.sh` is the local and release gate, and many tests that only
  repeated documentation wording are gone.

### Fixed

- A digest that Cloudflare had published is no longer reported as failed and
  redeployed, and a landed deployment is recorded before its page is live, so
  the next deploy no longer drops it.
- A SIGTERM stops `cyris run` and the container's run pass with exit 143, after
  the running step has shut down and the run's `digest_runs` row is written. A
  failed run keeps its status when the stop lands during `promote-sync`.
- A failed read from the RSS or newsletter Worker now counts as a failed source,
  so the run lists it and the failure alert can fire.
- `/settings` on a first boot can save every category, including Discord turned
  off and an empty style or email recipient.
- `/settings` refuses an empty publish hour beside its field, instead of storing
  `00`, and refuses a source no fetcher reads.
- An expired session on `/settings` says to sign in again instead of failing
  with a script error.
- A local `cyris triage-ui` no longer links to an archive it does not serve, and
  serves its own favicon.
- A focused field marked missing shows its focus ring, and an unavailable
  provider's reason is no longer dimmed.
- `/settings` copy no longer names one deployment's date and domains.
- `bun run deploy` ignores a local `.env`, which would replace the
  `wrangler login` session with a stale token.
- A deploy of a moved `:release` tag now rolls out: the deploy workflow names the
  resolved image digest.

## [0.3.0] — 2026-09-22

cyris now runs end to end on Cloudflare: a Container behind a Worker runs the
pipeline on an hourly Cron Trigger, keeps its state in D1 and publishes to Pages,
and `/settings` edits every runtime setting and source. A local install with the
`json` store still works and is the other supported path. This release removes
several 0.2.0 features and makes every runtime setting required, so read
*Upgrading from 0.2.0* first.

### Upgrading from 0.2.0

For a local (`json`) install:

- **Every runtime setting is required now.** There are no defaults in code: a
  missing key stops `cyris run` and names itself. Compare your `cyris.toml` with
  `cyris.toml.example` and add what is missing (new since 0.2.0: `[store]`,
  `[notify] discord_webhook_url`, in `[digest]` the three snippet lengths,
  `max_featured` and `type_scale`, and in `[vote_similarity]` `provider`,
  `model` and `max_seeds`), then run `cyris doctor`, which
  lists every missing key and fails on any table this build no longer reads.
- **Turn on `[html_output] enabled = true`.** It was off in the 0.2.0 example,
  and with the Obsidian writer gone it is a local install's only output.
- **Delete the tables that are gone:** `[miniflux]`, `[obsidian]` and `[email]`.
  The Discord webhook moved from `[general.notify]` to `[notify]
  discord_webhook_url`, and `CYRIS_DISCORD_WEBHOOK_URL` is no longer read.
- **Vote similarity names its embedding provider.** 0.2.0 embedded with Gemini
  at `threshold = 0.68`. Keep `provider = "gemini"`, or switch to
  `"workers_ai"` and delete `threshold`: each provider has its own calibrated
  cutoff, and 0.68 on `workers_ai` would stop it suppressing anything.
- **Choose the LLM provider explicitly.** `[llm_provider] provider` is
  `anthropic`, `gemini`, `openai`, `workers_ai`, or `none` for plain excerpts
  with no key. A real provider whose key is missing stops the run.
- **`output_language` is a BCP 47 tag** (`zh-Hant`, `en`, `ja`). A plain
  language name, as 0.2.0 used, still works unchanged.
- **Scheduling:** `cyris schedule` and launchd are gone. Either run
  `docker compose up -d`, which now keeps `./agent-vault` on the host, or add one
  hourly cron line, `cd <repo> && uv run cyris run --if-due`, which picks the
  period from `[general] digest_schedule`.
- **Miniflux is gone:** feeds are polled directly at digest time, or buffered by
  `workers/rss/` if you deploy it. `aliases:` in `sources.yaml` is no longer
  read. Email-only newsletters need `workers/newsletter/`, because the local
  email server is gone.

To move to Cloudflare, follow `docs/install-cloudflare.md`. `cyris store
migrate` copies the local store into D1, and `cyris settings push` and
`cyris sources push` fill D1 from your `cyris.toml` and `sources.yaml`.

### Added

- **The Cloudflare deployment** (`workers/app/`). One image runs in three roles:
  `run` does one pipeline pass per hourly Cron Trigger and exits, `ui` serves
  `/settings` and stops after five idle minutes, and `cron` is the supercronic
  loop a local `docker compose` install uses. The first boot creates the D1
  tables and the first publish creates the Pages project. An authenticated
  `POST /run` starts a pass by hand. A Deploy to Cloudflare button exists for
  each of the four Workers, and `.env.example` lists what the app's deploy form
  asks for.
- **Auth on `/settings` and the vote API.** `/login` takes `CYRIS_UI_TOKEN` and
  sets an HttpOnly cookie, and the Worker answers anything without it before the
  container wakes. Cloudflare Access is an optional second layer on a custom
  hostname (`CYRIS_UI_ACCESS_HOST`).
- **Article store on D1** (`[store] backend = "d1"`). The LLM usage log, tags,
  stories and story membership live in the same database. `cyris store migrate`
  copies the JSON store in without overwriting a decision already made there,
  and `cyris store diff` compares the two.
- **Every runtime setting on `/settings`**: twenty-one keys in the Model, Digest,
  Pipeline and Notifications categories, plus a Sources category that adds,
  edits and retires feeds. Missing settings are marked. An LLM or embedder is
  checked with one real call, and a Discord webhook with Discord itself, before
  it is saved.
- **`cyris settings push` and `cyris sources push|list`.** They fill D1 from
  `cyris.toml` and `sources.yaml`. `settings push` never overwrites a row and
  prints the database id first.
- **`cyris doctor`.** It checks sources, the LLM provider, the store, every
  Worker and whether the digest can be published, prints a fix line per problem
  and exits non-zero when a run would break. `--deployment <url>` adds which
  image production runs, how far this checkout is ahead of it, and the last
  run's commit and status.
- **Two more LLM providers**: OpenAI and Cloudflare Workers AI (`workers_ai`,
  with `CLOUDFLARE_AI_TOKEN`), plus `provider = "none"` for excerpt-only digests
  by choice. `cyris llm-compare` digests one window with several providers side
  by side.
- **Workers AI embeddings** (`@cf/baai/bge-m3`) are the default for vote
  similarity, with no local cache. Each embedding provider keeps its own
  calibrated threshold in `src/cyris/provider_defaults.json`.
- **Pages publishing over REST.** Publishing uses the Pages direct-upload API
  instead of shelling out to `wrangler`. With D1 the site's file list lives in
  the `pages_manifest` table, and a deploy is refused when it would drop more
  than one live digest page.
- **A record of every run.** Each run logs one `run_summary` JSON line and, with
  D1, writes one `digest_runs` row, on every path including a crash. A run whose
  configured LLM did no work is flagged as degraded in its Discord message.
- **A redesigned reader UI.** One design language (`docs/design/ui-language.md`)
  now covers the archive, digest, raw and settings pages. The archive leads with
  a headline card and groups issues by month, and every page has a site bar. A
  signed-in reader gets a triage view on the raw page. A site-wide type size is
  set on `/settings`.
- **Two rejection reasons**, `not_interested` and `already_known`. A down vote
  records `not_interested`, and `cyris articles reject --reason` takes either.
- **`[digest] max_featured`** sets how many sections lead the digest.
- **A CI release workflow and a deploy workflow**, both dispatch-only GitHub
  Actions. The first builds and publishes the container image by commit; the
  second deploys a published image by tag or digest. Container placement is limited to North America,
  because Gemini and OpenAI refuse some egress locations.

### Changed

- **One home for runtime settings and sources per deployment** (breaking). A
  `d1` deployment reads them from D1 alone and a `json` one from `cyris.toml` and
  `sources.yaml` alone. A missing setting or an empty source list stops the run,
  and `cyris doctor` fails on either. On `json`, `/settings` is read-only.
- **The RSS Worker reads the D1 `sources` table.** An empty table polls nothing
  and logs how to fill it; an unreadable one fails the poll.
- **Worker tokens.** `rss` and `newsletter` share `CYRIS_WORKER_TOKEN`. The vote
  Worker keeps its own `CYRIS_PROMOTE_TOKEN`, and it is no longer rendered into
  published pages: readers vote through the app Worker's `POST /api/vote`, which
  adds the token server-side. `CYRIS_D1_API_TOKEN` is gone; `CLOUDFLARE_API_TOKEN`
  covers D1 and Pages.
- **The raw page** lists every article this run judged plus what is still
  pending, grouped by source, and is written as HTML only.
- `--dry-run` renders the HTML digest.
- `ArticleRepository` declares every method its callers use, and a test checks
  each implementation against it.

### Fixed

- A malformed LLM batch no longer costs the scoring pass its other batches'
  scores and tags, and a malformed filter entry is skipped with a warning.
- News clustering no longer drops articles the model left out of its answer.
- A failed usage-log write no longer costs the period its digest.
- Newsletter issues that share a link are kept as separate articles.
- Discord webhook tokens and API keys no longer appear in the run log.
- `POST /run?period=morning|evening` runs that digest outside the scheduled
  hours; before, a manual run outside them did nothing.
- The app Worker forwards `CLOUDFLARE_AI_TOKEN` to the container, so the
  `workers_ai` provider gets its own token on Cloudflare.
- `cyris doctor` names `provider = "none"` as the alternative to a missing LLM
  key.
- `docker compose` bind-mounts `./agent-vault`, so a `json` install's store and
  HTML survive `--force-recreate`.

### Removed

All of these are breaking.

- **Miniflux**: `MinifluxSource`, the Postgres dependency, `[miniflux]`,
  `CYRIS_MINIFLUX_API_KEY` and the `miniflux` and `db` compose services. Source
  `aliases:` went with it.
- **The Obsidian digest writer**: `[obsidian]`, `CYRIS_VAULT_PATH`,
  `cyris articles export` and the `-raw.md` companion. The digest's output is
  the HTML page.
- **Preference learning**: `cyris learn` and `cyris run --disable-learning`.
  Vote similarity is the personalization.
- **The local email path**: `cyris email-server`, `[email]` and
  `CYRIS_EMAIL_WEBHOOK_SECRET`. The newsletter Worker is the only email path.
- **`cyris schedule`** and the launchd plists.
- **Tracked topics** and the event store behind them.
- **The swipe deck** and its container routes. The raw page's triage view
  replaced it; pending articles outside an issue are reached through
  `cyris articles list|accept|reject`.
- **Every fallback behind a runtime setting or a source**: the code defaults,
  the `cyris.toml` and `sources.yaml` fallbacks under D1, and the RSS Worker's
  bundled feed list.
- **`CYRIS_DISCORD_WEBHOOK_URL`.** The webhook is a runtime setting.

## [0.2.0] — 2026-08-24

### Added

- **Cloudflare RSS Worker** (`workers/rss/`): an hourly cron polls every feed in
  `sources.yaml` into D1, so the 24h digest window sees articles a digest-time
  poll would have missed. Measured against Miniflux over the same window, polling
  once at digest time missed 141 of 317 articles; the buffer misses none.
- **Cloudflare newsletter Worker** (`workers/newsletter/`): Email Routing parses
  forwarded mail into KV for `cyris run` to pull, replacing the local maildir path.
- **Digest votes.** 👍/👎 on any digest item post to the promote Worker;
  `cyris promote-sync` applies them to the store — down rejects, up accepts — and
  stamps `triaged_at`, so only real human decisions feed `cyris learn`.
- **Raw companion page.** Every run also writes `{date}-{period}-raw.md` and
  `-raw.html` listing every article the window collected, uncapped and unfiltered,
  so what the digest dropped stays visible. Its rows carry votes too, which is how
  a rejected article gets pulled back.
- **Vote-similarity filtering** (`[vote_similarity]`, off by default): suppresses
  candidates that sit close to what you downvoted, using embeddings of titles.
  Preview with `cyris vote-sim`; compare providers with `cyris embed-compare`.
- **Newsletter canonical links.** An issue's 原文 link is now chosen structurally
  from the sender's own domain rather than a hostname allowlist, with recipient
  tokens hard-blocked from ever becoming a stored URL. Runs report how many issues
  fell back to a synthetic URL and how many digest items ended up with no link.
- **`[digest] output_language` and `style_prompt`** — digest language and tone are
  configurable rather than hardcoded.
- `CYRIS_DISCORD_WEBHOOK_URL` — the webhook no longer has to live in `cyris.toml`.
- `.github/ISSUE_TEMPLATE/bug_report.md`.

### Changed

- `max_articles_per_digest` raised 200 → 400. At 200 every run truncated, which
  threw away the buffer's whole benefit. Measured cost went *down*, because the
  cap had been leaving a backlog for the next run to pay for.
- The digest reads as five numbered layers addressed to the reader, instead of
  sections named after pipeline stages.
- Publishing to Pages verifies the page is live before reporting success — an
  exit code missed a truncated upload that wrangler swallowed as success.

### Removed

- **Paywall support** (breaking). The `[paywall]` config section,
  `SourceConfig.paywall`, `adapters/cookies.py`, `extractor.py`, and the
  `trafilatura` / `browser-cookie3` dependencies are gone. Measured over August it
  captured zero paid articles, and it reached a browser detail into `ports.py`.
  See *Paywalled sources* in `docs/sources.md` for what to do instead.
- The TMTB source, whose feed had served one item dated 2023.

### Fixed

- Newsletter items no longer reach the digest, or Discord, as dead
  `newsletter:<hash>` links: `DigestItem.link` falls through to `ref_urls`, and a
  source's `homepage` backs it up.
- News clusters keep every member's original link instead of collapsing to one.
- A `&section=` in a link's query string is no longer mangled into an HTML entity.

## [0.1.0] — 2026-07-14

Initial public release.

- Fetch from Miniflux RSS + newsletters, tier-based LLM filtering/summarization,
  Obsidian markdown digest output.
- LLM providers: Anthropic Claude (default) and Google Gemini, with graceful
  degradation on LLM failure.
- Swipe-based triage web UI; preference learning from digest feedback.
- Docker Compose stack (Miniflux + Postgres + cyris) and macOS launchd scheduling.
- Optional Cloudflare Workers for email-newsletter ingestion and promote/HTML publish.

[Unreleased]: https://github.com/musingfox/cyris/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/musingfox/cyris/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/musingfox/cyris/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/musingfox/cyris/releases/tag/v0.2.0
[0.1.0]: https://github.com/musingfox/cyris/releases/tag/v0.1.0
