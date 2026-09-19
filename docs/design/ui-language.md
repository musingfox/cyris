---
status: accepted
accepted: 2026-09-17
updated: 2026-09-18
---

# cyris 介面設計規範

任何改動到 reader-facing 介面的修改都要遵守這份規範：`src/cyris/adapters/output/templates/`
（archive、digest、raw）、`src/cyris/entrypoints/templates/`（settings）、`src/cyris/entrypoints/static/`
（settings 的腳本與 `style.css`），以及之後新增的頁面。

- **規範是這份文件。** `docs/design/prototype.html` 是它的參考實作，用瀏覽器直接打開：
  `#system` 是 token 與元件總覽，`#archive`、`#digest`、`#raw`（含 triage view）、`#settings/model`
  是各頁。兩者不一致時以這份文件為準，並在同一次修改裡把原型改掉。
- **程式碼還沒有跟上。** §8 列出現況差距與落地順序。落地完成之前，碰到哪個元件就把哪個元件改成
  規範的樣子，不要照著現有的舊樣式再寫一份。
- **偏離規範要先改規範。** 需要規範沒有的元件或數值時，先在這份文件寫下它與理由，再寫程式碼。

## 1. 原則

1. **一套語言，兩種密度。** 閱讀面（archive、digest、raw）寬鬆，操作面（raw 的 triage view、settings）緊湊。
   兩者共用 token 與元件，只差間距的選段。
2. **分層靠底色與細線，不靠陰影。** 由深到淺是 `--bg` → `--bg-elev` → `--surface` → `--surface-2`，
   物件邊界用 1px `--border`。唯一的光暈是品牌方塊。
3. **控制項圓角，容器方角。** 按鈕、輸入框、下拉、分段控制、選項清單用 `--r-control`；tag 與 pill
   用 `--r-tag`。面板、卡片（含 triage 卡片）、表格、notice 一律方角。
4. **UI 標籤只有一種聲音。** 導覽、分頁、按鈕、欄位名、狀態都用 label 角色（§3）。
5. **顏色只有兩種語意。** `--accent` 表示主要動作、已選取、正向結果；`--warn` 表示破壞性動作與錯誤。
   中性結果（例如 rejected）用 `--text-faint`。語意色不拿來裝飾。
6. **不寫死值。** 顏色、間距、字級、圓角、時間都從 token 取；token 以外的字面值只允許 §2 列出的例外。

## 2. Token

下面是完整的 token 集合。`_tokens.css.j2` 與 `static/style.css` 必須逐字相同，
`tests/test_digest_css_partials.py` 負責比對。

```css
:root {
  /* 底色，由深到淺 */
  --bg: #07070a;
  --bg-elev: #0d0d12;
  --surface: #11111a;
  --surface-2: #161622;
  /* 線 */
  --border: #1f1f2e;
  --border-strong: #2a2a40;
  /* 文字 */
  --text: #ebebf2;
  --text-dim: #8a8aa0;
  --text-faint: #56566e;
  /* 語意 */
  --accent: #c6ff3d;
  --accent-dim: #8aa829;
  --warn: #ff5b8a;
  --accent-tint: rgba(198, 255, 61, 0.07);
  --warn-tint: rgba(255, 91, 138, 0.12);
  --grid: rgba(198, 255, 61, 0.035);
  /* 間距 */
  --s-1: 4px;  --s-2: 8px;  --s-3: 12px;  --s-4: 16px;  --s-5: 20px;
  --s-6: 24px; --s-8: 32px; --s-12: 48px; --s-16: 64px; --s-20: 80px;
  /* 字體與字級倍率 */
  --font-sans: 'Geist', system-ui, -apple-system, sans-serif;
  --font-mono: 'Geist Mono', ui-monospace, monospace;
  --font-serif: 'Instrument Serif', Georgia, serif;
  --type-scale: 1;
  /* 形狀、動態、版面 */
  --r-control: 6px;
  --r-tag: 999px;
  --t-fast: 150ms ease-out;
  --bar-h: 60px;
  --measure: 640px;
}
```

