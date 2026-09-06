# Cyris

**Only what matters should reach your brain.**

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
- Serves a swipe-based UI for triaging the borderline ones, plus `/settings` for the
  LLM provider, digest hours, and the source list

## Two ways to run it

Pick one before you read further — the rest of this file is written for both, marked as
such.

|  | **Local** | **On Cloudflare** |
|---|---|---|
| What runs it | `cyris run` on your machine, on your own schedule | a Container behind a Worker, on an hourly cron |
| Where the state lives | JSON files under `agent-vault/` | D1 |
| Where the digest goes | an HTML file on disk | published to Pages, with an archive |
| Feeds | polled when the digest runs | buffered hourly, so nothing expires between runs |
| 👍/👎 on the digest, triage UI | no | yes |
| Email-only newsletters | no | yes, with your own domain |
| Needs | Python, uv, an LLM key | the same, plus a Cloudflare account (Workers Paid, US$5/mo, for the schedule and the buffer) |

Local is a complete install, not a demo: same pipeline, same prompts, same digest. What
it gives up is everything that needs to be *somewhere* — an archive with a URL, buttons
a reader can press, an inbox that receives mail. You can start local and move later;
`cyris store migrate` exists for exactly that.

### What needs what

Within the Cloudflare track, nothing below the first row is required. Start at the top
and add only what you want.

| Feature | Needs | Cost |
|---|---|---|
| RSS digest, HTML output | An LLM API key | LLM usage only |
| Better feed coverage (see below) | A Cloudflare account, Workers Paid | US$5/mo |
| Production schedule + public triage UI | Same Workers Paid plan; a domain is an optional Access layer | same US$5/mo |
| Digest votes 👍/👎 | A Cloudflare account | Free tier |
| Published HTML digest | A Cloudflare account | Free tier |
| **Email-only newsletters** | A Cloudflare account **and your own domain** | Domain registration |
| State in the cloud (`[store] backend = "d1"`) | A Cloudflare account | Free tier |
| Vote-similarity filtering | A Workers AI embedding token, or Gemini | Inference only |

**Email Routing is the one thing that cannot be automated away.** It needs a domain you
control, so email-only newsletters — the ones with no feed at all — need one too.
Newsletters that publish RSS (Substack, Ghost, and most others) are just feeds and need
nothing extra. The triage UI and digest archive run on `*.workers.dev` with the
`CYRIS_UI_TOKEN` cookie as the only lock; Cloudflare Access is an optional second layer
if you attach your own hostname.

### Where RSS comes from

This is the sharpest difference between the two tracks.

**Local — polled at digest time.** cyris fetches each feed when the digest runs and
keeps the entries inside the window. Nothing to set up, but a feed only publishes its
current snapshot, and a busy one holds 2–4 hours of it. Measured against an hourly
aggregator over the same 24h window, a digest-time poll saw 176 of 317 articles.

**Cloudflare — buffered hourly.** `workers/rss` polls every feed on the hour into D1,
and cyris reads a window out of the buffer, so nothing expires between runs. Deploy
`workers/rss/`, then set `[rss] worker_url` in `cyris.toml` and `CYRIS_WORKER_TOKEN` in
`.env` — see [`workers/rss/README.md`](workers/rss/README.md).

## Running it locally

