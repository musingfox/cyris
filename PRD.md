# Cyris — AI-Powered Information Digest Agent

## Overview

Cyris is a local-first tool that subscribes to RSS feeds and newsletters, uses AI to filter, summarize, and contextualize information, and produces twice-daily digest notes in Obsidian. It acts as an intelligent first-layer filter, drastically reducing the volume of content a user needs to read while preserving signal.

## Problem Statement

- **Information overload**: Dozens of RSS feeds and newsletters produce hundreds of articles daily; most are noise.
- **Manual filtering is expensive**: Scanning headlines and skimming articles takes 30–60 minutes per session with diminishing returns.
- **Context is fragmented**: Important events unfold across multiple sources over weeks or months; no existing reader connects the dots.
- **Existing tools optimize for reading, not filtering**: Readwise Reader is excellent for deep reading and annotation, but offers no AI-driven triage layer.

## Goals

1. Reduce daily information intake by 80%+ through AI-powered filtering and summarization.
2. Provide cross-source thematic summaries for high-quality sources.
3. Build persistent event timelines that connect related news over time.
4. Enable proactive tracking of user-marked topics of interest.
5. Integrate with Readwise Reader as the downstream deep-reading platform.

## Non-Goals

- Replace Readwise Reader (it remains the deep reading + annotation platform).
- Real-time breaking news alerts.
- Social media monitoring (Twitter, Reddit, etc.).
- Original content generation or opinion synthesis.
- Mobile app — this is a local CLI/cron tool with Obsidian as the UI.

---

## User Workflow

```
Morning
  └─ Open Obsidian → morning digest note
       ├─ Scan tracked-topic updates (high priority)
       ├─ Read cross-source thematic summaries
       ├─ Glance at filtered headline list
       ├─ Mark articles: #deep-read → Readwise
       └─ Mark topics: #track → future monitoring

Evening
  └─ Same flow with evening digest

Periodically
  └─ Review tracked topics list
  └─ Adjust source tiers and tags
```

---

## Architecture

```
┌──────────────┐    ┌─────────────────┐
│  RSS Feeds   │    │  Newsletters    │
│  (Miniflux)  │    │  (Cloudflare    │
│              │    │   Email Routing │
└──────┬───────┘    │   → webhook)    │
       │            └────────┬────────┘
       │                     │
       │              Extract links +
       │              Fetch full text
       │              (trafilatura +
       │               cookie auth)
       │                     │
       └─────────┬───────────┘
                 ▼
        ┌────────────────┐
        │    Fetcher     │   cron, every 12 hours
        │  Collect +     │   classify by source tier
        │  Normalize     │
        └────────┬───────┘
                 ▼
        ┌────────────────┐
        │  Agent Vault   │   Obsidian vault (agent-owned)
        │  daily/        │   raw collected articles
        │  events/       │   persistent event timelines
        │  tracking/     │   interest tracking state
        └────────┬───────┘
                 ▼
        ┌────────────────┐
        │  AI Processor  │   Claude API
        │  Filter (L1)   │   general news → discard or headline
        │  Summarize(L2) │   quality sources → paragraph summary
        │  Match events  │   compare against events/ folder
        │  Match topics  │   compare against tracked interests
        └────────┬───────┘
                 ▼
        ┌────────────────┐
        │  Digest Writer │   Obsidian markdown
        │  → User Vault  │   morning / evening note
        └────────┬───────┘
                 │
          User marks articles
                 │
           ┌─────┴──────┐
           ▼             ▼
   ┌──────────────┐  ┌──────────┐
   │   Readwise   │  │  Track   │
   │   Save API   │  │  Topic   │
   └──────────────┘  └──────────┘
```

---

## Content Processing Model

Sources are classified into tiers that determine processing depth:

| Tier | Description | Processing | Example |
|------|-------------|------------|---------|
| `filter` | High-volume general news | Discard most; surface only significant headlines | TechCrunch, 聯合新聞網 |
| `summarize` | High-quality, lower-volume | Per-article or cross-source paragraph summary | Stratechery, Benedict Evans |