- **單位一律 px。** 不用 rem 表示尺寸；em 只用在字距。
- **斷點只有一個：720px。** 容器寬度：archive 與 raw 960px，digest 與 settings 1240px。720px 以上各寬度的
  版面問題（例如 digest 在 880–1160px 的欄數死區）用流體寫法解決，例如 `auto-fit`、`minmax`、`clamp`，
  不新增第二個斷點。
- **允許的字面值例外：** 品牌方塊光暈 `rgba(198,255,61,.45)`、site bar 半透明底 `rgba(7,7,10,.88)`、
  primary 按鈕 hover 底 `#d4ff66`、圓點的 `border-radius: 50%`。新增例外要寫進這一條。
- **digest 內文的間距暫不搬到刻度上。** 它現有的 14/18/22/28/36/44/56px 等值維持原樣，直到那個區塊
  因為別的理由被改寫。新寫的樣式一律用刻度。

## 3. 字級

下表是 2026-09-17 定下的基準，對應 `--type-scale: 1`。

- **每個 `font-size` 都寫成 `calc(基準值 * var(--type-scale))`**，clamp 也包在裡面，例如
  `calc(clamp(52px, 8vw, 96px) * var(--type-scale))`。之後的字級調整設定（`docs/architecture.md`
  §7 #35）只改倍率，不改這張表。
- 控制項高度、欄寬與間距不跟著倍率縮放。
- 大寫只寫在 CSS（`text-transform: uppercase`），HTML 裡的字維持正常大小寫。

| 角色 | 規格 | 用在 |
|---|---|---|
| display | Instrument Serif 400，clamp(52px, 8vw, 96px)，行高 1，字距 −0.03em，`em` 為斜體 `--accent` | 頁面大標（archive、raw、settings） |
| issue title | Instrument Serif 400，clamp(64px, 10vw, 136px)，行高 .92，字距 −0.04em | digest 刊頭大標 |
| heading | Geist 600，28px，行高 1.3，字距 −0.015em | 面板標題、settings 分類標題 |
| title | Geist 600，20–22px，行高 1.3 | 列表項目、來源名稱；triage 卡片標題 30px |
| body | Geist 400，20px，行高 1.65，`--text-dim` | 說明、摘要 |
| small | Geist 400，16px，行高 1.5，`--text-dim` | 欄位說明、表格內容 |
| label | Geist Mono 500，14px，大寫，字距 0.1em，`--text-faint` | 導覽、分頁、欄位名、按鈕、區塊標記 |
| data | Geist Mono 400，16px，`tabular-nums`；archive 日期 22px | 日期、分數、數量、URL |

## 4. 元件

每個元件只有一份樣式（§7）。表中的高度是固定值，不隨 `--type-scale` 變。

