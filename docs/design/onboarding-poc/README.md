# Immersive onboarding POCs

Throwaway prototypes on the unmerged branch `poc/onboarding-immersive`, made 2026-11-01. They
answer the vault card `pm/cyris/tasks/onboarding-first-impression.md`: where should a new
reader's sense of anticipation come from? Its candidates are the opening screen, step
transitions, preferences visibly forming, and the first-issue reveal. The direction the owner
picks goes into `docs/design/ui-language.md` §6 *onboarding* first; none of this code ships.

Each file is one self-contained HTML page, opened straight from disk. Every step runs on fake
data; no request leaves the page.

| File | Spine | Bets on |
|---|---|---|
| `a-sieve.html` | A stream of article dots falls through the reader's choices; what passes is the issue | opening, preference forming |
| `b-pebble-sand-water.html` | The reader drops each feed into a layer: pebble, sand or water | preference forming |
| `c-editors-letter.html` | Cyris speaks as an editor, one sentence at a time; each setting is a blank in a sentence | step transitions |
| `d-reveal.html` | Setup stays light; the two waits become a live view of the editor at work, ending in the first issue | first-issue reveal |
| `e-stream-letter.html` | C's editor speaks over A's stream, rendered in 3D as pebble, sand and water | opening, preference forming, first-issue reveal |
| `f-attention-funnel.html` | One claim, then learn by doing: every feed you add raises what comes in while what reaches you stays about twelve | preference forming, opening |

## The flow every POC walks (owner, 2026-11-01)

The POCs target the shape after ADR-0023 and `onboarding-topic-votes`, not today's six steps,
and lead with the story. Language and timezone are detected from the browser and only confirmed
at the end.

1. **Opening.** The core idea without a paragraph: many articles come in, only what matters
   reaches the reader ("Only what matters should reach your brain", README.md).
2. **Topics, then feeds.** The six topics of `src/cyris/onboarding_feeds.json`, then the
   chosen topics' feeds, plus a field for an own feed.
3. **Read whole, or skim.** For each picked feed, whether the reader reads its articles whole
   (pebble, the `read` lane) or skims them (sand when several skim articles share a topic,
   water when one does). Unanswered means skim.
4. **Wait one: fetching the feeds.** About 60 seconds for 12 feeds in production; the POC
   compresses it to about 8 seconds and lets the reader press on.
5. **Topic stances.** About six topics drawn from the fetched articles. Each takes "more like
   this", "less" or no answer; no answer counts for nothing.
6. **Wait two: the first issue.** Up to 15 minutes in production; compressed to about 12
   seconds. It ends by showing a small three-layer issue built from the reader's answers.
7. **The quiet settings.** One compact screen: detected language and timezone, the two
   publish hours (8 and 18 by default), Discord webhook and email. All optional.

## Shared contract

- **Focus.** Each screen has at most one moving element and one question.
- **Tokens.** Colours, fonts, spacing, radii and durations are copied verbatim from
  `docs/design/ui-language.md` §2. Pages are dark only. Layers come from background and
  hairlines, never shadows. Controls are rounded (`--r-control`); containers are square.
  `--accent` marks only the primary action, a selection or a positive result.
- **Language.** UI chrome is English, as the repo's convention requires. Feed names, article
  titles and topic names are the content's own language, mostly Traditional Chinese.
- **Every step can be skipped** and reached again with Back.
- **Motion.** Motion carries meaning; it never decorates. Under `prefers-reduced-motion`,
  every animation is replaced by its end state.
- **Layout.** Readable and usable at 390px wide with no horizontal scroll. 720px is the only
  breakpoint.
- **Stack.** Vanilla HTML, CSS and JS (Canvas or SVG are fine), Google Fonts the only external
  load. `#<step>` opens a step directly, for review.

## Criteria for comparing them

Set before any POC was built:

1. Someone who skips every word can still say what cyris does: few out of many, and the reader
   steers it.
2. The reader understands pebble, sand and water before the first issue arrives.
3. The screen never holds more than one moving element and one question.
4. Both waits feel like the product working, not a spinner.
5. All seven steps can be finished or skipped in under three minutes, waits excluded.
6. It still makes sense under reduced motion and at phone width.
