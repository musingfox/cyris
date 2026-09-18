---
status: accepted
delivered:
depends: [digest-shared-css-partial]
---

# digest 封存首頁：頭版卡片＋年月分段清單

票：`digest-index-archive-layout`（Obsidian vault `obsidian`，`pm/cyris/tasks/`）。
被 `digest-shared-css-partial` 擋著——那張票先把三個範本各自內嵌的 token 與 chrome CSS
收斂成一份 Jinja partial，這張票才動得了。frontmatter 的 `depends` 指的是那張**票**，
不是另一個里程碑；它沒有自己的里程碑檔案。

> 2026-09-18：這個版面已寫進 `docs/design/ui-language.md` §4（頭版卡片）與 §6 archive。元件、
> 間距與斷點以規範為準，下文「收斂為單一個 640px」由規範的 720px 取代；資料來源與不截斷的約束
> 仍以本文為準。

## 這個里程碑定下什麼

digest 封存首頁（`src/cyris/adapters/output/templates/index.html.j2`）從一疊等寬無層級的列，
改成**最新一期是頁面頂端的頭版卡片、其餘期數是它下面壓縮過的年月分段清單**。

每一列的資訊**不新增任何持久資料**：最新一期的頭版由渲染當下手上的 `DigestContent` 直接填，
下面的歷史列從既有的 `usage_log`（篇數）與 `stories` + `story_members`（主題）讀出來。
不新開表，`docs/architecture.md` §4 不動。

版面**不得把「一天兩期」寫死**。期別直接印標籤原文，不做顏色編碼；同一天的第二條以後
用一個與標籤內容無關的結構訊號區分。系統即將開放使用者自訂一天出幾份
（現行 `validate_schedule` 是 `len(times) != 2` 直接丟 ValueError，
`src/cyris/service_layer/schedule.py:20-22`；`Period = Literal["morning", "evening"]`，`:16`），
首頁不能是那條線上的第三個障礙。

## 現在已經夠具體、可以往下蓋的形狀

**版面**：候選 B，已用可執行的 prototype 驗證。五條驗收線在 65 期假資料、
360/800/900/1440 四個寬度下全綠：四個寬度都無水平捲軸；年月分段可辨識；頭版與清單列是
**結構上**不同而非字重不同；期別編碼在文字模糊後仍可辨；1440px 下內容區佔畫面 66.7%。
同一份資料下它是四個候選裡最密的（全頁 2600px，對照網格 3600、單欄清單 5816、側欄導覽 7683）。

**斷點**：收斂為單一個 640px。prototype 實測 640→1440 走同一套 flex/grid。票裡指認的
720–960px 間距死區，成因是「容器 960 而唯一斷點在 720」的錯配，把斷點降到 640 並讓中間
區段共用同一套規則即可消除，不需要第二個斷點。

**寬螢幕留白**：1440px 下兩側各約 240px，明知而接受。這是四個候選裡最保守的數字（網格可達
95.6%），換來的是單欄的掃描密度。票裡「寬螢幕下整頁幾乎都是背景」因此只被部分解決——
這是被選中的取捨，不是遺漏。

**頭版**：放當期第一篇文章的標題、篇數與主題，全部來自記憶體。
`_render_site(deps, content, collected)` 手上就有完整的 `DigestContent`，而它正是在那裡呼叫
`render_index` 的（`src/cyris/service_layer/run_digest.py:42-52`）；`DigestContent` 帶著
`featured_articles`、`articles_included`、`news_clusters`
（`src/cyris/domain/models.py:149-166`）。頭版永遠是最新一期，而最新一期就是當下這一輪，
所以它不需要任何資料庫讀取。真正的頭版空手狀態只剩兩條非日常路徑：本機 `write_index(dir)`
（`src/cyris/adapters/output/html_digest.py:140-157`）與不經過 run 的重建
（`scripts/backfill_pages_manifest.py`），留白即可，不做第二套版面。

**歷史列**：篇數取該 `(date, period)` 在 `usage_log` 的最後一列——它是 append-only，同一期
重跑會有多列（`src/cyris/adapters/output/usage_log.py:55-85`）。主題從 `stories` JOIN
`story_members` 數成員數，多的在前，取固定上限；`stories` 每個 window 重寫
（`src/cyris/adapters/store/stories.py:58`），查不到就不顯示，不是錯誤。
`stories` 沒有任何可排序的欄位——`created_at` 是整批 save 共用的同一個時間戳
（`stories.py:30-31`），`id` 是成員 URL 的雜湊（`src/cyris/domain/models.py:247`）——
所以成員數是唯一不改 schema 的排序依據。不加 `rank` 欄位：`schema.sql:14-16` 明說這裡沒有
演進機制，`ADD COLUMN` 只會到達全新部署。