| 元件 | 規格 |
|---|---|
| **site bar** | 全寬、sticky、高 `--bar-h`、底部 1px `--border`。左邊是品牌方塊（12px `--accent`，pulse 動畫）加 `CYRIS`；右邊是 label 角色的連結 `Archive` · `Settings`，間距 `--s-5`。目前所在頁為 `--text` 加 1px `--accent` 底線。`Settings` 沿用 `/api/vote` 探測，未授權時隱藏。手機寬度隱藏 `CYRIS` 字樣 |
| **issue bar** | digest 與 raw 共用，位於 site bar 下方，`--bg-elev` 底。左邊是日期（data）與時段（label），右邊是分段控制 `Digest` / `All articles`。兩者是同一期的兩個檢視 |
| **分段控制** | 外框 1px `--border-strong`、`--r-control`、格間 1px 分隔線；每格高 44px、左右 `--s-4`、label 角色、`--text-dim`。選中：`--surface-2` 底、`--accent` 字、2px `--accent` 底線。連結用 `aria-current="page"`，切換鈕用 `aria-pressed` |
| **按鈕** | 高 44px、左右 `--s-5`、label 角色、`--r-control`。**primary**：`--accent` 底、`--bg` 字。**secondary**：`--surface-2` 底、`--border-strong` 框、`--text-dim` 字，hover 字變 `--text`。**danger**：透明底、`--warn` 字與框，hover 為 `--warn-tint` 底。小尺寸高 34px、左右 `--s-2`、字距 0.06em。disabled 一律 opacity .4 |
| **輸入框 / 下拉** | 高 48px、左右 `--s-4`、`--bg-elev` 底、1px `--border-strong` 框、`--r-control`、Geist 18px。focus 時框變 `--accent`；驗證失敗時框變 `--warn`，下方接 notice |
| **欄位** | 由上而下是 label 角色的欄位名、控制項、small 角色的說明，間距 `--s-2`。超過一句的說明收進 `<details>`，摘要文字為 `More` |
| **選項清單** | 單選的多個選項（例如 provider）：`--bg-elev` 底、1px `--border` 框、`--r-control`；每列上下 `--s-3`、左右 `--s-4`、底線分隔、hover `--surface`。右側用 label 角色表示狀態；不可選的列 opacity .5 |
| **頭版卡片** | archive 最新一期專用。`--surface` 底、1px `--border-strong` 框、方角、內距 `--s-6`。由上而下：`--accent` 的 `Latest` label 加日期（data）與時段（label）；當期第一篇的標題（title 角色）；篇數（data）；成員最多的前兩個主題標題（small，以 ` · ` 相連）；兩個 secondary 小按鈕 `Digest` 與 `All articles`。沒有資料的欄位直接省略，不顯示佔位字 |
| **面板** | `--bg-elev` 底、1px `--border` 框、方角。可選的頭列：`--surface` 底、底線、上下 `--s-3` 左右 `--s-5`，左放標題、右放數量或動作。相鄰面板間距 `--s-5` |
| **列表列 / 表格列** | 上下 `--s-3`、左右 `--s-5`、底線分隔、hover `--surface`。表頭用 label 角色、`--surface` 底。表格放在自己的 `overflow-x: auto` 容器裡 |
| **pill** | Geist Mono 14px、`2px 10px`、1px `--border-strong` 框、`--surface-2` 底、`--r-tag`。score 變體為 `--accent` 字、`--accent-dim` 框、`--accent-tint` 底 |
| **狀態字** | Geist Mono 13px、大寫、字距 0.1em。accepted 為 `--accent`；pending 為 `--text-dim`；rejected 為 `--text-faint` |
| **notice** | 儲存結果與錯誤：左側 2px 色條（`--accent` 或 `--warn`）、對應 tint 底、方角、small 角色、`white-space: pre-wrap`，放在觸發它的按鈕旁邊。錯誤訊息要說明發生什麼、怎麼修 |
| **破壞性確認** | 不用 `window.confirm`。第一次按下，danger 按鈕原地變成 `Confirm …` 並套上 `--warn-tint` 底；3 秒內再按才執行，否則還原 |
| **區塊標記** | label 角色，前面一條 24px × 1px 的 `--accent` 線，間距 `--s-3` |
| **footer** | 上方 1px `--border`、Geist Mono 14px 大寫、字距 0.08em、`--text-faint`。只放產生資訊，不放導覽 |

### 互動

- **Focus：** 所有可互動元素 `:focus-visible { outline: 1px solid var(--accent); outline-offset: 2px; }`。
  只有輸入框可以移除 outline，改用框線變色表示 focus。
- **過渡：** 只改 `color`、`background-color`、`border-color`、`opacity`，時間 `--t-fast`，
  不寫 `transition: all`。唯一用 transform 的是 triage 卡片的拖曳與飛出。
- **Hover** 不位移、不縮放。按下狀態只降低 opacity。
- **Reduced motion：** `@media (prefers-reduced-motion: reduce)` 全域取消過渡、動畫與 smooth scroll。

