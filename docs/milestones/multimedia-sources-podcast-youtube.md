---
status: accepted
delivered:
depends: []
---

# 影音來源：英文 podcast 與 YouTube

2026-09-28 經兩輪設計討論定案。第一批只做英文 podcast 和 YouTube，台灣中文 podcast 之後再做。

## 這個里程碑定下什麼

**深度跟著 tier 走。** 影音來源沿用既有的 RSS 路徑進來，處理深度由來源的 tier 決定：

- summarize tier 的影音取得內容濃縮，稱為 B 級。
- filter 和 fan tier 的影音只顯示標題與說明，稱為 A 級。

A 級是 B 級的基礎，也是 B 級的退路。濃縮失敗、逾時，或濃縮設定留空時，那一集照樣以標題與說明出現在 digest。

**濃縮的位置。** B 級濃縮在 digest run 裡執行，位置在存檔去重之後、評分之前，只處理這次 run 新存的文章。濃縮文直接覆寫該文章的內容欄位，所以評分和摘要讀到的都是真正的內容。濃縮失敗時內容維持原本的節目筆記或影片說明，不重試。每篇文章會被 24 小時窗、一天兩次的 run 讀到兩次，靠存檔去重保證每集只濃縮一次（`src/cyris/service_layer/run_digest.py:248-269`）。濃縮的花費進 `run_summary` 和 `usage_log`。

**怎麼認出影音。** 逐項偵測：

- 帶音檔（`<enclosure>`）的項目是 podcast 單集。
- 連到 `youtube.com/watch` 的項目是 YouTube 影片。

不新增來源類型，也不加來源旗標。

**身分。**

- podcast 單集的鍵是 `podcast:{來源名稱}:{guid}`。Worker 的 JavaScript 解析器和 Python 的 `RssSource` 用同一條規則產生它，並由測試綁住兩邊一致。單集連結放在 `ref_urls`。
- YouTube 影片的鍵是它的觀看網址。

**濃縮用的模型。** 濃縮模型是新的 D 級執行期設定，在 `/settings` 的 Model 分類裡調整，存進 D1 `settings` 前會先打一次真實 API 驗證。值留空時影音只做 A 級，所以換模型、關掉 B 級都不用發新版。

**內容從哪裡來。**

- YouTube 走現有的 `generateContent` 端點，用 `file_data.file_uri` 送觀看網址。
- podcast 只用 feed 已經附的文字：`<podcast:transcript>` 指向的逐字稿，或內嵌在 `content:encoded` 的全文。沒有的就停在 A 級，不做語音轉文字。

## 為什麼不能照現況直接加來源

現有的解析器接不住 podcast 和 YouTube。這是 A 級就要修的問題（2026-09-28 用真實 feed 餵給 `workers/rss/src/parse.js` 測得）：

- **Buzzsprout 的節目完全進不來。** 單集沒有 `<link>`，255 集解析出 0 筆（`workers/rss/src/parse.js:46-58,97`）。
- **同一個網址的單集會永久遺失。** 一個 Captivate 節目 182 集全部塌成節目首頁一個網址；Hard Fork 最近幾集都指向專欄頁。buffer 用 `INSERT OR IGNORE`（`workers/rss/src/index.js:77-83`），store 又以網址為主鍵，所以第一集之後、網址相同的每一集都會被默默丟掉。
- **連結習慣會隨時間改變。** Acquired 較舊的 150 集指向首頁，最近 20 集才各有網址。
- **Python 那邊也一樣。** Python 的 `RssSource` 用 feedparser，同樣拿不到 Buzzsprout 的連結（`src/cyris/adapters/fetch/rss_source.py:110-112`）。
- **YouTube 沒有內容可讀。** feed 的影片說明放在 `media:group` 裡的 `media:description`，`parse.js` 不讀（`workers/rss/src/parse.js:71-77`），Python 的 feedparser 讀得到。頻道 feed 也混著 Shorts：3Blue1Brown 的 15 筆裡有 11 筆連到 `/shorts/`。
- **現有的 YouTube 來源從沒產出過文章。** 3Blue1Brown 走自架 RSSHub，那條路由一直回 503「this route is empty」，D1 `stored_articles` 裡它是 0 筆。單一 feed 的失敗只會寫進 Workers Logs，保留 7 天，`digest_runs` 和 `/settings` 都看不到。

## 成本

基準是 D1 `usage_log` 從 2026-08-29 到 2026-09-28 的 LLM 花費，合計 1.63 美元，平均每次 run 0.027 到 0.036 美元（2026-09-28 查詢）。

**YouTube 的實測。** 2026-09-28 用使用者的金鑰呼叫一次：

- 把 https://www.youtube.com/watch?v=Nbwv5wHQoj0 （51:55）送進 `gemini-3.8-flash` 的 `generateContent`，回應 HTTP 200，耗時 43 秒。
- 輸入 283,511 token，約每秒 91 token；輸出 620 token，思考 979 token。