**接線**：metadata 走新增的 `Deps` 欄位，由 `_render_site` 取來傳進 `render_index`，照
`site_filenames` 的先例（`src/cyris/bootstrap.py:189,256,279`）——不把 D1 塞進
`HtmlDigestWriter` 的建構子。兩個讀取方法放新的唯讀 adapter
`src/cyris/adapters/store/archive_meta.py`，形狀與 `D1PagesManifest` 相同；不加進
`usage_log.py`，它現在是純寫入而 `tests/test_local_writes.py:20` 正把它列為白名單成員。

**一條必須被測試鎖住的約束**：**封存首頁必須列出每一期，不得截斷、分頁或摺疊。**
`publish_site` 會抓線上的 `index.html`，當 manifest 少了超過四個列在上面的日期頁就拒絕發佈；
`scripts/backfill_pages_manifest.py` 靠「線上 index 仍列出每一期」重建 path → hash
（`docs/architecture.md:801-809`，解析器是
`src/cyris/adapters/output/publish.py:49-59`）。截斷會弄壞復原機制，而目前沒有任何測試
以這個形式鎖住它。改版的同時補上。

**範圍界線**：masthead、brand、footer、body 背景、token 集合、容器寬度與斷點的共同依據
都屬於 `digest-shared-css-partial`。這張票只擁有清單本體與頭版卡片。票裡原本列在驗收條件下的
「`prefers-reduced-motion` 下停止 pulse」與「宣告後未使用的 token」兩項因此移到那張票——
兩段 CSS 在三個範本各有一份複製，只改首頁不構成收尾。那張票的調查後來發現「未使用的 token」
指錯了對象：`--accent-dim` 在 digest 用了三處，真正零使用的是 `--info`，詳見
`docs/milestones/digest-shared-css-partial.md`。

**動工時會看到的紅燈**：`tests/test_html_digest.py:710` 斷言 `"morning-raw" not in index`，
`:184` 與 `:211` 斷言 `<a href=` 的精確計數。每列附 raw 連結會同時打到這三條。測試鎖的是舊
行為，改行為就改測試；發佈路徑是安全的，`_parse_archive_anchors` 早就跳過 `-raw.html`。

**`docs/architecture.md`**：不新開一節，在既有的復原段落補一句，把「首頁另外讀 `usage_log`
與 `stories`」與「清單不得截斷」寫成同一條約束的兩面。

## 刻意留給動工時的選擇

- **結構訊號的具體形式**：縮排、連接線、還是序號點。要看真實資料下「一天三期以上」長什麼樣
  才決定，而那個形態現在還不存在。
- **主題顯示的筆數上限**：要看真實 D1 裡一期通常有幾個叢集。現在給數字是憑空。
- **`render_index` 的確切簽名**：content 與 metadata 是兩個參數還是一個結構、歷史列查不到時
  回什麼。屬於介面層決定，不影響形狀。

## 什麼會推翻這個里程碑

動工的第一步是量兩個數字，兩個都可能翻掉下面的東西：

- **`usage_log` 或 `stories` 的歷史覆蓋率太差**：頭版已經不依賴它們，但清單仍然依賴。
  若多數舊期兩者皆空，清單退化成只有日期與期別，「為歷史列做兩個讀取方法」的收益接近零，
  值得整個拿掉。`append_usage_d1` 在 `api_calls == 0` 時直接 return
  （`usage_log.py:51-52`），`stories.save` 只在有 news 叢集時才寫得出東西
  （`run_digest.py:248-250`、`digest_pipeline.py:234-244`）——所以空列不是假想。
- **`featured_articles` 經常是空的**：頭版放「當期第一篇」的前提是這一輪真的有 featured
  article。若相當比例的 run 產出空的，頭版回到空手，「用永遠查得到的東西補位」重新開放。
- **叢集成員數與重要性不相關**：若最大的叢集總是同一類低價值內容，成員數排序就是把最沒價值
  的主題推到最前面。那麼固定筆數＋字典序反而誠實，或這個欄位根本不值得顯示。
- **使用者給期別取的名字很長**：「印標籤原文」的前提是名字短到能當一個 chip。若取的是
  「早上八點的科技新聞摘要」，版面假設就破，色彩編碼那三個候選重新開放。
