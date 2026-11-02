---
status: superseded
delivered:
depends: [clef-preference-order]
---

# 辨識有價值的資訊：點開劃出上下層，排序取代刪除

**2026-11-02 被 [ADR-0023](../decisions/0023-votes-go-on-topics-and-the-digest-has-three-layers.md) 與 [digest-pebble-sand-water.md](digest-pebble-sand-water.md) 取代。** ADR-0023 讓分層只看 lane 與這一期的成員數，不從點開學；它也拿掉輸出上限，所以沒有「只顯示上限內的項目」；S7 的領域配額隨領域骨架退場。Q2 到 Q8 不再回答。之後若要收點開紀錄，另開一份里程碑。以下內容是當時的決定，保留作為歷史。

2026-10-11 經兩層設計討論定案，起點是「cyris 要怎麼辨識出對讀者有價值的資訊」。同日第二場討論從「tier 分層還有沒有必要」出發，把版面和分類拆成另一份里程碑 `digest-by-topic-and-area.md`，並回答了這裡的 Q1，拆開了 Q8，把 Q7 從候選題升成新聞 lane 的前置。其餘的 Q2 到 Q7 還沒有答案。

## 這個里程碑定下什麼

- **價值分兩種，digest 也依此分層。** 「值得花時間讀」的文章放上層，附摘要；「需要知道」的放下層，讀者看過標題就夠。
- **每篇文章由逐篇的價值判斷，決定兩件事。** 一是要不要上 digest，二是放在哪個位置。
- **上下層的分界從讀者在 digest 上的行為學來。** 讀者點開一篇文章，代表它值得讀；只看標題，代表它屬於需要知道。這個訊號不另外向讀者提問。
- **點開紀錄由 app Worker 在送出頁面時注入回報機制來收。** email 和 Discord 通知不再帶文章連結，只連到網頁上的那一期，所以每一次點開都發生在收得到紀錄的網頁上。
- **「不上 digest」不是判決。** 每篇候選文章得到一個逐篇的值，依值排序，每期只顯示上限內的項目；其餘維持 pending。低密度來源（例如新聞台）只會被排到後面，機制上從不被剔除。
- **偏好不能僵化。** cyris 鼓勵讀者 triage，並定期盲標。
- **冷啟動有兩個輸入。** 讀者加入來源後，cyris 從來源分出幾個領域讓讀者在 `/settings` 確認和排序（2026-10-11 改：領域由 LLM 從來源提出，不是讀者從固定清單選，見 `digest-by-topic-and-area.md` 的 L1），再抓一部分文章給讀者 triage。

## 已經具體到可以往下做的部分

