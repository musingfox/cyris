---
status: accepted
accepted: 2026-09-17
updated: 2026-09-19
---

# cyris UI design spec

Every change that touches a reader-facing page follows this spec: `src/cyris/adapters/output/templates/`
(archive, digest, raw), `src/cyris/entrypoints/templates/` (settings), `src/cyris/entrypoints/static/`
(the settings scripts and `style.css`), and any page added later.

- **This document is the spec.** `docs/design/prototype.html` is its reference implementation; open it
  directly in a browser. `#system` is the overview of tokens and components, and `#archive`, `#digest`,
  `#raw` (with the triage view) and `#settings/model` are the pages. When the two disagree, this document
  wins, and the prototype is fixed in the same change.
- **The code has not caught up yet.** §8 lists the current gaps and the landing order. Until it has
  landed, restyle whichever component you touch to the spec, and do not write another copy of the old
  style.
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
   `--text-faint`. Semantic colours are not used for decoration.
6. **No hardcoded values.** Colours, spacing, font sizes, radii and durations all come from tokens; a
   literal outside the tokens is allowed only as one of the exceptions §2 lists.

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
  --text-faint: #56566e;
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
- **All spacing is on the scale.** The digest body's former values such as 14/18/22/28/36/44/56px moved
  onto the scale in step 7 (`digest-issue-page-layout`); there are no exceptions after it.

## 3. Type

The table below is the baseline, for `--type-scale: 1`. It was set on 2026-09-17 and lowered one step
across the board on 2026-09-21 (each value multiplied by 0.875 and rounded): after reading with it, the
original baseline was judged too large, and the multiplier's three steps are for readers to fine-tune, so
`Smaller` should not serve as the normal size.

