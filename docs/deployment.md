# Deployment Direction Assessment: Fully Local vs. Cloudflare

> Status: superseded for the Cloudflare direction — see [`cloud-migration.md`](cloud-migration.md).
> Option B below is kept for the local-vs-cloud comparison, but its "drop Miniflux and let
> cyris fetch RSS directly" recommendation was measured and is wrong: a digest-time poll
> misses 141 of 317 articles because feeds hold only 2-4h of snapshot.

## Current State (Starting Point)

- The `cyris` pipeline currently runs on **local Python + macOS launchd** (`src/cyris/schedule/launchd.py`).
- `docker-compose.yml` **only containerizes Miniflux + Postgres**; cyris itself is not containerized.
- All persistent state lives in local files, **centrally injected** by `bootstrap.py` rooted at `agent_vault.path` (the storage port is clean and easy to swap).
- Paid-source cookies are read from the browser's live DB on the local machine (`adapters/cookies.py`), staying fresh automatically through everyday browsing.

**Key point: the common factor across both directions = containerize cyris itself first.** With the same Docker image, direction A drops it into compose and direction B drops it into a Cloudflare Container. This step is done only once.

---

## Option A: Fully Local Deployment (Remove macOS Coupling)

Goal: anyone can run it on any Linux box with `docker compose up`, with no macOS coupling and no dependency on a local Python environment.

| Aspect | Current | Change |
|------|------|------|
| Compute | Local `cyris run` | Add a `cyris` service to compose |
| Scheduling | launchd (macOS-only) | Host crontab calling `docker compose run cyris run`, or in-container cron |
| Persistence | Local files | **Unchanged**, volume-mounted into the container |
| Output | Obsidian vault files | **Unchanged**, vault directory volume-mounted |
| Miniflux | Already in compose | Unchanged |
| Config | `.env` / `*.toml` | Unchanged, mounted into the container |

**Change size: small.** Storage and output need no changes; you only move compute into a container and swap scheduling for cron.
**Advantages**: fully runnable offline, best privacy, no cloud costs; paid-source cookies can later be handled by mounting host cookies.
**Cost**: requires an always-on machine; "reading the digest while out" needs a separate hookup (Pages/Tailscale).

---

## Option B: Cloudflare (Cookies Not Handled Yet)

Goal: no local machine, fully cloud-based. Around the US$5/mo tier.

| Aspect | Current | Change |
|------|------|------|
| Compute | Local `cyris run` | Same Python image → **Cloudflare Container**, Worker `scheduled` triggers `container.start()` |
| Scheduling | launchd | **Workers Cron Triggers** |
| Persistence | Local files | Swap the storage port → **R2** (move JSON blobs as-is) or D1 (only if dedup needs querying) |
| Config/secrets | `.env` | Workers Secrets + container envVars |
| LLM | Anthropic API | Unchanged (already cloud-based) |
| Output | Obsidian markdown | R2/Pages HTML (no write-back to Obsidian) |
| Mark-to-read | — | Existing promote KV loop |
| **Miniflux** | compose | **Pain point**: Containers have no persistent disk, so running Miniflux+Postgres on CF is unnatural. Recommend dropping it and letting cyris fetch RSS directly + store read state in D1 |

**Change size: medium.** Containers let the Python **move to the cloud in place, without a TypeScript rewrite** (the earlier "must rewrite" assessment is now outdated). The real work is: swap the storage port to R2/D1, dockerize, add a thin Worker cron wrapper, and retire Miniflux.
**Advantages**: no local machine, readable anytime while out, operations handed off to CF.
**Cost**: paid-source cookies lose automatic freshness (deferred this round); Miniflux must be retired.

---

## Comparison Table

| | A Fully Local | B Cloudflare |
|---|---|---|
| Always-on machine needed | Yes | No |
| Monthly cost (excl. Claude API) | $0 | ~US$5 |
| Change size | Small (storage untouched) | Medium (swap storage port + docker + worker) |
| Language rewrite | None | **None** (Container runs Python) |
| Reading digest while out | Separate hookup needed | Native |
| Paid-source cookies | Solvable by mounting host | **Lose automatic freshness** (deferred) |
| Miniflux | Kept | Retired, cyris fetches RSS itself |
| Privacy/offline | Best | Depends on CF |

**Recommendation**: the two are not mutually exclusive. Do the common factor first (dockerize cyris) → land A (a portable, open-source-ready default deployment) → when you want no local machine, layer B on the same image.

---

## Open-Source Readiness Gaps

To become a shippable open-source project, the gaps fall into two categories.

### Blockers Preventing Others From Running It (Hard Blockers, Must Fix)

| Gap | Fix | Status |
|------|------|------|
| No LICENSE | AGPL-3.0-or-later + README notice + pyproject metadata | ✅ |
| Defaults bound to a specific paid domain | Template `cookie_domains = []` placeholder | ✅ |
| Confusing duplicate template naming | Unify to `cyris.toml.example` (fix `[claude]`→`[llm_provider]`+`[digest]`, de-personalize) | ✅ |
| Scheduling bound to macOS | Docker uses supercronic; launchd kept as a macOS option | ✅ |
| Hardcoded personal vault path | `CYRIS_VAULT_PATH` env override; default still `~/Documents/ObsidianVault` | ◐ Partial |
| Cookies bound to macOS paths (`cookies.py:61`) | Marked optional; auto-skipped inside the container | ◐ Deferred |
| Hard dependency on self-hosted Miniflux + personal worker URL | Marked optional in README | ◐ |

### Project Quality (Open-Source Conventions, Should Fix)

| Gap | Fix | Status |
|------|------|------|
| No CI | `.github/workflows/ci.yml`: ruff check + format + pytest | ✅ |
| Missing human-facing architecture docs | README architecture + adapter section + `docs/architecture.md` | ✅ |
| No CONTRIBUTING / issue·PR template | README Contributing section (standalone templates not yet added) | ◐ |

**Good news**: `.env` / `cyris.toml` / `sources.yaml` are all gitignored, so personal email and worker URLs never entered the repo; the template files (`.env.example`, `sources.example.yaml`, `tracking.example.yaml`) are largely complete; test coverage is good. **The core code is clean; what's missing is mainly "de-personalized defaults + de-macOS-ing the deployment + open-source convention files".**

---

## Deferred Items

- **Paid-source cookie freshness (fully cloud)**: not handled this round. With no local machine at all, you lose automatic browser-based renewal. Future options: route paid sources through newsletter email wherever possible (a worker already exists, no cookies needed) > manually update KV > skip headless auto-login.
