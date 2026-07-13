# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Cyris is a local-first AI-powered information digest agent. It fetches articles from RSS (via Miniflux) and newsletters, processes them through an LLM (Anthropic Claude or Google Gemini) with tier-based filtering/summarization, and outputs Obsidian markdown digest notes.

## Commands

```bash
# Install dependencies (dev)
uv sync --dev

# Run CLI
uv run cyris

# Lint
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/

# Auto-fix lint issues
uv run ruff check --fix src/ tests/
uv run ruff format src/ tests/

# Run all tests
uv run pytest

# Run a single test file
uv run pytest tests/test_config.py

# Run a single test function
uv run pytest tests/test_config.py::test_function_name -v
```

## Architecture

Clean-architecture layering: **entrypoints → service_layer → domain**, with **adapters** implementing the service layer's Protocols. Pipeline flow: **Fetch → Store → Score → Process → Output**.

```
src/cyris/
├── bootstrap.py      # Composition root: Deps container + build_deps(cfg)
├── config.py         # Loads cyris.toml + sources.yaml (Pydantic validation)
├── domain/           # Pure business models and rules (pydantic/stdlib only)
│   ├── models.py            # Article, StoredArticle, DigestContent, Tier, ArticleState, ...
│   ├── selection.py         # Score-based selection: layer_by_score, split_summarize_tier_by_score
│   ├── language.py          # Language detection utilities
│   ├── tracking.py          # TrackedTopic model
│   └── triage.py            # RejectReason (canonical rejection reasons)
├── service_layer/    # Use cases and business services
│   ├── ports.py             # Protocols: LLMClient, ArticleRepository, FetchSource + complete_json
│   ├── run_digest.py        # Use case: full pipeline run (fetch→store→score→digest→output)
│   ├── learning.py          # Use case: learn preferences from triage/digest feedback
│   ├── digest_pipeline.py   # DigestPipeline: tier-based digest processing
│   ├── scoring.py           # AI article scoring (score_in_batches shared loop)
│   ├── filtering.py         # Filter tier: batch headline extraction (<10% pass)
│   ├── summarize.py         # Summarize tier: per-group thematic summaries
│   ├── cluster_news.py      # News clustering for news-tagged filter-tier articles
│   ├── fetching.py          # fetch_all_articles across FetchSources with dedup
│   ├── triage.py            # Digest feedback scanning and triage processing
│   ├── prompts.py           # Claude API prompt templates
│   └── parse.py             # AI response JSON extraction
├── adapters/         # Concrete IO implementations
│   ├── anthropic_client.py  # AnthropicClient (implements LLMClient)
│   ├── store/               # ArticleStore (JSON, dedup by URL) + event store/schema
│   ├── fetch/               # Miniflux client/source, newsletter archive + Cloudflare-worker sources, extractor, email parser
│   ├── output/              # DigestWriter, HTML digest, article export, publish, usage log
│   ├── notify.py            # Discord and ntfy.sh notifications
│   ├── promotions.py        # Cloud Worker promotion sync
│   ├── tracking_yaml.py     # tracking.yaml load/upsert
│   ├── cookies.py           # Browser cookie extraction (Zen/Chrome/Firefox)
│   └── http_client.py       # Shared httpx client
├── entrypoints/      # CLI and web servers
│   ├── cli.py               # Typer CLI (entry point: cyris.entrypoints.cli:app)
│   ├── triage_server.py     # Swipe-based triage web UI (aiohttp) + static/
│   └── webhook_server.py    # Email webhook receiver for newsletter ingestion
├── learn/            # Preference learning helpers (profile, embeddings, feedback parsing)
├── schedule/         # macOS launchd plist management
└── utils/            # timezone helpers (cross-cutting)

workers/              # Cloudflare Workers (deployed to the user's CF account)
├── promote/          # Promote-button clicks: KV queue, cyris pulls (adapters/promotions.py)
└── newsletter/       # Email→RSS ingestion: Email Worker parses mail → KV, cyris pulls
                      #   (adapters/fetch/newsletter_worker_source.py). See its README to deploy.
```

### Key Data Flow

`service_layer/run_digest.py` is the single pipeline orchestrator (the CLI only parses args and calls it via `bootstrap.build_deps`):

