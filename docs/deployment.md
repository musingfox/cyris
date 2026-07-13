# 部署方向評估：完全本地 vs Cloudflare 化

> 狀態：評估中。cookie 保鮮暫不納入（見文末「暫緩項」）。

## 現況（起點）

- `cyris` pipeline 目前以 **本機 Python + macOS launchd** 執行（`src/cyris/schedule/launchd.py`）。
- `docker-compose.yml` **只容器化了 Miniflux + Postgres**，cyris 本身沒進容器。
- 持久狀態全在本機檔案，由 `bootstrap.py` 以 `agent_vault.path` 為根**集中注入**（storage port 乾淨，易抽換）。
- 付費源 cookie 由本機讀瀏覽器活 DB（`adapters/cookies.py`），靠日常上網自動保鮮。

**關鍵：兩個方向的公因數 = 先把 cyris 本身容器化。** 同一個 docker image，A 丟進 compose、B 丟進 Cloudflare Container。這一步只做一次。

---

## 方案 A：完全本地部署（去 macOS 綁定）

目標：任何人在任意 Linux/機器上 `docker compose up` 就能跑，不綁 macOS、不綁本機 Python 環境。

| 面向 | 現況 | 改造 |
|------|------|------|
| 運算 | 本機 `cyris run` | 新增 `cyris` service 進 compose |
| 排程 | launchd（macOS 專屬）| 宿主 crontab 呼叫 `docker compose run cyris run`，或容器內 cron |
| 持久化 | 本機檔案 | **不動**，volume mount 進容器 |
| 輸出 | Obsidian vault 檔案 | **不動**，vault 目錄 volume mount |
| Miniflux | compose 已有 | 不動 |
| 設定 | `.env` / `*.toml` | 不動，mount 進容器 |

**改動量：小。** storage/輸出全不用改，只是把運算搬進容器、排程換 cron。
**優勢**：完全離線可跑、隱私最好、無雲端費用、付費源 cookie 之後可用 mount 宿主 cookie 解決。
**代價**：需要一台常開的機器；「外出看 digest」要另接（Pages/Tailscale）。

---

## 方案 B：Cloudflare 化（暫不處理 cookie）

目標：無本機、全雲端。付費 US$5/mo 等級。

| 面向 | 現況 | 改造 |
|------|------|------|
| 運算 | 本機 `cyris run` | 同一 Python image → **Cloudflare Container**，Worker `scheduled` 觸發 `container.start()` |
| 排程 | launchd | **Workers Cron Triggers** |
| 持久化 | 本機檔案 | 換 storage port → **R2**（JSON blob 直搬）或 D1（要 query 去重才上） |
| 設定/secret | `.env` | Workers Secrets + container envVars |
| LLM | Anthropic API | 不變（本來就雲端） |
| Embeddings | Ollama 本機 | 換 API（Workers AI 內建 embedding，或 Voyage）— 唯一換 provider 的點 |
| 輸出 | Obsidian markdown | R2/Pages HTML（不寫回 Obsidian） |
| 標記想讀 | — | 現有 promote KV 迴圈 |
| **Miniflux** | compose | **痛點**：Container 無持久磁碟，CF 上跑 Miniflux+Postgres 不自然。建議砍掉，讓 cyris 直接抓 RSS + D1 存已讀狀態 |

**改動量：中。** Container 讓 Python **原地上雲、不用重寫成 TypeScript**（先前「必須重寫」的評估已過時）。真正的工是：storage port 換 R2/D1、docker 化、薄 Worker cron wrapper、embedding 換 provider、Miniflux 退場。
**優勢**：無本機、外出隨時可看、維運交給 CF。
**代價**：付費源 cookie 失去自動保鮮（本次暫緩）；Miniflux 要退場。

---

## 差異總表

| | A 完全本地 | B Cloudflare |
|---|---|---|
| 需常開機器 | 是 | 否 |
| 月費（不含 Claude API） | $0 | ~US$5 |
| 改動量 | 小（不動 storage） | 中（換 storage port + docker + worker） |
| 語言重寫 | 無 | **無**（Container 跑 Python） |
| 外出看 digest | 要另接 | 原生 |
| 付費源 cookie | 可 mount 宿主解決 | **失去自動保鮮**（暫緩） |
| Miniflux | 保留 | 退場，cyris 自抓 RSS |
| 隱私/離線 | 最佳 | 依賴 CF |

**建議**：兩者不互斥。先做公因數（docker 化 cyris）→ 落地 A（可攜、可開源的預設部署）→ 想要無本機時，同一 image 疊 B。

---

## 開源就緒缺口

要變成可推廣的開源專案，缺口分兩類。

### 阻擋別人跑起來（硬傷，必補）

| 缺口 | 位置 | 修法 |
|------|------|------|
| 無 LICENSE | README 寫 "Private project" | 選一個授權（MIT/Apache-2.0）|
| 寫死個人 vault 路徑 | `config.py:117` `~/Documents/ObsidianVault` | 預設改成 repo 內相對路徑或必填 |
| 預設綁特定付費網域 | `cyris.example.toml`（stratechery/theinformation）| 範本清空成佔位 |
| 排程綁 macOS | `schedule/launchd.py` | 部署改用 docker/cron，launchd 降級為 macOS 選配 |
| cookie 綁 macOS 路徑 | `cookies.py:61`（Zen sqlite）| 參數化 / 標為選配（cookie 本次暫緩）|
| 重複範本命名混亂 | `cyris.example.toml` 與 `cyris.toml.example` | 統一成一個，對齊 README |
| 硬依賴自架 Miniflux + 個人 worker URL | README Requirements | 文件化為選配，B 方向下 Miniflux 可退場 |

### 專案品質（開源慣例，應補）

| 缺口 | 現況 | 修法 |
|------|------|------|
| 無 CI | 只有本地 `.githooks/pre-commit`（`CLAUDECODE=1` 時 skip） | `.github/workflows` 跑 ruff + pytest（已有 ~480 測試，涵蓋佳） |
| 無 CONTRIBUTING / issue·PR template | 無 `.github/` | 補基本模板 |
| 缺人類向架構文檔 | `CLAUDE.md` 是給 AI 的、`PRD.md` 是規格 | 本檔 + 一份 ARCHITECTURE.md |

**好消息**：`.env` / `cyris.toml` / `sources.yaml` 皆已 gitignore，個人信箱與 worker URL 未入庫；範本檔（`.env.example`、`sources.example.yaml`、`tracking.example.yaml`）大致齊全；測試涵蓋佳。**核心程式是乾淨的，缺的主要是「去個人化預設 + 部署去 macOS 化 + 開源慣例檔」。**

---

## 暫緩項

- **付費源 cookie 保鮮（全雲端）**：本次不處理。全無本機時失去瀏覽器自動續期，未來選項：付費源盡量走 newsletter email（已有 worker，不需 cookie）> 手動更新 KV > 不做 headless 自動登入。
