---
status: accepted
accepted: 2026-09-17
updated: 2026-10-20
---

# cyris UI design spec

Every change that touches a reader-facing page follows this spec: `src/cyris/adapters/output/templates/`
(archive, digest, raw), `src/cyris/entrypoints/templates/` (settings, labels, onboarding),
`src/cyris/entrypoints/static/` (the settings, labels and onboarding scripts and `style.css`), and any
page added later.

- **This document is the spec.** `docs/design/prototype.html` is its reference implementation; open it
  directly in a browser. `#system` is the overview of tokens and components, and `#archive`, `#digest`,
  `#raw` (with the triage view), `#settings/model` and `#onboarding` are the pages. When the two disagree, this document
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
   an English control inside it (a vote group, a sources fold) carries `lang="en"`. An article's own
   title inside such a block, which is a Top story or Features card title and each title under a
   grouped Top story, is the source's text and carries `lang=""`; a group's heading, the summaries,
   the news paragraphs and the Wire's titles, which the model writes in the output language, keep
   its tag. The archive
   headline card's title follows its lead, so a group's heading keeps the tag and a single article's
   title carries `lang=""`. Text passed through
   untranslated, which is Following, the radar rows, Tracking and every raw row, carries `lang=""`, HTML's unknown
   language: no reliable tag for it exists. So does a Top story, Features card or Wire row that holds the
   article's own title and excerpt because no usable LLM wrote it or the model wrote no summary for
   it; the step that writes that fallback marks the item, so the page and the mail tag those items by
   what wrote them, not by their section. A value that is not a BCP 47 tag, such as a plain language
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
  --measure-wide: 760px;
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
  (`digest.type_scale`, ADR-0013) changes only the multiplier, never this table.
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
| **site bar** | Full width, sticky, height `--bar-h`, 1px `--border` at the bottom. On the left, the brand square (12px `--accent`, pulse animation) plus `CYRIS`; on the right, links in the label role, `Archive` · `Settings`, spaced `--s-5`. The current page is `--text` with a 1px `--accent` underline. `Settings` reuses the `/api/vote` probe and is hidden when not authorized. `Sign in` shows in its place only on an Access redirect, linking to `/login?next=` plus this page, so signing in returns here; every other probe outcome hides it, the app Worker's own 403 and a failed probe included. On /settings the brand link and `Archive` show only once the probe answers authorized, because a local `cyris triage-ui` serves no archive. At phone width the word `CYRIS` is hidden |
| **interest graph** | Only on the interest page (§6 *interest*). Each tag is a circle that always shows its vote polarity: its fill is `--accent` for net up and `--text-dim` for net down, at half strength mixed with `--text-faint` when no hold is on it. Brightness, the tag's largest share of suppression time left, adds the other half on top, so a lit node is suppressed now and a dimmer one has less time left. A tied tag is a hollow ring in `--text-dim`. Its label is in `--text`. Edges between tags that co-occur are `--border-strong` hairlines. The page's table gives the same counts, polarity and days left in words, and is the text equivalent of the map |
| **issue bar** | Shared by digest and raw, below the site bar, on `--bg-elev`. On the left, the date (data) and the period (label); on the right, the segmented control `Digest` / `All articles`. The two are two views of the same issue |
| **segmented control** | 1px `--border-strong` outline, `--r-control`, 1px dividers between cells; each cell is 44px high, `--s-4` left and right, label role, `--text-dim`. Selected: `--surface-2` background, `--accent` text, 2px `--accent` underline. Links use `aria-current="page"`, toggle buttons use `aria-pressed` |
| **button** | Height 44px, `--s-5` left and right, label role, `--r-control`. **primary**: `--accent` background, `--bg` text. **secondary**: `--surface-2` background, `--border-strong` border, `--text-dim` text, text turns `--text` on hover. **danger**: transparent background, `--warn` text and border, `--warn-tint` background on hover. The small size is 34px high, `--s-2` left and right, letter spacing 0.06em, and still takes taps over 44 × 44 (**tap area**). Pressed (`:active`) lowers opacity to .7 on top of whatever hover state the button already shows, and changes nothing else; .7 stays clear of disabled's .4, so a pressed button never reads as disabled. Disabled is always opacity .4, and a disabled button does not change on hover or press |
| **tap area** | Every tap target takes taps over at least 44 × 44px, at every width, and a tap lands on exactly one target. A target drawn smaller keeps its drawn size and gets the rest from a transparent `::after`, `position: absolute`, `inset: min(0px, calc((100% - 44px) / 2))`, centred on it: the small button, the site bar's links, and the brand link, which is the 12px mark alone at phone width. Hit areas never cover each other or another target's drawn box, with one exception: the archive's `Digest` link takes taps over its whole headline card or row, and the `All articles` button sits on top of that area because it is positioned and follows `Digest` in the markup. The ↑ and ↓ votes keep their `--s-1` gap and split it at its midpoint, each taking the rest of its 44px outward, and the vote group sits `--s-2` further from what comes before it, so that outward part clears its neighbour: the arrow glyphs fall back to whatever monospace the reader has, so a gap wide enough for two centred areas would hold only for one font's width. The digest's ↗ original link is a 44px-wide box with its glyph centred, because a centred area would reach 18px over the source names and votes on either side. A settings fold's summary takes `--s-1` above it, half the field gap so the control above keeps its taps, and the rest below. A settings tab is itself at least 44px high, because its tab bar scrolls at phone width and clips anything drawn outside it. A cluster's `N sources` fold is exempt: a 44px area would cover the source links it unfolds. Text links inside a title or a line of text are exempt, as WCAG 2.5.8 exempts inline targets. The page probes' `tap-*` checks measure every target here at 375px, and the digest probe's `archive-*` checks test the `Digest` exception at 375 and 1440px |
| **input / select** | Height 48px, `--s-4` left and right, `--bg-elev` background, 1px `--border-strong` border, `--r-control`, Geist 16px. On focus the border turns `--accent`, even on a field that failed validation; on failed validation the border turns `--warn`, with a notice below. Disabled is opacity .4, as a disabled button is |
| **multi-line input** | `<textarea class="input">`, reusing the input's background, border, radius, font size, and its focus and failed-validation rules; its height comes from `rows`, with `--s-3` padding top and bottom, and it resizes vertically only |
| **field** | From top to bottom: the field name in the label role, the control, and help in the small role, spaced `--s-2`. Help is always in view and is one or two short sentences: it keeps the facts a reader decides by and leaves the background to the docs, so it is never folded away. A field whose setting does nothing until another holds a value has a **dependency**: while it is unmet, the control is disabled, and one line in the small role below the help says what it waits for, such as `Needs vote similarity on, under Model.` That line is the control's description (`aria-describedby`, beside any refusal notice), since a disabled control leaves the tab order and a screen reader would otherwise never hear why. The page reads the dependency from the values it holds, the other field's unsaved value included, and the line goes and the control returns the moment that field changes, with no reload. A dependency of one option, such as Clef's token, disables that option alone and leaves the rest of the select in reach. A required setting is never locked out of being stored: a disabled control with no stored value shows the value a run takes while the dependency is unmet, such as `Off`, and its category's `Save` stores it, as a detected timezone is stored (§6 *settings*). It replaced help folded under `More` on 2026-10-20: readers took `More` for finer settings and found an explanation |
| **settings fold** | `<details class="more">` that holds finer settings: what it unfolds has at least one control, and its summary names them, such as `Advanced: retry limits`, never a bare `More`. Help is never folded (**field**). The one fold without a control is a list that only reports, Areas' `Closed`. `tests/test_ui_spec.py` scans the settings and onboarding templates for any other |
| **category list dots** | Two 6px dots on the right of each item in the settings category list: `--accent` means the category has unsaved changes, `--warn` means the category is missing a required value. Both can show at once, with the missing-value dot on the left |
| **choice list** | Several options for a single choice (for example provider): `--bg-elev` background, 1px `--border` border, `--r-control`; each row has `--s-3` top and bottom and `--s-4` left and right, rows divided by a bottom line, `--surface` on hover. The right side shows the state in the label role; a state too long to sit beside the name moves to its own line below it, still on the right, and is never cut off. An unselectable row sets its radio and name to opacity .5, never the state text that says why. On failed validation the border turns `--warn`, with a notice below, as an input's does |
| **checklist** | Several options that can be chosen together: a `fieldset` with its legend in the label role, `--bg-elev` background, 1px `--border` border and `--r-control`, and one row per option. Each row is laid out like a choice list's row, with a checkbox in `--accent` and the name in the name role; rows are divided by a bottom line and `--surface` on hover. Every option starts checked, except in onboarding's feed lists, where the reader picks from none (§6 onboarding). The fieldset takes no browser border or padding. An unselectable row sets its checkbox and name to opacity .5 |
| **toggle tile** | Several options that can be chosen together, each a short name the reader picks at a glance rather than reads down a list: onboarding's topics. One press chooses a tile and the next unchooses it. A `role="group"` named by a line in the label role holds one `<button type="button" class="tile">` per option, carrying `aria-pressed`, in a grid of `repeat(auto-fill, minmax(min(100%, 160px), 1fr))` with `--s-2` gaps. A tile is at least `--s-16` high, with `--s-3` top and bottom, `--s-4` on the left and `--s-8` on the right; `--bg-elev` background, 1px `--border-strong` border, `--r-control`, its text left-aligned: the name in the title role at 18px, then a count or state in the label role. A `--s-3` square mark sits `--s-3` in from its top right corner, a 1px `--border-strong` outline while the tile is not pressed and filled `--accent` while it is, so the state never rests on colour alone. Pressed: `--accent` border, `--accent-tint` background, the label in `--accent`. Hover turns an unpressed tile `--surface`; pressing lowers it to opacity .7, as a button's `:active` does. It is a native button, so Tab reaches it and Enter or Space presses it, and it is its own tap target, over 44 × 44 at every width. It replaced a checklist for topics on 2026-10-20: two checklists one above the other read as one long list with no order between them |
| **headline card** | Only for the archive's latest issue. `--surface` background, 1px `--border-strong` border, square corners, `--s-6` padding. From top to bottom: an `--accent` `Latest` label with the date (data) and the period (label); the title of the issue's first story, which is its group heading when that story is a group (title role); the article count (data); the titles of the two topics with the most members (small, joined by ` · `); and two small secondary buttons, `Digest` and `All articles`. A tap anywhere on the card outside `All articles` opens the digest, because `Digest` takes taps over the whole card (**tap area**); hovering or pressing the card hovers or presses the `Digest` button. A field with no data is left out, with no placeholder text |
| **panel** | `--bg-elev` background, 1px `--border` border, square corners. An optional head row: `--surface` background, a bottom line, `--s-3` top and bottom and `--s-5` left and right, the title on the left and a count or action on the right. Adjacent panels are spaced `--s-5` |
| **list row / table row** | The class name is `.list-row` (`.row` is taken by the digest stats card). `--s-3` top and bottom, `--s-5` left and right, rows divided by a bottom line, `--surface` on hover. Table headers use the label role on `--surface`. A table sits in its own `overflow-x: auto` container |
| **pill** | Geist Mono 12px, `2px 10px`, 1px `--border-strong` border, `--surface-2` background, `--r-tag`. The score variant has `--accent` text, an `--accent-dim` border and an `--accent-tint` background, and only a score takes it: a source's lane is a plain pill, because no lane is the positive one (§1) |
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
  on raw and on labels, and to rotate the feature flow's static chevrons.
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
     └─ Onboarding (/onboarding): where /settings sends a deployment with no source