Python 3.12+, [uv](https://github.com/astral-sh/uv), and one LLM API key — Anthropic
Claude, Google Gemini, OpenAI, or a Cloudflare Workers AI token. Without a key the
pipeline still runs and digests plain excerpts.

```bash
git clone https://github.com/musingfox/cyris.git && cd cyris
uv sync --dev

cp cyris.toml.example cyris.toml           # LLM provider, store backend, digest hours
cp .env.example .env                       # add your API key
cp sources.example.yaml sources.yaml       # then define your RSS/newsletter sources

uv run cyris doctor                # says what is still missing before you find out at 08:00
uv run cyris run                   # fetch → score → digest
```

Keep `[store] backend = "json"`: the article store and the usage log are files under
`[agent_vault] path`, and the digest is written to `agent-vault/html/`. Scheduling is
yours — a cron entry per digest hour, or `docker compose up -d`, which runs the same
image with supercronic reading `docker/crontab`.

`sources.yaml` is the source list, and `cyris.toml` holds everything else. There is no
`/settings` here, because there is no server running to serve it.

## Running it on Cloudflare

The deployment is a Container fronted by a Worker: an hourly Cron Trigger runs
`cyris run --if-due` plus `promote-sync` and the instance exits, while the triage UI
wakes on request and sleeps again. State is D1 and the digest is published to Pages.
Digest hours and the LLM provider live in D1, so changing either is a write on
`/settings`, not a rebuild. Deploy steps, the secret list and auth are in
[`workers/app/README.md`](workers/app/README.md).

[![Deploy to Cloudflare](https://deploy.workers.cloudflare.com/button)](https://deploy.workers.cloudflare.com/?url=https://github.com/musingfox/cyris)

The button clones this repo into your own GitHub account, builds the container image
with Workers Builds, and deploys the Worker, its Durable Object and the hourly cron. It
asks for each secret in [`.env.example`](.env.example).

**One thing to do after it finishes.** The LLM provider is a runtime setting in D1, not
a deploy field — a deployed container has no `cyris.toml` to read one from. Open
`/settings` and pick the provider matching the key you pasted. Until you do, the hourly
run still publishes, but as plain excerpts.

**What the button cannot provision.** One step before the form, two values decided in
it, and one dashboard step:

1. **Create the D1 database** (`wrangler d1 create cyris`, or the dashboard) and pass
   its UUID as `CYRIS_STORE_DATABASE_ID`. The container reaches D1 over REST rather than
   through a binding, so there is nothing for the deploy to provision. `cyris` creates
   the tables on first boot.
2. **Name the Pages project** in `CYRIS_PROMOTE_PAGES_PROJECT` — the first publish
   creates it. `DIGEST_ORIGIN` is only needed when the archive lives somewhere other
   than `<project>.pages.dev`, such as a custom domain.
3. **Attach a domain and Cloudflare Access**, if you want the second auth layer or
   email-only newsletters. Both are dashboard steps.

The other three Workers are separate deploys — one button deploys one Worker — and each
is optional: [`workers/rss/`](workers/rss/README.md) (feed buffer),
[`workers/promote/`](workers/promote/README.md) (vote queue),
[`workers/newsletter/`](workers/newsletter/README.md) (email ingestion). `rss` and
`newsletter` accept the `CYRIS_WORKER_TOKEN` you set on the app; `promote` has its own
`CYRIS_PROMOTE_TOKEN`, kept apart because a vote button is a public capability.

**Point the rss Worker at the same D1 database as the app.** Its button provisions a
fresh one, and a fresh one has an empty `sources` table — the Worker then falls back to
the feed list bundled in `src/feeds.json` and buffers feeds you never chose.

**Do not leave a local install running against the same Pages project.** Two schedulers
publishing one archive is the failure mode; `docker compose down` before you cut over.

### Moving from local to Cloudflare

`cyris store migrate` copies the JSON store into D1 without overwriting anything, and
`cyris store diff` compares them article by article before you switch. Then flip
`[store] backend` to `"d1"` and `cyris sources push` to fill the source table. **Pick
one backend** — they are alternatives, never a pair; running both splits decisions that
`INSERT OR IGNORE` cannot heal.

## The CLI

**On a local install, the CLI is the whole application.** `cyris run` is the pipeline,
`cyris triage-ui` is the triage deck, `cyris articles ...` is how the store is managed.
`cyris --help` lists everything.

**On a Cloudflare install, most of it is not yours to type.** The container already runs
`cyris run --if-due`, `cyris promote-sync` and `cyris triage-ui`, and `/settings` covers
the provider, the digest hours and editing one source. What is left is the work that has
no UI — and it runs from a clone anywhere, because every command reaches D1 over REST:
an `.env` with the deployment's database id and Cloudflare token is the whole setup.

```
cyris doctor                  Before the first run, and after any config change: exits
                              non-zero on anything that would break a run
cyris store migrate|diff      The move into D1, and the comparison to run before you
                              trust it
cyris sources push|list       Make D1 match sources.yaml, removals included; show what
                              it serves. /settings edits one source, this replaces all
cyris articles list|accept|   Bulk work on the store, which the swipe UI is too slow
      reject|score|clean      for — and the only way to delete old rows
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
digest, the triage UI, and `cyris articles accept|reject` all stamp `triaged_at`, and
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

Digest output language is `[digest] output_language`, a BCP 47 tag (default `zh-Hant`);
`[digest] style_prompt` injects a custom tone or focus into the prompts.

## License

[AGPL-3.0-or-later](LICENSE) © 2026 musingfox

Self-host, use, and modify cyris freely. If you run a modified version as a network
service, the AGPL requires you to offer users its source. For use outside AGPL terms
(e.g. a closed-source/commercial deployment), contact the author about a commercial
license.
