# Cyris

AI-powered information digest agent. Fetches articles from RSS (via Miniflux) and newsletters, processes them through Claude API with tier-based filtering and summarization, and outputs Obsidian markdown digest notes.

## What it does

- Subscribes to 50+ RSS feeds and newsletters via self-hosted Miniflux
- Reduces daily article volume by 80%+ through AI-powered filtering
- Scores and routes articles: high-relevance to digest, lower to triage queue
- Generates twice-daily Obsidian markdown digest notes with thematic summaries
- Learns user preferences from digest feedback to improve filtering over time
- Provides a swipe-based web UI for triaging borderline articles

## Requirements

- Python 3.12+
- [uv](https://github.com/astral-sh/uv) package manager
- [Miniflux](https://miniflux.app/) RSS aggregator (Docker)
- An LLM API key — Anthropic Claude (default) or Google Gemini
- Obsidian vault for digest output
- Optional: Ollama with `nomic-embed-text` for embedding-based pre-filtering
- Optional: a Cloudflare account — only for **email-only newsletter** ingestion and
  the promote / HTML-publish features (RSS feeds + the digest work without it)

## Setup

```bash
# Clone and install
git clone <repo-url> && cd cyris
uv sync --dev

# Configure
cp cyris.toml.example cyris.toml   # edit API endpoints, vault paths
cp .env.example .env               # add API keys
# edit sources.yaml                # define RSS/newsletter sources

# Run
uv run cyris run                   # full pipeline (fetch → score → digest)
```

## Docker Deployment

Run the full stack (Miniflux + cyris) in containers — no macOS/launchd dependency:

```bash
cp .env.example .env        # API keys: ANTHROPIC/GEMINI, CYRIS_MINIFLUX_API_KEY, ...
# edit cyris.toml + sources.yaml as usual

docker compose up -d        # builds cyris, starts Miniflux + scheduled cyris
```

Scheduling runs in-container via [supercronic](https://github.com/aptible/supercronic)
(`docker/crontab`): digest at 08:00 & 20:00, promote-sync hourly. Set `TZ` to change the clock.

Container config is injected via environment (compose overrides your `cyris.toml`, so
the same config file works locally and in Docker):

| Env var | Purpose | Default |
|---------|---------|---------|
| `CYRIS_MINIFLUX_URL` | Miniflux endpoint | `http://miniflux:8080` (compose service) |
| `CYRIS_VAULT_PATH` | Obsidian digest output (in-container) | `/vault` |
| `CYRIS_VAULT_HOST_PATH` | Host path mounted to `/vault` | `./vault` |
| `CYRIS_AGENT_VAULT_PATH` | Persistent article store | `/data/agent-vault` (named volume) |

Notes:
- Paywall cookies are unavailable in-container (no browser); paywall full-text extraction is skipped — see [`docs/deployment.md`](docs/deployment.md).
- HTML digest publish (`bunx wrangler pages deploy`) needs `CLOUDFLARE_API_TOKEN` in `.env`.

See [`docs/deployment.md`](docs/deployment.md) for local-vs-Cloudflare tradeoffs and
[`docs/architecture.md`](docs/architecture.md) for the core↔adapter map.

## CLI Commands

```
cyris run                     Full pipeline: fetch, score, digest
cyris learn                   Update preference profile from digest feedback
cyris schedule install        Install launchd jobs (digest + hourly promote-sync)
cyris promote-sync            Pull deep-read promotions from the Worker to the vault
cyris email-server            Legacy local email webhook (see workers/newsletter for the Cloudflare path)
cyris triage-ui               Start swipe-based triage web UI
cyris articles list           List articles in store
cyris articles export         Export accepted articles to vault
cyris articles score          Score articles via AI
cyris articles triage         Process digest feedback and export
cyris articles clean          Delete old rejected articles
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
| **Fetch source** (input) | `FetchSource` | `MinifluxSource`, `NewsletterArchiveSource`, `CloudflareNewsletterSource` | ingest a new article source |
| **LLM** | `LLMClient` | `AnthropicClient`, `GeminiClient` | add an AI provider |
| **Storage** | `ArticleRepository` | `ArticleStore` (JSON) | swap persistence (SQL, object store) |
| **Output** (sinks) | *direct inject* | `DigestWriter` (Obsidian md), `HtmlDigestWriter`, `publish` (Cloudflare Pages), `notify` (Discord/ntfy) | send the digest somewhere new |

Core code (`service_layer/` + `domain/`) never changes when you swap or add an
adapter — that is the point of the Protocol seams.

#### Example: add a fetch source

```python
# 1. implement the FetchSource Protocol (service_layer/ports.py)
class MySource:
    async def fetch_articles(self, after, before, sources,
                             aliases=None, limit=200, cookies=None) -> list[Article]: ...
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

- **RSS newsletters** (Substack, Ghost, Squarespace, and most others) — subscribe
  via Miniflux like any other feed. No extra setup.
- **Email-only newsletters** — require a **Cloudflare Email Worker** (needs a
  Cloudflare account + Email Routing). It parses forwarded mail into KV for
  `cyris run` to pull, matched to a source by `email_match`. Deploy + setup:
  [`workers/newsletter/README.md`](workers/newsletter/README.md). A legacy local
  webhook (`cyris email-server`) also exists but is superseded.

**In short: email-only subscriptions depend on Cloudflare** (or the legacy local
webhook); RSS newsletters do not.

### Article Lifecycle

```
pending → scored → routed → accepted / rejected / awaiting_triage
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
| RSS aggregator | Miniflux (Docker) |
| AI processing | Anthropic Claude or Google Gemini |
| Full-text extraction | trafilatura |
| Paywall handling | browser cookies (Zen/Chrome/Firefox) + httpx |
| Preference learning | Claude API + Ollama embeddings |
| Scheduling | macOS launchd (local) · supercronic (Docker) |
| Output | Obsidian (filesystem) |
| Notifications | Discord webhook, ntfy.sh |

## License

[AGPL-3.0-or-later](LICENSE) © 2026 musingfox

Self-host, use, and modify cyris freely. If you run a modified version as a
network service, the AGPL requires you to offer users its source. For use
outside AGPL terms (e.g. a closed-source/commercial deployment), contact the
author about a commercial license.