Sign in (/login): on the site bar in Settings' place, and only when Access redirects the probe; every other probe outcome hides it, the Worker's own 403 and a failed probe included
Labels (/labels): reached by its URL alone, behind the same sign-in as Settings; not on the site bar
```

- Every page has the site bar at the top; digest and raw also have the issue bar. Navigation does not go
  in the footer; the footer's project link (§4) leaves the site rather than moving within it.
- A tap anywhere on the archive's headline card or on a row opens that issue's `Digest`, except on its
  `All articles` button, which opens the raw page and shows only when the issue has one.
- There is no separate triage page. Articles are judged in raw's triage view; the deck was retired on
  2026-09-19 (§8 step 4). `/labels` is not one: it answers a sample drawn to measure the filter, and
  is reached by the URL `docs/operations.md` gives while a sample is being labeled.
- The interest map (`/interest`) is a container page behind sign-in whatever `CYRIS_PRIVATE_ARCHIVE` is, as `/settings` is, with no copy on `pages.dev`. The archive links it in a line under its page head that shows only to a signed-in reader (`.gated`, as the probe reveals Settings).
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
- The headline card renders from the run's own `DigestContent`, and a row's article count from
  `usage_log` (`adapters/store/archive_meta.py`). Why the list is never cut is in
  `docs/architecture.md` §6, *Recovery reads the live site*.

### digest

At the top are the site bar and the issue bar. The masthead has the issue title (`h1`) on the left and the
stats card on the right; the stats column is `min(37vw, 320px)` wide and narrows with the page, so the
wider fallback font before the webfont loads does not run into it. Below 720px it becomes a single
`minmax(0, 1fr)` column. The masthead is always `overflow-x: clip`, so a title widened by the fallback font
is clipped on a phone instead of scrolling the whole page sideways. At 360px the fallback-font title fits
whole at font size `Default` and `Smaller`; only `Larger` clips its right end, until the webfont loads.
That is accepted rather than shrinking the issue title role at phone width. The digest probe's
`title-whole-360` check holds the `Default` case.

The body is the Top story, then Tracking, then one section per active area in /settings order, cards above
rows and rows above news paragraphs (`layout_by_area` places every entry). With no areas, one unnamed section holds
them all. Unclaimed entries close the body as "Other". Empty sections are left out, and Discord follows the
same order, titling the unnamed section "Digest". Each section is one `<section class="section">`, and its name is printed once, only by the opening
section marker (§4), with no number and no repeated large section title. The section marker is an `h2`; Top
story is the exception and uses a `div`, because the lead card's title is itself that section's `h2`.

- **Heading levels follow the structure.** `h1` is the issue title; `h2` is the section markers and the
  lead title; `h3` is the titles of features, news groups and topic blocks; `h4` is the articles inside a
  topic block. `h3` and `h4` are both the title role (§3), with the class
  `.item-title`: `h3` adds `.lg` for 19px, and `h4` uses 18px. A row has no heading element.
- **News is paragraphs that cite their articles.** An issue's news is one `.news-paragraph` per
  paragraph, after the area's rows: a left rule like a news group's, the paragraph as a `p.summary` in
  the body role (§3), then one `.meta` row per cited unit: a link per article in the unit, each
  labelled with its source (a unit that carries reference links lists those by host instead),
  folded into `<details>` with the summary text `N sources` past two as a news group's are, and the
  unit's vote buttons last. One vote is cast for every article in the unit, as a news group's is.
  An article with no web link is its label alone. The rows sit in one `.citations` column `--s-3`
  apart, so one row's 44px tap areas do not cover the next row's buttons. A paragraph that holds a unit's own summary or
  title because the model did not write it carries `lang=""` (§1). An issue stored before the
  paragraphs keeps its news groups and Wire rows as described below.
- **A row is its title and the filter's one sentence.** Each row is a number, the title link and,
  below the title, the one-sentence summary the filter wrote for that article, in the small role (§3)
  under a title in `--text`; the source fold and the votes follow, the votes last. The summary takes
  `overflow-wrap: anywhere`: an unbroken run such as a URL breaks inside it rather than widen a row
  whose title column is narrow on a phone. A row with no summary is only the number and the title link. When the filter ran without an LLM, by choice under provider
  `none` or because the configured one could not be used, the sentence is the article's plain excerpt,
  as a Features card the model wrote no summary for shows its excerpt, and the row carries `lang=""`
  (§1).
- **Only the Top story may be a group.** When two or more articles on one topic score at least
  `routing.score_threshold`, the lead card takes the group's heading as its title and prints the group's
  summary once. Each article of the group follows as an `.article-item` whose title is `h3 .lg`, one level
  below the card title, with its own title link and meta row, so each keeps its own score and vote: they
  are different articles, unlike a news group's reports of one event. A member scoring below the
  threshold stays in the group, because the group's summary covers it. With no such group, the lead is
  the highest-scoring single article.
- **A Features card is one article.** It prints that article's own summary, never a group's; an article
  the model wrote no summary for shows its excerpt, and the issue counts as degraded. Cards follow score,
  highest first, and `digest.max_featured` is how many there are at most; an article past it is left out
  of the issue, as one past the issue's article limit is, and stays on `All articles`. A card's summary is
  two or three sentences; the Top story's group summary is three to five.
- **The issue's article limit keeps a group whole.** The limit is applied after the Top story and Features are laid
  out: it takes the Top story first, whole, then Features by score. A group that does not fit is replaced
  in its slot by its best article.
- **Tracking lists each tracked topic that has a hit this issue.** A topic is a block like Following's:
  its name as the `h3`, then each hit as an `.article-item` whose `h4` is the article's own title link,
  nearest first, with a meta row of its source, the original link and the votes, and no summary. A
  topic with no hit is left out, and so is the whole section when none has one. Hits sit outside the
  issue's article limit and its article count, because a hit is a view of the run's candidates, not a
  verdict on them. It follows the Top story because a topic is something the reader asked to see.
- **Every item has a meta row below it.** `.meta` is flex, wrapping, spaced `--s-3`, and its font is the
  label role; it holds the source and the original link in that order (a feature has a score pill in
  front), and the small vote buttons are always last. Only the lead card's meta row adds `.ruled`, which
  puts a dashed line above it. A news group with more than two sources folds them into `<details>`, with
  the summary text `N sources`.
- **Feature cards share one fluid column rule:**
  `repeat(auto-fit, minmax(min(100%, 320px), 1fr))`, with no column count set at any width. The Features
  grid lines are drawn on each card's right and bottom edges, so when the last row is not full, the empty
  cells are the page background, not a solid block of border colour.
- The only breakpoint is 720px, all body spacing is on the §2 scale, and the only glow is the brand square.
- **Prose stops at `--measure-wide`.** Every summary and the empty or degraded
  note is at most `--measure-wide` wide, so a line stays readable when the container is 1240px; a card
  narrower than that is unaffected. Headings and meta rows take the full width. It is wider than
  `--measure` because at 640px a full-width card's summary ended halfway across and read as a broken
  line; 760px is about 42 Chinese characters at 18px. `--measure` stays the mail's and `/settings`'
  width.
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
  Enter on it opens the original the same way. While the card or a deck button has focus, ← votes down
  and → votes up as the swipes do, one vote per press; a held key votes once, and a modified arrow is
  left to the browser. Focus stays where it was, or moves to the `Triage` switch once no card is left.
  Below it, a danger `Down` and a primary `Up` button sit side by side, both 56px high, for desktops
  without touch; above it, a label shows `N remaining`. Cards take only articles whose state is still
  pending and that have not been voted on yet; to overturn an article the pipeline has already judged,
  vote in the list view. With no cards left, only `0 remaining` remains.
- A voted article shows its state in both views: the list's state text turns `accepted` for up and
  `rejected` for down. On a later visit, a vote this browser remembers changes only a row rendered
  `pending`, because each run applies the votes cast before it. Votes go through the existing promote Worker, with no new backend.
- The drag and fly-out are the only transform motion on the site, besides labels' card, and are
  cancelled under reduced motion.

### labels

`/labels` answers the blind-label sample `cyris labels draw` keeps in D1, one item at a time. The same
container serves it as serves settings, behind the same sign-in, and it reuses raw's triage card with
these differences, which keep it blind:

- The card's face is the source (label), the title (title role, 26px) and the article's excerpt (small),
  the last two with `lang=""`. Nothing the pipeline decided shows: no state text, score, lane, tags or
  rejection reason, and the server deals the sample in its own order, which is shuffled across verdicts.
- Below the card, a danger `Down`, a secondary `Skip` and a primary `Up` sit side by side, all 56px high.
  Up and down are written to the article as a digest vote; a skip is kept on the sample alone.
- Above it, the page head's label shows `N of M answered · K remaining`. With no item left the card and
  its buttons go, and one small sentence says which command to run next.
- The swipes, the tap and Enter that open the article, the lean colours, the answer-first fly-out and
  reduced motion are raw's triage view's; raw's arrow keys do not answer here. A skip does not fly:
  the next card replaces it. An answer that does not land leaves the card in place with an error
  notice below the buttons saying why.
- The page is 600px wide, raw's deck width, with settings' gutters. The site bar is settings', with no
  item marked current.

### settings

`/settings` is a single page that switches categories by hash. It is not split into several paths, because
the Worker's `PROTECTED` only matches a path exactly equal to `/settings` (`workers/app/src/router.js:10`).

| hash | Category | Contents |
|---|---|---|
| `#model` | Model | LLM provider (a choice list, showing on the right whether the key is ready, with a last option `None — plain excerpts` meaning no LLM is called and only original excerpts are shown), model, a real call to verify before saving — a small Test button under each pair's own fields makes the same call and stores nothing; an `openai_compatible` choice carries a Base URL field of its own (`llm_provider.base_url`), with `OPENAI_COMPATIBLE_API_KEY` optional because a LAN or localhost endpoint may answer with none — the embedder's pair likewise, with a key of its own (`OPENAI_COMPATIBLE_EMBEDDING_API_KEY`) — embedding provider and model likewise (`vote_similarity.base_url`), plus the vote similarity switch, the number of votes compared against |
| `#digest` | Digest | The two publishing periods, the time zone (a select, below), the number of Features cards, the news per paragraph (a number from 1 to `UNITS_PER_BATCH`, the units one paragraph call is given), the article limit per issue, which counts the read lane and any news outside every named area, never a named area's news or the follow lane, the output language (a select, below), the style prompt (multi-line input), the font size (a select: Smaller 0.875 / Default 1 / Larger 1.125; shows `Not set` when unset), the vote order (a select: Off / On), which depends on vote similarity being on (§4 *field*), and who judges it (a select: Title similarity / Clef), whose Clef option depends on a Workers AI token, which `GET /api/settings` reports as `clef_ready` |
| `#theme` | Theme | The page palette (a select: Default — the dark palette of §2, Light, One Dark, Catppuccin, Nord, Tokyo Night, GitHub Dark). Picking one re-skins the settings page itself at once — the preview is the point of the choose — and Save stores the same id for every page that serves one, where it is injected on read (§5). Colour keeps the two meanings of §5 in every palette: the accent what the reader voted up, the warn what they voted down, everything else neutral |
| `#pipeline` | Pipeline | The hours to look back, the article limit per pass, the two score thresholds, one for grouping the Top story and one for summarizing, how many characters each of the three steps reads, the reasoning effort (a select: Default / Low / Medium / High) |
| `#notifications` | Notifications | Discord webhook, with a stored value shown masked; a danger `Turn off` uses the destructive confirm, and after it the field is cleared and `Notifications are off.` is shown; then Email to and Email from, shown in full, where an empty Email to means no mail and Save sends a test message before storing the pair |
| `#sources` | Sources | A type filter (All / RSS / Newsletter), `Add source`, and `Import OPML…` — an OPML file's feeds, its folder structure flattened to one name and url each, deduplicated, imported to the skim lane one row per feed with a progress count; table columns are name, type, lane, feed or sender, and tags; an RSS source that `cyris doctor` would warn about (the feed health thresholds in `docs/architecture.md` §5) shows one line under its name, in the small role coloured `--warn` because a failing feed is an error (§1), saying why, such as `3 failures in a row · HTTP 503` or `no article in 30 days`, and a healthy one shows nothing; clicking a row, or pressing Enter or Space on it, expands it for editing in place, with focus on the Name field, and `Cancel` puts focus back on the row, or on `Add source` for a new one; a save or retire that fails puts focus back on its button, and a retire that lands puts it on `Add source`; the form shows only the fields that type needs; its `Area` select holds `None` and the active areas, a stored area that is not active or whose list has not loaded shows as `Area N (not active)` or `Area N (list not loaded)` so that a save keeps it, and the select is disabled until the area list loads; `Suggest` selects the area the model suggests, marked by a `Suggested` tag beside the select until the reader changes or saves, or says `No suggestion` beside it when there is none; while no area is active `Suggest` is disabled with the title `Confirm an area first`, and while it asks, `Suggest` reads `Suggesting…` and the select is disabled; `Retire` uses the destructive confirm, and retires the last source like any other: a table with none is a deployment waiting for onboarding |
| `#areas` | Areas | Five parts from one read, in this order. `Areas` lists the active areas in the order the next run offers them to the model, each row with a `Position` select (1 to N) that moves the row there at once, on the page, with no request; `Save order` under the table sends the whole order and stays disabled until it differs from the stored one. With no active area the table says `No areas yet. Use "Propose areas from my sources" under Sources, or wait for the next run to suggest some.` A row opens an in-place editor on a click, Enter or Space; an active area's editor holds `Save name` and `Cancel`. `Proposed` shows each proposal with `N articles / M issues`, a `Meets birth threshold` pill when it reaches the threshold, and a small-role line `N sources picked` while unsaved selects in the Sources table point at it; with none it says `No proposals yet.` A proposal's editor holds `Activate` (the only primary button), `Save name` and `Cancel` in one group, and `Dismiss` and a `Merge into` field with `Merge` beside it in a second group; `Dismiss` and `Merge` use the destructive confirm, and `Merge into` lists the active areas or says `No active area to merge into`. `Sources` lists every source with an `Area` select holding `None`, the active areas in order, then an optgroup `Proposed`, and a stored area that is neither stays selected as `Area N (not active)` so an untouched row keeps its value; `Propose areas from my sources` sits in this part's head and reads `Proposing…` while the model answers. Its answer adds new proposals to `Proposed`, pre-fills the select of each source that has no area with the suggestion, marked by the `Suggested` tag of the source editor until the reader changes it, and gives a source that already has another area one small-role line `Suggests <area>` under its name, leaving its value alone. Changing a select is how a suggestion is turned down; nothing remembers it. `Save assignments` sends only the changed sources, and a proposal that receives a source becomes active in the same save, appended last; the notice says how many sources were assigned and which areas became active. With no source it says `No sources yet.` with a link to Sources. `Birth threshold` holds the two number inputs `Articles` and `Issues` with `Save`, which only marks proposals; `Birth threshold not set` shows while none is saved. A folded `Closed` list holds the dismissed areas and the merged ones, each with its target. A row's `Cancel` and an activate put focus back on the row; a save keeps the editor open on its row with the result notice, and focus returns to the row. A `Confirm dismiss` or `Confirm merge` press that lands closes the editor and moves the row to `Closed`, so focus goes to the `Closed` summary; a 404 (`This area no longer exists.`) closes the editor, and focus goes to the row if the list still shows it, else to the `Closed` summary. A reload after any action keeps unsaved positions and selects. Below 720px a source row puts its name above a full-width select. A read that fails shows an error notice and an empty table; a deployment without an area table shows its read-only text and disables every control |
| `#tracking` | Tracking | `Add topic` and a table of tracked topics: name, description, threshold and embedding model; a row, its editor, `Cancel`, focus and the locked editor behave as a source's do; the editor holds the name (read-only once saved), the description (multi-line input), the threshold (a number from 0 to 1, with no default) and the embedding model, which a new topic fills with the model the next run embeds with; a topic set for another model shows one line under its name, in the small role coloured `--warn` because the run skips it, saying so; `Remove` uses the destructive confirm. No topic is a valid answer: the table says `No topics yet.` and the category never takes the `--warn` dot |

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
  marks disappear together after saving. A deployment with no source publishes nothing until one is
  added, so Sources gets the `--warn` dot while its table says so, until a source is saved.
