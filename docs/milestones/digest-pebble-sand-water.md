---
status: accepted
delivered:
depends: []
---

# digest 改成 pebble、sand、water 三層，票投在題目上

2026-10-28 到 10-29 的討論定案，取代 [digest-by-topic-and-area.md](digest-by-topic-and-area.md) 的領域骨架與合成段落。決定與落選方案記在 [ADR-0023](../decisions/0023-votes-go-on-topics-and-the-digest-has-three-layers.md)。設計、待決問題與所有實驗在 Obsidian vault `pm/cyris/docs/digest-pebble-sand-water.md`（2026-10-29 版）與旁邊的 `digest-pebble-sand-water/` 資料夾，父卡是 vault `pm/cyris/tasks/m-digest-pebble-sand-water.md`。repo 基準是 `9a7fa61b`。

## 這個里程碑定下什麼

以下是 owner 2026-10-28 到 10-29 定的事，父卡的清單為準：

- cyris 是讀者的編輯，只做篩選與說明，不做分析。頁面不顯示來源，來源收起來，讀者在乎時才展開。
- 每一則都是題目。三層只決定呈現：pebble 是一篇 `read` 或 `follow`，sand 是兩篇以上的 `skim`，water 是一篇 `skim`。分層只看 lane 與這一期的成員數：每篇 `read`、`follow` 各自是 pebble，題目在這一期的 `skim` 文章依篇數成 sand 或 water，延續中的題目這期只來一篇也是 water。
- 票投在題目上，題目 id 跨期沿用，票每 7 天減半。
- 選項 I 現階段採用：只有實體 tag 承接偏好（1/df 折扣），領域與地區 tag 只顯示與檢索。理由是 pebble 已經獨立成層、不會被淹沒，sand 與 water 的排序改進另外推動。
- 嵌入不參與排序，用在題目分配。
- Clef 不用來擴散題目票；Jev 方向正確，可當小權重輔助。
- 不設輸出上限，保留進料上限 `max_articles_per_digest`。

已知弱點：題目分配的可行性驗證兩輪都沒過門檻，召回只有 0.49–0.61，同一個故事常被拆成兩個名字，拆開後的題目接不上原本的票。owner 2026-10-29 決定接受現況往下做，eval 由 `topic-assignment-eval` 補上。數字與出處見 ADR-0023 的 Known weakness 一節。

設計文件 §5 列的問題（題目存在哪裡、題目票要不要蓋 `triaged_at`、sand 的說明由誰寫、filter 的批次與準則、pebble 要不要顯示來源名、Clef 的去留、題目向量的算法）還沒定，各由下方負責的票決定。

## 分階段計畫

每一階段是 vault `pm/cyris/tasks/` 底下的一張票。第一步端到端先以替身跑通：三層頁面上線時，題目暫用這一期現有的新聞分群，之後的票逐一換掉替身。

| 順序 | 票 | 結果 |
|---|---|---|
| 1 | `topic-assignment-feasibility` | 離線量 LLM 分配題目的準確度。已完成，未過門檻，owner 接受現況 |
| 2 | `adr-topic-votes-three-tiers` | ADR-0023 與這份文件 |
| 3 | `digest-three-tiers-page` | 網頁以三層出刊，題目暫用替身；拿掉輸出截尾 |
| 4 | `topic-store-and-votes` | 題目跨期存放、題目票與選項 I 排序；`docs/architecture.md` §4 加列 |
| 5 | `filter-assigns-topics` | filter 分配題目與 tag，換掉替身題目，`NEWS_CLUSTER_SYSTEM` 退役 |
| 6 | `sand-what-happened` | 每期一次呼叫寫 sand 的「發生了什麼」，換掉替身說明 |

接在 `digest-three-tiers-page` 之後：

- `pebble-explanation-prompt`：pebble 的說明改成幫讀者判斷要不要讀。
- `email-discord-three-tiers`：email 與 Discord 通知改成三層。
- `retire-area-skeleton`：拆掉領域骨架與合成段落。
- `low-score-source-demotion`：持續低分的 `read` 來源，引導降級或移除。

接在 `topic-store-and-votes` 之後：

- `onboarding-topic-votes`：onboarding 改成問哪些整篇讀、哪些掃過，投票改成對題目表態。
- `topic-mute`：永久靜音一個題目或實體 tag。
- `jev-auxiliary-ranking`：Jev 當小權重擴散器輔助 sand 與 water 排序。

另有 `topic-assignment-eval`：用另外三期人工標的答案重量題目分配，結論寫進 `filter-assigns-topics`。

閱讀方式與來源小卡（og image）留在設計文件 §7，未開票。

## 完成條件

父卡的驗收條件：所有子票完成；正式站一期 digest 以三層出刊，題目票跨期生效，D1 的題目票表有列，被投 ↑ 的題目下一期名次上升。