- **兩種訊號各做一件事。** 讚和倒讚維持現在的意義：它們是去留的人工判斷，也是 vote similarity 的種子（`src/cyris/adapters/promotions.py:65-79`、`src/cyris/service_layer/vote_similarity.py:84-85`）。點開只用來學上下層的分界：不經過 `record_votes`，不蓋 `triaged_at`，也不在 promote Worker 的 `VOTES` 裡新增值（`workers/promote/src/index.js:7,39`）。
- **app Worker 有改寫頁面的能力，但現在不會每頁都改。** 字級為 1 時，頁面逐位元組原樣送出；只有字級不是 1，才經過 `injectStyle`（`workers/app/src/router.js:131-132`、`workers/app/src/index.js:157-164`、`docs/decisions/0013-reader-type-size-is-injected-by-the-worker.md`）。注入回報會打破這條邊界：被注入的讀者永遠拿不到 304，每頁都要跑一次 `HTMLRewriter`。所以要寫一份新的 ADR 取代 0013，寫明誰會被改寫、會丟掉哪些 header，並改寫 `workers/app/test/router.test.js:729-759` 的對應斷言。
- **注入條件要和投票的歸屬條件相同：在 Access 主機上，或帶著 `cyris_session` cookie。** `handleVote` 在 `CYRIS_UI_ACCESS_HOST` 上不檢查 cookie（`workers/app/src/router.js:143-150`、`docs/architecture.md:830`），而正式站的自訂網域在 Access 後面。如果只看 cookie，正式站主要的閱讀主機上一筆紀錄都收不到。Access 的 JWT cyris 不驗證（`docs/architecture.md:834`），所以歸屬只能到「這個部署的讀者」為止；一人一套的部署夠用，共用形態不夠。
- **回報端點由 Worker 自己處理，不叫醒容器。** 比照 `/api/vote` 的路由位置（`workers/app/src/router.js:269-271`）；其他 `/api/*` 都會轉給容器（`router.js:293`），那正是 ADR-0010 要避免的成本。
- **紀錄同時包含曝光和點開。** 沒點開只有在讀者看過那一期時才算「需要知道」。所以要記下「這個部署的讀者打開了這一期」，也要記下每一次點開，連同文章所在的區塊和順序。2026-09-24 以前沒有 D1 `digests`（commit `5ee1171`），那些期數的位置從頁面上的 `data-urls`、`data-story-id` 讀（`src/cyris/adapters/output/templates/digest.html.j2:2`）；2026-09-18 以前的頁面結構不同，區塊的判斷不可靠（ADR-0013）。
- **點開紀錄住在 D1 的一張新表，`docs/architecture.md` §4 要新增一列。** 可擴充的欄位放在 JSON 欄，因為 `ADD COLUMN` 到不了既有部署（`docs/architecture.md:756-761`）；`tests/test_residency_covers_schema.py` 會強制 §4 這一列。這一列要寫明保存期間和刪除路徑，因為點開和曝光是行為紀錄，屬於個資（vault `pm/cyris/docs/hosted-eval-legal.md:245,265`）。託管版的隱私告知草稿目前沒有提到行為紀錄（vault `pm/cyris/docs/hosted-business-evaluation.md:146,183`）。期別和 URL 就夠了，不必複製標題，因為 `digests` 已經存了每一期的內容。
- **email 和 Discord 只做通知。** 現在的信件帶文章連結（`src/cyris/adapters/output/templates/email.html.j2:21,24`），Discord 訊息帶 markdown 連結（`src/cyris/adapters/notify.py:100,155,178`），兩者都改成只連到那一期。這個連結必須是 Worker 主機的網址：退回 `pages.dev` 的那一期不經過 Worker，也就不會被注入（`docs/architecture.md:507`）。會變紅的斷言：`tests/test_email_digest.py:108,157,175,184,299`、`tests/test_notify.py:153,184,302,874`。
- **排序取代刪除，pipeline 的順序要改成「排值 → 截斷 → 寫字」。這條只對密度高的 lane 成立。** 新聞 lane 若採合成段落（`digest-by-topic-and-area.md` 的 N1 候選 a），寫段落本身就是挑選，順序倒過來。現在 filter 的同一次 LLM 呼叫同時負責挑文章和寫標題（`src/cyris/service_layer/filtering.py:29`、`src/cyris/service_layer/prompts.py:67-78`）。如果只拿掉刪除判決、順序不變，LLM 得替整個候選池寫標題，輸出 token 會隨候選數線性增加。排值落在上限之前，就等於對整個候選池判斷，這會推翻 `docs/milestones/clef-preference-order.md` 裡「只判斷已經進到這一期的項目」的範圍，要回頭改那份里程碑，並在 §7 記一列。
- **拿掉 `rejected` 會牽動這些地方。**
  - 狀態寫入：`src/cyris/service_layer/run_digest.py:558-564`。
  - 盲標的候選池和分層：`src/cyris/adapters/store/blind_labels.py:79-91`、`src/cyris/diagnostics/blind_labels.py:22,26,29`。
  - `cyris labels report` 的精確率和召回率：`src/cyris/diagnostics/blind_labels.py:88-100`。
  - raw 頁：`src/cyris/adapters/output/templates/raw.html.j2:91`。
  - e2e 的兩個收據測試：`tests/test_e2e_digest.py:377,420`。
  - `cyris articles clean` 的預設值：`src/cyris/entrypoints/cli.py:969-972`。
  - 文件：`docs/architecture.md:314,405-408`。
  - 不受影響的：人工倒讚仍然寫 `REJECTED`，vote similarity 的種子照舊。
- **vote similarity 的壓制也是一種刪除，要併進排序。** 它在當輪用絕對門檻移除候選（`src/cyris/service_layer/run_digest.py:423-426`），和「不判刪除」不一致。把倒讚相近度變成排序裡的負值，也正是 vault `pm/cyris/tasks/suppression-reason-and-clock.md` 要的相對門檻。
- **判斷者只要排得準。** 用排序品質衡量，也就是 AUC 和「前 15 項的命中率」，不校準絕對門檻（ADR-0006）。
- **新聞也要有一個逐篇的值。** 現在新聞不評分（`src/cyris/service_layer/scoring.py:31`），排序需要它。
- **盲標改成依「有顯示、沒顯示」分層，並凍結在抽樣當時。** pending 的文章下一輪可能被顯示，所以「沒顯示」不是一個狀態；它要定義成「被哪一輪的上限截掉」，資料只能從 D1 `digests` 的 JSON 推出來（`src/cyris/adapters/store/schema.sql:186-193`）。
- **tier 決定預設的形式，逐篇的值可以推翻它。** 2026-10-11 讀者定案：filter 來源預設進下層，summarize 來源依分數分上下層（`src/cyris/domain/selection.py:12-50`），但一篇值夠高的新聞可以升格拿到卡片。所以新聞仍然需要一個逐篇的值（上一條與 Q6），只是可以晚於版面上線。形式的細節在 `digest-by-topic-and-area.md`。