- Until the stored values arrive, each category's description is followed by `Loading settings…` in the
  small role, and the Sources table holds one `Loading sources…` row. Each goes when its answer arrives,
  or the error notice that replaces it.
- Each category has exactly one primary `Save`, disabled when nothing has changed; a category with unsaved
  changes gets an `--accent` dot in the list. Areas is the exception: its order, its source assignments and
  its birth threshold are three tables saved apart, so each has its own `Save` (`Save order`, `Save
  assignments`, `Save`) under its table, with its notice beside it, and one dot covers all three. A missing value whose answer may be empty, such as Style or
  Email to, keeps `Save` enabled without a dot, so a first boot can store it as it stands.
- A save the server refuses for one field marks that field as failed validation, with the reason in a
  notice below it, named by the field's label; the notice beside `Save` says it too. Editing the field
  clears its mark.
- While a save is out, its `Save` stays disabled and the notice beside it says what is happening, such as
  `Saving…` or `Checking with the provider…`, until the result replaces it. A source editor is locked
  while its save is out, and its fields show the disabled opacity.
- Every notice that reports a save, a retire or a load is a polite live region (`role="status"`), so a
  screen reader reads its result out.
- The prototype governs the fields' names, help text and dependency lines.
- **Output language and Timezone are selects,** never free text. Output language offers each
  language `service_layer/languages.json` lists, in its order, valued by its BCP 47 tag and shown by
  the file's name for it, such as `繁體中文 (Traditional Chinese)`. Timezone offers every zone the
  browser lists (`Intl.supportedValuesOf("timeZone")`), plus `UTC`, which some browsers leave out,
  sorted; a native select already jumps to an option as its first letters are typed, so no custom
  dropdown replaces it. A stored value neither list holds is still a value the run uses — a tag the
  model is told as written, or a zone the server knows — so it stays an option of its own, shown as
  the value itself, and a save that leaves the field alone stores it back, as the `Area` select keeps
  a stored area that is not active. A timezone with no stored value opens on the browser's own zone
  (`Intl.DateTimeFormat().resolvedOptions().timeZone`), with one small line below its help,
  `Detected from this browser, and not saved yet`, until it is changed or saved: it differs from what
  is stored, so the category's `Save` is live and stores it as shown, and the missing-value marks
  stay until it does. Only with no browser zone either does the field show `Not set`.