## 5. 導覽

```
Archive (/) ──► 一期 ─┬─ Digest       ◄── Discord 通知的連結落在這裡
                      └─ All articles (raw) ─┬─ List（預設）
                                             └─ Triage（授權後才出現）
Settings (/settings)：site bar 上，授權後才出現
```

- 所有頁面頂部都有 site bar；digest 與 raw 另有 issue bar。導覽不放在頁尾。
- archive 的頭版卡片與每一列都有 `Digest` 與 `All articles` 兩個入口。
- 沒有獨立的 triage 頁。判定文章在 raw 的 triage view 做，deck 已於 2026-09-19 退場
  （`triage-raw-list-merge`）。
- 不做上一期與下一期。使用情境是當天讀完當天那期。
- 返回連結必須指向實際存在的路徑。`/triage` 在正式環境是 404。

## 6. 各頁版面

### archive

page head（label、display、small 說明）之下，最新一期是頭版卡片，其餘期數依年月分成多個面板。
面板頭列左邊是年月（data），右邊是期數。每期一列：日期、時段（label）、篇數（small，有紀錄的期數才有）、
兩個 secondary 小按鈕；手機寬度時兩個按鈕換到下一行。歷史列不顯示主題：主題標題是一句話而不是標籤，
而且 2026-09-19 量到的 91 期裡只有 20 期有主題紀錄。

- 每一期都要列出，不截斷、不分頁、不摺疊，因為 Pages 的復原機制靠首頁列出的每一期重建。
- 期別直接印標籤原文，不做顏色編碼，版面不假設一天只有兩期。同一天第二期以後用一個與標籤無關的
  結構訊號區分，形式在實作時依真實資料決定。
- 資料來源與取捨記在 `docs/milestones/digest-archive-index-layout.md`；斷點以本文件 §2 為準。

### digest

刊頭沿用現有結構（issue title 加 stats card），上方加 site bar 與 issue bar。內文結構不變。

### raw

page head 的 label 寫出文章數與來源數，下方是分段控制 `List` / `Triage`，預設 `List`。`Triage`
沿用 `/api/vote` 探測，未授權時整個分段控制隱藏，頁面只剩 list。兩個 view 共用同一份資料，
切換不重新載入。

- **list view：** 每個來源一個面板；每列依序是狀態字、分數（data）、標題連結、投票小按鈕。
  手機寬度時標題換到下一行。
- **triage view：** 一次一張卡片，卡面是來源（label）與標題（title 角色，30px）。卡片方角、
  `--surface` 底、1px `--border-strong` 框、不用陰影；向 up 傾斜時框變 `--accent`，向 down 傾斜時
  框變 `--warn`。左滑 down、右滑 up、點擊在新分頁開原文。下方並排 danger `Down` 與 primary `Up`
  兩個高 56px 的按鈕，補足沒有觸控的桌機；上方以 label 顯示 `N remaining`。卡片只取狀態仍是 pending、
  而且還沒投過的文章；pipeline 已判過的文章要推翻，在 list view 投。沒有卡片時只剩 `0 remaining`。
- 已投的文章在兩個 view 都看得出狀態，投票走既有的 promote Worker，不新增後端。
- 拖曳與飛出是全站唯一用 transform 的動作，reduced-motion 下取消。

### settings

`/settings` 是單一頁面，用 hash 切換分類。不拆成多個路徑，因為 Worker 的 `PROTECTED` 只比對
完全等於 `/settings` 的路徑（`workers/app/src/router.js:10`）。

| hash | 分類 | 內容 |
|---|---|---|
| `#model` | Model | provider（選項清單，右側顯示金鑰是否就緒）、model、儲存前的實際呼叫驗證。embedding provider（§7 #17）之後放這裡 |
| `#digest` | Digest | 兩個發布時段、featured 區塊數 |
| `#notifications` | Notifications | Discord webhook，已存的值遮罩顯示 |
| `#sources` | Sources | 類型篩選（All / RSS / Newsletter）與 `Add source`；表格欄位為名稱、類型、tier、feed 或 sender、tags；點列在原地展開編輯；表單只顯示該類型需要的欄位；`Retire` 用破壞性確認 |