## 分階段計畫

原則是先刪除、再簡化、再加速、最後自動化。每個階段只有一個可觀察的結果，下一個階段要等收據到手才開始。

| 階段 | 結果 | 前置 | 收據 | 改回成本 |
|---|---|---|---|---|
| S0 保住正在跑的量測 | 第一輪盲標 100 列（含 position、stratum_size、pipeline_state、label）匯出到 repo 外，在第二輪 `--replace` 之前；第二輪抽完標完 | 無 | `~/cyris-jev-eval/blind-labels-round-one.json` 有 100 列；vault `pm/cyris/tasks/blind-labels-round-two.md` 的驗收條件勾掉（Clef 的計分 2026-10-11 起暫停，見 `clef-preference-order.md`） | 零 |
| S1 通知不帶文章連結 | email 和 Discord 每一項都沒有原文連結，只有那一期在 Worker 主機上的網址 | 無 | 上面列的斷言改成反向後通過；記下改動前兩週每期的投票數當基準 | 低 |
| S2 新 ADR 取代 0013 | 寫明哪些頁面會被改寫、丟哪些 header | 無 | ADR 進 `docs/decisions/`；vitest 通過 | 低 |
| S3 最小的點開紀錄 | 一張表、一個端點、§4 一列，含保存期間和刪除路徑 | S2；Q3、Q4 | 正式站一期內至少一筆曝光、一筆點開；託管試用部署首次開機後也有這張表 | 低到中：已收的個資要能刪 |
| S4 量測推翻條件 | 一份離線報告回答下方推翻條件的前三條，閾值事先寫進 vault 卡片 | S3 上線後累積 N 期（Q5） | 三個數字和事前閾值並列 | 零；條件成立就停在這裡 |
| S5 逐篇的值先當影子 | 每輪對整個候選池算一個值，只寫紀錄，不改排序和狀態 | S0；S3 | 用盲標和點開算出 AUC 和前 15 項命中率 | 低 |
| S6a 密度高 lane 排序取代刪除 | summarize 來源拿掉 `rejected`；順序改成排值、截斷、寫字；壓制併入排序；盲標改分層；raw 頁與 e2e 改寫 | S4 證明有訊號；S5 前 15 項命中率過門檻；驗收輪已結束 | 一期正式站的紀錄顯示 summarize 來源沒有 `rejected` 寫入；新一輪盲標抽得出「沒顯示」層；LLM 輸出 token 不高於改動前的中位數 | **高**：`sources.tier` 的意義、`stored_articles.state` 的分布、盲標分層都會變 |
| S6b 新聞 lane 以窗口過期取代刪除 | filter 來源拿掉 `rejected`；掛不上主題的新聞以 Q7 定下的過期收尾；形式由 `digest-by-topic-and-area.md` 的 L4 決定 | S6a；Q7；那邊的 L3 | 一期正式站沒有新聞寫入 `rejected`；raw 頁與盲標池能認出「窗口過期」 | **高**：和 S6a 共用 `state` 分布的改變 |
| S7 上下層與領域配額由點開學 | 上下層由排序值加點開歷史決定，取代 `summarize_score_threshold` 分層；各領域的配額由每個曝光的點開率衰減回基線（那邊的 L6）；位置偏差的修正寫進 ADR | S6a 穩定數週；S4 的位置偏差有結論 | 人工抽查上下層和讀者事後點開的一致率，門檻事先定；一個領域的配額在半衰期左右跟上點開率 | 中 |
| S8 自動化 | 盲標依固定節奏自動抽；冷啟動用確認過的領域抓第一批 triage 文章 | S6a；S7；那邊的 L1 | 連續兩輪自動抽樣與報告不需人工；一個新部署只靠選興趣領域就產出第一期 | 低 |

