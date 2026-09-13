---
status: accepted
delivered:
depends: []
---

# digest 三頁的 CSS 收斂成參數化的 partial

票：`digest-shared-css-partial`（Obsidian vault `obsidian`，`pm/cyris/tasks/`）。
沒有 blocker。它自己是 `digest-index-archive-layout` 的 blocker
（`docs/milestones/digest-archive-index-layout.md`）。

## 這個里程碑定下什麼

`index.html.j2`、`digest.html.j2`、`raw.html.j2` 共用的 CSS 收斂成**參數化的 Jinja partial**。
三頁之間那些「近似但不相同」的地方**不被抹平**，而是成為 `{% include %}` 時傳入的具名值，
**每一個參數都要帶一句它為什麼不同**——寫不出理由的那些，就是該收斂的。

「視覺輸出不變」由**兩層收據**證明，而不是由人眼：正規化後的規則集合比對，加上 headless
瀏覽器的 `getComputedStyle` 比對。收斂後的唯一一份 token 是三頁的聯集減掉零使用的 `--info`。
容器寬度是 CSS token，斷點是 Jinja 變數。

這張票不改任何視覺。它存在的理由是讓後面的版面票不會撞在同一片複製貼上的區域裡。

## 現在已經夠具體、可以往下蓋的形狀

### 收據是兩層，而且基準要先錄

- **第一層：正規化後的規則集合比對。** 抽出三頁的 `<style>`，去掉空白與註解，比對改前改後的
  規則集合。它答「有沒有哪條規則或哪個值不見了／變了」，而且**指得出是哪一條**——這是它比
  計算值有用的地方，計算值只說某個元素的某個屬性不對。
- **第二層：headless 取 `getComputedStyle` 比對。** 在數個寬度下取關鍵元素的實際計算值。
  它答「cascade 順序有沒有變」，而那是第一層結構上抓不到的：集合相等不蘊含順序相等，
  而 partial 化正是一種重排。

**基準必須在動第一行之前錄**，否則就沒有「改動前」了。

實作限制：這台機器的 Chromium 對 `--window-size` 有 500px CSS 寬度硬地板，要走 CDP 的
`Emulation.setDeviceMetricsOverride`（`docs/milestones/digest-archive-index-layout.md:146-150`）。

**這修掉票的一條空驗收條件。** 票寫「既有測試全綠」，但 `tests/test_html_digest.py` 全檔沒有
任何斷言碰 CSS 值、選擇器或 `@media`——碰 `<style>` 的只有 `:43` 與 `:98` 的
`assert "<style>" in html`。那句話目前什麼都不保證；這兩層收據是它本來應該指的東西。

### 需要參數化的差異（已查證的完整清單）

票只列了 footer margin 與 body 漸層。實際讀下去長得多，而且有一項是**結構**不同而非值不同：

- **raw 的 brand 沒有 `.brand-name`**，mono 字型直接掛在 `.brand` 上，markup 是無 class 的
  `<div>`（`src/cyris/adapters/output/templates/raw.html.j2:46-58,206`）。這是唯一一項無法純靠
  CSS 參數解決的：要嘛 partial 參數化到連選擇器都可變，要嘛改 raw 的 markup 讓它長出
  `.brand-name`。**後者是改 markup 不是改視覺**（`.brand-name` 的樣式可設成與現況相同），
  因此不違反「不改視覺」。
- **`.brand-mark`**：raw 12px、無 glow、無 animation（`raw.html.j2:60-65`）；index 與 digest
  14px、兩層 box-shadow、pulse（`index.html.j2:66-72`、`digest.html.j2:98-106`）。
- **`.masthead`**：raw 無 `::after` 漸層；三份的 padding/margin 是 24/20 mb40、28/24 mb56、
  28/24 mb48。
- **`body`**：raw 無 radial gradient（與 `src/cyris/entrypoints/static/style.css:36-39` 一致），
  index `80% 50%`，digest `80% 60%`；digest 另有 `font-size: 16px`、`line-height: 1.65`、
  `font-feature-settings`（`digest.html.j2:54-56`），index 與 raw 是 `1.6` 且無另外兩項；
  `html { scroll-behavior: smooth }` 只有 digest（`digest.html.j2:43`）。**「body 背景共用」
  實際上是把一條 `body` 規則拆成 partial 與頁面兩半**，這是參數化的必然結果，不是妥協。
- **`.footer`**：三種形狀不是三組值——index 純區塊 mt56 pt24、raw flex gap24 mt48 pt20、
  digest flex space-between gap18 mt80 pt32。
