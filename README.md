# Cyris

AI-powered information digest agent. Fetches articles from RSS feeds and newsletters, processes them through Claude API with tier-based filtering and summarization, and outputs Obsidian markdown digest notes.

## What it does

- Subscribes to 50+ RSS feeds and newsletters, listed in one `sources.yaml`
- Reduces daily article volume by 80%+ through AI-powered filtering
- Scores and routes articles: high-relevance to digest, lower to triage queue
- Generates twice-daily Obsidian markdown digest notes with thematic summaries
- Ships each digest with a companion listing everything the window collected, grouped by
  source, so what the filters dropped stays inspectable instead of disappearing
- Learns user preferences from digest feedback to improve filtering over time
- Provides a swipe-based web UI for triaging borderline articles

## Requirements

- Python 3.12+
- [uv](https://github.com/astral-sh/uv) package manager
- An LLM API key — Anthropic Claude (default) or Google Gemini
- Obsidian vault for digest output
- Optional: a Cloudflare account — for the **feed buffer** (see below), **email-only
  newsletter** ingestion, and the promote / HTML-publish features

## Setup

```bash
# Clone and install
git clone https://github.com/musingfox/cyris.git && cd cyris
uv sync --dev

# Configure
cp cyris.toml.example cyris.toml           # edit API endpoints, vault paths
cp .env.example .env                       # add API keys
cp sources.example.yaml sources.yaml       # then define your RSS/newsletter sources

# Run
uv run cyris doctor                # check the config before the first run
uv run cyris run                   # full pipeline (fetch → score → digest)
```

`cyris doctor` is the fastest way to find out what is still missing — it checks the
config, both vault paths, the store, every Worker and every Cloudflare token, and
exits non-zero on anything that would break a run.

### What needs what

Nothing below the first row is required. Start at the top and add only what you want.

| Feature | Needs | Cost |
|---|---|---|
| RSS digest to Obsidian | An LLM API key | LLM usage only |
| Better feed coverage (see below) | A Cloudflare account | Workers Paid, US$5/mo |
| Digest votes 👍/👎 | A Cloudflare account | Free tier |
| Published HTML digest | A Cloudflare account | Free tier |
| **Email-only newsletters** | A Cloudflare account **and your own domain** | Domain registration |
| State in the cloud (`[store] backend = "d1"`) | A Cloudflare account | Free tier |

**The domain is the one thing that cannot be automated away.** Cloudflare Email
Routing needs a domain you control, so email-only newsletters — the ones with no
feed at all — need one too. Everything else works on a `*.workers.dev` subdomain.
Newsletters that publish RSS (Substack, Ghost, and most others) are just feeds; they
need nothing extra.

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
`cyris.toml` and `CYRIS_RSS_TOKEN` in `.env`. See [`workers/rss/README.md`](workers/rss/README.md).

Once the buffer is deployed you can also keep the source list in D1 with
`cyris sources push`, so adding a feed is a write instead of a redeploy.
`sources.yaml` stays the file you edit, and stays the fallback.

### Where the state lives

By default the article store is JSON files under `[agent_vault] path`, which is fine
until the machine holding them dies. Setting `[store] backend = "d1"` moves the store
and the usage log to Cloudflare D1 instead. `cyris store migrate` copies what you
already have across without overwriting anything, and `cyris store diff` compares the
two before you commit to the switch. The full order is in
[`docs/cloud-migration.md`](docs/cloud-migration.md).

## Docker Deployment

Run cyris in a container — no macOS/launchd dependency:

```bash
cp .env.example .env        # API keys: ANTHROPIC/GEMINI, CYRIS_RSS_TOKEN, ...
# edit cyris.toml + sources.yaml as usual

docker compose up -d        # builds cyris and runs it on a schedule
```

Scheduling runs in-container via [supercronic](https://github.com/aptible/supercronic)
(`docker/crontab`): digest at 08:00 & 20:00, promote-sync hourly. Set `TZ` to change the clock.

Container config is injected via environment (compose overrides your `cyris.toml`, so
the same config file works locally and in Docker):

| Env var | Purpose | Default |
|---------|---------|---------|
| `CYRIS_VAULT_PATH` | Obsidian digest output (in-container) | `/vault` |
| `CYRIS_VAULT_HOST_PATH` | Host path mounted to `/vault` | `./vault` |
| `CYRIS_AGENT_VAULT_PATH` | Persistent article store | `/data/agent-vault` (named volume) |

Notes:
- HTML digest publish (`wrangler pages deploy`) needs `CLOUDFLARE_API_TOKEN` in `.env`.

See [`docs/deployment.md`](docs/deployment.md) for local-vs-Cloudflare tradeoffs and
[`docs/architecture.md`](docs/architecture.md) for the core↔adapter map.

## CLI Commands

```
cyris doctor                  Check the config; non-zero exit if a run would break
cyris run                     Full pipeline: fetch, score, digest
cyris learn                   Update preference profile from digest feedback
cyris schedule install        Install launchd jobs (digest + hourly promote-sync)
cyris promote-sync            Pull digest votes from the Worker (👍 accepts, 👎 rejects)
cyris email-server            Legacy local email webhook (see workers/newsletter for the Cloudflare path)
cyris triage-ui               Start swipe-based triage web UI
cyris articles list           List articles in store
cyris articles export         Export accepted articles to vault
cyris articles score          Score articles via AI
cyris articles triage         Process digest feedback and export
cyris articles clean          Delete old rejected articles
cyris store migrate           Copy the local article store into D1
cyris store diff              Compare the JSON and D1 stores field by field
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
```

Pipeline: **Fetch → Store → Score → Process → Output**.
`service_layer/run_digest.py` orchestrates it; the CLI only parses args and
calls it via `bootstrap.build_deps`. See
[`docs/architecture.md`](docs/architecture.md) for the full core↔adapter diagram.

### Adapters — the extension points

Everything pluggable lives in `adapters/`, wired in `bootstrap.build_deps()`.
Three Protocols in `service_layer/ports.py` are the clean seams:

| Kind | Protocol | Existing implementations | Add one to… |
|------|----------|--------------------------|-------------|
| **Fetch source** (input) | `FetchSource` | `RssSource`, `CloudflareRssSource`, `NewsletterArchiveSource`, `CloudflareNewsletterSource` | ingest a new article source |
| **LLM** | `LLMClient` | `AnthropicClient`, `GeminiClient` | add an AI provider |
| **Storage** | `ArticleRepository` | `ArticleStore` (JSON) | swap persistence (SQL, object store) |
| **Output** (sinks) | *direct inject* | `DigestWriter` (Obsidian md + raw list), `HtmlDigestWriter` (digest + raw page), `publish` (Cloudflare Pages), `notify` (Discord) | send the digest somewhere new |

Core code (`service_layer/` + `domain/`) never changes when you swap or add an
adapter — that is the point of the Protocol seams.

#### Example: add a fetch source

```python
# 1. implement the FetchSource Protocol (service_layer/ports.py)
class MySource:
    async def fetch_articles(self, after, before, sources,
                             aliases=None, limit=200) -> list[Article]: ...
    async def mark_as_read(self, article_ids) -> None: ...   # no-op if unsupported
    async def health_check(self) -> bool: ...

# 2. register it in bootstrap.build_deps()
fetch_sources.append(MySource(...))
```

The pipeline picks it up automatically. An **output sink** is the same shape:
write the sink, add it to the `Deps` container, call it from `run_digest`.

### Source Tiers

| Tier | Processing | Example |
|------|-----------|---------|
| `filter` | Discard most; surface only significant headlines (<10% pass) | TechCrunch, 聯合新聞網 |
| `summarize` | Per-article or cross-source paragraph summary | Stratechery, Benedict Evans |

### Newsletter Ingestion

Two paths, depending on how the newsletter is delivered:

- **RSS newsletters** (Substack, Ghost, Squarespace, and most others) — add the
  feed to `sources.yaml` like any other. No extra setup.
- **Email-only newsletters** — require a **Cloudflare Email Worker** (needs a
  Cloudflare account + Email Routing). It parses forwarded mail into KV for
  `cyris run` to pull, matched to a source by `email_match`. Deploy + setup:
  [`workers/newsletter/README.md`](workers/newsletter/README.md). A legacy local
  webhook (`cyris email-server`) also exists but is superseded.

**In short: email-only subscriptions depend on Cloudflare** (or the legacy local
webhook); RSS newsletters do not.

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

Digest output language is configurable via `[digest] output_language` (default
繁體中文); `[digest] style_prompt` injects a custom tone/focus into the prompts.

## Tech Stack

| Component | Choice |
|-----------|--------|
| Language | Python 3.12+ |
| Package manager | uv |
| Feed buffer | Cloudflare Worker cron → D1 (optional) |
| AI processing | Anthropic Claude or Google Gemini |
| Preference learning | Claude API |
| Scheduling | macOS launchd (local) · supercronic (Docker) |
| Output | Obsidian (filesystem) |
| Notifications | Discord webhook |

## License

[AGPL-3.0-or-later](LICENSE) © 2026 musingfox

Self-host, use, and modify cyris freely. If you run a modified version as a
network service, the AGPL requires you to offer users its source. For use
outside AGPL terms (e.g. a closed-source/commercial deployment), contact the
author about a commercial license.