- 頁寬 1240px。720px 以上左邊是 220px 的分類清單（label 角色，選中時 2px `--accent` 左線），
  以下改為頂部可橫向捲動的分頁列。
- 每個分類頂部是 heading、一句 small 說明、值的來源 pill（`Stored in D1` 或 `Fallback: cyris.toml`）。
  `/api/settings` 目前沒有逐鍵回傳來源，做到之前不顯示這個 pill（§8 第 5 步）。
- 每個分類只有一個 primary `Save`，沒有改動時停用；有未儲存改動的分類在清單上加一個 6px
  `--accent` 圓點。

## 7. 樣式放在哪裡

- digest 是部署到 Pages 的獨立 HTML，不能連結外部 stylesheet，所以 token 與元件樣式都放在
  `templates/_*.css.j2` partial：`_tokens.css.j2` 放 §2，`_components.css.j2` 放 §4。
- site bar 與 issue bar 的標記放在 `_site_bar.html.j2` 與 `_issue_bar.html.j2`。
- `static/style.css` 是這些 partial 的副本，settings 從它取得同一份樣式。
- `tests/test_digest_css_partials.py` 要同時比對 token 與元件規則，否則兩邊會再分岔。

## 8. 現況差距與落地順序

2026-09-17 盤點的差距中，第 1 到 3 步已於 2026-09-18 落地，第 4 與第 6 步已於 2026-09-19 落地：第 1 步
補上 §2 token、§3 字級、focus 與 reduced-motion、兩個硬寫顏色，以及元件 CSS 比對測試；第 2 步讓 archive、
digest、raw 掛上 site bar 與 issue bar；第 3 步依 §6 重做 settings；第 4 步依 §6 重做 raw 的列表，並加上
登入後才出現的 triage view，deck 也於同日刪除（`triage-raw-list-merge`）；第 6 步依 §6 把 archive 改成
頭版卡片加年月面板，同一天的後續列以淡化的日期區分。其餘差距由下面第 5 與第 7 步處理。

落地順序，每一步都能單獨上線。括號裡是 Obsidian vault `pm/cyris/tasks/` 的票：

1. §2 token、§3 字級、focus 與 reduced-motion、刪除兩個硬寫顏色，並把 CSS 比對測試擴大到元件（`ui-spec-tokens-and-type`）
2. site bar 與 issue bar 上 archive、digest、raw；archive 列加入兩個入口；頁尾導覽移除（`ui-site-bar-and-issue-bar`）
3. settings 依 §6 重做（`settings-page-layout`）
4. raw 加上 triage view（`raw-page-triage-view`）；deck 已刪（`triage-raw-list-merge`）
5. `/api/settings` 逐鍵回傳值的來源，settings 顯示來源 pill（`settings-value-origin-per-key`）
6. archive 改成頭版卡片加年月分段（`digest-index-archive-layout`）
7. digest 內文的層級、`.meta` 與寬度死區，內文間距在這一步搬上刻度（`digest-issue-page-layout`）

## 9. 改介面前的檢查清單

- [ ] 顏色、間距、圓角、時間都來自 §2 token，或是 §2 列出的例外
- [ ] 每個 `font-size` 都乘上 `--type-scale`，角色對得上 §3
- [ ] 控制項圓角、容器方角
- [ ] 用的是 §4 已有的元件；新元件已先寫進 §4
- [ ] 可互動元素有 `:focus-visible`，過渡沒有寫 `all`
- [ ] 400px 寬度下整頁不橫向捲動，表格在自己的容器裡捲動
- [ ] 導覽在 site bar，返回連結指向存在的路徑
- [ ] `docs/design/prototype.html` 已同步修改