- **約 2026-10-14 的第二輪盲標。** 原本是 Clef 排序的驗收輪；Clef 路線 2026-10-11 暫停（`clef-preference-order.md`），這一輪改為 Lb、Lm 與這裡 S 階段共用的測試集（vault `pm/cyris/tasks/blind-labels-round-two.md`）。分層以 `state == REJECTED` 為鍵（`src/cyris/diagnostics/blind_labels.py:22,26`），所以任何動到 `rejected` 的改動都要排在標完之後。
- **有期限的查詢。** `cyris labels draw --replace` 會刪掉第一輪的樣本列，而樣本列是唯一記得「抽樣當時 pipeline 判了保留還是刪除」的地方（`src/cyris/adapters/store/schema.sql:239`），所以 S0 的匯出必須在下一次抽樣之前，由人類對正式站 D1 執行。
- **可以平行的部分。** S1 和 S2 不碰 `rejected`，可以和 S0 同時進行。

## 還要人類決定的問題

- **Q1 層級和上限的形狀：已於 2026-10-11 選 (b)。** 原題：現在的 15 項不是「排序後取前 15」，而是依區段優先填充：Features、主題摘要、新聞群組、簡短提及、標題依序填滿（`src/cyris/domain/selection.py:198-290`），偏好排序只在 Features 和標題內部重排。候選是 (a) 單一全域排序取代區段填充；(b) 各區段內排序，區段配額另定；(c) 只有 filter tier 的標題走排序。選 (b)，區段是領域、配額由點開率老化，形狀寫在 `digest-by-topic-and-area.md`。(a) 連同「tier 給基本分」一起落選，理由在那份文件。
- **Q2 怎麼量「拿掉連結後讀者還讀不讀通知」（S1）。** 候選：(a) 用改動前後每期的投票數當代理；(b) 一次性問讀者；(c) 承認量不到，從推翻條件中刪掉。
- **Q3 點開紀錄的寫入路徑（S2）。** 候選：(a) app Worker 經 REST 直寫 D1，但它和 `workers/app/src/type_scale.js:6-8` 刻意限流的理由衝突；(b) 送進 promote Worker 的 KV，每輪 run 排空後寫進 D1，投票已經是這個形狀（`docs/architecture.md:442`）；(c) 給 app Worker 加 D1 binding，要推翻 ADR-0012 的理由。
- **Q4 網頁上的連結要不要改寫成轉址（S2）。** 候選：(a) 只監聽點擊，接受右鍵「在新分頁開啟」和 iOS 長按會漏掉；(b) 改寫成經過 Worker 的轉址，要防開放轉址，也和 cyris 剝除進來的電子報追蹤參數的做法相反（`src/cyris/adapters/fetch/keywords.json:48,52`）。
- **Q5 S4 要累積幾期，推翻條件的閾值各是多少。** 要在看資料之前寫下。
- **Q6 新聞的值從哪裡來（S5）。** 仍然需要：新聞升格成卡片（那邊的 L5）和沒有密度高文章時由新聞當頭條（那邊的 N2 候選 b）都靠它，但兩者都可以晚於版面上線。候選：(a) 對新聞也問 Clef，第一輪盲標新聞層 0.824 是間接證據（vault `pm/cyris/tasks/jev-feature-vector-eval.md:74`）；(b) 讓評分 prompt 開始評新聞，成本是新聞量級的 LLM 呼叫；(c) 新聞群組的值取成員中最好的一篇，和現在的投票排序一致。
- **Q7 pending 文章的過期（S6b 的前置，不再是可選）。** 新聞不再寫 `rejected` 之後，掛不上主題也沒被寫進段落的新聞不會有任何狀態轉換，「窗口過了就不是新聞」是這一題最自然的答案。現在沒有任何程式會改變超出窗口的 pending 文章（`src/cyris/service_layer/run_digest.py:567-570`），`cyris articles clean` 預設只清 rejected（`src/cyris/entrypoints/cli.py:971`）。候選：(a) 一超出窗口就改成新的 `expired` 狀態；(b) 不改狀態，raw 頁和盲標池以 `first_seen_at` 過濾；(c) 讓 `articles clean` 預設清掉超出窗口的 pending。
- **Q8 拆成兩半。** 領域的分類已解：由 LLM 從讀者的來源分出來、讀者在 `/settings` 確認（那邊的 L1），所以不需要全域固定的領域清單。還開著的一半是託管版要不要替每個領域附入門來源清單：沒有來源的新讀者，LLM 沒有東西可分類。那是 §4 的新資料，也是託管版的產品決定。

## 什麼會推翻這個決定

