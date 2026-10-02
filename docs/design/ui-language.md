---
status: accepted
accepted: 2026-09-17
updated: 2026-10-02
---

# cyris UI design spec

Every change that touches a reader-facing page follows this spec: `src/cyris/adapters/output/templates/`
(archive, digest, raw), `src/cyris/entrypoints/templates/` (settings), `src/cyris/entrypoints/static/`
(the settings scripts and `style.css`), and any page added later.

- **This document is the spec.** `docs/design/prototype.html` is its reference implementation; open it
  directly in a browser. `#system` is the overview of tokens and components, and `#archive`, `#digest`,
  `#raw` (with the triage view) and `#settings/model` are the pages. When the two disagree, this document
  wins, and the prototype is fixed in the same change.
- **Every §8 step has landed.** Where a component still differs from the spec, restyle it to the spec
  when you touch it, and do not write another copy of the old style.
- **To depart from the spec, change the spec first.** When you need a component or value the spec does
  not have, write it and its reason into this document before writing the code.

## 1. Principles

1. **One language, two densities.** Reading surfaces (archive, digest, raw) are loose; operating surfaces
   (raw's triage view, settings) are compact. Both share the tokens and components and differ only in
   which part of the spacing scale they use.
2. **Layers come from background and hairlines, not shadows.** From deepest to lightest the layers are
   `--bg`, `--bg-elev`, `--surface` and `--surface-2`, and object edges are a 1px `--border`. The only glow
   is the brand square.
3. **Controls are rounded, containers are square.** Buttons, inputs, selects, segmented controls and
   choice lists use `--r-control`; tags and pills use `--r-tag`. Panels, cards (triage cards included),
   tables and notices are always square.
4. **UI labels have one voice.** Navigation, tabs, buttons, field names and states all use the label role
   (§3).
5. **Colour has only two meanings.** `--accent` means the primary action, the selected item or a positive
   result; `--warn` means a destructive action or an error. A neutral result (for example rejected) uses
   `--text-dim`. Semantic colours are not used for decoration.
6. **No hardcoded values.** Colours, spacing, font sizes, radii and durations all come from tokens; a
   literal outside the tokens is allowed only as one of the exceptions §2 lists.
7. **The chrome is English; content names its own language.** A screen reader picks its voice from
   `lang` (WCAG 3.1.1, 3.1.2). Every page's `<html lang>` is `en`, the language of its labels. A block of
   digest text, which the model writes in `[digest] output_language`, carries that tag as its `lang`, and
   an English control inside it (a vote group, a sources fold) carries `lang="en"`. Text passed through
   untranslated, which is Following, On the Radar and every raw row, carries `lang=""`, HTML's unknown
   language: no reliable tag for it exists. A value that is not a BCP 47 tag, such as a plain language
   name an older config still holds, also renders as `lang=""`, never as a wrong tag.

## 2. Token

Below is the complete token set. `_tokens.css.j2` and `static/style.css` must match it verbatim, and
`tests/test_digest_css_partials.py` compares them.

```css
:root {
  /* backgrounds, deepest to lightest */
  --bg: #07070a;
  --bg-elev: #0d0d12;
  --surface: #11111a;
  --surface-2: #161622;
  /* lines */
  --border: #1f1f2e;
  --border-strong: #2a2a40;
  /* text */
  --text: #ebebf2;
  --text-dim: #8a8aa0;
  --text-faint: #62627a;
  /* semantic */
  --accent: #c6ff3d;
  --accent-dim: #8aa829;
  --warn: #ff5b8a;
  --accent-tint: rgba(198, 255, 61, 0.07);
  --warn-tint: rgba(255, 91, 138, 0.12);
  --grid: rgba(198, 255, 61, 0.035);
  /* spacing */
  --s-1: 4px;  --s-2: 8px;  --s-3: 12px;  --s-4: 16px;  --s-5: 20px;
  --s-6: 24px; --s-8: 32px; --s-12: 48px; --s-16: 64px; --s-20: 80px;
  /* fonts and the type scale multiplier */
  --font-sans: 'Geist', system-ui, -apple-system, sans-serif;
  --font-mono: 'Geist Mono', ui-monospace, monospace;
  --font-serif: 'Instrument Serif', Georgia, serif;
  --type-scale: 1;
  /* shape, motion, layout */
  --r-control: 6px;
  --r-tag: 999px;
  --t-fast: 150ms ease-out;
  --bar-h: 60px;
  --measure: 640px;
}
```

- **Units are always px.** Sizes are never in rem; em is used only for letter spacing.
- **There is one breakpoint: 720px.** Container widths are 960px for archive and raw and 1240px for digest
  and settings. Layout problems at widths above 720px (for example digest's dead zone in column count at
  880–1160px) are solved with fluid rules such as `auto-fit`, `minmax` and `clamp`, never with a second
  breakpoint.
- **Allowed literal exceptions:** the brand square's glow `rgba(198,255,61,.45)`, the site bar's
  translucent background `rgba(7,7,10,.88)`, the primary button's hover background `#d4ff66`, and the dots'
  `border-radius: 50%`. A new exception must be written into this bullet.
- **Text reaches 4.5:1 on every background.** Every text colour passes 4.5:1 against `--bg`,
  `--bg-elev`, `--surface` and `--surface-2`, so `--text-dim` is the dimmest text there is: labels,
  footers and the rejected state all use it. `--text-faint` is not a text colour; it is for non-text
  marks such as the triage card's hover border, and passes 3:1 against the same four. A faint text
  that passed 4.5:1 would sit within 1.2:1 of `--text-dim`, so the two were merged for text
  (2026-10-02). `tests/test_ui_spec.py` computes the ratios.
- Inline SVG illustration coordinates, paths and stroke widths are drawing geometry, not UI spacing.
  Illustrations inherit token colours; their outer size and margins still use the spacing scale.
- **All spacing is on the scale.** The digest body's former values such as 14/18/22/28/36/44/56px moved
  onto the scale in step 7; there are no exceptions after it.

### Light palette

The app's pages are dark only. Two surfaces also have a light mode, and both take this one palette:
the landing page (`website/index.html`, under `:root[data-theme="light"]`) when its reader switches
theme, and the mail (§6 *email*, `adapters/output/email_palette.json`) by default. It replaces the
colour tokens above by name; every other token is shared.

- **The same contrast rule holds.** `--text`, `--text-dim`, `--accent` and `--warn` pass 4.5:1 against
  the four backgrounds, and `--text-faint` passes 3:1 as a mark.
- **The accent is darker in light.** `#c6ff3d` cannot carry text on a light ground, so the light
  `--accent` is a dark lime. As text it also passes 4.5:1 on `--accent-tint` laid over `--bg` and
  `--surface`, where the score pill sits, and the primary button's `--bg` label passes 4.5:1 on it.
- **`--accent-hover` is light only.** It is the primary button's hover background, darker than
  `--accent` so the `--bg` label still passes 4.5:1. The dark hover stays the `#d4ff66` exception above.
- The landing page's palette became the only one on 2026-10-02. The mail's former palette put its
  accent under 4.5:1 on `--surface-2` and on the pill's tint. `tests/test_email_digest.py` computes the
  ratios and holds the palette file to this table; `tests/test_ui_spec.py` holds the landing page's copy
  to the palette file.

| Token | Light |
|---|---|
| `--bg` | `#f5f5ed` |
| `--bg-elev` | `#fffff7` |
| `--surface` | `#eaece3` |
| `--surface-2` | `#e1e5d8` |
| `--border` | `#d4d7c9` |
| `--border-strong` | `#b9c1ab` |
| `--text` | `#22271c` |
| `--text-dim` | `#606753` |
| `--text-faint` | `#737b68` |
| `--accent` | `#436600` |
| `--accent-dim` | `#5f7c20` |
| `--accent-hover` | `#365400` |
| `--warn` | `#b52d59` |
| `--accent-tint` | `rgba(67, 102, 0, 0.08)` |
| `--warn-tint` | `rgba(181, 45, 89, 0.1)` |
| `--grid` | `rgba(67, 102, 0, 0.06)` |

## 3. Type

The table below is the baseline, for `--type-scale: 1`. It was set on 2026-09-17 and lowered one step
across the board on 2026-09-21 (each value multiplied by 0.875 and rounded): after reading with it, the
original baseline was judged too large, and the multiplier's three steps are for readers to fine-tune, so
`Smaller` should not serve as the normal size.

- **Every `font-size` is written as `calc(baseline * var(--type-scale))`**, with a clamp wrapped inside,
  for example `calc(clamp(46px, 7vw, 84px) * var(--type-scale))`. The later font size setting
  (`docs/architecture.md` §7 #35) changes only the multiplier, never this table.
- Control heights, column widths and spacing do not scale with the multiplier.
- The multiplier has only three values: 0.875, 1 and 1.125. It is `digest.type_scale` in D1 `settings`,
  set in the Digest category of `/settings`; the app Worker adds `<style>html:root{--type-scale:X}</style>`
  to every HTML page it serves, so published issues follow it too (issues rendered before this setting existed
  have hardcoded font sizes and are unaffected). Opening pages.dev directly always gives 1, and so does the
  mail (§6 *email*).
- Uppercase is applied only in CSS (`text-transform: uppercase`); text in the HTML keeps its normal case.

| Role | Spec | Used for |
|---|---|---|
| display | Instrument Serif 400, clamp(46px, 7vw, 84px), line height 1, letter spacing −0.03em, `em` in italic `--accent` | Page titles (archive, raw, settings); the landing page's section headings (`h2`) |
| issue title | Instrument Serif 400, clamp(56px, 8.75vw, 119px), line height .92, letter spacing −0.04em | The digest masthead title; the landing page's hero `h1`, so it stays larger than the section headings below it. Under `lang="zh-Hant"` its maximum is 102px, which keeps the 6/7 the earlier zh-Hant cap took (72px of 84px) and stays above display's 84px |
| heading | Geist 600, 24px, line height 1.3, letter spacing −0.015em | Panel titles, settings category titles |
| title | Geist 600, 18–19px, line height 1.3 | List items, source names; triage card title 26px |
| body | Geist 400, 18px, line height 1.65, `--text-dim` | Descriptions, summaries |
| small | Geist 400, 14px, line height 1.5, `--text-dim` | Field help, table content |
| label | Geist Mono 500, 12px, uppercase, letter spacing 0.1em, `--text-dim` | Navigation, tabs, field names, buttons, section markers |
| data | Geist Mono 400, 14px, `tabular-nums`; archive dates 19px | Dates, scores, counts, URLs, commands |

## 4. Components

Each component has exactly one stylesheet (§7). Heights in the table are fixed and do not change with
`--type-scale`.

| Component | Spec |
|---|---|
| **site bar** | Full width, sticky, height `--bar-h`, 1px `--border` at the bottom. On the left, the brand square (12px `--accent`, pulse animation) plus `CYRIS`; on the right, links in the label role, `Archive` · `Settings`, spaced `--s-5`. The current page is `--text` with a 1px `--accent` underline. `Settings` reuses the `/api/vote` probe and is hidden when not authorized. When the probe gets the app Worker's own 401, `Sign in` takes its place and links to `/login?next=` plus this page, so signing in returns here; on pages.dev and behind a Cloudflare Access redirect there is no sign-in form to reach, and it stays hidden. On /settings the brand link and `Archive` show only once the probe answers authorized, because a local `cyris triage-ui` serves no archive. At phone width the word `CYRIS` is hidden |
| **issue bar** | Shared by digest and raw, below the site bar, on `--bg-elev`. On the left, the date (data) and the period (label); on the right, the segmented control `Digest` / `All articles`. The two are two views of the same issue |
| **segmented control** | 1px `--border-strong` outline, `--r-control`, 1px dividers between cells; each cell is 44px high, `--s-4` left and right, label role, `--text-dim`. Selected: `--surface-2` background, `--accent` text, 2px `--accent` underline. Links use `aria-current="page"`, toggle buttons use `aria-pressed` |
| **button** | Height 44px, `--s-5` left and right, label role, `--r-control`. **primary**: `--accent` background, `--bg` text. **secondary**: `--surface-2` background, `--border-strong` border, `--text-dim` text, text turns `--text` on hover. **danger**: transparent background, `--warn` text and border, `--warn-tint` background on hover. The small size is 34px high, `--s-2` left and right, letter spacing 0.06em, and still takes taps over 44 × 44 (**tap area**). Pressed (`:active`) lowers opacity to .7 on top of whatever hover state the button already shows, and changes nothing else; .7 stays clear of disabled's .4, so a pressed button never reads as disabled. Disabled is always opacity .4, and a disabled button does not change on hover or press |
| **tap area** | Every tap target takes taps over at least 44 × 44px, at every width, and a tap lands on exactly one target. A target drawn smaller keeps its drawn size and gets the rest from a transparent `::after`, `position: absolute`, `inset: min(0px, calc((100% - 44px) / 2))`, centred on it: the small button, the site bar's links, and the brand link, which is the 12px mark alone at phone width. Hit areas never cover each other or another target's drawn box. The ↑ and ↓ votes keep their `--s-1` gap and split it at its midpoint, each taking the rest of its 44px outward, and the vote group sits `--s-2` further from what comes before it, so that outward part clears its neighbour: the arrow glyphs fall back to whatever monospace the reader has, so a gap wide enough for two centred areas would hold only for one font's width. The digest's ↗ original link is a 44px-wide box with its glyph centred, because a centred area would reach 18px over the source names and votes on either side. `More` takes `--s-1` above it, half the field gap so the control above keeps its taps, and the rest below. A settings tab is itself at least 44px high, because its tab bar scrolls at phone width and clips anything drawn outside it. A cluster's `N sources` fold is exempt: a 44px area would cover the source links it unfolds. Text links inside a title or a line of text are exempt, as WCAG 2.5.8 exempts inline targets. The page probes' `tap-*` checks measure every target here at 375px |
| **input / select** | Height 48px, `--s-4` left and right, `--bg-elev` background, 1px `--border-strong` border, `--r-control`, Geist 16px. On focus the border turns `--accent`, even on a field that failed validation; on failed validation the border turns `--warn`, with a notice below |
| **multi-line input** | `<textarea class="input">`, reusing the input's background, border, radius, font size, and its focus and failed-validation rules; its height comes from `rows`, with `--s-3` padding top and bottom, and it resizes vertically only |
| **field** | From top to bottom: the field name in the label role, the control, and help in the small role, spaced `--s-2`. Help longer than one sentence goes into `<details>`, with the summary text `More` |
| **category list dots** | Two 6px dots on the right of each item in the settings category list: `--accent` means the category has unsaved changes, `--warn` means the category is missing a required value. Both can show at once, with the missing-value dot on the left |
| **choice list** | Several options for a single choice (for example provider): `--bg-elev` background, 1px `--border` border, `--r-control`; each row has `--s-3` top and bottom and `--s-4` left and right, rows divided by a bottom line, `--surface` on hover. The right side shows the state in the label role; a state too long to sit beside the name moves to its own line below it, still on the right, and is never cut off. An unselectable row sets its radio and name to opacity .5, never the state text that says why. On failed validation the border turns `--warn`, with a notice below, as an input's does |
| **headline card** | Only for the archive's latest issue. `--surface` background, 1px `--border-strong` border, square corners, `--s-6` padding. From top to bottom: an `--accent` `Latest` label with the date (data) and the period (label); the title of the issue's first story, which is its group heading when that story is a group (title role); the article count (data); the titles of the two topics with the most members (small, joined by ` · `); and two small secondary buttons, `Digest` and `All articles`. A field with no data is left out, with no placeholder text |
| **panel** | `--bg-elev` background, 1px `--border` border, square corners. An optional head row: `--surface` background, a bottom line, `--s-3` top and bottom and `--s-5` left and right, the title on the left and a count or action on the right. Adjacent panels are spaced `--s-5` |
| **list row / table row** | The class name is `.list-row` (`.row` is taken by the digest stats card). `--s-3` top and bottom, `--s-5` left and right, rows divided by a bottom line, `--surface` on hover. Table headers use the label role on `--surface`. A table sits in its own `overflow-x: auto` container |
| **pill** | Geist Mono 12px, `2px 10px`, 1px `--border-strong` border, `--surface-2` background, `--r-tag`. The score variant has `--accent` text, an `--accent-dim` border and an `--accent-tint` background, and only a score takes it: a source's tier is a plain pill, because no tier is the positive one (§1) |
| **state text** | Geist Mono 11px, uppercase, letter spacing 0.1em. accepted is `--accent`; pending is `--text-dim`; rejected is `--text-dim` |
| **notice** | Save results and errors: a 2px bar on the left (`--accent` or `--warn`), the matching tint background, square corners, small role, `white-space: pre-wrap`, placed beside the button that triggered it. An error message says what happened and how to fix it |
| **destructive confirm** | No `window.confirm`. On the first press, the danger button turns into `Confirm …` in place and takes an `--warn-tint` background; only a second press within 3 seconds acts, otherwise it reverts |
| **section marker** | The label role, preceded by a 24px × 1px `--accent` line, spaced `--s-3` |
| **footer** | `--s-16` above it, then a 1px `--border` and `--s-5` above its text, the same on archive, digest and raw; Geist Mono 12px uppercase, letter spacing 0.08em, `--text-dim`. It holds generation info, never site navigation. Its one link is the word `Cyris`, pointing to the project's site, so a page that reaches someone who has never heard of cyris says where it came from; the link takes the footer's colour. The mail (§6 *email*) is the one exception: it has no site bar, so its footer also carries the `Archive` · `Settings` links |
| **feature flow** | Landing-page explanation, not an app mockup. An ordered list of three illustrated steps: subscriptions, per-source processing, reading. Three equal columns with `--s-12` gaps; one column with `--s-16` gaps at 720px. SVGs are decorative (`aria-hidden`, non-focusable), 100% wide and `2 × --s-20` high, followed by `--s-6`, a 19px title and 14px small text with `--s-2` between them. Text carries the full meaning without the drawings. Line art uses `--text-dim`; `--accent` marks the resulting digest only. Static chevrons point along reading order, right on desktop and down on phones; no animation, fake controls, dates or fictional articles. The `#system` prototype shows the same illustration |

### Interaction

- **Focus:** every interactive element has `:focus-visible { outline: 1px solid var(--accent); outline-offset: 2px; }`.
  Only inputs may remove the outline, showing focus by a border colour change instead.
- **Transitions:** only `color`, `background-color`, `border-color` and `opacity` change, over `--t-fast`,
  and `transition: all` is never written. Transform is used only for the triage card's drag and fly-out,
  and to rotate the feature flow's static chevrons.
- **Hover** neither moves nor scales. The pressed state only lowers opacity, to the button's .7 (§4).
  A disabled button neither hovers nor presses.
- **Reduced motion:** `@media (prefers-reduced-motion: reduce)` cancels transitions, animations and smooth
  scroll globally.
- **Votes:** each small ↑ / ↓ vote button is named `More like this` / `Less like this` and carries
  `aria-pressed`, true on the vote this browser cast. While a vote is in flight both buttons of its group
  are disabled, and the request gives up after a fixed timeout; once it settles, focus goes back to the
  button that was pressed, or to the `Triage` switch when the deck has no card left. A vote that does not land puts an error notice right below the row holding its
  buttons, saying why, and a lapsed sign-in says to sign in again; the next vote on that group clears it.
  The triage deck's notice gives the same reason.

## 5. Navigation

```
Archive (/) ──► an issue ─┬─ Digest       ◄── the Discord notification's link lands here
                          └─ All articles (raw) ─┬─ List (default)
                                                 └─ Triage (appears only when authorized)
Settings (/settings): on the site bar, appears only when authorized
Sign in (/login): on the site bar in Settings' place, when the app Worker answers signed out
```

- Every page has the site bar at the top; digest and raw also have the issue bar. Navigation does not go
  in the footer; the footer's project link (§4) leaves the site rather than moving within it.
- The archive's headline card and every row have two entries, `Digest` and `All articles`.
- There is no separate triage page. Articles are judged in raw's triage view; the deck was retired on
  2026-09-19 (§8 step 4).
- There is no previous or next issue. The use case is reading each day's issue on that day.
- A back link must point to a path that exists. `/triage` is a 404 in production.

## 6. Page layouts

Every page's head links the brand favicon (`favicon.svg`, published at the site root with every
deployment), carries a one-sentence meta description saying what the page holds, and declares
`<meta name="color-scheme" content="dark">`: the §2 palette is dark only, and without it the browser
draws scrollbars, select popups and other native controls light. The mail declares its own (§6 *email*).

### archive

Below the page head (label, display, small description), the latest issue is the headline card, and the
remaining issues are split into panels by year and month. A panel's head row has the year and month (data)
on the left and the issue count on the right. Each issue is one row: the date, the period (label), the
article count (small, only for issues with a record), and two small secondary buttons; at phone width the
two buttons wrap to the next line. Past rows show no topics: a topic title is a sentence rather than a
tag, and of the 91 issues measured on 2026-09-19 only 20 had a topic record.

- The page head's small description opens with one sentence saying what Cyris is, because the archive is
  where the landing page sends a stranger.
- An archive with no issue shows one sentence in the small role where the headline card would be, its
  command in the data role. Like the digest's empty sentence it is not a notice, and it has no box, no
  accent and no spacing of its own (§1).
- Every issue is listed, never truncated, paginated or collapsed, because Pages' recovery rebuilds from
  every issue the index lists.
- The period is printed as its label text, with no colour coding, and the layout does not assume two
  issues a day. A day's issues follow the order in which the schedule fires; rows after the first one
  show their date in `--text-dim`, a signal independent of the labels. On a row with no article count,
  the two buttons still line up in the same column.
- The data sources and trade-offs are recorded in `docs/milestones/digest-archive-index-layout.md`; for
  breakpoints, §2 of this document governs.

### digest

At the top are the site bar and the issue bar. The masthead has the issue title (`h1`) on the left and the
stats card on the right; the stats column is `min(37vw, 320px)` wide and narrows with the page, so the
wider fallback font before the webfont loads does not run into it. Below 720px it becomes a single
`minmax(0, 1fr)` column. The masthead is always `overflow-x: clip`, so a title widened by the fallback font
is clipped on a phone instead of scrolling the whole page sideways.

The body is six sections in order: Top story, Features, In Focus, Following, On the Radar and The Wire; a
section with no content is left out entirely. Each section is one `<section class="section">`, and its name
is printed once, only by the opening section marker (§4), with no number and no repeated large section
title. The section marker is an `h2`; Top story is the exception and uses a `div`, because the lead card's
title is itself that section's `h2`.

- **Heading levels follow the structure.** `h1` is the issue title; `h2` is the section markers and the
  lead title; `h3` is the titles of features, news groups and topic blocks; `h4` is the articles inside a
  topic block and the On the Radar items. `h3` and `h4` are both the title role (§3), with the class
  `.item-title`: `h3` adds `.lg` for 19px, and `h4` uses 18px. Each row of The Wire is only a number and a
  link, with no heading element.
- **A summarize group is one card.** When the model summarized several articles together, the lead or
  feature card takes the group's heading as its title and prints the summary once. Each article follows as
  an `.article-item` with its own title link and meta row, so each keeps its own score and vote: they are
  different articles, unlike a news group's reports of one event. Those titles are `h4` in a feature and
  `h3 .lg` in the lead, one level below the card title. A group of one article is drawn as that article.
- **Every item has a meta row below it.** `.meta` is flex, wrapping, spaced `--s-3`, and its font is the
  label role; it holds the source and the original link in that order (a feature has a score pill in
  front), and the small vote buttons are always last. Only the lead card's meta row adds `.ruled`, which
  puts a dashed line above it. A news group with more than two sources folds them into `<details>`, with
  the summary text `N sources`.
- **Features and On the Radar share one fluid column rule:**
  `repeat(auto-fit, minmax(min(100%, 320px), 1fr))`, with no column count set at any width. The Features
  grid lines are drawn on each card's right and bottom edges, so when the last row is not full, the empty
  cells are the page background, not a solid block of border colour.
- The only breakpoint is 720px, all body spacing is on the §2 scale, and the only glow is the brand square.
- **Prose stops at `--measure`.** Every summary, snippet, section description and the empty or degraded
  note is at most `--measure` wide, so a line stays readable when the container is 1240px; a card
  narrower than that is unaffected. Headings and meta rows take the full width.
- **An issue says when it is empty or degraded.** An issue that includes no article opens its body with
  one sentence in the small role: the run judged the articles it received and kept none, and
  `All articles` (linked when the raw page exists) lists each one with its verdict. It is not a notice,
  because an empty issue is neither a success nor an error (§1). A degraded issue, one where a step went
  on without a configured LLM that could not be used, opens its body with a `--warn` notice saying some
  or all of it is unscored or plain excerpts and that the provider and its key on Settings are what to
  check. An issue made with provider `none` is plain excerpts by choice and shows no notice.

### raw

The page head's label gives the article count and the source count, and below it is the segmented control
`List` / `Triage`, defaulting to `List`. `Triage` reuses the `/api/vote` probe; when not authorized the
whole segmented control is hidden and the page shows only the list. The two views share one set of data,
and switching does not reload.

- **list view:** one panel per source; each row is, in order, the state text, the score (data), the title
  link and the small vote buttons. At phone width the title wraps to the next line.
- **triage view:** one card at a time, whose face is the source (label) and the title (title role, 26px).
  The card is square, on `--surface`, with a 1px `--border-strong` border and no shadow; tilting towards
  up turns the border `--accent`, tilting towards down turns it `--warn`. Swiping left is down, swiping
  right is up, and a tap opens the original in a new tab. The card takes keyboard focus as a link, and
  Enter on it opens the original the same way. Below it, a danger `Down` and a primary `Up` button sit
  side by side, both 56px high, for desktops without touch; above it, a label shows
  `N remaining`. Cards take only articles whose state is still pending and that have not been voted on
  yet; to overturn an article the pipeline has already judged, vote in the list view. With no cards left,
  only `0 remaining` remains.
- A voted article shows its state in both views: the list's state text turns `accepted` for up and
  `rejected` for down. On a later visit, a vote this browser remembers changes only a row rendered
  `pending`, because each run applies the votes cast before it. Votes go through the existing promote Worker, with no new backend.
- The drag and fly-out are the only transform motion on the site, and are cancelled under reduced motion.

### settings

`/settings` is a single page that switches categories by hash. It is not split into several paths, because
the Worker's `PROTECTED` only matches a path exactly equal to `/settings` (`workers/app/src/router.js:10`).

| hash | Category | Contents |
|---|---|---|
| `#model` | Model | LLM provider (a choice list, showing on the right whether the key is ready, with a last option `None — plain excerpts` meaning no LLM is called and only original excerpts are shown), model, a real call to verify before saving; embedding provider and model, the vote similarity switch, the number of votes compared against |
| `#digest` | Digest | The two publishing periods, the time zone, the number of featured blocks, the article limit per issue, the output language, the style prompt (multi-line input), the font size (a select: Smaller 0.875 / Default 1 / Larger 1.125; shows `Not set` when unset) |
| `#pipeline` | Pipeline | The hours to look back, the article limit per pass, the two score thresholds for featured and summary, how many characters each of the three steps reads |
| `#notifications` | Notifications | Discord webhook, with a stored value shown masked; a danger `Turn off` uses the destructive confirm, and after it the field is cleared and `Notifications are off.` is shown; then Email to and Email from, shown in full, where an empty Email to means no mail and Save sends a test message before storing the pair |
| `#sources` | Sources | A type filter (All / RSS / Newsletter) and `Add source`; table columns are name, type, tier, feed or sender, and tags; clicking a row, or pressing Enter or Space on it, expands it for editing in place, with focus on the Name field, and `Cancel` puts focus back on the row, or on `Add source` for a new one; a save or retire that fails puts focus back on its button, and a retire that lands puts it on `Add source`; the form shows only the fields that type needs; `Retire` uses the destructive confirm, but refuses to retire the last source |

- The page width is 1240px. Above 720px, the left side is a 220px category list (label role, with a 2px
  `--accent` left line when selected); below it, the list becomes a horizontally scrollable tab bar at the
  top. While a tab lies past the bar's right edge, a `›` in the label role stays pinned to that edge over
  a fade to `--bg`, so the tabs out of view and their dots are known to be there; it takes no taps, so
  one at the edge reaches the tab beneath it. Once the bar is scrolled to its end, or fits, the `›` is
  gone.
- The sources table scrolls sideways in its own container (§4), but an open source editor is as wide as
  the container's visible part and stays in view however far the rows are scrolled, so every field and
  button in it is reachable at phone width without scrolling.
- Each deployment has only one settings source, so the page does not show where a value comes from. Each
  category opens with a heading and a one-sentence small description.
- When a required value is missing, its field is left empty and marked as failed validation, the top of
  the category lists the missing fields in one error notice, and the list gets a `--warn` dot. All three
  marks disappear together after saving. A deployment with no source stops its next run too, so Sources
  gets the `--warn` dot while its table says why, until a source is saved.
- Until the stored values arrive, each category's description is followed by `Loading settings…` in the
  small role, and the Sources table holds one `Loading sources…` row. Each goes when its answer arrives,
  or the error notice that replaces it.
- Each category has exactly one primary `Save`, disabled when nothing has changed; a category with unsaved
  changes gets an `--accent` dot in the list. A missing value whose answer may be empty, such as Style or
  Email to, keeps `Save` enabled without a dot, so a first boot can store it as it stands.
- A save the server refuses for one field marks that field as failed validation, with the reason in a
  notice below it, named by the field's label; the notice beside `Save` says it too. Editing the field
  clears its mark.
- While a save is out, its `Save` stays disabled and the notice beside it says what is happening, such as
  `Saving…` or `Checking with the provider…`, until the result replaces it. A source editor is locked
  while its save is out, and its fields show the disabled opacity.
- Every notice that reports a save, a retire or a load is a polite live region (`role="status"`), so a
  screen reader reads its result out.
- The prototype governs the fields' names, help text and `More` text.

### email

The mail a run sends is its own document, not the digest page: mail clients run no script, and Gmail
drops CSS custom properties and `prefers-color-scheme`. `templates/email.html.j2` renders the §2 tokens in
by value, from `_tokens.css.j2` and the §2 light palette (`adapters/output/email_palette.json`).

- **Light is the default and dark follows the reader.** The light palette applies first; under
  `@media (prefers-color-scheme: dark)` the §2 colours replace it, and the head declares
  `color-scheme: light dark` so Apple Mail does not re-tint either. Gmail ignores the media query and
  inverts the light message itself, which stays legible; an inverted dark one would not.
- **One column.** `--measure` wide, `--s-4` gutters, no breakpoint. Sections keep the page's labels and
  order: Top story, Features, In Focus, Following, On the Radar, The Wire. The lead is the headline card's
  square panel; every other item is separated by a `--border` hairline.
- **No script, no vote, absolute links only.** The issue links to its page, its raw page when one was
  published, the archive and Settings, all on the digest link's own host; a failed publish shows a
  `--warn` notice instead. The footer's project link (§4) is the one link off that host.
- **Empty and degraded as on the page.** The empty sentence and the degraded notice (§6 *digest*) come
  after the buttons, the empty sentence in the small role with its `All articles` link only when the raw
  page was published.
- **Type at the baseline.** Nothing serves the mail, so `digest.type_scale` does not reach it. Web fonts
  come from the same Google Fonts link; Apple Mail loads them, Gmail falls back to each stack.

## 7. Where styles live

- The digest is standalone HTML deployed to Pages and cannot link an external stylesheet, so the token
  and component styles live in `templates/_*.css.j2` partials: `_tokens.css.j2` holds §2 and
  `_components.css.j2` holds §4.
- The site bar and issue bar markup lives in `_site_bar.html.j2` and `_issue_bar.html.j2`.
- `static/style.css` is a copy of these partials, and settings gets the same styles from it.
- `tests/test_digest_css_partials.py` must compare both the token and the component rules, or the two
  sides will diverge again.

## 8. Landing history

Of the gaps surveyed on 2026-09-17, steps 1 to 3 landed on 2026-09-18 and steps 4 to 7 landed on
2026-09-19, so every step has landed. Step 1 added the §2 tokens, the §3 type, focus and reduced motion,
removed two hardcoded colours, and added the component CSS comparison test. Step 2 put the site bar and
issue bar on archive, digest and raw. Step 3 rebuilt settings to §6. Step 4 rebuilt raw's list to §6 and
added the triage view that appears only after signing in, and the deck was deleted the same day. Step 5
gave each deployment one settings source: settings shows no origin, lists every runtime setting and marks
a missing one in its field, at the top of its category and with a `--warn` dot. Step 6 turned the archive
into a headline card plus year-and-month panels, with a day's later rows set apart by a dimmed date. Step
7 rebuilt the digest body to §6: each section's name is printed once by its tag, heading levels follow the
structure, `.meta` is folded into one base rule, the two body grids share one fluid column rule, 720px is
the only breakpoint left, and all body spacing moved onto the scale.

The steps in their planned order, each of which shipped alone. Each step was its own ticket, tracked
privately outside this repository:

1. §2 tokens, §3 type, focus and reduced motion, removing two hardcoded colours, and extending the CSS comparison test to components
2. The site bar and issue bar on archive, digest and raw; the two entries added to archive rows; footer navigation removed
3. Settings rebuilt to §6
4. The triage view added to raw; the deck deleted
5. Each deployment has only one settings source; settings lists every runtime setting and marks missing values, with no origin pill
6. The archive turned into a headline card plus year-and-month sections
7. The digest body's heading levels, `.meta` and the width dead zone, with body spacing moved onto the scale in this step

## 9. Checklist before changing the UI

- [ ] Colours, spacing, radii and durations all come from §2 tokens, or are exceptions §2 lists
- [ ] Every `font-size` is multiplied by `--type-scale`, and its role matches §3
- [ ] Controls are rounded, containers are square
- [ ] The components used already exist in §4; a new component was written into §4 first
- [ ] Interactive elements have `:focus-visible`, and no transition is written as `all`
- [ ] At 400px width the page does not scroll sideways, and tables scroll in their own container
- [ ] Every new tap target takes taps over 44 × 44 at 375px without covering a neighbour (§4 *tap area*)
- [ ] Navigation is in the site bar, and back links point to paths that exist
- [ ] `docs/design/prototype.html` has been updated to match
