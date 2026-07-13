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
- Anthropic API key (Claude Sonnet for batch processing)
- Obsidian vault for digest output
- Optional: Ollama with `nomic-embed-text` for embedding-based pre-filtering

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

```
Miniflux / Newsletters
        │
        ▼
   Fetch & Store ─── article_store (JSON, dedup by URL)
        │
        ▼
   AI Scoring ────── Claude API rates article relevance
        │
   ┌────┴─────┐
   ▼          ▼
Digest     Triage UI
Pipeline   (swipe web UI)
   │
   ▼
Obsidian Markdown ── user vault digest note
   │
   ▼
Learn Loop ───────── feedback → preference profile + embeddings
```

### Source Tiers

| Tier | Processing | Example |
|------|-----------|---------|
| `filter` | Discard most; surface only significant headlines (<10% pass) | TechCrunch, 聯合新聞網 |
| `summarize` | Per-article or cross-source paragraph summary | Stratechery, Benedict Evans |

### Newsletter Ingestion

Most newsletters (Substack, Ghost, Squarespace) expose RSS — subscribe via Miniflux.
Genuinely email-only newsletters route through a Cloudflare Email Worker that queues
parsed mail in KV for `cyris run` to pull. Deploy + setup: [`workers/newsletter/README.md`](workers/newsletter/README.md).

### Article Lifecycle

```
pending → scored → routed → accepted / rejected / awaiting_triage
```

## Development

```bash
uv run ruff check src/ tests/    # lint
uv run ruff format src/ tests/   # format
uv run pytest                    # test (480 tests)
```

## Tech Stack

| Component | Choice |
|-----------|--------|
| Language | Python 3.12+ |
| Package manager | uv |
| RSS aggregator | Miniflux (Docker) |
| AI processing | Claude API (Sonnet) |
| Full-text extraction | trafilatura |
| Paywall handling | browser cookies (Zen/Chrome/Firefox) + httpx |
| Preference learning | Claude API + Ollama embeddings |
| Scheduling | macOS launchd (local) · supercronic (Docker) |
| Output | Obsidian (filesystem) |
| Notifications | Discord webhook, ntfy.sh |

## License

Private project.