Within each digest cycle, the AI processor:

1. **Groups** articles by topic across all sources.
2. **Filters** tier=filter articles: keeps only truly noteworthy items (< 10% pass rate).
3. **Summarizes** tier=summarize articles: generates a concise summary preserving key arguments.
4. **Matches events**: checks incoming articles against existing event files; appends to timeline if matched.
5. **Matches tracked topics**: highlights any articles related to user-tracked interests.
6. **Composes** the digest note with sections ordered by priority.

---

## Event Tracking System

The most differentiating feature. Events are persistent files in the agent vault that accumulate context over time.

### Lifecycle

```
New significant development detected
  → Create event file with initial entry
  → Tag with topics and entities

Related article appears days/weeks later
  → Match against existing events (semantic + keyword)
  → Append to timeline with date and summary
  → Update digest note with "related to [[event]]" link

Event becomes stale (no updates for 30+ days)
  → Mark as inactive
  → Still available for matching but deprioritized
```

### Event File Schema

```markdown
---
title: TSMC Arizona Fab Phase 2
created: 2026-01-15
last_updated: 2026-03-16
tags: [semiconductor, geopolitics, tsmc, us-china]
status: active  # active | inactive
---

## Summary
One-paragraph current state of this event.

## Timeline
- **2026-03-16**: Production timeline delayed by 6 months due to...
- **2026-02-20**: Workforce training partnership announced with...
- **2026-01-15**: TSMC confirms Phase 2 expansion at Arizona site...

## Key Entities
- TSMC, Arizona, US CHIPS Act, Intel

## Source References
- 2026-03-16-am: TechCrunch, Nikkei Asia
- 2026-01-15-pm: Reuters, Stratechery
```

---

## Interest Tracking System

When the user marks a topic as interesting in a digest note, Cyris begins proactive monitoring.

### Interaction

In the digest note, each article/section has action markers:

```markdown
### EU AI Act Enforcement Begins
Summary of the development...
`Sources: TechCrunch, The Verge`
- [ ] deep-read
- [ ] track
```

When the user checks `track`, the next digest run picks it up and adds it to the tracking config. Alternatively, the user can directly edit `tracking.yaml`.

### Tracking Config

```yaml
topics:
  - name: "EU AI Act"
    keywords: ["EU AI Act", "歐盟 AI 法案", "AI regulation Europe"]
    created: 2026-03-16
    status: active

  - name: "台積電亞利桑那廠"
    keywords: ["TSMC Arizona", "台積電亞利桑那", "CHIPS Act TSMC"]
    created: 2026-03-10
    status: active
```

---

## Digest Output Format

```markdown
---
date: 2026-03-16
period: morning
sources_processed: 47
articles_received: 182
articles_included: 15
---

# 早報 2026-03-16

## 追蹤主題更新

> 你正在追蹤的主題有新動態

### 台積電亞利桑那廠
Phase 2 production timeline delayed by 6 months. Arizona workforce training
program expanded with $200M federal grant.
`Sources: Nikkei Asia, Reuters` · [[events/tsmc-arizona-fab]]
- [ ] deep-read
- [ ] track

---

## 主題摘要

### AI 產業本週動態
Cross-source summary spanning multiple quality sources...
`Sources: Stratechery, Benedict Evans, The Information`
- [ ] deep-read
- [ ] track

### 半導體供應鏈
Summary...
`Sources: SemiAnalysis, Nikkei Asia`
- [ ] deep-read
- [ ] track

---

## 重要標題

> 從一般新聞來源過濾出的重要標題

- **Apple Vision Pro 第二代發表** — 價格降至 $2499，新增企業功能 (The Verge)
  - [ ] deep-read
- **日本央行維持利率不變** — 市場預期年中升息 (Reuters)
  - [ ] deep-read

---

## 統計
- 處理來源：47 個
- 收到文章：182 篇
- 過濾後保留：15 篇（8.2%）
- 事件更新：2 則
- 追蹤主題命中：1 則
```

---

## Technical Stack