### onboarding

`/onboarding` is the guided setup of a new deployment. An empty `sources` table is the
waiting-for-onboarding state (`docs/architecture.md` §5), so /settings sends such a deployment here.
A deployment that already has a source can still open it, because the sources step writes one and a
reload on a later step must keep the reader in the wizard. A `backend = "json"` deployment, whose
sources live in a file the app never writes, has no wizard at all: this page sends it back to
/settings. The same container serves it as serves settings, behind the same sign-in.

- **Six steps, in order:** language and timezone, publish hours, sources, votes, notifications, first
  issue. The step a hash opens is the step shown, as settings' categories are, so a reload keeps the
  place: the Worker's `PROTECTED` matches `/onboarding` exactly, and a hash never leaves the browser.
- The page is `--measure` wide, one column, with settings' gutters. Below the page head (the label
  `Step N of 6`, the display title and a small description) the step list names every step in order,
  each with a 6px dot: `--accent` dot and `--accent` text for the step open now, a `--text-dim` dot
  for one already passed, and a 1px `--border-strong` ring for one still to come. The list is a
  progress mark rather than navigation — it takes no taps, and the three buttons below it are what
  move the wizard.
- Each step is a heading, a one-sentence description and the fields of the settings it writes, in the
  field component (§4), with the same names and help text as the settings category it shares them
  with. Output language and Timezone are the selects settings has (§6 *settings*); a
  timezone opened on the browser's zone says `Detected from this browser, and not saved yet: Next
  saves it`, and `Skip` leaves the setting unset. A step writes through the route `/settings` saves those fields with, so the store
  cannot tell the two pages apart.
- **Back, Skip and Next** sit below the step in that order, two secondary buttons and one primary.
  `Next` saves what changed on this step and moves on; `Skip` moves on and saves nothing, so a
  skipped setting keeps the value it had. `Back` is left out on the first step. On the last step
  `Next` is gone: the step's own primary button takes its place, and a primary `Finish` link leads
  to `/settings?from=onboarding` — the mark that lets a deployment whose table is still empty
  through, rather than sending it back here. `Skip` there leaves through that same link and starts
  nothing. The site bar's `Settings` link carries the same mark on this page, so it leaves the
  wizard too.
- One notice sits beside the buttons (`role="status"`): what a save is doing while it is out, then
  its result, shown on the step the reader has moved to. A refusal keeps the reader on the step, says
  why in `--warn`, and marks the field it is about as failed validation, as settings does; an empty
  publish hour is refused on the page, because padded it would reach the server as `00:00`.
- **Sources** offers the project's feed list (`src/cyris/onboarding_feeds.json`), which the server
  sends with its topics (`GET /api/onboarding/feeds`), in two screens of the one step: topics, then
  the chosen topics' feeds. The step list, `Step 3 of 6` and the heading stay the same on both; the
  description under the heading says what the open screen asks for. The hash names the screen,
  `#sources` for topics and `#sources/feeds` for feeds, so a reload keeps it, as it keeps the step.
  A topic is what a feed is about, never its form.
  - **Topics** are toggle tiles (§4) in a group labelled `Topics`, in the list's order, none pressed
    at first: each is the topic's label over how many feeds it holds. Until one is pressed, one small
    line below them says to pick topics to see their feeds, and that `Next` with none picked goes
    straight to adding an own feed. `Next` is never disabled here: it opens the feeds screen and
    saves nothing; `Back` goes to the step before.
  - **Feeds** shows one checklist (§4) per chosen topic, legended with its label, in the topics'
    order; their rows start unchecked, and each is the entry's name, as the list writes it, and its
    tags on the right, in the list's order. A feed sits under the first of its own topics that is
    chosen, so a feed in two chosen topics shows once. A topic unchosen back on the topics screen
    takes its unchecked feeds with it; a checked one, added or not, that no chosen topic holds any
    more stays checked and shown, in a first checklist legended `Picked, outside the chosen topics`,
    and leaves it only when unchecked, so `Next` never sends a feed the reader cannot see. `Back`
    returns to the topics screen and keeps every check. Below the checklists, just above the
    buttons, one small line counts `N feeds picked`, a polite live region; past 12 it adds that the
    Votes step fetches every picked feed before it deals cards, which takes about a minute for 12
    and longer for more. It warns and blocks nothing; 12 is what the poll's wait holds to a minute
    (`docs/architecture.md` §5, *Onboarding poll wait*).
  - **Add your own feed** sits below the checklists on the feeds screen, for a feed the list lacks:
    a field (§4) labelled `Add your own feed`, its URL input and a secondary `Add` button side by
    side and wrapping one under the other when the row is too narrow, one small help line, and a
    notice of its own below them (`role="status"`), beside the button it reports on. The reader
    pastes a feed's address or a site's home page; `Add`, or Enter in the input, asks the server
    (`POST /api/onboarding/feed-check`), which follows a home page to the first RSS or Atom feed it
    links and names the feed by its own title. While it asks, `Add` is disabled and the notice,
    marked `aria-busy`, says `Checking <address>…`. A feed that passes joins a checklist legended
    `Your feeds` right below the field, checked, and counts in `N feeds picked`; the input empties
    and the notice says `Next` adds it with the others. A refusal says why in `--warn`, in the
    server's words (unreachable, the HTTP status, not a feed, a home page that links none, a feed
    with no article), marks the input as failed validation, and adds nothing. A feed the list
    already holds is not added twice: its row is checked instead, under its topic or under
    `Picked, outside the chosen topics`, and the notice says so. A name another listed or stored
    source has takes the feed's host after it, in parentheses. An own feed unchecked keeps its row.
    `Next` posts each checked own feed with the list's picks, one `POST /api/sources` each, its
    body `{name, url, tags: []}` with the lane left to a source's default, and its row takes the
    same `Added` and `Not added` states. Own feeds are not kept across a reload.
  - The chosen topics are kept for the tab (`sessionStorage`), so a reload opens the step on the
    same topics and feeds; a feed's own check is not kept, so a reload on the feeds screen with no
    topic kept shows only the own-feed field.
  - `Next` on the feeds screen writes each checked feed through `POST /api/sources`, one request per
    feed, with the entry less its `topics` as its body (topics are the list's grouping, not a
    source's field), so the stored source is the list's own, tags and lane included; the busy notice
    names the feed being added. An added row reads `Added` before its tags, and its checkbox and name
    take the unselectable opacity. When some feeds are not added, the reader stays on the feeds
    screen, the notice says how many were added and names each one that was not with the server's
    reason, and each such row reads `Not added` and is marked as failed validation; `Next` then
    sends only the rows not yet added. `Next` with nothing checked writes no source, and `Skip` on
    either screen leaves the whole step and writes none.
  - The topics and the list show only while the table is empty; once it has a source, the step is
    one screen, one line saying to add more later under Sources in Settings, and `Next` only moves
    on.
- **Votes** deals cards from the deployment's RSS feeds as soon as the step opens
  (`POST /api/onboarding/cards`), read through the run's own fetch, so a card's link is the URL the
  first issue reads. While they are fetched, a small line marked `aria-busy` says so and that it can
  take a minute or two, the rss Worker's poll of the feeds just picked. The cards sit in one panel (§4),
  one list row each: the source in the label role, the title in the title role as a link to the
  article (a feed URL that is not `http(s)` is shown unlinked), the excerpt in the small role when
  there is one, both with `lang=""`, and below them a segmented control (§4) of two toggle buttons,
  `Interested` and `Not interested`, with `aria-pressed`. A press marks the card; pressing the marked
  answer again leaves the card untouched, which counts for nothing. Each feed gives at most a fixed
  number of its older articles, alternating between feeds (`docs/architecture.md` §5). A feed the read
  found no article for is named in an error notice above the cards, which says it stays in the sources;
  a failed poll is said there too. With no feed at all, or no card to deal, one small line says so
  and that `Next` goes on. `Next` writes the marked cards, and only those, as votes
  (`POST /api/onboarding/votes`): each is stored, then accepted or rejected with `triaged_at`, as a
  `/labels` answer is. A recorded card shows `Recorded · Interested` or `Recorded · Not interested`
  in the label role in place of its buttons, and a later `Next` does not send it again. A card whose
  article left its feed after the deal shows `No longer in the feed · not recorded` there instead, and
  the notice counts it; the other votes are still recorded. `Skip`, and `Next` with no card marked,
  write nothing.
- **First issue** builds an issue now rather than at the next publish hour. While the server says
  which issue it would be (`GET /api/onboarding/first-issue`), a small line marked `aria-busy` says
  so, and `Back` and `Skip` show. Once that answer names a source, the step turns vote similarity on
  through `/api/settings/vote-similarity` with the embedder the deployment already stores, unless it
  is on already, whether or not a card was voted on: the votes on later issues count only with it
  on, and the reader is not asked, so no `Skip` before or on this step keeps it off. `Start` waits
  for that save, since the run reads its settings as it starts. A refused save leaves `Start` in
  reach: one small line below the plan says vote similarity is still off, why, and that it is
  turned on under Model in Settings, and the next visit or `Start` tries it again. Then one small line says what `Start the first issue`, a primary
  button in `Next`'s place, builds: the date and period of today's latest publish hour already
  begun, so no later issue that day replaces it, or, while both of today's hours are still to come,
  yesterday's evening, with the line saying it is dated yesterday and naming today's two hours and
  the timezone. `Start` posts the app Worker's own `POST /run?period=&date=`, the route any manual
  run takes. With nothing to start, the line says why and `Finish` takes `Skip`'s and `Start`'s
  place: there is no source, or an issue already holds that date and period. While the run is out,
  the notice is marked `aria-busy`, names the issue and the time so far, and says the reader can
  leave with `Finish`, the only button left; it reads the run's record again every few seconds. A
  run that published ends in a notice naming the issue's date and period, with two links, `Read it`
  to the issue and `archive` to the archive. Any other end is an error notice saying how the run
  ended and why, with the run's own error when it raised, and `Start` reads `Try again` beside
  `Finish`; a run with no record after the run container's 15-minute cap is reported the same way.
  A reload while the run is out goes on waiting for it rather than offering a second run.

### interest

The page head has the label `Interest`, the display title and one small sentence saying what the map shows.
Below it is the map and its table, in that order, on the archive's width. The small role under the map says what
the colour and brightness mean: 'Lit = suppressed now; dimmer = less time left; half colour = not suppressed.'
The data role gives `As of <generated_at>`, the instant the container drew the page. The small
role never says no suppression is active while one is: when holds are active but none lights a tag on the map
(its downvote has no tag, or its tags fell outside the map), it says 'Suppressions are active, but none is near a
tag on this map.' instead, and only with no active suppression at all does it say 'No suppression is active.'

- **Empty:** with no voted article that carries a tag, one sentence in the small role, 'No voted article carries a
  tag yet.', its command `cyris run` in the data role. No box, no accent and no SVG, as the archive's empty state (archive).

### email

The mail a run sends is its own document, not the digest page: mail clients run no script, and Gmail
drops CSS custom properties and `prefers-color-scheme`. `templates/email.html.j2` renders the §2 tokens in
by value, from `_tokens.css.j2` and the §2 light palette (`adapters/output/email_palette.json`).

- **Light is the default and dark follows the reader.** The light palette applies first; under
  `@media (prefers-color-scheme: dark)` the §2 colours replace it, and the head declares
  `color-scheme: light dark` so Apple Mail does not re-tint either. Gmail ignores the media query and
  inverts the light message itself, which stays legible; an inverted dark one would not.
- **One column.** `--measure` wide, `--s-4` gutters, no breakpoint. Sections keep the page's order, with "Other"
  last. Tracking is not in the mail yet; the page is where it is read. The lead is the headline card's
  square panel; every other item is separated by a `--border` hairline.
- **A news paragraph is its text and its links.** In the HTML part it is the paragraph in the body role
  and a line of source links below it, after the area's rows, with no fold; in the plain-text part it
  is the paragraph, then one indented `source — link` line per cited article.
- **A Wire row keeps its sentence.** Its summary follows its title as on the page, in the small role in
  the HTML part, breaking an unbroken run as the page does, and on its own indented line in the
  plain-text part; a row with no summary is its title alone.
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
- [ ] Help is in view, one or two sentences; a dependency disables its control with a line saying why; a fold holds settings and names them (§4 *field*, *settings fold*)
- [ ] Navigation is in the site bar, and back links point to paths that exist
- [ ] `docs/design/prototype.html` has been updated to match