依文件，讀 YouTube 網址在預覽期間不收費（https://ai.google.dev/gemini-api/docs/generate-content/video-understanding ）。如果照 `gemini-3.8-flash` 的現價收費，這支影片約 0.22 美元；2027 年起單價加倍（`src/cyris/domain/models.py:94`），約 0.44 美元。

**podcast。** 用 `gemini-3.8-flash` 濃縮一份 120 分鐘的英文逐字稿，約 0.022 美元，2027 年起約 0.044 美元。

- 逐字稿大小依據：Acquired 215 分鐘的逐字稿是 206,650 bytes（https://share.transistor.fm/s/8ecb4ed4/transcript.txt ），120 分鐘約 115 KB。
- 換算 token 用的「每 4 bytes 約 1 token」是未驗證的經驗值。

**示意情境（量是假設）。** 5 個英文 podcast 和 5 個 YouTube 頻道，各每週更新一次：

- podcast 每月約 0.35 美元。
- YouTube 預覽期間每月 0。開始收費後，每月約 2.0 美元，2027 年起約 4.0 美元；改用 `gemini-3.5-flash-lite`（輸入每百萬 token 0.30 美元，https://ai.google.dev/gemini-api/docs/pricing ）約 0.8 美元。

主要的成本風險是 YouTube 預覽期結束後的定價。濃縮模型是可在 `/settings` 調整的設定，就是為了讓這個風險不用發版就能處理。

**容器多醒的時間。** 超出方案內含額度時，basic 規格每多醒一分鐘約 0.00017 美元（https://developers.cloudflare.com/containers/pricing/ ），可以忽略。

## 現在可以當作前提的形狀

- **兩個解析器都要做到的事。** Worker 的 `parse.js` 和 Python 的 `RssSource` 都要：產生單集的 guid 鍵；帶出單集連結與 feed 附的逐字稿網址；讀出 YouTube 的 `media:description`；丟掉連到 `/shorts/` 的項目。`/shorts/` 這條路徑規則是資料，不寫死在程式碼裡。兩邊的一致性由測試綁住，追蹤參數清單已經用同樣的做法（`workers/rss/src/parse.js:11-17`）。
- **buffer 要多存兩樣東西。** buffer 的 `articles` 表目前只有一個網址欄位，同時也是主鍵，要能額外帶出單集連結和逐字稿網址。這張表只保留 8 天資料，由 Worker 用 `CREATE TABLE IF NOT EXISTS` 建立（`workers/rss/src/index.js:18-21,34`），換表的成本低。架構文件 §4 要跟著更新。
- **YouTube 來源用頻道 feed。** 格式是 `https://www.youtube.com/feeds/videos.xml?channel_id=...`，這是 YouTube 唯一有文件記載的 feed（https://developers.google.com/youtube/v3/guides/push_notifications ）。3Blue1Brown 要從 RSSHub 網址換成這個格式。
- **濃縮文寫給讀者看。** 它用 `[digest] output_language` 指定的語言寫成，並當作該集在 digest 裡的摘要，仍然放在原本的標籤分組裡，`DigestContent` 的形狀不變。
- **時間預算。** 實測一支 52 分鐘影片 43 秒；目前正式 run 最慢 196 秒（D1 `digest_runs`），run 實例的上限是 15 分鐘（`workers/app/src/index.js:119`）。
- **對既有來源的影響。** 現有 50 個 RSS 來源中有 4 個帶音檔：Product Growth、區塊勢、Tidy First、Citations Needed。上線後，它們附音檔的文章會改用 guid 鍵；上線當下還在 buffer 8 天窗口內的文章，會用新鍵再出現一次，這是一次性的。其中三個是 summarize tier，附音檔的文章會被濃縮。
- **不動的東西。**
  - `synthetic_url_count` 維持只算電子報的 `newsletter:` 網址（`src/cyris/service_layer/run_digest.py:376`）。
  - 預覽模式不存檔，所以不會觸發濃縮，不用特別處理。

## 留給實作時決定的事

- **濃縮設定的形狀。** 一個鍵或兩個（provider 和 model）；podcast 和 YouTube 要不要分開設定，因為 YouTube 只能用 Gemini；真實 API 驗證要怎麼確認 YouTube 能力。設定要照架構文件 §5 的 D 級規則放進 `settings_fields.json`、`cyris.toml.example` 和 `cyris settings push`。
- **上線順序。** 新的必填 D 級鍵在 D1 填好之前，每次 run 都會停下（`docs/architecture.md:551-552`），所以要先填值，才能部署會讀它的版本。
- **第二個模型的花費怎麼記。**
  - `usage_log` 每次 run 只有一列、只記一個模型（`src/cyris/adapters/output/usage_log.py:49-87`）。封存頁的文章數讀的是同一期最後一列（`src/cyris/adapters/store/archive_meta.py:19-29`），多寫一列會讓它變成 0。
  - `UsageStats` 目前用 digest 的模型計價整次 run（`src/cyris/service_layer/run_digest.py:271`）。
  - 價格表還沒有 Flash-Lite 那一列（`src/cyris/domain/models.py:91-100`）。