| Component | Choice | Reason |
|-----------|--------|--------|
| Language | Python 3.12+ | Best ecosystem for article extraction + AI |
| Package manager | uv | Fast, reliable, user preference |
| RSS aggregator | Miniflux (Docker) | Lightweight, excellent API, self-hosted |
| Email ingestion | Cloudflare Email Routing | User has own domain; serverless processing |
| Full-text extraction | trafilatura | Most mature article extraction library |
| Paywall handling | browser-cookie3 + httpx | Reads cookies from user's browser session |
| AI processing | Claude API (claude-sonnet-4-6) | Cost-effective for daily batch processing |
| HTTP client | httpx | Async support, cookie handling |
| Scheduling | launchd (macOS) | Native, reliable, no extra dependency |
| Output | Obsidian (filesystem) | User's existing knowledge management tool |
| Deep reading | Readwise Reader (Save API) | User's existing reading platform |

---

## Source Configuration

```yaml
# sources.yaml

defaults:
  tier: filter
  language: auto  # auto-detect, or force: zh, en, ja

sources:
  # --- High-quality, summarize tier ---
  - name: "Stratechery"
    url: "https://stratechery.com/feed/"
    tier: summarize
    tags: [tech, business-strategy]
    paywall: true

  - name: "Benedict Evans Newsletter"
    type: newsletter
    email_match: "from:list@benedictevans.com"
    tier: summarize
    tags: [tech, trends]

  # --- General news, filter tier ---
  - name: "TechCrunch"
    url: "https://techcrunch.com/feed/"
    tier: filter
    tags: [tech, startup]

  - name: "聯合新聞網 - 國際"
    url: "https://udn.com/rssfeed/news/6809"
    tier: filter
    tags: [international, news]
    language: zh
```

---

## Application Configuration

```toml
# cyris.toml

[general]
digest_schedule = ["08:00", "20:00"]
timezone = "Asia/Taipei"

[miniflux]
url = "http://localhost:8080"
# api_key via env: CYRIS_MINIFLUX_API_KEY

[claude]
model = "claude-sonnet-4-6"
max_articles_per_digest = 200
# api_key via env: ANTHROPIC_API_KEY

[obsidian]
user_vault_path = "~/Documents/ObsidianVault"
digest_folder = "Digests"

[agent_vault]
path = "./agent-vault"

[readwise]
# token via env: CYRIS_READWISE_TOKEN

[paywall]
use_browser_cookies = true
browser = "chrome"
# Additional cookie domains that need auth
cookie_domains = [
  "stratechery.com",
  "theinformation.com",
]

[email]
# Cloudflare Email Routing webhook endpoint (Phase 2)
webhook_secret = ""  # via env: CYRIS_EMAIL_WEBHOOK_SECRET
```

---

## Project Structure

```
cyris/
├── pyproject.toml
├── cyris.toml               # App configuration
├── sources.yaml               # Source definitions
├── tracking.yaml              # Tracked topics
├── .env.example               # Required environment variables
├── .gitignore
├── PRD.md                     # This document
│
├── src/
│   └── cyris/
│       ├── __init__.py
│       ├── cli.py             # CLI entry point (click/typer)
│       ├── config.py          # Load cyris.toml + sources.yaml
│       ├── models.py          # Pydantic data models
│       │
│       ├── fetch/
│       │   ├── __init__.py
│       │   ├── miniflux.py    # Miniflux API client
│       │   ├── email.py       # Newsletter email processor
│       │   └── extractor.py   # Full-text extraction + paywall cookies
│       │
│       ├── process/
│       │   ├── __init__.py
│       │   ├── pipeline.py    # Main processing orchestrator
│       │   ├── filter.py      # L1: filter tier logic
│       │   ├── summarize.py   # L2: summarize tier logic
│       │   └── prompts.py     # Claude API prompt templates
│       │
│       ├── track/
│       │   ├── __init__.py
│       │   ├── events.py      # Event file CRUD + matching
│       │   └── interests.py   # Interest tracking + matching
│       │
│       ├── output/
│       │   ├── __init__.py
│       │   ├── digest.py      # Digest note generator
│       │   ├── readwise.py    # Readwise Save API client
│       │   └── vault.py       # Agent vault file operations
│       │
│       └── utils/
│           ├── __init__.py
│           └── http.py        # Shared httpx client with cookie support
│
├── tests/
│   ├── conftest.py
│   ├── test_config.py
│   ├── test_fetcher.py
│   ├── test_processor.py
│   ├── test_events.py
│   ├── test_digest.py
│   └── fixtures/              # Sample RSS entries, email HTML, etc.
│
├── agent-vault/               # Agent's working vault
│   ├── .obsidian/             # Minimal Obsidian config
│   ├── daily/                 # Raw article collections (gitignored)
│   └── events/                # Persistent event files (tracked)
│
└── docs/
    ├── setup-miniflux.md      # Miniflux Docker setup guide
    ├── setup-email.md         # Cloudflare Email Routing guide
    └── setup-launchd.md       # macOS scheduling guide
```

