# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Cyris is a local-first AI-powered information digest agent. It fetches articles from RSS feeds and newsletters, processes them through an LLM (Anthropic, Gemini, OpenAI or Cloudflare Workers AI) with tier-based filtering/summarization, and publishes an HTML digest to Cloudflare Pages.

## Commands

```bash
# Install dependencies (dev)
uv sync --dev

# Everything CI runs — JS tests, ruff, pytest — in one command
scripts/check.sh

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

# Real newsletter extractor checks (skip if samples are absent)
# Samples live outside the repo: $CYRIS_NEWSLETTER_FIXTURES
# (default ~/cyris-newsletter-fixtures): manpao.text.txt, ieo.html, fenshi.html
uv run pytest tests/test_newsletter_real_fixtures.py
```

## Architecture

**Read `docs/architecture.md` before any non-trivial change.** It is the authoritative description of this system, and it is written to be acted on, not just read:

- **§2 Wiring** — which boundaries have a Protocol (cheap to swap) and which are direct injections. It also names what is *out* of the target architecture, not merely unfinished: Obsidian output is gone, and the local-filesystem edges are closed — with D1 the only writes left are the `json` backend's, which is the documented fallback.
- **§3 How a digest is made** — the two ingestion paths and why they have different shapes (RSS is an idempotent buffer, email is a pull/ack queue). Read this before touching a Worker or a `FetchSource`.
- **§4 Data residency** — every persistent datum, where it lives, where it is going. **Do not introduce a new place for state without adding a row here.** Scattering data across new homes to finish a feature is the failure this table exists to prevent.
- **§5 Configuration: four grades** — A baked / B deployment identity / C secrets / D runtime-mutable. Every new setting must be assigned a grade and put in that grade's home. `cyris.toml` is not a default home.
- **§7 Outstanding work** — the open items with their tickets, plus the record of how the closed ones closed. Work is driven from here. If you name a new destination anywhere in the doc, add it to §7 in the same edit.

Keep the document current in the same change that makes it stale — an architecture doc that lags the code is worse than none, because it is still trusted.

Clean-architecture layering: **entrypoints → service_layer → domain**, with **adapters** implementing the service layer's Protocols. Pipeline flow: **Fetch → Store → Score → Process → Output**.