- **「一天兩期」在可見的未來都不會改**：若自訂期數那條線被無限期擱置，「期別編碼不得查表」
  就是為不存在的需求付的複雜度。這由路線圖決定，不由這張票決定。
- **寬螢幕留白被實際使用推翻**：若在真實的 1440px 以上螢幕看，兩側各 240px 的格線背景讀起來
  仍是「整頁都是背景」，票的第三個問題並未解決，年月分欄網格（95.6%）重新開放——它已被
  驗證站得住，只是輸在 masonry 的參差與無 metadata 時放大的高度不齊。

落選的三個版面各留一句：年月分段單欄清單輸在密度（5816px）且沒把「最新一期」做成結構；
年月分欄網格輸在四欄 masonry 的參差與右下角空白，但它是唯一真正吃掉寬螢幕留白的，保留為
翻案選項；左側年月導覽輸在最不密（7683px）、六個月資料下左欄明顯偏空、且高亮不隨捲動連動
還要額外 JS。期別編碼的三個落選：雜湊色相輸在色相不受設計控制、會與 `--accent` 打架；固定
調色盤取模輸在第 N+1 個標籤開始撞色；當日序位取模輸在它需要同日期別的排序，而時刻資料不存在
——選它這張票就被「自訂期數」那條線擋住。

## 這個里程碑不解決、但被它照出來的事

**一天超過兩期時，同一天的期數沒有東西可以排序。** `render_index` 現在依 `(date, period)`
字串反向排序（`html_digest.py:136`），`"morning" > "evening"` 純屬字母序的巧合。三期以上要
正確排序需要每期的時刻，而 `usage_log`、`pages_manifest`、檔名都沒有存它。這是「自訂期數」
那條線的上游資料問題，不屬於這張票——這張票只需保證版面不假設期數為二（包括一）。

## 證據

四個版面候選不是敘述，是跑過的東西。證據在分支
`spiral/prototype-digest-index-layout`（commit `ec7e2cc`，沒有人會合併它）：

- 被選中的：`git show spiral/prototype-digest-index-layout:spiral-prototype/b-hero-list/index.html`
  （另有 `degraded.html`、`verdict.md`）
- 保留為翻案選項的網格：同分支 `spiral-prototype/c-month-grid/verdict.md`
- 另外兩份的裁決：同分支 `spiral-prototype/a-grouped-list-verdict.md`、
  `spiral-prototype/d-year-rail-verdict.md`
- 四份的 1440px 截圖：同分支 `spiral-prototype/shots/`
- 四份共用的 65 期假資料：同分支 `spiral-prototype/data.json`
- 完整報告（五條驗收線、各自撞到的限制）：同分支 `spiral-prototype/README.md`

上面所有全頁高度與內容佔比都出自這些截圖與各自的 verdict。

**一個環境事實，任何人日後驗這頁的窄螢幕行為都會踩到**：這台機器的 Chromium 152 對
`--window-size` 有 500px CSS 寬度硬地板，`--headless` 與 `--headless=new` 皆然。傳
`--window-size=360` 實際以 500px 排版，PNG 只是硬裁——看起來測了 360，其實沒有。三份
prototype 各自獨立撞到，分別用 CDP `Emulation.setDeviceMetricsOverride` 與內嵌
`iframe(width:360px)` 繞過。裸 `--window-size` 不可信。

## 一個實作時一定會再遇到的坑

整列可點擊、而列內另有 raw 連結，不能寫成巢狀 `<a>`：HTML 不允許，瀏覽器解析時會自動關掉
外層，raw 連結會被搬出 flex 容器掉到下一行並與 sticky 標題打架。其中一份 prototype 實際撞到
並修成兄弟節點。被選中的版面有同樣的結構需求。

## Suggested skills

- `spec` — 「封存首頁必須列出每一期」是跨 run 必須成立的不變式，不是一次性的決定。它的
  兩個消費端（發佈的 shortfall 守門、manifest backfill）都不在首頁的程式碼裡，所以沒有東西
  會在版面改動時提醒下一個人。這正是 spec 存在的形狀。
- `code-review` — 這次改動同時碰渲染、新的唯讀 adapter、`Deps` 接線與四條既有測試的預期，
  跨層而每層都不大，是 review 抓得到東西的形狀。
- `run` — 版面改動要看真的跑出來的頁，不是只看測試綠。
