---
status: accepted
date: 2026-11-16
decision-makers: ""
---

# The UI is worded from MessageFormat 2 catalogs, in the digest's output language

## Context and Problem Statement

Every reader-facing string in cyris is hard-coded English. `CLAUDE.md` says so on purpose: "i18n
has no framework here yet; English is what makes adding one cheap". The digest's content already
has a language, `digest.output_language`, which is a BCP 47 tag (`src/cyris/config.py:210`). Each
issue stores the value it was made with as `DigestContent.output_language`
(`src/cyris/domain/models.py:298`). The chrome around the content does not follow it. A zh-Hant
reader gets Chinese summaries inside English navigation, buttons, headings, errors and mail.

An inventory of `2a95c40f` found about 1,000 literal strings in about 25 files. Three paths render
them:

* Jinja at run time, for pages published to Pages;
* Jinja in the container;
* plain browser scripts with no build step.

cyris has two editions, local and Cloudflare, and plans a third, hosted. The two existing editions
run the same Python image, which copies all of `src/` (`Dockerfile:33`). The planned hosted edition
is multi-tenant on Cloudflare. Each tenant has its own SQLite-backed Durable Object, holding that
tenant's settings, `digest.output_language` among them. It is undecided whether one container
process serves several tenants, and whether the app Worker renders a tenant's digest page itself.

This record answers four questions:

* What decides the UI language?
* How is a requested tag matched to a catalog?
* What form do the catalog entries take?
* How do the editions and their runtimes share one set of catalogs?

## Decision Drivers

* Language handling uses the standard that covers the most languages and is the most widely used,
  not a hand-rolled scheme (owner, 2026-11-16).
* Every runtime that words reader text reads the same catalog files.
* An English copy edit does not touch a template or a script.
* Translations are drafted by an agent and reviewed by the owner. A translation that no longer
  matches its English is caught by a test.
* A missing translation shows readable English, never a raw key or an error.
* There is no build step for the browser or the image.
* One English word can need two Chinese words. The inventory found 4 identical strings with
  different meanings, and about 40 that are ambiguous on their own.
* A value the code stores or compares does not change with the UI language.

## Considered Options

The language's source:

* `digest.output_language`: an issue's own pages use the language the issue stored, and every other
  page uses the current setting
* A separate UI-language setting

Matching a tag to a catalog:

* CLDR Language Matching, as UTS #35 specifies it
* RFC 4647 Lookup
* Case-insensitive equality plus a hand-written alias table

The entries' form:

* Symbolic keys, each value a Unicode MessageFormat 2.0 message
* Symbolic keys, each value an ICU MessageFormat 1 message
* Symbolic keys in i18next JSON v4
* Symbolic keys with a hand-rolled `{identifier}` syntax and plural rule
* The English text as the key, gettext style
* gettext `.po`/`.mo` files with Babel and `jinja2.ext.i18n`

## Decision Outcome

Chosen options:

* "`digest.output_language`" (owner, 2026-11-14);
* "CLDR Language Matching" (owner, 2026-11-16);
* "symbolic keys, each value a Unicode MessageFormat 2.0 message" (owner, 2026-11-16).

The reasons for each are under Pros and Cons of the Options.

### The language

* **An issue's own pages:** the digest page, raw page, mail and Discord message for one issue are
  worded in that issue's `DigestContent.output_language`. An issue stored with no language (`""`)
  uses the current setting.
* **Pages about the deployment:** the archive index, `/interest`, `/settings`, `/labels` and the run
  alerts use the current `digest.output_language`.
* **Hosted tenants:** in the hosted edition, "the current setting" is the requesting tenant's,
  read from that tenant's Durable Object for each request or run. Two tenants with different
  languages are worded independently, even when one process serves both.
* **The language is chosen per render, never once per process.** A saved language takes effect on
  the next request. A language change republishes nothing: issues on Pages keep their language, and
  the index takes the new language from the next run.
* **No language set yet:** a new deployment has no value, because `settings_starting_values.json`
  holds none. Until one is saved, the container pages follow the browser's `Accept-Language`, and
  the page reloads in the saved language once onboarding saves it (owner, 2026-11-16).
* **`<html lang>`:** it is the tag of the catalog the page was worded from. Content blocks keep
  their own `lang`.

### Matching

* **Tags:** tags are BCP 47 in the UTS #35 canonical form. A catalog is named by its tag, such as
  `en` and `zh-Hant`, and the tag must also load as a Babel locale.