- **存檔怎麼回報哪些是新文章。** `SaveResult` 只回傳筆數（`src/cyris/domain/models.py:251-255`）。電子報路徑先用 `get_by_urls` 查既有列，是可參考的前例（`src/cyris/adapters/store/d1_store.py:148-169`）。D1 支不支援 `RETURNING` 還沒驗證。
- **並行度與每次 run 的處理上限。** `GeminiClient` 單次呼叫逾時 120 秒、最多重試 2 次（`src/cyris/adapters/gemini_client.py:39-40`）。
- **新介面的形狀。** YouTube 要送非文字內容，但現有的 `LLMClient.complete` 只收文字（`src/cyris/service_layer/ports.py:27-39`）。
- **代理模式。** 文件號稱最多省 88% token，但能不能用在 YouTube 網址上還沒驗證。
- **先從 Worker 實測 YouTube 頻道 feed。** 2026-09-28 只從 Mac 抓過，Cloudflare Worker 抓不抓得到還沒驗證；有兩則第三方紀錄說 Worker 抓得到（https://gist.github.com/walkure/cf3b112b4705bdfffdec9cedee6d6e4d 、https://philippdubach.com/posts/degoogling-cost-me-my-youtube-feed-so-i-made-my-own/ ）。這個 feed 有兩個已知風險：
  - YouTube 的 robots.txt 在 `User-agent: *` 底下禁止 `/feeds/videos.xml`（https://www.youtube.com/robots.txt ，2026-09-28 查）。
  - miniflux 維護者回報，從 2025 年 12 月起，這個 feed 每天 09:00 到 12:00 UTC 會間歇回 404，所有客戶端都受影響（https://github.com/miniflux/v2/issues/4261 ）。buffer 每小時輪詢一次，feed 保留最近 15 筆，所以這段空窗只會延後抓取，不會漏掉影片。

## 什麼會推翻這個里程碑

- **Gemini 的 YouTube 網址功能收回，或 `generateContent` 不再接受 YouTube 網址。** YouTube 停在 A 級：在 `/settings` 清空設定即可，podcast 不受影響。
- **YouTube 的 B 級月花費超過目前整個 LLM 月花費。** 這時換便宜的模型或清空設定。
- **濃縮讓 run 逼近 15 分鐘上限，並行和上限都壓不下來。** 這時改成在容器每小時的空檔濃縮。
- **某個節目的 guid 在不同次輪詢間改變。** Apple 規範要求 guid 永遠不變（https://podcasters.apple.com/support/823-podcast-requirements ）。不照做的來源要另訂鍵的規則。
- **需要原本的節目筆記。** 例如想重試失敗的濃縮，或並列原文與濃縮文。覆寫內容欄位做不到，要改成獨立的表。
- **逐項偵測誤判造成實際損失。** 這時改用來源旗標。
- **YouTube 頻道 feed 在 Worker 上持續抓不到。** 這時改用 YouTube Data API 的 `playlistItems.list`：每次呼叫 1 單位配額，預設每天 10,000 單位，但需要一把新的 C 級 API key（https://developers.google.com/youtube/v3/docs/playlistItems/list 、https://developers.google.com/youtube/v3/getting-started ）。影片的鍵仍是觀看網址，所以不用重新產生任何鍵；但這是 RSS 以外的新抓取路徑，要寫新程式碼。

沒選的方向：

- **濃縮的位置。**
  - 在 Worker 收進來時濃縮：金鑰會放到沒有檢查關卡的 Worker 上，提示詞要重寫一份 JavaScript，花費也進不了 `usage_log`。
  - 在容器每小時的空檔濃縮：要多一張表，還有時間差問題。
  - 評分後只濃縮高分的：會修改「summarize tier 的影音都要有內容」這條規則。
- **單集的鍵。**
  - 音檔網址：要一直維護分析轉址的前綴清單，讀者點進去只會打開 MP3。
  - 每個來源自己宣告：把會隨時間改變的連結習慣凍結成設定。
  - 全域雜湊：短 guid 可能撞鍵，JavaScript 和 Python 還要做到逐位元組一致的雜湊。
  - guid 是網址就直接用：兩個解析器都要實作兩條規則。
- **濃縮的存放。**
  - 新開 D1 表：§4 要加一列，而且只有 D1 能用。
  - 先做資料表演進機制：範圍會多出整套遷移機制。
- **模型的選擇。**
  - 沿用 digest 的 LLM：不發版就沒有模型選擇，也沒有 YouTube 的開關。
  - 程式裡固定一個模型：換模型要發版。
- **怎麼認出影音。**
  - 新增來源類型：這是對外契約，也會重新打開「每個來源自己宣告規則」。
  - 來源旗標：它和 tier 重疊，是第二個成本開關。

## 不變式

這個里程碑產生五條 spec，都在 `docs/spec/`，目前是 `proposed`：`podcast-episode-keyed-by-guid`、`rss-parsers-agree`、`media-condensed-once-before-scoring`、`media-condensation-falls-back-to-announcement`、`youtube-content-not-scraped`。