- **點開不帶層級訊號。** 讀者按讚的文章大多沒被點開（摘要已經足夠），或點開與否和讚、倒讚無關。計算要以「期別加項目」為單位，因為新聞群組的一票會寫到群內每一個 URL（`docs/architecture.md:403-404`）。
- **位置偏差把訊號吃掉。** 排除位置的影響之後，點開率不再有差別。一位讀者、每期 15 項，這個估計會很慢。
- **點開太稀疏。** 深讀按鈕據說每週約 4 次點擊，但 commit `e235cc0`、`6931727` 的訊息裡都沒有這個數字，未驗證；而且那是「排入佇列並匯出」的刻意動作，不能預測標題點擊。「多稀疏算太稀疏」要事先定數字。
- **讀者主要不在網頁上看。** 通知不帶連結之後，這一條就量不到了，只能用「通知發出後，那一期有沒有在網頁上被打開」當代理，見 Q2。
- **從通知點進來的讀者沒被注入。** 如果 email 或 Discord 的內建瀏覽器沒有帶 Access 的 session 或 `cyris_session`（未驗證），從通知打開的那一期就收不到點開紀錄。查法：分別從 Discord app 和 mail app 打開一期，看 Worker 日誌有沒有注入。
- **拿掉連結讓讀者不再讀通知。** 如果改動後每期的投票數明顯下降，這個代價就比收不到點開更大。
- **素材少的時候填進雜訊。** 如果靠排序填滿的期數讓讀者明顯覺得品質下降，就要加一個相對於當期分布的下限。
- **排序整體準，但前 15 項不準。** 讀者只看得到前幾項，如果 AUC 和前 15 項命中率落差大，衡量方式就要改。
- **「沒點開」混了兩種狀態。** 看了標題覺得夠，和看了標題不在乎、懶得倒讚，是同一個觀測值。上面第一條只量得到上層訊號，量不到下層標籤的純度。

落選的方向：
- 來源 tier 直接決定層級：filter 來源的好文章永遠拿不到摘要。2026-10-11 補充：tier 決定的是預設形式，逐篇的值可以把新聞升格，所以這條維持落選，tier 本身留下。
- tier 給基本分再加逐篇的值：預設所有 tier 共用一條校準過的尺，和上面「只排得準、不校準」相反；理由全文在 `digest-by-topic-and-area.md`。
- 讀者對來源宣告密度：永遠累積不到逐篇的層級資料，低密度來源的好文章永遠留在下層。
- 通用的逐篇深度判斷：上層會變成編輯眼中的值得讀，不是這位讀者的；新聞從不評分，要新增新聞量級的 LLM 成本。
- 讀者在盲標頁明說「值得讀、需要知道、不要」：要重建盲標表，每輪約 100 題，學得慢。
- 回報寫進每期頁面：只收得到上線後的期數，回報格式永久留在那些頁面。
- email 和 Discord 用轉址連結：寄出的信永久帶著 cyris 網址，要防開放轉址。
- 逐篇判刪除、依固定門檻：每個判斷者和模型都要校準門檻，投票只會落在通過的文章上。
- 來源預設去留、逐篇判例外：被預設隱藏的文章永遠收不到票。

## 依據的資料

- 第一輪盲標開發集，98 題，只抽 filter tier：filter LLM 保留判斷 AUC 0.598，非新聞 0.517；embedding 最近投票 0.596；Clef 只給通用條件 0.668；Clef 加最近 5 篇投票標題 0.754；再加通用條件 0.747（vault `pm/cyris/tasks/jev-feature-vector-eval.md`）。數字被反覆看過，偏樂觀。
- 各層讚數：新聞保留 25 篇中 12 讚、被刪 25 篇中 4 讚；非新聞保留 23 篇中 9 讚、被刪 25 篇中 9 讚（vault `pm/cyris/tasks/filter-pool-blind-labels.md`）。
- 早期 59 題子集中的 23 篇非新聞，現有 LLM 分數 AUC 0.862，讀者按讚的 7 篇裡有 4 篇在 72 到 86 分時被 filter 刪掉（`~/cyris-jev-eval/clef-2026-10/export.json`）。樣本小、偏樂觀，匯出的 SQL 沒有存下來，未驗證。

## Suggested skills

- `spiral:spiral`：Q2 到 Q7 還沒有答案，Q7 現在擋著 S6b，適合再收斂一層。
- `adr:adr`：S2 取代 ADR-0013、S6「刪除判決退場」、S7 的位置偏差修正，都要先判斷值不值得寫 ADR；「單位是主題」的翻案已在那邊列為明確候選。
- `cf:cf`：S1 和 S2 的範圍已經夠清楚，可以各自當成實作的種子；cf 自己跑 research、plan 和審核關卡。
