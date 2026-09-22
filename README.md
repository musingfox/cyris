# Cyris

**Only what matters should reach your brain.**

[Website](https://cyris.musingfox.com/)

You already do this for your agents. An LLM is only as good as what makes it into the
context window, so you curate that ruthlessly — the wrong thousand tokens and the
answer is worse, not just longer. Your own attention works the same way and gets none
of the same care: the feeds keep arriving, the good stuff is in there somewhere, and
the cost of finding it is the whole evening.

Cyris is that curation, pointed at you. It reads the feeds and newsletters you
subscribed to and stopped opening, scores every article against the topics you said you
care about, and publishes a digest twice a day that is short enough to actually finish.
Not drowning and not missing anything are usually a trade; this is the machine that
takes it.

Nothing is hidden to make it short. Every digest ships with a full listing of what the
same window collected and what was dropped, so a filtered article is one you can still
go and read. A 👍 or 👎 on any item is a decision the next run acts on.

Under the hood: RSS feeds and newsletters in, an LLM with tier-based filtering and
summarization in the middle, an HTML digest on Cloudflare Pages out.

## What it does

- Reads RSS feeds and newsletters, listed in `sources.yaml` (or the D1 `sources` table
  once you push / edit them)
- Scores and routes articles: high-relevance to the digest, lower to a triage queue,
  cutting daily volume by 80%+
- Groups what survives into thematic summaries, and clusters news by topic rather than
  printing the same story five times
- Turns a 👍/👎 into an accept or reject in the store, and can suppress later articles
  sitting close to a downvote
- Lets you triage the borderline ones card by card in the raw page's triage view, and
  serves `/settings` for every runtime setting and the source list

## Two ways to run it

|  | **Local** | **On Cloudflare** |
|---|---|---|
| What runs it | `cyris run` on your machine, hourly from cron or `docker compose` | a Container behind a Worker, on an hourly Cron Trigger |
| Where the state lives | JSON files under `agent-vault/` | D1 |
| Where the settings live | `cyris.toml` and `sources.yaml` | D1, edited on `/settings` |
| Where the digest goes | HTML files on disk | published to Pages, with an archive at a URL |
| Feeds | polled when the digest runs | the same, or buffered hourly by `workers/rss` |
| 👍/👎 on the digest and the raw page | no | with `workers/promote` |
| Email-only newsletters | no | with `workers/newsletter` and your own domain |
| Needs | Python 3.12+ and uv | Workers Paid (US$5/mo), plus Docker and bun to deploy |

Either way, an LLM API key is optional: provider `none` lists plain excerpts.

Local is a complete install, not a demo: same pipeline, same prompts, same digest. What
it gives up is everything that needs to be *somewhere* — an archive with a URL, buttons
a reader can press, an inbox that receives mail. You can start local and move later;
`cyris store migrate` exists for exactly that.

### What needs what on Cloudflare

Only the first row is required. Start at the top and add only what you want.

| Feature | Needs | Cost |
|---|---|---|
| The app: schedule, D1, Pages archive, `/settings` | A Cloudflare account on Workers Paid | US$5/mo; see [what one deployment spends](docs/hosting-and-cost.md) |
| LLM summaries | An LLM API key, or provider `none` | LLM usage only |
| Better feed coverage (`workers/rss`) | The same Workers Paid plan | included |
| Digest votes 👍/👎 (`workers/promote`) | A KV namespace; readers log in on the app's hostname | Free tier |
| **Email-only newsletters** (`workers/newsletter`) | **Your own domain** on Cloudflare, with Email Routing | Domain registration |
| Cloudflare Access in front of `/settings` | Your own domain | Domain registration |
| Vote-similarity filtering | A Workers AI embedding token, or Gemini | Inference only |

**Email Routing is the one thing that cannot be automated away.** It needs a domain you
control, so email-only newsletters — the ones with no feed at all — need one too.
Newsletters that publish RSS (Substack, Ghost, and most others) are just feeds and need
nothing extra. `/settings` and the digest archive run on `*.workers.dev` with the
`CYRIS_UI_TOKEN` cookie as the only lock; Cloudflare Access is an optional second layer
if you attach your own hostname.

### Where RSS comes from

This is the sharpest difference between the two tracks.

**Polled at digest time.** cyris fetches each feed when the digest runs and keeps the
entries inside the window. Nothing to set up, but a feed only publishes its current
snapshot, and a busy one holds 2–4 hours of it, so part of a 24-hour window is gone
before the digest looks.

**Buffered hourly, on Cloudflare.** [`workers/rss`](workers/rss/README.md) polls every
feed on the hour into D1, and cyris reads a window out of the buffer, so nothing
expires between runs. It is wired into the app with the `CYRIS_RSS_WORKER_URL` and
`CYRIS_WORKER_TOKEN` secrets.

## Installing

Pick a track and follow its guide from the top:

- **Local**: [docs/install-local.md](docs/install-local.md)
- **Cloudflare**: [docs/install-cloudflare.md](docs/install-cloudflare.md)

Moving a local install to Cloudflare later is the last section of the local guide.
Updating, rolling back and checking a running deployment are in
[docs/operations.md](docs/operations.md).

## The CLI

**On a local install, the CLI is the whole application.** `cyris run` is the pipeline,
`cyris triage-ui` shows `/settings` read-only, `cyris articles ...` is how the store is
managed.
`cyris --help` lists everything.

**On a Cloudflare install, most of it is not yours to type.** The container already runs
`cyris run --if-due`, `cyris promote-sync` and `cyris triage-ui`, and `/settings` covers
every runtime setting and editing one source. What is left is the work that has
no UI — and it runs from a clone anywhere, because every command reaches D1 over REST.
The clone's `.env` is in
[Running the CLI against the deployment](docs/install-cloudflare.md#running-the-cli-against-the-deployment).

```
cyris doctor                  Before the first run, and after any config change: exits
                              non-zero on anything that would break a run
cyris store migrate|diff      The move into D1, and the comparison to run before you
                              trust it
cyris settings push           Copy the runtime settings D1 lacks from cyris.toml;
                              never overwrites a row
cyris sources push|list       Make D1 match sources.yaml, removals included; show what
                              it serves. /settings edits one source, this replaces all
cyris articles list|accept|   Bulk work on the store: the only way to reach pending
      reject|score|clean      rows outside an issue, and to delete old rows
cyris llm-compare             Digest one window with several providers, side by side,
cyris embed-compare           or judge it with both embedding providers, before
cyris vote-sim                switching; vote-sim previews what similarity would
                              suppress before you enable it
```

## How sources are processed

Each source declares a tier in `sources.yaml`, and the tier decides how much LLM
attention it gets:

| Tier | Processing | Example |
|------|-----------|---------|
| `filter` | Discard most; surface only significant headlines (<10% pass). News-tagged articles are clustered by topic | TechCrunch, 聯合新聞網 |
| `summarize` | Scored, then split by `[routing] summarize_score_threshold` into full summaries and brief mentions | Stratechery, Benedict Evans |
| `fan` | Passthrough. Never scored, filtered, or summarized | followed groups and newsletters |

An article moves `pending → accepted / rejected / awaiting_triage`. A 👍/👎 on the
digest or the raw page, and `cyris articles accept|reject`, all stamp `triaged_at`, and
that stamp is what vote similarity later treats as a human decision.

**Paywalled sources are not supported.** cyris takes a feed at face value and will not
log in or carry a session. If you subscribe to something, look for the subscriber-only
RSS feed many paid publications issue (treat that URL as a secret), or route the email
edition through the newsletter path.

## Architecture

Clean architecture — dependencies point inward, all IO lives at the edges:

```
entrypoints/     CLI + web servers (parse args, call a use case)
service_layer/   use cases + Protocols (ports.py)   ← business logic
domain/          pure models & rules (no IO)
adapters/        concrete IO implementing the Protocols
bootstrap.py     composition root: wires adapters into a Deps container
diagnostics/     off the pipeline: doctor + the comparisons, whose subject is the
                 deployment rather than the digest
```

Pipeline: **Fetch → Store → Score → Process → Output**, orchestrated by
`service_layer/run_digest.py`. New IO — a fetch source, an LLM provider, a store, an
output sink — is written in `adapters/` against a Protocol in `service_layer/ports.py`
and wired in `bootstrap.build_deps()`; core code never changes for it.

[`docs/architecture.md`](docs/architecture.md) is the authoritative description: the
core↔adapter map, where every persistent datum lives, how settings are graded, and what
is deliberately *out* of the design.

## Contributing

```bash
uv sync --dev
scripts/check.sh        # everything CI runs: JS tests, ruff, pytest
```

- **Adapters**: new IO goes in `adapters/` behind a Protocol, never in `service_layer/`
  or `domain/`
- **Tests**: inject `FakeLLM` (`tests/fakes.py`) instead of patching the SDK; patch
  where a symbol is *used*, not defined; give external resource names unique per-test
  suffixes
- **Style**: ruff (line length 100); Pydantic v2 for all models and config
- **Commits**: keep them atomic; lint + tests green before a PR

Digest output language is `[digest] output_language`, a BCP 47 tag with no default
(the example uses `zh-Hant`); `[digest] style_prompt` injects a custom tone or focus
into the prompts.

## License

[AGPL-3.0-or-later](LICENSE) © 2026 musingfox

Self-host, use, and modify cyris freely. If you run a modified version as a network
service, the AGPL requires you to offer users its source. For use outside AGPL terms
(e.g. a closed-source/commercial deployment), contact the author about a commercial
license.