- **Every `font-size` is written as `calc(baseline * var(--type-scale))`**, with a clamp wrapped inside,
  for example `calc(clamp(52px, 8vw, 96px) * var(--type-scale))`. The later font size setting
  (`docs/architecture.md` §7 #35) changes only the multiplier, never this table.
- Control heights, column widths and spacing do not scale with the multiplier.
- The multiplier has only three values: 0.875, 1 and 1.125. It is `digest.type_scale` in D1 `settings`,
  set in the Digest category of `/settings`; the app Worker adds `<style>html:root{--type-scale:X}</style>`
  to every HTML page it serves, so published issues follow it too (issues from before 2026-09-18 have
  hardcoded font sizes and are unaffected). Opening pages.dev directly always gives 1.
- Uppercase is applied only in CSS (`text-transform: uppercase`); text in the HTML keeps its normal case.

| Role | Spec | Used for |
|---|---|---|
| display | Instrument Serif 400, clamp(46px, 7vw, 84px), line height 1, letter spacing −0.03em, `em` in italic `--accent` | Page titles (archive, raw, settings) |
| issue title | Instrument Serif 400, clamp(56px, 8.75vw, 119px), line height .92, letter spacing −0.04em | The digest masthead title |
| heading | Geist 600, 24px, line height 1.3, letter spacing −0.015em | Panel titles, settings category titles |
| title | Geist 600, 18–19px, line height 1.3 | List items, source names; triage card title 26px |
| body | Geist 400, 18px, line height 1.65, `--text-dim` | Descriptions, summaries |
| small | Geist 400, 14px, line height 1.5, `--text-dim` | Field help, table content |
| label | Geist Mono 500, 12px, uppercase, letter spacing 0.1em, `--text-faint` | Navigation, tabs, field names, buttons, section markers |
| data | Geist Mono 400, 14px, `tabular-nums`; archive dates 19px | Dates, scores, counts, URLs |

## 4. Components

Each component has exactly one stylesheet (§7). Heights in the table are fixed and do not change with
`--type-scale`.

| Component | Spec |
|---|---|
| **site bar** | Full width, sticky, height `--bar-h`, 1px `--border` at the bottom. On the left, the brand square (12px `--accent`, pulse animation) plus `CYRIS`; on the right, links in the label role, `Archive` · `Settings`, spaced `--s-5`. The current page is `--text` with a 1px `--accent` underline. `Settings` reuses the `/api/vote` probe and is hidden when not authorized. At phone width the word `CYRIS` is hidden |
| **issue bar** | Shared by digest and raw, below the site bar, on `--bg-elev`. On the left, the date (data) and the period (label); on the right, the segmented control `Digest` / `All articles`. The two are two views of the same issue |
| **segmented control** | 1px `--border-strong` outline, `--r-control`, 1px dividers between cells; each cell is 44px high, `--s-4` left and right, label role, `--text-dim`. Selected: `--surface-2` background, `--accent` text, 2px `--accent` underline. Links use `aria-current="page"`, toggle buttons use `aria-pressed` |
| **button** | Height 44px, `--s-5` left and right, label role, `--r-control`. **primary**: `--accent` background, `--bg` text. **secondary**: `--surface-2` background, `--border-strong` border, `--text-dim` text, text turns `--text` on hover. **danger**: transparent background, `--warn` text and border, `--warn-tint` background on hover. The small size is 34px high, `--s-2` left and right, letter spacing 0.06em. Disabled is always opacity .4 |
| **input / select** | Height 48px, `--s-4` left and right, `--bg-elev` background, 1px `--border-strong` border, `--r-control`, Geist 18px. On focus the border turns `--accent`; on failed validation the border turns `--warn`, with a notice below |
| **multi-line input** | `<textarea class="input">`, reusing the input's background, border, radius, font size, and its focus and failed-validation rules; its height comes from `rows`, with `--s-3` padding top and bottom, and it resizes vertically only |
| **field** | From top to bottom: the field name in the label role, the control, and help in the small role, spaced `--s-2`. Help longer than one sentence goes into `<details>`, with the summary text `More` |
| **category list dots** | Two 6px dots on the right of each item in the settings category list: `--accent` means the category has unsaved changes, `--warn` means the category is missing a required value. Both can show at once, with the missing-value dot on the left |
| **choice list** | Several options for a single choice (for example provider): `--bg-elev` background, 1px `--border` border, `--r-control`; each row has `--s-3` top and bottom and `--s-4` left and right, rows divided by a bottom line, `--surface` on hover. The right side shows the state in the label role; an unselectable row is opacity .5 |
| **headline card** | Only for the archive's latest issue. `--surface` background, 1px `--border-strong` border, square corners, `--s-6` padding. From top to bottom: an `--accent` `Latest` label with the date (data) and the period (label); the title of the issue's first article (title role); the article count (data); the titles of the two topics with the most members (small, joined by ` · `); and two small secondary buttons, `Digest` and `All articles`. A field with no data is left out, with no placeholder text |
| **panel** | `--bg-elev` background, 1px `--border` border, square corners. An optional head row: `--surface` background, a bottom line, `--s-3` top and bottom and `--s-5` left and right, the title on the left and a count or action on the right. Adjacent panels are spaced `--s-5` |
| **list row / table row** | The class name is `.list-row` (`.row` is taken by the digest stats card). `--s-3` top and bottom, `--s-5` left and right, rows divided by a bottom line, `--surface` on hover. Table headers use the label role on `--surface`. A table sits in its own `overflow-x: auto` container |
| **pill** | Geist Mono 14px, `2px 10px`, 1px `--border-strong` border, `--surface-2` background, `--r-tag`. The score variant has `--accent` text, an `--accent-dim` border and an `--accent-tint` background |
| **state text** | Geist Mono 13px, uppercase, letter spacing 0.1em. accepted is `--accent`; pending is `--text-dim`; rejected is `--text-faint` |
| **notice** | Save results and errors: a 2px bar on the left (`--accent` or `--warn`), the matching tint background, square corners, small role, `white-space: pre-wrap`, placed beside the button that triggered it. An error message says what happened and how to fix it |
| **destructive confirm** | No `window.confirm`. On the first press, the danger button turns into `Confirm …` in place and takes an `--warn-tint` background; only a second press within 3 seconds acts, otherwise it reverts |
| **section marker** | The label role, preceded by a 24px × 1px `--accent` line, spaced `--s-3` |
| **footer** | 1px `--border` above, Geist Mono 14px uppercase, letter spacing 0.08em, `--text-faint`. It holds generation info only, never navigation |

### Interaction

- **Focus:** every interactive element has `:focus-visible { outline: 1px solid var(--accent); outline-offset: 2px; }`.
  Only inputs may remove the outline, showing focus by a border colour change instead.
- **Transitions:** only `color`, `background-color`, `border-color` and `opacity` change, over `--t-fast`,
  and `transition: all` is never written. The only use of transform is the triage card's drag and fly-out.
- **Hover** neither moves nor scales. The pressed state only lowers opacity.
- **Reduced motion:** `@media (prefers-reduced-motion: reduce)` cancels transitions, animations and smooth
  scroll globally.

## 5. Navigation

```
Archive (/) ──► an issue ─┬─ Digest       ◄── the Discord notification's link lands here
                          └─ All articles (raw) ─┬─ List (default)
                                                 └─ Triage (appears only when authorized)
Settings (/settings): on the site bar, appears only when authorized
```

- Every page has the site bar at the top; digest and raw also have the issue bar. Navigation does not go
  in the footer.
- The archive's headline card and every row have two entries, `Digest` and `All articles`.
- There is no separate triage page. Articles are judged in raw's triage view; the deck was retired on
  2026-09-19 (`triage-raw-list-merge`).
- There is no previous or next issue. The use case is reading each day's issue on that day.
- A back link must point to a path that exists. `/triage` is a 404 in production.

## 6. Page layouts

### archive

Below the page head (label, display, small description), the latest issue is the headline card, and the
remaining issues are split into panels by year and month. A panel's head row has the year and month (data)
on the left and the issue count on the right. Each issue is one row: the date, the period (label), the
article count (small, only for issues with a record), and two small secondary buttons; at phone width the
two buttons wrap to the next line. Past rows show no topics: a topic title is a sentence rather than a
tag, and of the 91 issues measured on 2026-09-19 only 20 had a topic record.

- Every issue is listed, never truncated, paginated or collapsed, because Pages' recovery rebuilds from
  every issue the index lists.
- The period is printed as its label text, with no colour coding, and the layout does not assume two
  issues a day. A day's issues follow the order in which the schedule fires; rows after the first one
  show their date in `--text-faint`, a signal independent of the labels. On a row with no article count,
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
  `.item-title`: `h3` adds `.lg` for 22px, and `h4` uses 20px. Each row of The Wire is only a number and a
  link, with no heading element.
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

### raw

The page head's label gives the article count and the source count, and below it is the segmented control
`List` / `Triage`, defaulting to `List`. `Triage` reuses the `/api/vote` probe; when not authorized the
whole segmented control is hidden and the page shows only the list. The two views share one set of data,
and switching does not reload.

- **list view:** one panel per source; each row is, in order, the state text, the score (data), the title
  link and the small vote buttons. At phone width the title wraps to the next line.
- **triage view:** one card at a time, whose face is the source (label) and the title (title role, 30px).
  The card is square, on `--surface`, with a 1px `--border-strong` border and no shadow; tilting towards
  up turns the border `--accent`, tilting towards down turns it `--warn`. Swiping left is down, swiping
  right is up, and a tap opens the original in a new tab. Below it, a danger `Down` and a primary `Up`
  button sit side by side, both 56px high, for desktops without touch; above it, a label shows
  `N remaining`. Cards take only articles whose state is still pending and that have not been voted on
  yet; to overturn an article the pipeline has already judged, vote in the list view. With no cards left,
  only `0 remaining` remains.
- A voted article shows its state in both views, and votes go through the existing promote Worker, with no
  new backend.
- The drag and fly-out are the only transform motion on the site, and are cancelled under reduced motion.

### settings

`/settings` is a single page that switches categories by hash. It is not split into several paths, because
the Worker's `PROTECTED` only matches a path exactly equal to `/settings` (`workers/app/src/router.js:10`).

| hash | Category | Contents |
|---|---|---|
| `#model` | Model | LLM provider (a choice list, showing on the right whether the key is ready, with a last option `None — plain excerpts` meaning no LLM is called and only original excerpts are shown), model, a real call to verify before saving; embedding provider and model, the vote similarity switch, the number of votes compared against |
| `#digest` | Digest | The two publishing periods, the time zone, the number of featured blocks, the article limit per issue, the output language, the style prompt (multi-line input), the font size (a select: Smaller 0.875 / Default 1 / Larger 1.125; shows `Not set` when unset) |
| `#pipeline` | Pipeline | The hours to look back, the article limit per pass, the two score thresholds for featured and summary, how many characters each of the three steps reads |
| `#notifications` | Notifications | Discord webhook, with a stored value shown masked; a danger `Turn off` uses the destructive confirm, and after it the field is cleared and `Notifications are off.` is shown |
| `#sources` | Sources | A type filter (All / RSS / Newsletter) and `Add source`; table columns are name, type, tier, feed or sender, and tags; clicking a row expands it for editing in place; the form shows only the fields that type needs; `Retire` uses the destructive confirm, but refuses to retire the last source |

- The page width is 1240px. Above 720px, the left side is a 220px category list (label role, with a 2px
  `--accent` left line when selected); below it, the list becomes a horizontally scrollable tab bar at the
  top.
- Each deployment has only one settings source, so the page does not show where a value comes from. Each
  category opens with a heading and a one-sentence small description.
- When a required value is missing, its field is left empty and marked as failed validation, the top of
  the category lists the missing fields in one error notice, and the list gets a `--warn` dot. All three
  marks disappear together after saving.
- Each category has exactly one primary `Save`, disabled when nothing has changed; a category with unsaved
  changes gets an `--accent` dot in the list.
- The prototype governs the fields' names, help text and `More` text.

## 7. Where styles live

- The digest is standalone HTML deployed to Pages and cannot link an external stylesheet, so the token
  and component styles live in `templates/_*.css.j2` partials: `_tokens.css.j2` holds §2 and
  `_components.css.j2` holds §4.
- The site bar and issue bar markup lives in `_site_bar.html.j2` and `_issue_bar.html.j2`.
- `static/style.css` is a copy of these partials, and settings gets the same styles from it.
- `tests/test_digest_css_partials.py` must compare both the token and the component rules, or the two
  sides will diverge again.

## 8. Current gaps and landing order

Of the gaps surveyed on 2026-09-17, steps 1 to 3 landed on 2026-09-18 and steps 4, 6 and 7 landed on
2026-09-19. Step 1 added the §2 tokens, the §3 type, focus and reduced motion, removed two hardcoded colours,
and added the component CSS comparison test. Step 2 put the site bar and issue bar on archive, digest and
raw. Step 3 rebuilt settings to §6. Step 4 rebuilt raw's list to §6 and added the triage view that appears
only after signing in, and the deck was deleted the same day (`triage-raw-list-merge`). Step 6 turned the
archive into a headline card plus year-and-month panels, with a day's later rows set apart by a dimmed
date. Step 7 rebuilt the digest body to §6: each section's name is printed once by its tag, heading levels
follow the structure, `.meta` is folded into one base rule, the two body grids share one fluid column rule,
720px is the only breakpoint left, and all body spacing moved onto the scale. Step 5 below handles the
remaining gaps.

The landing order, where each step can ship alone. The tickets in parentheses are in the Obsidian vault's
`pm/cyris/tasks/`:

1. §2 tokens, §3 type, focus and reduced motion, removing two hardcoded colours, and extending the CSS comparison test to components (`ui-spec-tokens-and-type`)
2. The site bar and issue bar on archive, digest and raw; the two entries added to archive rows; footer navigation removed (`ui-site-bar-and-issue-bar`)
3. Settings rebuilt to §6 (`settings-page-layout`)
4. The triage view added to raw (`raw-page-triage-view`); the deck deleted (`triage-raw-list-merge`)
5. Each deployment has only one settings source; settings lists every runtime setting and marks missing values, with no origin pill (`settings-value-origin-per-key`, repurposed for this)
6. The archive turned into a headline card plus year-and-month sections (`digest-index-archive-layout`)
7. The digest body's heading levels, `.meta` and the width dead zone, with body spacing moved onto the scale in this step (`digest-issue-page-layout`)

## 9. Checklist before changing the UI

- [ ] Colours, spacing, radii and durations all come from §2 tokens, or are exceptions §2 lists
- [ ] Every `font-size` is multiplied by `--type-scale`, and its role matches §3
- [ ] Controls are rounded, containers are square
- [ ] The components used already exist in §4; a new component was written into §4 first
- [ ] Interactive elements have `:focus-visible`, and no transition is written as `all`
- [ ] At 400px width the page does not scroll sideways, and tables scroll in their own container
- [ ] Navigation is in the site bar, and back links point to paths that exist
- [ ] `docs/design/prototype.html` has been updated to match