1. `service_layer/fetching.py` pulls from all FetchSources (Miniflux, newsletter archive, and the Cloudflare newsletter Worker when configured) within a time window
2. Articles are saved to the ArticleStore for dedup and persistent lifecycle tracking
3. `service_layer/scoring.py` scores non-news articles via the LLM for relevance ranking
4. `service_layer/digest_pipeline.py` processes articles: filter tier batches for headline extraction, summarize tier generates per-article summaries (split by score threshold)
5. `service_layer/cluster_news.py` clusters news-tagged filter-tier articles by topic
6. `adapters/output/digest.py` renders the final Obsidian markdown note; `domain/selection.py` layers featured articles by score
7. `service_layer/learning.py` turns triage/digest feedback into a preference profile + embedding centroid

### Adapter Extension Points

All IO is behind `adapters/`, wired in `bootstrap.build_deps()`. When adding or swapping IO, work at these seams — never touch `service_layer/` or `domain/`:

- **`FetchSource`** (`ports.py`) — input sources. Implement `fetch_articles` / `mark_as_read` / `health_check`, then append to `fetch_sources` in `build_deps()`. Existing: `MinifluxSource`, `NewsletterArchiveSource`, `CloudflareNewsletterSource`.
- **`LLMClient`** (`ports.py`) — AI providers. Implement `complete()`; selected in `build_llm()`. Existing: `AnthropicClient`, `GeminiClient`.
- **`ArticleRepository`** (`ports.py`) — persistence. `ArticleStore` satisfies it structurally (no explicit inheritance).
- **Output sinks** — `DigestWriter`, `HtmlDigestWriter`, `publish`, `notify` are injected directly (single impl, no Protocol). Add a sink by extending the `Deps` dataclass + wiring in `build_deps()`, then calling it from `run_digest`.

`ports.py` rule: only genuine IO boundaries get a Protocol; single-implementation components are injected directly. Full map: `docs/architecture.md`.

### CLI Commands

| Command | Description |
|---------|-------------|
| `cyris run` | Full pipeline: fetch → store → score → digest |
| `cyris learn` | Analyze digest feedback, generate preference profile + embeddings |
| `cyris schedule install\|uninstall\|status` | Manage launchd runs (digest + hourly promote-sync jobs) |
| `cyris promote-sync` | Pull deep-read promotions from the Worker to the vault (no fetch/LLM) |
| `cyris email-server` | Legacy local email webhook receiver (superseded by the Cloudflare newsletter Worker) |
| `cyris triage-ui` | Start swipe-based web UI for article classification |
| `cyris articles list\|accept\|reject\|export\|clean\|score\|triage` | Article store management |

### Configuration Files

- `cyris.toml` — app config (API endpoints, vault paths, LLM provider/model, digest limits, schedule, routing thresholds, `[promote]`/`[newsletter]` Worker URLs)
- `sources.yaml` — RSS/newsletter source definitions with tier, tags, and aliases; email-only sources use `type: newsletter` + `email_match: "from:..."`
- `agent-vault/tracking.yaml` — tracked topics list (name/keywords/created; gitignored, copy from tracking.example.yaml)
- `.env` — secrets (API keys for Miniflux, Anthropic/Gemini; `CYRIS_PROMOTE_TOKEN`, `CYRIS_NEWSLETTER_TOKEN`; Discord webhook)

### Agent Vault (`agent-vault/`)

Agent-owned Obsidian vault for persistent state. `agent-vault/daily/` holds raw article collections (gitignored). `agent-vault/articles/` holds the persistent article store (gitignored). `agent-vault/learning/` holds preference profile and embedding centroid. `agent-vault/events/` holds persistent event timeline files (tracked in git). `agent-vault/tracking.yaml` holds tracked interest topics (gitignored).

## Conventions

- Python 3.12+ required
- Ruff for linting and formatting (line-length 100, see `pyproject.toml [tool.ruff]` for rule selection)
- Pydantic v2 for all data models and config validation
- pytest with `pytest-asyncio` (auto mode) for async tests
- Source tiers determine processing depth: `filter` = aggressive discard, `summarize` = full summary
- Article lifecycle states: `pending` → `accepted`/`rejected`/`awaiting_triage`
- Digest output is in 繁體中文 (Traditional Chinese) section headings with mixed-language content
- Test isolation: external resource names (labels, paths, IDs) must be unique per test — use `tmp_path` or random suffixes, never share production identifiers
- Mock patching: always patch where the function is **used**, not where it is **defined** (e.g. patch `cyris.service_layer.run_digest.now_in_timezone`, not `cyris.utils.timezone.now_in_timezone`)
- LLM calls in tests: inject `FakeLLM` (tests/fakes.py) instead of patching the Anthropic SDK; only CLI-level e2e tests patch the single adapter point `cyris.adapters.anthropic_client.anthropic.AsyncAnthropic`
