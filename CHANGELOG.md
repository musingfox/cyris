# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- **cyris runs in a Cloudflare Container.** `workers/app/` fronts the existing
  image with a Worker: an hourly Cron Trigger replaces `docker/crontab`
  (`--if-due` reads its schedule from D1, so the logic moved unchanged), and any
  HTTP request wakes the triage UI, which sleeps five minutes later via
  `onActivityExpired → stop()`. One image, three roles, picked by `CYRIS_ROLE`:
  `run` does one pipeline pass and exits so the instance stops billing, `ui`
  serves the deck, and the default keeps the local supercronic loop for
  development. The Mac mini's `docker compose` was stopped the same day — two
  schedulers publishing to one Pages manifest is the failure this cutover had to
  avoid, so it is not additive.
- **Auth on the triage UI**, which had none: `127.0.0.1` was the whole security
  boundary, and a Container is a public write surface. Two layers —
  Cloudflare Access on the route (who you are; a dashboard step, because the
  hostname and policy are deployment identity) and `CYRIS_UI_TOKEN` checked in
  the Worker, where `/login` sets an HttpOnly cookie holding the token's
  SHA-256. Anything without it gets the form or a `401` before a byte reaches
  the container. Preview URLs are disabled: an Access application binds to one
  hostname, so a second hostname is a door it does not cover.
- **Source editing on `/settings`.** `POST /api/sources` upserts one row and
  `DELETE /api/sources/{name}` retires it — name, url, type, tier, tags,
  `homepage`, `email_match`, over the `sources` row that already existed. Adding
  a feed was previously an edit to `sources.yaml` plus `cyris sources push`, and
  in the Container that file is baked into the image. A write against an *empty*
  table seeds it with the run's effective sources first: an empty table means
  "use `sources.yaml`", so a single insert would otherwise flip the pipeline to
  D1 holding one feed and silently stop every other one.

- **`cyris doctor`.** A read-only pass over sources, LLM provider, vault paths,
  the article store, every Worker, and whether the digest can actually be
  published — with a fix line per problem and a non-zero exit when something
  would break a run. Every credential is checked by asking the API it is *for*:
  `/user/tokens/verify` answers only for user tokens and calls a working
  account-owned token invalid, so the store check runs a real query and the
  publish check asks the Pages API about the project.
- **`cyris sources push` / `cyris sources list`.** Source definitions can live in
  D1, which is what makes adding a feed a write instead of an image rebuild —
  `workers/rss/` reads the same table at poll time. `sources.yaml` stays the
  editable format and the fallback: an empty or unreachable table falls back to
  the file on both sides, so a half-migrated deployment keeps fetching rather
  than silently polling nothing.
- **Article store on Cloudflare D1** (`[store] backend = "d1"`, off by default).
  `D1ArticleStore` implements the same contract as the JSON store over D1's HTTP
  query API, and the usage log moves into the same database. `cyris store migrate`
  copies the local store in without ever overwriting a decision already made
  there; `cyris store diff` compares the two backends field by field. With state
  in D1, a dead local machine loses nothing.

### Changed

- **Grade C is seven environment variables, down from twelve.**
  `CYRIS_D1_API_TOKEN` was `CLOUDFLARE_API_TOKEN` under another name — the same
  string in `.env` twice, so `StoreConfig`'s fallback chain had never chosen its
  second branch. Probed against the live account, the two are indistinguishable
  (D1 200, Pages 200, upload-token 200, Workers AI 401, R2 403).
  `CLOUDFLARE_EMBEDDING_API_TOKEN` stays: it is genuinely a different permission.
  The three Worker bearers became one `CYRIS_WORKER_TOKEN` — three random values
  but never three trust domains, since they shared one `.env` and now one Worker
  secret store, so separating them bought independent rotation of keys nobody
  rotates. `PromoteConfig`, `NewsletterConfig` and `RssConfig` collapse into one
  `WorkerConfig`; the Worker-side secret names are unchanged.
- `ArticleRepository` now declares all 13 methods its callers use, not the 10 the
  digest run touches — a partial implementation used to fail at the triage UI
  instead of at the boundary.

### Fixed

- **A malformed LLM response no longer costs a scoring run its scores and tags.**
  `score_in_batches` wrapped no batch in a `try`, and `persist_tags` ran only
  after the loop, so one bad batch aborted the pass and took every completed
  batch's tags with it — `run_digest` caught it and the digest shipped silently
  unscored. Each batch now carries its own guard, and so does the tag write.

### Removed

- **Miniflux.** `MinifluxSource`, `MinifluxClient` and `SourceMatcher` are gone,
  and with them the Postgres dependency, the `[miniflux]` config section,
  `CYRIS_MINIFLUX_API_KEY`, and the `miniflux` + `db` services in
  `docker-compose.yml`. RSS now comes from the Cloudflare feed buffer, or from
  `RssSource` polling feeds directly when `[rss]` is unconfigured. Re-measured on
  2026-08-25 against a live direct poll: buffer 179 URLs, poll 95, **0 the poll
  saw that the buffer had not** — the buffer is a strict superset, and direct
  polling alone would have lost 84 of 179 articles to feed snapshots expiring.
- **Source aliases.** `aliases:` in `sources.yaml` mapped a feed's `<title>` to
  a source name, which only mattered while Miniflux served articles keyed by feed
  title. Every remaining source names itself from `sources.yaml` directly, so the
  whole parameter was being threaded through four adapters and read by none.
- **`FetchSource.mark_as_read`** and `SaveResult.miniflux_ids`. Every remaining
  source implemented `mark_as_read` as a no-op — the newsletter Worker ACKs its
  queue inside `fetch_articles` — so the read state that is left lives entirely in
  the article store.

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
  See *Paywalled Sources* in the README for what to do instead.
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

[Unreleased]: https://github.com/musingfox/cyris/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/musingfox/cyris/releases/tag/v0.2.0
[0.1.0]: https://github.com/musingfox/cyris/releases/tag/v0.1.0