```
src/cyris/
├── bootstrap.py      # Composition root: Deps container + build_deps(cfg)
├── config.py         # Loads cyris.toml + sources.yaml (Pydantic validation)
├── domain/           # Pure business models and rules (pydantic/stdlib only)
│   ├── models.py            # Article, StoredArticle, DigestContent, Tier, ArticleState, ...
│   ├── selection.py         # Score-based selection: layer_by_score, split_summarize_tier_by_score
│   ├── language.py          # Language detection utilities
│   └── triage.py            # RejectReason (canonical rejection reasons)
├── service_layer/    # Use cases and business services
│   ├── ports.py             # Protocols: LLMClient, ArticleRepository, FetchSource + complete_json
│   ├── run_digest.py        # Use case: full pipeline run (fetch→store→score→digest→output)
│   ├── digest_pipeline.py   # DigestPipeline: tier-based digest processing
│   ├── scoring.py           # AI article scoring (score_in_batches shared loop)
│   ├── filtering.py         # Filter tier: batch headline extraction (<10% pass)
│   ├── summarize.py         # Summarize tier: per-group thematic summaries
│   ├── cluster_news.py      # News clustering for news-tagged filter-tier articles
│   ├── fetching.py          # fetch_all_articles across FetchSources with dedup
│   ├── prompts.py           # Claude API prompt templates
│   └── parse.py             # AI response JSON extraction
├── adapters/         # Concrete IO implementations
│   ├── anthropic_client.py  # AnthropicClient (implements LLMClient)
│   ├── openai_client.py     # OpenAIClient (implements LLMClient)
│   ├── workers_ai_client.py # WorkersAIClient (implements LLMClient) over Cloudflare Workers AI
│   ├── store/               # ArticleStore (JSON partitions) + D1ArticleStore (schema.sql), both dedup by URL;
│   │                        #   settings.py = grade-D runtime settings, D1 first / cyris.toml fallback
│   ├── fetch/               # RSS sources (direct + Worker buffer), Cloudflare newsletter Worker source, email parser
│   ├── output/              # HTML digest, raw collected-article listings, usage log;
│   │                        #   publish.py + pages_deploy.py = Pages direct upload over REST
│   ├── notify.py            # Discord notifications
│   ├── promotions.py        # Cloud Worker promotion sync
│   └── http_client.py       # Shared httpx client
├── entrypoints/      # CLI and web servers
│   ├── cli.py               # Typer CLI (entry point: cyris.entrypoints.cli:app)
│   ├── triage_server.py     # Swipe-based triage web UI (aiohttp) + static/
│   └── webhook_server.py    # Email webhook receiver for newsletter ingestion
└── utils/            # timezone helpers (cross-cutting)

workers/              # Cloudflare Workers (deployed to the user's CF account)
├── app/              # The Container and its door: hourly Cron Trigger runs the pipeline
│                     #   (CYRIS_ROLE=run, one pass then exits), any HTTP request wakes the
│                     #   triage UI (CYRIS_ROLE=ui). Auth = Cloudflare Access + CYRIS_UI_TOKEN
│                     #   cookie. See its README to deploy; wrangler must run from that directory
├── promote/          # Digest vote clicks (up/down): KV queue, cyris pulls (adapters/promotions.py)
├── newsletter/       # Email→RSS ingestion: Email Worker parses mail → KV, cyris pulls
│                     #   (adapters/fetch/newsletter_worker_source.py). See its README to deploy.
└── rss/              # Hourly feed buffer: cron polls sources.yaml feeds → D1, cyris pulls
                      #   (adapters/fetch/rss_worker_source.py). Needs Workers Paid.
```

### Key Data Flow

`service_layer/run_digest.py` is the single pipeline orchestrator (the CLI only parses args and calls it via `bootstrap.build_deps`):

1. `service_layer/fetching.py` pulls from all FetchSources (the RSS Worker buffer or direct polling, and the Cloudflare newsletter Worker when configured) within a time window
2. Articles are saved to the ArticleStore for dedup and persistent lifecycle tracking
3. `service_layer/scoring.py` scores non-news articles via the LLM for relevance ranking
4. `service_layer/digest_pipeline.py` processes articles: filter tier batches for headline extraction, summarize tier generates per-article summaries (split by score threshold)
5. `service_layer/cluster_news.py` clusters news-tagged filter-tier articles by topic
6. `adapters/output/html_digest.py` renders the digest page; `domain/selection.py` layers featured articles by score
7. Alongside each digest, the writer emits a companion listing every article this run judged plus what is still pending — uncapped, so what the digest dropped stays visible; rows an earlier run in the overlapping window already judged are left off: `{date}-{period}-raw.html` grouped by source, linked from the digest footer
8. Publishing is Pages **direct upload over REST** (`adapters/output/pages_deploy.py`), never a `wrangler` shell-out. With D1 wired, the pages are rendered in memory and the site's file list comes from the `pages_manifest` table — a Pages deployment is a full snapshot, so every deploy names every file. Without D1, the local `agent-vault/html/` directory is the fallback

### Adapter Extension Points

All IO is behind `adapters/`, wired in `bootstrap.build_deps()`. When adding or swapping IO, work at these seams — never touch `service_layer/` or `domain/`:

- **`FetchSource`** (`ports.py`) — input sources. Implement `fetch_articles` / `health_check`, then append to `fetch_sources` in `build_deps()`. Existing: `CloudflareRssSource` (or `RssSource` when no buffer is configured) and `CloudflareNewsletterSource`.
- **`Embedder`** (`ports.py`) — vote-similarity embeddings, selected in `build_embedder()`: `WorkersAIEmbedder` (`@cf/baai/bge-m3`, the default) or `GeminiEmbedder`. Neither caches — a run is ~600 texts ≈ 20 neurons. Each provider carries its **own** threshold, in `src/cyris/provider_defaults.json` (reasons in `docs/architecture.md` §5); the cosine scales differ, so reusing one number across providers silently disables the feature.
- **`LLMClient`** (`ports.py`) — AI providers. Implement `complete()`; selected in `build_llm()`. Existing: `AnthropicClient`, `GeminiClient`, `OpenAIClient`, `WorkersAIClient` (Cloudflare Workers AI; see `cyris llm-compare` before switching to it).
- **`ArticleRepository`** (`ports.py`) — persistence. `ArticleStore` (JSON) and `D1ArticleStore` (Cloudflare D1) both satisfy it structurally; `[store] backend` picks one via `bootstrap.build_store()`. The Protocol lists every method callers use, not just the digest run's — a partial implementation fails at the CLI or the triage UI, not at import.
- **Output sinks** — `HtmlDigestWriter`, `publish`, `notify` are injected directly (single impl, no Protocol). Add a sink by extending the `Deps` dataclass + wiring in `build_deps()`, then calling it from `run_digest`.

`ports.py` rule: only genuine IO boundaries get a Protocol; single-implementation components are injected directly. Full map, and what each of these is being replaced by: `docs/architecture.md`.

### CLI Commands

| Command | Description |
|---------|-------------|
| `cyris run` | Full pipeline: fetch → store → score → digest. `--if-due` makes the hourly cron tick a no-op except on the two scheduled hours |
| `cyris doctor` | Health check; exits non-zero on anything that would break a run — including a config table *this build* does not understand. Its checks read only, but the command creates the D1 tables if they are missing (every entrypoint does), so it needs a token with D1 edit |
| `cyris promote-sync` | Pull digest votes from the Worker: down rejects, up accepts (no fetch/LLM) |
| `cyris vote-sim` | Preview what vote similarity would suppress, without running the pipeline |
| `cyris embed-compare` | Judge one window with both embedding providers; report disagreements, cost and latency |
| `cyris llm-compare` | Digest one window with several providers (`--arm provider:model`, repeatable), side by side |
| `cyris triage-ui` | Start swipe-based web UI for article classification; `/settings` picks the LLM provider and model (verified against the live API before storing) and the two digest hours, both written to D1 `settings`, and adds/edits/retires sources in D1 `sources` |
| `cyris articles list\|accept\|reject\|clean\|score` | Article store management (`export` went with the vault in M1) |
| `cyris store migrate\|diff` | Copy the JSON store into D1; compare the two backends. Like every command that opens D1, they create the tables first — `diff` reads only, but not from a database it leaves untouched |
| `cyris sources push\|list` | Make D1's source table match `sources.yaml`; show what it serves. Both create the tables first, `list` included — a `database_id` pointing somewhere else gets them |

### Configuration Files

- `cyris.toml` — app config (API endpoints, LLM provider/model, digest limits, schedule, routing thresholds, `[store]` backend, `[promote]`/`[newsletter]`/`[rss]` Worker URLs). For grade-D keys it is the **fallback**, not the source of truth: `bootstrap.load_effective_config` overlays D1 `settings` on top, always in that order — see `docs/architecture.md` §5
- `sources.yaml` — RSS/newsletter source definitions with tier and tags. The editable format and the fallback; with `[store] backend = "d1"` the pipeline and `workers/rss/` both read D1's `sources` table instead, and `cyris sources push` is what fills it. An empty or unreachable table falls back to the file on both sides, so a half-migrated deployment keeps fetching; email-only sources use `type: newsletter` + `email_match: "from:..."`, plus an optional `homepage` doing double duty: its host identifies the sender's own domain when extracting an issue's canonical link, and when an issue has no link at all it is appended to `ref_urls` so the reader still has somewhere to go (never `Article.url` — see below)
- `.env` — secrets (API keys for Anthropic/Gemini/OpenAI; `CLOUDFLARE_EMBEDDING_API_TOKEN` for `bge-m3`, which is **not** the wrangler `CLOUDFLARE_API_TOKEN`; `CYRIS_WORKER_TOKEN`, the one bearer all three Workers accept; Discord webhook)

