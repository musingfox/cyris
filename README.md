# Cyris

AI-powered information digest agent. Fetches articles from RSS feeds and newsletters, processes them through an LLM with tier-based filtering and summarization, and publishes an HTML digest to Cloudflare Pages.

## What it does

- Subscribes to RSS feeds and newsletters, listed in `sources.yaml` (or the D1
  `sources` table once you push / edit them)
- Reduces daily article volume by 80%+ through AI-powered filtering
- Scores and routes articles: high-relevance to the digest, lower to a triage queue
- Generates twice-daily HTML digest pages with thematic summaries (hours are
  configurable; news-tagged `filter` sources are clustered by topic)
- Ships each digest with a companion listing everything the window collected, grouped by
  source, so what the filters dropped stays inspectable instead of disappearing
- Lets you 👍/👎 items on the published digest; those votes accept or reject in the
  store. Optional vote-similarity filtering can suppress later candidates that sit
  close to a downvote
- Provides a swipe-based web UI for triaging borderline articles, plus `/settings`
  for the LLM provider, digest hours, and the source list

## Requirements

- Python 3.12+
- [uv](https://github.com/astral-sh/uv) package manager
- An LLM API key — Anthropic Claude, Google Gemini, OpenAI, or a Cloudflare Workers AI token
- Optional: a Cloudflare account — for the **feed buffer** (see below), **email-only
  newsletter** ingestion, digest votes, HTML publishing, D1 state, and the production
  Container

Without an LLM key the pipeline still runs, in degraded mode: fetch and store as
usual, digest as plain excerpts.

## Setup

```bash
# Clone and install
git clone https://github.com/musingfox/cyris.git && cd cyris
uv sync --dev

# Configure
cp cyris.toml.example cyris.toml           # LLM provider, store backend, Worker URLs
cp .env.example .env                       # add API keys
cp sources.example.yaml sources.yaml       # then define your RSS/newsletter sources

# Run
uv run cyris doctor                # check the config before the first run
uv run cyris run                   # full pipeline (fetch → score → digest)
```

`cyris doctor` is the fastest way to find out what is still missing — it checks the
config (and whether this build understands every table in it), the store, every
Worker and every Cloudflare token, and exits non-zero on anything that would break
a run. With `[store] backend = "json"` it also checks that the agent-vault path is
writable; with D1 there is nothing left on disk to probe.

Runtime settings (LLM provider, digest hours) and the source list are **D1 first,
file fallback**. `cyris.toml` and `sources.yaml` are what a fresh deployment starts
from; `/settings` and `cyris sources push` write the live copies.

### What needs what

Nothing below the first row is required. Start at the top and add only what you want.

| Feature | Needs | Cost |
|---|---|---|
| RSS digest, HTML output | An LLM API key | LLM usage only |
| Better feed coverage (see below) | A Cloudflare account, Workers Paid | US$5/mo |
| Production schedule + public triage UI | Same Workers Paid plan; a domain is an optional Access layer | same US$5/mo |
| Digest votes 👍/👎 | A Cloudflare account | Free tier |
| Published HTML digest | A Cloudflare account | Free tier |
| **Email-only newsletters** | A Cloudflare account **and your own domain** | Domain registration |
| State in the cloud (`[store] backend = "d1"`) | A Cloudflare account | Free tier |
| Vote-similarity filtering | A Workers AI embedding token, or Gemini | Inference only |

**Email Routing is the one thing that cannot be automated away.** It needs a
domain you control, so email-only newsletters — the ones with no feed at all —
need one too. The triage UI and digest archive run on `*.workers.dev` with the
`CYRIS_UI_TOKEN` cookie as the only lock. Cloudflare Access is an optional second
layer if you attach your own hostname. Newsletters that publish RSS (Substack,
Ghost, and most others) are just feeds; they need nothing extra.

### Where RSS comes from

Feeds are listed in `sources.yaml`, and there are two ways to read them.

**Directly** (the default — nothing to set up). At digest time cyris fetches each feed
and keeps the entries inside the window. Simple, but a feed only publishes its current
snapshot, and a busy one holds 2–4 hours of it. Against a 24h window that loses
articles: measured against an hourly aggregator over the same window, a digest-time
poll saw 176 of 317 articles.

**Through the Cloudflare feed buffer** (recommended, needs a Workers Paid plan). A cron
Worker polls every feed hourly into D1, and cyris reads a window out of the buffer, so
nothing expires between runs. Deploy `workers/rss/`, then set `[rss] worker_url` in
`cyris.toml` and `CYRIS_WORKER_TOKEN` in `.env`. See [`workers/rss/README.md`](workers/rss/README.md).

Once the buffer is deployed you can keep the source list in D1 — `cyris sources push`
from the file, or add/remove a feed on `/settings` — so adding a feed is a write
instead of a redeploy. `sources.yaml` stays the file you edit, and stays the fallback
when the table is empty or unreachable.

### Where the state lives

By default the article store is JSON files under `[agent_vault] path`, which is fine
until the machine holding them dies. Setting `[store] backend = "d1"` moves the store
and the usage log to Cloudflare D1 instead, and (when D1 is on) also holds sources,
runtime settings, topic tags, news-cluster stories, and the Pages file list.

`cyris store migrate` copies what you already have across without overwriting
anything, and `cyris store diff` compares the two before you commit to the switch.
**Pick one backend.** They are alternatives, never a pair — running both splits
decisions that `INSERT OR IGNORE` cannot heal. The full order is in
[`docs/cloud-migration.md`](docs/cloud-migration.md).

## Deployment

cyris runs as a Cloudflare Container fronted by a Worker — the schedule is an hourly
Workers Cron Trigger (`cyris run --if-due` plus `promote-sync`, then the instance
exits), and the triage UI wakes on request and sleeps again. Digest hours live in D1,
so changing them does not need a rebuild. See [`workers/app/README.md`](workers/app/README.md)
for the deploy steps, the secret list, and auth (`CYRIS_UI_TOKEN` cookie, with
optional Cloudflare Access on a hostname you own).

[![Deploy to Cloudflare](https://deploy.workers.cloudflare.com/button)](https://deploy.workers.cloudflare.com/?url=https://github.com/musingfox/cyris)

The button clones this repo into your own GitHub account, builds the container
image with Workers Builds, and deploys the Worker, its Durable Object and the
hourly cron. It asks for each secret in [`.env.example`](.env.example), with the
guidance in `package.json`'s `cloudflare.bindings` shown beside every field.

**One thing to do after it finishes.** The LLM provider is a runtime setting in
D1, not a deploy field — a deployed container has no `cyris.toml` to read one
from. Open `/settings`, pick the provider matching the key you pasted, and save.
Until you do, the hourly run still publishes, but as plain excerpts: unscored,
unsummarised. Every run logs a warning saying so — `wrangler tail cyris-app`
shows it.

**Three things it cannot do**, because Cloudflare's automatic provisioning does
not cover them — do these first and paste the results into the deploy form:

1. **Create the D1 database** (`wrangler d1 create cyris`, or the dashboard) and
   pass its UUID as `CYRIS_STORE_DATABASE_ID`. The container reaches D1 over
   REST rather than through a binding, so there is nothing for the deploy to
   provision. `cyris` creates the tables on first boot.
2. **Create the Pages project** — Deploy buttons only support Workers. Its name
   goes in `CYRIS_PROMOTE_PAGES_PROJECT` and its origin in `DIGEST_ORIGIN`.
3. **Attach a domain and Cloudflare Access**, if you want the second auth layer
   or email-only newsletters. Both are dashboard steps; see the table above for
   which features need a domain.

The other three Workers are separate deploys — one button deploys one Worker —
and each is optional. Their buttons live in their own READMEs:
[`workers/rss/`](workers/rss/README.md) (feed buffer),
[`workers/promote/`](workers/promote/README.md) (vote queue),
[`workers/newsletter/`](workers/newsletter/README.md) (email ingestion).
`rss` and `newsletter` accept the `CYRIS_WORKER_TOKEN` you set on the app; `promote` has
its own `CYRIS_PROMOTE_TOKEN`, kept apart because a vote button is a public capability.

**Point the rss Worker at the same D1 database as the app.** Its button
provisions a fresh one, and a fresh one has an empty `sources` table — the
Worker then falls back to the feed list bundled in `src/feeds.json` and buffers
feeds you never chose, logging one line about it and nothing more.

Deploying by hand instead:

```bash
cp .env.example .env        # API keys: ANTHROPIC/GEMINI/OPENAI, CYRIS_WORKER_TOKEN, ...
# docker build needs no cyris.toml / sources.yaml — the image copies
# sources.example.yaml to /app/sources.yaml. compose bind-mounts over that path.

# From the repo root: this Worker's wrangler.toml lives there, not in
# workers/app/, because its image is built from the whole repo. `--env-file
# /dev/null` keeps the .env you just wrote from overriding your wrangler login.
bun install && bunx wrangler deploy --env-file /dev/null
```

The same image runs locally with `docker compose up -d`, where the default `CYRIS_ROLE`
is a supercronic loop reading `docker/crontab` (digest at 08:00 & 20:00, promote-sync
hourly; set `TZ` to change the clock). That is the development path; the deployment is
the Container above, and running both at once means two schedulers publishing to one
Pages project.

See [`docs/architecture.md`](docs/architecture.md) for the core↔adapter map, data
residency, and how settings are graded.

## CLI Commands

```
cyris doctor                  Check the config; non-zero exit if a run would break
cyris run                     Full pipeline: fetch, score, digest
                              (--if-due only runs on a digest hour; --dry-run renders
                              without writing)
cyris promote-sync            Pull digest votes from the Worker (👍 accepts, 👎 rejects)
cyris triage-ui               Swipe-based triage web UI; /settings picks the LLM provider
                              and model, the digest hours, and edits the source list
cyris articles list           List articles in store
cyris articles accept|reject  Accept or reject articles by URL
cyris articles score          Score articles via AI
cyris articles clean          Delete old articles by state
cyris sources push|list       Make D1's source table match sources.yaml; show what it serves
cyris store migrate           Copy the local article store into D1
cyris store diff              Compare the JSON and D1 stores field by field
cyris llm-compare             Digest one window with several LLM providers, side by side
cyris embed-compare           Judge one window with both embedding providers
cyris vote-sim                Preview what vote similarity would suppress
```

## Architecture

Clean architecture — dependencies point inward, all IO lives at the edges:

```
entrypoints/     CLI + web servers (parse args, call a use case)
    │
service_layer/   use cases + Protocols (ports.py)   ← business logic
    │
domain/          pure models & rules (no IO)
    ▲
adapters/        concrete IO implementing the Protocols
    │
bootstrap.py     composition root: wires adapters into a Deps container

diagnostics/     off the pipeline: doctor + the two comparisons, whose subject
                 is the deployment rather than the digest
```

Pipeline: **Fetch → Store → Score → Process → Output**.
`service_layer/run_digest.py` orchestrates it; the CLI only parses args and
calls it via `bootstrap.build_deps`. Config is resolved by
`bootstrap.load_effective_config` (file, then D1 overlays). See
[`docs/architecture.md`](docs/architecture.md) for the full core↔adapter diagram.

### Adapters — the extension points

Everything pluggable lives in `adapters/`, wired in `bootstrap.build_deps()`.
Protocols in `service_layer/ports.py` are the clean seams:

| Kind | Protocol | Existing implementations | Add one to… |
|------|----------|--------------------------|-------------|
| **Fetch source** (input) | `FetchSource` | `RssSource`, `CloudflareRssSource`, `CloudflareNewsletterSource` | ingest a new article source |
| **LLM** | `LLMClient` | `AnthropicClient`, `GeminiClient`, `OpenAIClient`, `WorkersAIClient` | add an AI provider |
| **Storage** | `ArticleRepository` | `ArticleStore` (JSON), `D1ArticleStore` | swap persistence |
| **Embeddings** | `Embedder` | `WorkersAIEmbedder`, `GeminiEmbedder` | a new vote-similarity backend |
| **Output** (sinks) | *direct inject* | `HtmlDigestWriter` (digest + raw page), `publish` / `publish_site` (Cloudflare Pages), `notify` (Discord) | send the digest somewhere new |

`ports.py`'s rule: only genuine IO boundaries get a Protocol; single-implementation
components (`D1TagStore`, `D1StoryStore`, promote-sync, usage log) are injected
directly.

Core code (`service_layer/` + `domain/`) never changes when you swap or add an
adapter — that is the point of the Protocol seams.

#### Example: add a fetch source

```python
# 1. implement the FetchSource Protocol (service_layer/ports.py)
class MySource:
    async def fetch_articles(self, after, before, sources,
                             limit=200) -> list[Article]: ...
    async def health_check(self) -> bool: ...

# 2. register it in bootstrap.build_deps()
fetch_sources.append(MySource(...))
```

The pipeline picks it up automatically. An **output sink** is the same shape:
write the sink, add it to the `Deps` container, call it from `run_digest`.

### Source Tiers

| Tier | Processing | Example |
|------|-----------|---------|
| `filter` | Discard most; surface only significant headlines (<10% pass). News-tagged articles are clustered by topic. | TechCrunch, 聯合新聞網 |
| `summarize` | Scored by the LLM, then split by `[routing] summarize_score_threshold` into full summaries and brief mentions | Stratechery, Benedict Evans |
| `fan` | Passthrough. Never scored, filtered, or summarized | followed groups and newsletters |

Between scoring and the pipeline, **vote similarity** (off by default) can suppress
candidates sitting close to a downvoted article. It is the only personalization in
the pipeline. Preview with `cyris vote-sim` before turning `[vote_similarity]` on.

### Newsletter Ingestion

Two paths, depending on how the newsletter is delivered:

- **RSS newsletters** (Substack, Ghost, Squarespace, and most others) — add the
  feed to `sources.yaml` like any other. No extra setup.
- **Email-only newsletters** — require a **Cloudflare Email Worker** (needs a
  Cloudflare account + Email Routing). It parses forwarded mail into KV for
  `cyris run` to pull, matched to a source by `email_match`. Deploy + setup:
  [`workers/newsletter/README.md`](workers/newsletter/README.md).

**In short: email-only subscriptions depend on Cloudflare**; RSS newsletters do not.

### Paywalled Sources

**Not supported.** A paid source's public feed usually carries only the first
paragraph or two, and cyris takes the feed at face value — it will not log in,
carry a session, or otherwise reach behind a paywall.

If you subscribe to something and want its full text in your digest, check what
the publisher already offers you as a subscriber, in this order:

1. **A subscriber-only RSS feed.** Many paid publications issue a personalised
   feed URL — Stratechery's Passport (`stratechery.passport.online/feed/rss/<id>`)
   is one, Substack paid subscriptions are another. Add it to `sources.yaml` like
   any feed. This is the only option that needs nothing from cyris. Treat that URL
   as a secret: it identifies you, and sharing it usually violates the publisher's
   terms.
2. **The email edition.** If the paid content arrives in your inbox in full,
   route it through the newsletter path above and it becomes a normal source.
3. **Neither.** Then read it in your browser — cyris will not log in on your
   behalf, and there is no plan for it to.

An earlier version of cyris read cookies out of the local browser's SQLite to do
this. It was removed in 2026-08: it only worked while a logged-in desktop browser
sat next to the pipeline, and it tied a fetch-path detail to one machine.

### Article Lifecycle

```
pending → accepted / rejected / awaiting_triage
```

👍/👎 on a published digest, the triage UI, and `cyris articles accept|reject`
all stamp `triaged_at`. That stamp is what later vote-similarity treats as a
human decision.

## Contributing

```bash
uv sync --dev
uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/   # lint
uv run pytest                                                             # tests
```

Conventions:
- **Adapters**: new IO goes in `adapters/` behind a Protocol (see above), never
  in `service_layer/` or `domain/`.
- **Tests**: inject `FakeLLM` (`tests/fakes.py`) instead of patching the SDK;
  patch where a symbol is *used*, not defined; give external resource names
  unique per-test suffixes.
- **Style**: ruff (line length 100); Pydantic v2 for all models and config.
- **Commits**: keep them atomic; lint + tests green before a PR.

Digest output language is configurable via `[digest] output_language`, a BCP 47
tag (default `zh-Hant`); `service_layer/languages.json` maps it to the wording the
model is given, and an unlisted tag is passed through as-is.
`[digest] style_prompt` injects a custom tone/focus into the prompts.

## Tech Stack

| Component | Choice |
|-----------|--------|
| Language | Python 3.12+ |
| Package manager | uv |
| Feed buffer | Cloudflare Worker cron → D1 (optional) |
| Article store | JSON files, or Cloudflare D1 |
| AI processing | Anthropic Claude, Google Gemini, OpenAI, or Cloudflare Workers AI |
| Scheduling | Workers Cron Trigger → Cloudflare Container (`CYRIS_ROLE=run`) |
| Triage UI | same Container, on request (`CYRIS_ROLE=ui`), behind Access |
| Output | Cloudflare Pages (HTML) |
| Notifications | Discord webhook |

## License

[AGPL-3.0-or-later](LICENSE) © 2026 musingfox

Self-host, use, and modify cyris freely. If you run a modified version as a
network service, the AGPL requires you to offer users its source. For use
outside AGPL terms (e.g. a closed-source/commercial deployment), contact the
author about a commercial license.