---

## Development Phases

### Phase 1: RSS → Digest (MVP)

The minimum viable loop: fetch from Miniflux, process with AI, output to Obsidian.

- [ ] Project scaffolding: uv init, linting (ruff), testing (pytest)
- [ ] Configuration loader: `cyris.toml` + `sources.yaml` with Pydantic validation
- [ ] Miniflux client: fetch unread entries within time window, mark as read
- [ ] Content processor: tier-based Claude API processing
  - Filter tier: batch articles, return only noteworthy headlines
  - Summarize tier: per-article or grouped summaries
- [ ] Digest writer: generate Obsidian markdown with sections and action markers
- [ ] CLI: `cyris digest` — run full pipeline once
- [ ] CLI: `cyris digest --dry-run` — preview without writing
- [ ] Setup guide: Miniflux Docker + initial source config

**Deliverable**: Run `cyris digest`, get a useful morning/evening note in Obsidian.

### Phase 2: Newsletter Ingestion

Implemented as a **pull/KV** loop (Cloudflare Email Worker stores parsed mail in
KV; `cyris run` pulls it), mirroring the promote loop so nothing runs locally and
it stays cloud-portable. RSS-capable newsletters go through Miniflux instead;
this path is only for genuinely email-only sources. See `workers/newsletter/README.md`.

- [x] Cloudflare Email Routing setup: dedicated address → Email Worker
- [x] Email Worker receiver + KV queue (`workers/newsletter`), cyris pulls via HTTP
- [x] Email HTML parser: extract main content links (`email_parser.py`)
- [x] Full-text fetch via extractor for linked articles (`fetch_newsletter_articles`)
- [x] Integrate newsletter articles into the same pipeline (`CloudflareNewsletterSource` FetchSource)
- [x] Setup guide: `workers/newsletter/README.md` (deploy + Email Routing + Gmail forwarding)

### Phase 3: Paywall Handling

- [ ] browser-cookie3 integration: read Chrome/Firefox cookies
- [ ] Cookie-aware httpx client for trafilatura
- [ ] Fallback chain: direct fetch → cookie retry → skip with `[paywalled]` note
- [ ] Per-source cookie domain configuration in `sources.yaml`
- [ ] Cookie freshness check: warn when cookies are likely expired

### Phase 4: Event Tracking

- [x] Event file schema: frontmatter + timeline + entities
- [x] Event creation: tracked-topic hits create/seed event timelines
- [x] Event matching: keyword prescreen + LLM confirm, upsert by topic
- [x] Timeline updates: append new entries with date and source
- [x] Digest integration: show event links and context in digest notes
- [x] Event lifecycle: auto-mark inactive after 30 days of silence

### Phase 5: Interest Tracking

- [ ] Tracking config: `tracking.yaml` schema + loader
- [ ] Digest parser: read user checkbox marks from previous digest notes
- [ ] Interest matching: keyword + semantic matching across incoming articles
- [ ] Tracked topics section in digest output
- [ ] CLI: `cyris track add "topic name" --keywords "kw1,kw2"`
- [ ] CLI: `cyris track list`, `cyris track remove`