- **`.promote-btn`**：兩份有七項值不同（`digest.html.j2:629-649` 對 `raw.html.j2:116-130`）：
  display、border token、padding、背景 token、letter-spacing、`.done` 的 fallback、
  `.error` 的背景。`.vote-group` 另差一個 `vertical-align`。
- **字型 `<link>` 的字重集合**：index 與 digest 載 Geist 300-900、Mono 300-600，raw 載
  300-700、300-500。不在票的驗收條件裡，但 partial 邊界若含 head chrome 就會碰到。

### token：聯集減掉 `--info`

收斂後的唯一一份是 digest 現有的 13 個減掉 `--info`。四個檔案合計零次 `var(--info)`，而它
原本承載的「evening」語意已被首頁那張票取消（首頁改成直接印期別標籤原文、不做顏色編碼，
`docs/milestones/digest-archive-index-layout.md:23-24`）。

**票的驗收條件有一條方向寫錯，這裡更正**：票說 `--accent-dim` 宣告未使用——那只在 index
成立，digest 用了它三次（`digest.html.j2:265,393,566`），收斂成單一來源後它自動被用上，
不需要決定。要處理的是 `--info`。

`--warn` 宣告在 digest 卻只被 `style.css:159` 用到——宣告與使用在不同檔案。它不是死的，
收斂後這個錯位自動消失。

### 具名值：容器是 token，斷點是 Jinja

硬限制：**`@media` 的條件不求值 `var()`**，斷點不可能是 CSS custom property。所以兩種表達
並存，各自用在對的地方——容器寬度出現在一般規則裡，是 `var(--container)`；斷點是 partial 裡的
`{{ breakpoint }}`，頁面在 include 前 `{% set %}`。

實作陷阱：`src/cyris/adapters/output/html_digest.py:41` 的 `select_autoescape(default=True)`
對 `.css.j2` 也開 autoescape。數字與 `px` 無事，但帶單引號的值（字型清單）會被轉成 `&#39;`。

首頁那張票已定案的斷點 640px 與容器 960px 是這套機制的第一個下游消費者，但**不在這張票裡
落地**——這張票只讓那個值變得可具名地傳入，不改它。

**這一節與實際落地不符，見文末「落地後與這份里程碑的兩處偏離」。** 容器寬度走的是 Jinja
參數而非 `var(--container)`，斷點則維持三頁各自的字面值。

### 其餘已定案的形狀

- **`prefers-reduced-motion` 下停 pulse**：在 reduce 下重新定義 `@keyframes pulse`，所有 stop
  都 `opacity: 1`。不需要知道消費者是誰——而消費者比票以為的多：`digest.html.j2:105` 的
  `.brand-mark` 與 `:142` 的 `.meta-strip span.live::before`，加上 `index.html.j2:71`。
  列舉消費者寫 `animation: none` 的做法，下一個加 pulse 的人會漏掉。
- **raw 不長出 pulse**：它現在沒有 animation，partial 允許單頁不帶；長出來是改視覺。
- **promote CSS 的「同一處出貨」**：拆成 `_promote.css.j2` 與現有的 script 兩個檔，head 端
  include 前者、body 端 include 後者。「同一處」解讀為同一個功能模組而不是同一個檔案——
  把 `<style>` 塞進 body 端的 partial 雖然瀏覽器會套用，但 HTML 標準只允許 `<style>` 出現在
  metadata content 的位置。
- **`style.css` 與 partial 的關係**：不只改註解，加一條測試斷言 `style.css` 的 `:root` 區塊與
  partial 渲染結果相符。註解攔不住下一次漂移，而「第四份手動同步的副本」正是這張票要解決的。
- **同一個改動裡要一起更新**：`style.css:1-5` 的開頭註解與 `docs/architecture.md:954`
  （§7 #16「the copy is the sharing mechanism」）在這張票落地後都會過時。

### 已知可用的機制

Jinja 環境是 `FileSystemLoader(templates_dir)`（`html_digest.py:35-42`），`{% include %}` 直接
可用，raw 與 digest 已各自在用。`tests/test_html_digest.py:735` 的 credential 不變式是
`glob("*.j2")`，新增的 partial 會自動被掃到，不需手動登記。

**範圍外**：`static/settings.html` 的內嵌樣式不處理——那是 triage server 服務的檔案，link 得到
`style.css`，屬於 `settings-page-layout`。

## 刻意留給動工時的選擇