### Agent Vault (`agent-vault/`)

Agent-owned state directory, entirely gitignored — nothing under it is in version control. `agent-vault/daily/` holds raw article collections and `agent-vault/articles/` the persistent article store, both only while `[store] backend` is `json`; with D1 the directory stays empty.

## Conventions

- Python 3.12+ required
- **No hardcoded values.** A word the code matches, a label a human reads, a name of a
  language or a place — all of it is data (`adapters/fetch/keywords.json`,
  `service_layer/languages.json` are the pattern), reachable without a code edit. What
  legitimately stays in code is *structure* (a regex's shape, an algorithm's steps) and
  the tuned constants `docs/architecture.md` §5 grades **A** with a stated reason. If
  something is a proof of concept, say so in the identifier or the comment above it —
  an unlabelled placeholder becomes load-bearing by default
- User-facing strings are English, even while the digest's content is not. i18n has no
  framework here yet; English is what makes adding one cheap
- Ruff for linting and formatting (line-length 100, see `pyproject.toml [tool.ruff]` for rule selection)
- Pydantic v2 for all data models and config validation
- pytest with `pytest-asyncio` (auto mode) for async tests
- Source tiers determine processing depth: `filter` = aggressive discard, `summarize` = full summary
- Article lifecycle states: `pending` → `accepted`/`rejected`/`awaiting_triage`. A non-null `triaged_at` is what marks a state as a *human* decision (digest vote, triage UI, `cyris articles accept|reject`) rather than the pipeline's own verdict — `update_states` refuses to overwrite stamped rows, and only stamped rows seed vote similarity
- Digest output language is configurable via `[digest] output_language`, a **BCP 47 tag** (default `zh-Hant`). `service_layer/languages.json` maps the tag to the wording the model receives; an unlisted tag is substituted verbatim, which is what keeps an older config holding a plain language name working. Prompts inject it via the `<output_language>` placeholder in `service_layer/prompts.py`. `[digest] style_prompt` injects reader-defined tone/focus
- Newsletter canonical links (`adapters/fetch/newsletter.py`): an issue's 原文 link is chosen structurally — normalize candidates, keep content URLs, take the sender's host (from the source's `homepage`, else the most frequent host), then deepest path → most frequent → first seen. The hostname allowlist and the "網頁版/view in browser" keyword scan are fallbacks behind it. The load-bearing constraint is that a returned URL must never repeat across issues — `ArticleStore` dedups by URL, so a repeated one silently drops every later issue. `tests/test_newsletter.py` enforces it (distinct post URLs, distinct synthetic URLs, and where `homepage` may land); read those before changing the extractor. Real-sample coverage is in `tests/test_newsletter_real_fixtures.py`; samples stay outside this repo
- Link-health counters on `DigestContent` measure two different things: `synthetic_url_count` counts every article fetched this run whose URL is the synthetic `newsletter:` fallback (extractor health); `dead_link_count` counts only items that reached the digest with no clickable link (what a reader hits). Each has its own test, but nothing asserts they disagree on one run — so don't "fix" them into agreement
- Test isolation: external resource names (labels, paths, IDs) must be unique per test — use `tmp_path` or random suffixes, never share production identifiers
- Mock patching: always patch where the function is **used**, not where it is **defined** (e.g. patch `cyris.service_layer.run_digest.now_in_timezone`, not `cyris.utils.timezone.now_in_timezone`)
- LLM calls in tests: inject `FakeLLM` (tests/fakes.py) instead of patching the Anthropic SDK; only CLI-level e2e tests patch the single adapter point `cyris.adapters.anthropic_client.anthropic.AsyncAnthropic`