* **The matcher:** a single requested tag is matched to the shipped catalogs by CLDR Language
  Matching (<https://www.unicode.org/reports/tr35/tr35.html#LanguageMatching>). Python uses
  `langcodes.closest_supported_match`, which takes one tag. A JS runtime that ever has to match
  uses `@formatjs/intl-localematcher`. That is a second implementation, with different CLDR data.
* **`Accept-Language`:** neither library reads the header. cyris splits it, orders the tags by `q`
  (<https://www.rfc-editor.org/rfc/rfc9110#section-12.5.4>), drops `*` and `q=0`, and takes the
  first tag that matches a catalog.
* **No match:** a tag with no match, and a value that is not a well-formed tag, render English. A
  Japanese digest therefore has Japanese content in an English UI. A legacy `output_language` such
  as a plain language name also falls here.
* **Where matching runs:** on a server, never in the browser. The first catalogs are `en` and
  `zh-Hant`.

### The catalog

* **Files:** each language is `src/cyris/locales/<tag>.json`. It is grade A data, and its §5 row
  replaces the rows whose wording moves into it. §4 gains nothing, because no state is added.
* **Entries:** `en.json` is a flat map from key to MF2 message. Every other catalog is a flat map
  from key to `{text, source}`. `text` is the MF2 message, and `source` is the exact English message
  it was translated from. JSON has no standard review state (only PO's `fuzzy` and XLIFF's `state`
  have one), so `source` is what catches a stale translation.
* **Keys:** keys are flat `area.element` names, such as `labels.skip` or `lane.read`. One English
  word with two meanings is two keys. A key built from a value is declared with the value's range,
  either a closed code type or a shipped data file's ids, and the coverage test enumerates it. A
  key over an open set of values passes the value itself as its default.
* **Messages:** a value is an MF2 message
  (<https://www.unicode.org/reports/tr35/tr35-messageFormat.html>).
  * Plurals and variants use `.match` on CLDR categories. The formatter's CLDR data picks the
    category, so cyris writes no plural rule.
  * A message uses only functions that are Stable in the spec and on by default in both formatters:
    `:string`, `:number` and `:integer`. Dates are formatted outside the message and passed in as
    strings.
  * A sentence is one message, never fragments joined in code, because Chinese word order differs.
    A sentence that holds a link or other inline markup marks the span with MF2 markup, as in
    `{#link}…{/link}`. The template or script supplies the element, and the catalog never holds
    HTML.
* **Data-file wording:** the English wording in shipped data files (`settings_fields.json`,
  `onboarding_feeds.json`, `feed_check.json`, `alert_messages.json`) moves into the catalogs. The
  data files keep only their structure.
* **Not translated:** the language names in `service_layer/languages.json`. The pickers show them
  as they are.

### Formatting

* **One function:** Python and Jinja format through `messageformat2` behind one function,
  `t(key, **variables)`. It always passes the catalog's Babel locale and never formats with the
  process default. No render call or template may bind `t` to anything else.
* **Fallback:** a lookup tries the matched catalog, then `en.json`. A message is formatted with the
  locale of the catalog it came from, so an English fallback is formatted as English. A key missing
  from `en.json` is a programming error and raises.
* **Missing variables:** a variable the message names and the caller does not pass is a programming
  error. Python raises, and the browser formatter is given an error handler that throws, so both
  fail the same way in tests.
* **Bidi isolation:** it is off in both formatters while every catalog is left-to-right. The change
  that adds the first right-to-left catalog turns it on in both.
* **Browser:** the JS `messageformat` package (4.x) is bundled once into one ES module file, at a
  pinned version, with the command recorded beside it. The server serves one file of browser
  messages per catalog. It holds every message the page scripts use, with English filling any
  the catalog lacks, and each message carries the tag of the catalog it came from. A page names
  the file for its language. The browser caches the file and revalidates it.
* **Plain text:** a formatted message is plain text. Jinja's autoescape escapes it, and a script
  writes it with `textContent`.
* **No `jinja2.ext.i18n`:** its gettext calls use `%(name)s` placeholders and two-id plurals
  (`jinja2/ext.py:180, 189-191`, Jinja 3.1.6), which are not MF2.

### Sharing across editions

* **One source:** `src/cyris/locales/` is the only copy of the text. The editions share it through
  the Python image. Pages output carries text already rendered.
* **Hosted tenants share the catalogs:** every tenant reads the catalogs shipped in the image. A
  tenant chooses a language, not a catalog, so adding a language reaches all tenants at once.
  Per-tenant wording is out of scope, and a tenant never stores catalog text in its Durable Object.
* **Workers:** no Worker words reader text from the catalogs today. The hosted edition may need one
  to, if the app Worker renders a tenant's digest page from the tenant's Durable Object. That Worker
  would match with the JS matcher and format with the same MF2 runtime. The first Worker that words
  reader text reads the same files, either by importing them or through a generated copy that a
  parity test guards. A generated copy guarded by a parity test is how
  `workers/app/src/type_scale.js:12` and `tests/test_type_scale_steps.py:24-34` work today. That
  change settles which of the two applies.

### Scale

Nothing in this design grows with the number of readers or tenants except the revalidations a
container page makes.

* **Catalogs in memory:** catalogs are read once per process, and each parsed message is cached per
  catalog and key. Their size grows with the number of languages and strings, never with readers.
* **Choosing the language:** a request reads the language from the settings it already loads. In the
  hosted edition, that is the tenant's Durable Object, which the request reads anyway. No read is
  added.
* **Pages output:** pages published to Pages carry rendered text, so serving them costs the catalogs
  nothing.
* **Container pages:** each view of `/settings`, `/labels` or `/onboarding` names its language's
  file of browser messages, and the browser revalidates its copy. The answer is a 304 with no body
  while the file is unchanged. The file's bytes are paid once per language per catalog change. A
  revalidation is the only cost that grows with use.
* **Both editions:** the container serves `/static/` in both, so the local edition needs no other
  path.
* **The adjustment, if the measured revalidations warrant it:** put a content hash in the file's
  name and let the browser cache it as immutable, so a page view makes no request for it. A page
  already names its file, so the change does not reverse this decision.

### Display versus value

* **Identifiers stay English:** a value the code stores or compares stays an English identifier
  whatever the UI language. Examples are periods, article states, lanes, area statuses, interest
  polarity and the `none` provider. Only their displayed name comes from the catalog.
* **Codes, not English text:** a client that matches a server message matches a code.
* **"Other":** it stays the reserved area name that `is_unplaced_label` compares
  (`domain/areas.py:35-40`). No output draws it today (`docs/architecture.md:845`). A catalog's
  rendering of it becomes reserved in the change that first draws the label, which also says what
  becomes of an existing area by that name. `domain/` does not read catalogs.

### Out of scope

* the plain strings the Workers return today;
* the CLI;
* Pydantic's built-in validation messages;
* the LLM prompts, which already take the output language.

### Consequences

* Good, because plurals and variants follow CLDR for every locale. A new language is a catalog
  file, plus bidi isolation if it is the first right-to-left one.
* Good, because browsers that send `zh-TW`, `zh-HK` or `zh-MO` reach the `zh-Hant` catalog with no
  alias table to maintain.
* Good, because the catalog syntax is a published standard. If a formatter is abandoned, the
  catalogs stay valid for another implementation.
* Good, because an English copy edit changes catalogs only, a stale translation is caught by its
  `source`, and a missing one falls back to English.
* Good, because every edition reads one set of files, and a rerendered issue keeps its own
  language.
* Bad, because Python gains `messageformat2` and `langcodes`. Babel, which is installed today only
  through `trafilatura`, also becomes a direct dependency that cyris must keep.
* Bad, because `messageformat2` reached 1.0 only on 2026-11-04, and it has one main maintainer. It
  is pinned, and replacing it means swapping the formatter, not rewriting the catalogs.
* Bad, because `langcodes` carries CLDR 40 matching data, older than CLDR 49. It is not identical
  to the JS matcher: `cmn-TW` gave `zh-Hant` in `langcodes` and `en` in formatjs.
* Bad, because the browser gains a bundled file of about 7.6 KB gzipped. It is under Apache-2.0,
  which is compatible with cyris's AGPL-3.0, and it must keep its licence notice.
* Bad, because MF2 is more verbose than a plain template, and no translation platform is known to
  support it. MF2's escaping also changes some existing copy: `{` and `\` must be escaped.
* Bad, because number formatting comes from two CLDR copies, Babel's and the browser engine's, which
  can differ by engine.
* Bad, because templates and scripts no longer show their copy, and about 1,000 strings need key
  names.
* Bad, because English is still special: it is the fallback, and the `source` that other catalogs
  check against.
* Bad, because tests that assert English copy must read it from the catalog as each surface
  converts, or they go red when the copy changes. The shared test configuration's language is
  `en` (`tests/fakes.py:25`).
* Neutral, because the `CLAUDE.md` convention "User-facing strings are English" becomes false once
  the last surface converts. It is rewritten then, with the owner's approval.

### Confirmation

A catalog coverage test fails when any of these happens:

* a catalog message does not parse as MF2, or it uses a function outside `:string`, `:number` and
  `:integer`;
* a key used under `src/cyris/` is missing from `en.json`;
* a non-English entry's `source` differs from the current English;
* a translation's input variables (those no `.local` declares) differ from its English entry's;
* the file a page names lacks a message that a script the page loads uses, through its imports;
* a page carries messages inline.

Each converted surface also gets render tests in both languages. `scripts/check.sh` runs them.

## Pros and Cons of the Options

### `digest.output_language` as the UI language

* Good, because the reader sets one language, and the chrome agrees with the content, on a rerender
  too.
* Bad, because no reader can have content in one language and the UI in another.
* Bad, because the browser decides until a language is saved, which is a second rule.

### A separate UI-language setting

* Good, because a reader could read Japanese content in a Chinese UI.
* Bad, because it is a second setting for a combination no reader has asked for.

### CLDR Language Matching

* Good, because UTS #35 recommends it over plain lookup and names `Accept-Language` as an input.
* Good, because CLDR's likely-subtag data maps regional tags such as `zh-TW` to `zh-Hant`, so cyris
  keeps no table of its own.
* Bad, because Python needs `langcodes`. Babel's `Locale.negotiate` is not CLDR matching: it
  returned `en` for `zh-TW`.

### RFC 4647 Lookup

* Good, because it is simple, and it is the matching RFC 4647 defines.
* Bad, because it never maps a region to a script. It returned `en` for `zh-TW`, `zh-HK` and `zh-MO`.

### Case-insensitive equality plus a hand-written alias table

* Good, because it needs no dependency.
* Bad, because it is a hand-written copy of CLDR data, and every language needs its own entries.

### Unicode MessageFormat 2.0

* Good, because it is the current Unicode standard for messages. It has been Stable since CLDR 47
  (<https://blog.unicode.org/2025/03/unicode-cldr-47-release-messageformat-2.html>).
* Good, because it is the only message-format standard among the options with a maintained
  formatter in both Python and plain JS. A plural message gave identical output in both for `en`,
  `zh-Hant` and `ru`.
* Bad, because the Python formatter is new, and the syntax is verbose.

### ICU MessageFormat 1

* Good, because Weblate, Crowdin and Lokalise support it, the widest tooling support of all the
  options.
* Bad, because Python has no maintained formatter that installs from a wheel. PyICU needs system ICU
  headers, and the pure-Python ports are unmaintained or only parse.

### i18next JSON v4

* Good, because it is light: plurals are key suffixes named by CLDR category, and no formatter
  library is needed.
* Bad, because it is one vendor's convention, not a standard, and it has no `select`.

### A hand-rolled `{identifier}` syntax and plural rule

* Good, because it needs no dependency.
* Bad, because cyris would maintain its own syntax and a plural rule per language, which the owner
  ruled out.

### English text as the key, gettext style

* Good, because templates show their copy, and a changed English string falls back with no `source`
  field.
* Bad, because an English copy edit touches code, and a word with two meanings needs a context in
  every runtime.

### gettext `.po`/`.mo` with Babel and `jinja2.ext.i18n`

* Good, because it is the standard gettext toolchain, with a `fuzzy` state for stale translations.
* Bad, because it adds a compile step, and the browser needs the catalog converted separately.
* Bad, because its `%(name)s` placeholders and two-id plurals do not match the browser.

## More Information

* **Prototypes:** two key styles, symbolic keys and English text as the key, were built as throwaway
  prototypes from `2a95c40f`. They covered the same slice of the UI and were judged against criteria
  fixed before either was written. Neither is merged, and neither used MF2 or CLDR matching. They
  showed two things: one catalog can word Jinja, Python and the browser, and a coverage test catches
  missing and stale keys.
* **Measurements:** on 2026-11-16, the matching results, the MF2 parity between Python and JS, the
  error behaviour and the bundle size were measured outside the repo. The versions were:
  * `langcodes` 3.5.1;
  * `@formatjs/intl-localematcher` 0.10.1;
  * `messageformat2` 1.1.0;
  * `messageformat` 4.1.0;
  * Babel 2.18.0.
* **Workers:** whether workerd ships `en` and `zh-Hant` locale data is unverified. It uses
  Chromium's filtered ICU data (<https://github.com/cloudflare/workerd/issues/64>).
* **Kept outside the repo:** the inventory, the evaluation, the comparison criteria, the prototype
  reports and the standards research. The implementation notes that this record leaves out are
  with them.
