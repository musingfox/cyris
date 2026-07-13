# Architecture: Core ↔ Adapter Wiring

> Drawn for the dockerize / cloud decision. Highlights which boundaries have a Protocol
> (clean to swap) and which are direct injections (need work to move to the cloud).
> Source of truth: `bootstrap.py` (who injects what) + `service_layer/ports.py` (boundary contracts).

## Wiring diagram

```mermaid
flowchart TB
    subgraph EP["Entrypoints"]
        CLI["cli.py"]
        TRI["triage_server.py"]
        WH["webhook_server.py"]
    end

    subgraph ROOT["Composition Root · bootstrap.py"]
        DEPS["build_deps → Deps container"]
    end

    subgraph CORE["Core · service_layer + domain"]
        RUN["run_digest orchestrator"]
        UC["use cases: fetching · scoring · digest_pipeline<br/>filtering · summarize · cluster_news · learning · triage"]
        DOM["domain (pure logic): selection · models"]
        RUN --> UC
        RUN --> DOM
    end

    subgraph PORTS["Ports · Protocols (real IO boundaries)"]
        P1["LLMClient"]
        P2["ArticleRepository"]
        P3["FetchSource"]
    end

    subgraph ADP["Adapters"]
        LLM["Anthropic / Gemini Client"]
        STORE["ArticleStore (JSON)"]
        MF["MinifluxSource"]
        NLA["NewsletterArchiveSource"]
        CFNL["CloudflareNewsletterSource"]
        WRITER["DigestWriter"]
        HTML["HtmlDigestWriter"]
        PUB["publish_html_digest"]
        SYNC["sync_promotions"]
        COOK["load_browser_cookies"]
        USAGE["append_usage"]
        EVT["EventStore"]
        TRKY["VaultConfigSource (tracking)"]
        NOTI["send_discord / send_ntfy"]
    end

    subgraph EXT["External / IO"]
        MFSVC[("Miniflux + Postgres")]
        API(("Claude / Gemini API"))
        FS["Local filesystem"]
        VAULT["Obsidian Vault"]
        CFW{{"Cloudflare Workers · KV · Pages"}}
        BROWSER["Browser cookies.sqlite"]
        DISC{{"Discord / ntfy"}}
    end

    CLI --> DEPS
    TRI --> DEPS
    WH --> DEPS
    DEPS -. inject .-> CORE

    UC -->|Protocol| P3
    UC -->|Protocol| P1
    RUN -->|Protocol| P2

    P1 -. impl .-> LLM
    P2 -. impl .-> STORE
    P3 -. impl .-> MF
    P3 -. impl .-> NLA
    P3 -. impl .-> CFNL

    RUN -->|direct inject · no Protocol| WRITER
    RUN -->|direct inject · no Protocol| HTML
    RUN -->|direct inject · no Protocol| PUB
    RUN -->|direct inject · no Protocol| SYNC
    RUN -->|direct inject · no Protocol| USAGE
    RUN -->|direct inject · no Protocol| EVT
    RUN -->|direct inject · no Protocol| TRKY
    RUN -->|direct inject · no Protocol| NOTI
    UC -->|direct inject| COOK

    LLM --> API
    STORE --> FS
    MF --> MFSVC
    NLA --> FS
    CFNL --> CFW
    WRITER --> VAULT
    HTML --> FS
    PUB --> CFW
    SYNC --> CFW
    COOK --> BROWSER
    USAGE --> FS
    EVT --> FS
    TRKY --> FS
    NOTI --> DISC

    classDef port fill:#1b5e20,stroke:#66bb6a,color:#fff;
    classDef move fill:#e65100,stroke:#ffb74d,color:#fff;
    classDef local fill:#b71c1c,stroke:#ef5350,color:#fff;
    classDef cloud fill:#0d47a1,stroke:#64b5f6,color:#fff;
    class P1,P2,P3 port;
    class STORE,WRITER,HTML,USAGE,EVT,TRKY move;
    class COOK,MF,MFSVC,BROWSER local;
    class CFNL,PUB,SYNC,CFW cloud;
```

**Legend**: 🟢 Protocol boundary (clean to swap)　🟠 directly-injected local-file sink (needs work to move to the cloud, no Protocol)　🔴 local/macOS-bound　🔵 already cloud

## Key: two wiring strengths

`bootstrap.build_deps()` packs every adapter into the `Deps` container and injects it into
the core. But the core consumes them at **two strengths**:

| Wiring | Targets | Swap difficulty |
|--------|---------|-----------------|
| **Via Protocol** (`ports.py`) | `LLMClient`, `ArticleRepository` (ArticleStore satisfies it structurally), `FetchSource` | **Low** — swapping the implementation doesn't touch the core; the contract is fixed |
| **Direct concrete injection** (no Protocol) | DigestWriter, HtmlDigestWriter, publish, sync_promotions, EventStore, tracking, append_usage, cookies, notify | **Medium** — the core calls them directly; swapping the backend first needs a Protocol or an implementation change |

The `ports.py` comment states the design intent: "Only genuine IO boundaries get a Protocol;
single-implementation components are injected directly." These sinks currently have a single
implementation, so no Protocol was extracted. When the cloud move needs a second implementation
(R2/D1), this is the first boundary to add.

## Dockerize / cloud swap-point map

| Component | Current | Dockerize (local) | Cloud (Cloudflare) |
|-----------|---------|-------------------|--------------------|
| ArticleStore 🟠 | JSON @ local | volume mount, unchanged | extract Protocol → R2/D1 impl |
| EventStore / usage / tracking 🟠 | files | volume mount, unchanged | same, swap to R2/D1 |
| DigestWriter 🟠 | writes Obsidian vault | vault volume mount | disable (use HtmlDigestWriter → R2/Pages) |
| LLMClient 🟢 | Anthropic/Gemini | unchanged | unchanged (already cloud) |
| FetchSource · Miniflux 🔴 | self-hosted Docker | already in compose | retire, cyris fetches RSS itself |
| FetchSource · CloudflareNewsletter 🔵 | Worker+KV | unchanged | unchanged |
| load_cookies 🔴 | reads browser sqlite | mount host cookies (deferred) | loses auto-freshness (deferred) |
| publish / sync_promotions 🔵 | Cloudflare | unchanged | unchanged |
| Scheduling | launchd | cron | Workers Cron Trigger |

**Conclusion**: the core (`service_layer` + `domain`) **never changes** across all three
deployments. Every change is confined to the adapter layer — that is the payoff of the
Protocol + composition-root design. Dockerize needs only volume mounts (🟠 all stay put);
only the cloud move needs to extract Protocols for 🟠 and swap to R2/D1.
