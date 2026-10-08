# CLAUDE.md

Guidance for coding agents working in this repository. `AGENTS.md` is a symlink to this
file, so a tool that looks for either name reads the same document.

## Project Overview

Cyris is an AI-powered information digest agent. The deployment is a Cloudflare Container
fronted by a Worker; the `json` store plus `docker compose` remain the local development
path, and a fork can run the whole pipeline that way. It fetches articles from RSS feeds and newsletters, processes them through an LLM (Anthropic, Gemini, OpenAI or Cloudflare Workers AI) with tier-based filtering/summarization, and publishes an HTML digest to Cloudflare Pages.

## Commands

Setup, the gate and test subsets are in `CONTRIBUTING.md`. Run `scripts/check.sh` before you
push: `main` has no branch protection, so a red CI run stops nothing from landing. Only the app
Worker deploys through a workflow; the `rss`, `promote` and `newsletter` Workers deploy outside
any workflow, with no gate, so a merged change to them is not live until someone deploys it.

## Architecture

**Read `docs/architecture.md` before any non-trivial change.** It is the authoritative description of this system, and it is written to be acted on, not just read:

- **§1 Layers** — `entrypoints → service_layer → domain`, with `adapters` implementing the
  service layer's Protocols and `bootstrap.build_deps()` the only place they meet.
- **§2 Wiring** — which boundaries have a Protocol (cheap to swap) and which are direct injections. Local-filesystem edges are closed: with D1 the only writes left are the `json` backend's, which is the documented fallback.
- **§3 How a digest is made** — the two ingestion paths and why they have different shapes (RSS is an idempotent buffer, email is a pull/ack queue). Read this before touching a Worker or a `FetchSource`.
- **§4 Data residency** — every persistent datum and where it lives. **Do not introduce a new place for state without adding a row here.** Scattering data across new homes to finish a feature is the failure this table exists to prevent.
- **§5 Configuration: four grades** — A baked / B deployment identity / C secrets / D runtime-mutable. Every new setting must be assigned a grade and put in that grade's home. `cyris.toml` is not a default home.
- **`docs/decisions/`** — why each hard-to-reverse decision was made (MADR ADRs). Read the ADR before reversing a decision; architecture.md describes the system, not why.

Keep §1–§6 current in the same change that makes them stale — an architecture doc that lags the
code is worse than none, because it is still trusted. Two more doc rules:

- **After an architecture change lands, add its row to §7** in a separate docs commit on main,
  citing the hash it landed as. §7 opens with why the row cannot ride in the change itself.
- **A decision whose reason needs recording gets an ADR** in `docs/decisions/`, in the MADR
  format `docs/decisions/0000-use-madr.md` sets out.

### Where IO goes

When adding or swapping IO, work in `adapters/` and `bootstrap.build_deps()` — never touch
`service_layer/` or `domain/` (architecture.md §8). Only a genuine IO boundary gets a Protocol in
`ports.py`; a single-implementation component is injected directly. Each embedding model
carries its **own** similarity threshold, in `src/cyris/provider_defaults.json`: the cosine
scales differ, so reusing one number across models silently disables vote similarity (ADR-0006).

## Testing

Every `tests/test_*.py` file carries one level and any number of tags. The rules for choosing them,
and the conventions for writing a test, are in `tests/CLAUDE.md`, which loads when you open a file
under `tests/`; `tests/test_level_markers.py` rejects a file whose marks break them.

## Conventions

- **No hardcoded values.** A word the code matches, a label a human reads, a name of a
  language or a place — all of it is data (`adapters/fetch/keywords.json`,
  `service_layer/languages.json` are the pattern), reachable without a code edit. What
  legitimately stays in code is *structure* (a regex's shape, an algorithm's steps) and
  the tuned constants `docs/architecture.md` §5 grades **A** with a stated reason. If
  something is a proof of concept, say so in the identifier or the comment above it —
  an unlabelled placeholder becomes load-bearing by default
- **UI changes follow `docs/design/ui-language.md`.** It covers every reader-facing page — the
  digest templates, `/settings` — and `docs/design/prototype.html` is its reference
  implementation. Every step of the spec's §8 has landed; where a component still differs from
  the spec, restyle it to the spec when you touch it, never copy the old style
- User-facing strings are English, even while the digest's content is not. i18n has no
  framework here yet; English is what makes adding one cheap
- A non-null `triaged_at` is what marks an article's state as a *human* decision (a digest or
  raw-page vote, `cyris articles accept|reject`) rather than the pipeline's own verdict.
  `update_states` leaves stamped rows alone and only stamped rows seed vote similarity, so
  nothing but a human action may stamp it
- Newsletter canonical links (`adapters/fetch/newsletter.py`): an issue's 原文 link is chosen structurally — normalize candidates, keep content URLs on the sender's own host (the source's `homepage` host exactly or a `host_aliases` alias of it, else the From address's domain and its subdomains; never the most frequent host) and, on a large platform's host, only that platform's post shape (`platform_post_paths` in `keywords.json`: one rule per platform, never per newsletter), collapse links naming one page (a page and its query-string variant, or one path on two hosts `host_aliases` in `keywords.json` names as one site; other hosts under one parent domain stay distinct), and accept the result only when exactly one page remains. Depth and frequency are not canonical confidence: an ambiguous issue gets no link from this step. The "網頁版/view in browser" text scan, then the ESP-archive hostname allowlist, are the fallbacks behind it; every step drops the per-recipient `tracking_params`. The constraint is that a returned URL should not repeat across issues — the store dedups by URL, so a later issue of the same source whose link repeats under a different subject falls back to its synthetic `newsletter:{id}` URL and loses its link (a different source, or the same subject, is still skipped; see `adapters/store/newsletter_dedup.py`). `tests/test_newsletter.py` enforces it (distinct post URLs, distinct synthetic URLs, and where `homepage` may land); read those before changing the extractor. Real-sample coverage is in `tests/test_newsletter_real_fixtures.py`; samples stay outside this repo
- Link-health counters on `DigestContent` measure two different things: `synthetic_url_count` counts every article fetched this run whose URL is the synthetic `newsletter:` fallback (extractor health); `dead_link_count` counts only items that reached the digest with no clickable link (what a reader hits). Each has its own test, but nothing asserts they disagree on one run — so don't "fix" them into agreement
