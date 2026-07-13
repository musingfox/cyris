# 架構：Core ↔ Adapter 串接

> 為 dockerize / 上雲決策而畫。凸顯哪些邊界有 Protocol（乾淨可抽換）、哪些是直接注入（上雲要動）。
> 串接真相來源：`bootstrap.py`（誰注入誰）+ `service_layer/ports.py`（邊界契約）。

## 串接圖

```mermaid
flowchart TB
    subgraph EP["Entrypoints"]
        CLI["cli.py"]
        TRI["triage_server.py"]
        WH["webhook_server.py"]
    end

    subgraph ROOT["Composition Root · bootstrap.py"]
        DEPS["build_deps → Deps 容器"]
    end

    subgraph CORE["Core · service_layer + domain"]
        RUN["run_digest 主編排"]
        UC["use cases: fetching · scoring · digest_pipeline<br/>filtering · summarize · cluster_news · learning · triage"]
        DOM["domain 純邏輯: selection 分層選文 · models"]
        RUN --> UC
        RUN --> DOM
    end

    subgraph PORTS["Ports · Protocols（真正 IO 邊界）"]
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
        FS["本機檔案系統"]
        VAULT["Obsidian Vault"]
        CFW{{"Cloudflare Workers · KV · Pages"}}
        BROWSER["瀏覽器 cookies.sqlite"]
        DISC{{"Discord / ntfy"}}
    end

    CLI --> DEPS
    TRI --> DEPS
    WH --> DEPS
    DEPS -. 注入 .-> CORE

    UC -->|Protocol| P3
    UC -->|Protocol| P1
    RUN -->|Protocol| P2

    P1 -. 實作 .-> LLM
    P2 -. 實作 .-> STORE
    P3 -. 實作 .-> MF
    P3 -. 實作 .-> NLA
    P3 -. 實作 .-> CFNL

    RUN -->|直接注入·無 Protocol| WRITER
    RUN -->|直接注入·無 Protocol| HTML
    RUN -->|直接注入·無 Protocol| PUB
    RUN -->|直接注入·無 Protocol| SYNC
    RUN -->|直接注入·無 Protocol| USAGE
    RUN -->|直接注入·無 Protocol| EVT
    RUN -->|直接注入·無 Protocol| TRKY
    RUN -->|直接注入·無 Protocol| NOTI
    UC -->|直接注入| COOK

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

**圖例**：🟢 Protocol 邊界（抽換乾淨）　🟠 直接注入的本機檔案 sink（上雲要動、無 Protocol 保護）　🔴 本機/macOS 硬綁　🔵 已雲端

## 關鍵：兩種串接方式

`bootstrap.build_deps()` 把所有 adapter 塞進 `Deps` 容器注入 core。但 core 取用它們有**兩種強度**：

| 串接方式 | 對象 | 抽換難度 |
|----------|------|----------|
| **經 Protocol**（`ports.py`）| `LLMClient`、`ArticleRepository`（ArticleStore 結構化滿足）、`FetchSource` | **低**——換實作不動 core，契約已定 |
| **直接注入具體實作**（無 Protocol）| DigestWriter、HtmlDigestWriter、publish、sync_promotions、EventStore、tracking、append_usage、cookies、notify | **中**——core 直接呼叫，換後端需先抽 Protocol 或改實作 |

`ports.py` 註解點明設計意圖：「Only genuine IO boundaries get a Protocol；single-implementation components are injected directly.」——目前這些 sink 只有一種實作，所以沒抽 Protocol。上雲要加第二種實作（R2/D1）時，這是第一個要補的邊界。

## Dockerize / 上雲抽換點對照

| 元件 | 現況 | Dockerize（本地） | 上雲（Cloudflare） |
|------|------|-------------------|--------------------|
| ArticleStore 🟠 | JSON @ 本機 | volume mount，不改 | 抽 Protocol → R2/D1 實作 |
| EventStore / usage / tracking 🟠 | 檔案 | volume mount，不改 | 同上，換 R2/D1 |
| DigestWriter 🟠 | 寫 Obsidian vault | vault volume mount | 停用（改 HtmlDigestWriter→R2/Pages）|
| LLMClient 🟢 | Anthropic/Gemini | 不改 | 不改（本來就雲端）|
| FetchSource · Miniflux 🔴 | 自架 Docker | compose 已有 | 退場，cyris 自抓 RSS |
| FetchSource · CloudflareNewsletter 🔵 | Worker+KV | 不改 | 不改 |
| load_cookies 🔴 | 讀瀏覽器 sqlite | mount 宿主 cookie（暫緩）| 失去自動保鮮（暫緩）|
| publish / sync_promotions 🔵 | Cloudflare | 不改 | 不改 |
| 排程 | launchd | cron | Workers Cron Trigger |

**結論**：core（service_layer + domain）在三種部署下**完全不動**。所有變化都收斂在 adapter 層——這正是 Protocol + composition root 設計的紅利。Dockerize 只需 volume mount（🟠 全部原地）；上雲才需要把 🟠 抽 Protocol 換 R2/D1。