- **partial 切幾個、邊界在哪**：一個 `_chrome.css.j2` 還是 tokens / reset / masthead / footer
  分開。要先看參數清單實際有多長才知道——參數多到一定程度時，切細比切粗好維護。
- **raw 的 brand 走「參數化選擇器」還是「改 markup」**：兩條路都不改視覺，選哪條取決於
  參數化後 partial 的可讀性。
- **收據要取哪些元素、哪些寬度**：要先把 partial 的邊界定下來，才知道哪些元素是風險點。

## 什麼會推翻這個里程碑

- **參數清單長到 partial 比三份副本更難讀**：參數化的前提是「差異數量有限且每個都有理由」。
  若實際做下去參數超過十幾個、而且半數寫不出理由，那麼「全部收斂到一組值、接受視覺被改」
  反而誠實——那些寫不出理由的差異本來就是抄漏。**動工第一步就會知道**：先把參數清單列完
  再開始寫 partial。
- **計算值收據抓不到東西**：若基準與改後的計算值完全相同、但人眼看得出差異，表示取樣的元素
  不對，收據要重新設計。反之若它噪音太大（字型載入時機造成的浮動），那一層退回只比結構值。
- **`docs/architecture.md:954` 的 §7 #16 其實還有別的消費者**：這裡假設「the copy is the
  sharing mechanism」只描述 `style.css` 那一份。若它也涵蓋別處的複製，改動範圍會比這張票大。

落選方向各留一句：「全部收斂到一組值」輸在它讓票的「不改視覺」失效，而且 raw 會長出它現在
沒有的 glow、pulse 與漸層；「partial 只出逐字相同的部分」輸在覆蓋面縮到只剩 token 與 reset，
驗收條件第二條實質達不到；「只收斂 token」輸在直接違反驗收條件第二條；收據的「只人工比對」
輸在這張票的本質正是人眼最不可靠的任務——把幾百行 CSS 搬家而不能動到任何一個值；
「截圖像素比對」輸在字型載入時機與抗鋸齒造成的假紅。

## 落地後與這份里程碑的兩處偏離（記錄，不是缺陷）

**「容器寬度是 CSS token、斷點是 Jinja 變數」兩者都沒有以那個形式落地。** 實際做法是容器寬度
走 Jinja 參數（`container_width`），斷點維持三頁各自的字面值。理由，兩個都在動工後才看得清楚：

- 容器寬度三頁不同（960 / 1240 / 900）。做成 `var(--container)` 會逼 `_tokens.css.j2` 本身
  參數化——而它是唯一一個零參數的 partial，那正是它的價值。
- 斷點在這張票裡**沒有收斂對象**：三個 `@media`（720 / 880 / 640）的內層規則彼此零重疊，
  沒有任何共用規則住在 media block 裡，`{{ breakpoint }}` 無處可放。硬把三個互不相干的
  media block 塞進 partial 只是為了用上一個機制。

**這一段必須存在，因為下游那張票明文依賴它。**
`docs/milestones/digest-archive-index-layout.md` 把「三頁的容器寬度與斷點的共同依據」列為這張
票的成果，而它自己要把 index 收斂到單一 640px 斷點。接手的人會發現那個共同依據不存在——現在
他至少知道是為什麼，以及要從哪裡開始：容器寬度的參數化已經在 `_page.css.j2` 裡，斷點則要先有
第一條真正共用的 media 規則，才值得抽。

**斷點的字面值現在有一道守門。** `scripts/css_computed.py` 的 `WIDTHS` 一度複製了三個範本的
斷點數字而沒有任何連結——改了斷點，計算值收據就會靜靜不再取樣那個 media block 內部，而
「規則跨越自己的 `@media` 搬家」正是第二層收據存在的唯一理由。現在它從具名的
`BREAKPOINTS = {"digest": 880, "index": 720, "raw": 640}` 導出，並有一條測試把渲染輸出裡的
`@media (max-width: Npx)` 讀回來比對。耦合是被檢查的，不只是被記錄的。

## Suggested skills

- `run` — 兩層收據都要真的渲染出三個頁面才錄得到基準，這不是測試綠就算數的工作。
- `code-review` — 這次改動的失敗模式是「某條規則的優先順序變了」，那是 review 讀 diff
  讀得出來、而測試不一定抓得到的東西。
- `spec` — 只有在參數化落地後才值得：「三個範本的共用 CSS 只有一份來源」是一條會被下一個
  趕時間的人破壞的不變式，而破壞它不會讓任何測試變紅。動工時再判斷。