### Phase 6: Readwise Integration

- [ ] Readwise Reader Save API client
- [ ] Digest parser: detect `[x] deep-read` checkboxes
- [ ] CLI: `cyris send` — scan recent digests, send marked articles to Readwise
- [ ] Confirmation: note in next digest which articles were sent

---

## API Contracts (Phase 1)

### Miniflux Client

```python
class MinifluxClient:
    async def fetch_entries(
        self,
        after: datetime,
        before: datetime,
        status: str = "unread",
    ) -> list[Article]:
        """Fetch entries from Miniflux within time window."""
        ...

    async def mark_as_read(self, entry_ids: list[int]) -> None:
        """Mark entries as read in Miniflux."""
        ...
```

### Content Processor

```python
class ContentProcessor:
    async def process(
        self,
        articles: list[Article],
        sources: dict[str, SourceConfig],
    ) -> DigestContent:
        """
        Process articles through tier-based pipeline.
        Returns structured digest content ready for rendering.
        """
        ...
```

### Digest Writer

```python
class DigestWriter:
    def render(self, content: DigestContent, period: str) -> str:
        """Render DigestContent to Obsidian markdown string."""
        ...

    def write(self, content: DigestContent, period: str) -> Path:
        """Render and write digest note to user vault. Returns file path."""
        ...
```

### Data Models

```python
from pydantic import BaseModel
from datetime import datetime
from enum import Enum

class Tier(str, Enum):
    FILTER = "filter"
    SUMMARIZE = "summarize"

class SourceConfig(BaseModel):
    name: str
    url: str | None = None
    type: str = "rss"  # rss | newsletter
    tier: Tier = Tier.FILTER
    tags: list[str] = []
    paywall: bool = False
    language: str = "auto"
    email_match: str | None = None

class Article(BaseModel):
    id: int
    title: str
    url: str
    content: str  # HTML or text
    author: str | None = None
    published_at: datetime
    source_name: str
    source_tier: Tier
    source_tags: list[str] = []

class DigestItem(BaseModel):
    title: str
    summary: str
    sources: list[str]
    urls: list[str]
    event_ref: str | None = None  # link to event file
    is_tracked_topic: bool = False

class DigestSection(BaseModel):
    heading: str
    description: str | None = None
    items: list[DigestItem]

class DigestContent(BaseModel):
    date: str
    period: str  # morning | evening
    sources_processed: int
    articles_received: int
    articles_included: int
    tracked_updates: DigestSection | None = None
    thematic_summaries: list[DigestSection] = []
    filtered_headlines: list[DigestItem] = []
```

---

## Cost Estimation (Phase 1)

Assuming daily usage with ~200 articles per digest cycle:

| Component | Estimate |
|-----------|----------|
| Miniflux | Free (self-hosted Docker) |
| Claude API (Sonnet) | ~$0.10–0.30 per digest × 2/day ≈ $6–18/month |
| Cloudflare Email Routing | Free tier |
| Readwise Reader | Existing subscription |

The main variable cost is Claude API usage, which depends on article volume and summary depth. Using Sonnet keeps costs reasonable for daily batch processing.

---

## Backlog (deferred)

- **AI autonomous event detection**: detect significant developments across *all* articles (not only tracked-topic hits) and match them semantically + by keyword against existing event files. Deferred 2026-07-12 — the tracked-topic pipeline already produces persistent event timelines; revisit if untracked events prove worth surfacing.

---

## Open Questions

1. **Digest language**: Should the digest be in 繁體中文 regardless of source language, or preserve original language?
2. **Agent vault git tracking**: Should event files be version-controlled separately from the main repo?
3. **Miniflux vs lightweight fetcher**: Miniflux adds a Docker dependency — is that acceptable, or prefer a pure-Python RSS fetcher as a lighter alternative?
4. **Obsidian plugin**: Eventually, a companion Obsidian plugin could provide better UX for marking articles (instead of parsing checkboxes). Worth considering for later phases?
