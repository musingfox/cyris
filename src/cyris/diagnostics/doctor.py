"""Check a deployment before it has to prove itself at 08:00.

The checks themselves are read-only and cheap, but `doctor` as a command is not:
`load_effective_config` creates the D1 tables if they are absent, so a token
without D1 edit cannot run it, and a wrong-but-valid `database_id` gets nine
empty tables. That is the price of the checks working on a clean deployment at
all — a first boot has nothing to check until the tables exist.

The reason this module exists: a Cloudflare API
token sat expired in `.env` while `wrangler pages deploy` reported it as its own
intermittent failure, and nothing in the pipeline ever asked the question
directly. Silent misconfiguration is the failure mode this closes.
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Literal, get_args

import httpx

from cyris.config import GRADE_D_KEYS, Config
from cyris.domain.models import Lane

Status = Literal["ok", "warn", "fail", "skip"]


@dataclass(frozen=True)
class Check:
    name: str
    status: Status
    detail: str
    fix: str = ""


def _writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".cyris-doctor-probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError:
        return False
    return True


def _check_sources(cfg: Config) -> Check:
    if cfg.app.store.is_sql:
        home, fix = cfg.app.store.sources_home, "Add one on /settings, or run `cyris sources push`."
    else:
        home, fix = "sources.yaml", "Add feeds to sources.yaml (see sources.example.yaml)."
    if cfg.awaiting_onboarding():
        # Not a failure: a deployment ships with no source, and a run with none
        # ends quietly instead of stopping (`Config.awaiting_onboarding`). It is
        # still a warn, because nothing is published until a source is added.
        return Check("sources", "warn", f"none in {home} yet — waiting for onboarding", fix)
    feeds = [s for s in cfg.sources.values() if s.url and s.type == "rss"]
    email = [s for s in cfg.sources.values() if s.email_match]
    read = [s for s in cfg.sources.values() if s.lane == Lane.READ]
    if not feeds and not email:
        return Check("sources", "fail", "no usable sources", fix)
    return Check(
        "sources",
        "ok",
        f"{len(feeds)} RSS, {len(email)} email-only, {len(read)} on the read lane — from {home}",
    )


def _check_config_file(cfg: Config, config_path: Path | None) -> Check:
    """Where this run's file came from, so a silent-default host is visible."""
    if cfg.config_file_found:
        where = str(config_path) if config_path is not None else "cyris.toml"
        return Check("config file", "ok", where)
    if cfg.app.store.is_sql:
        return Check("config file", "ok", "not found — running from the environment")
    return Check(
        "config file",
        "warn",
        "not found",
        "Set CYRIS_STORE_BACKEND (and the other CYRIS_ keys), or place a cyris.toml here.",
    )


def _check_settings(cfg: Config) -> Check:
    """Every runtime setting is in this deployment's one home, or the run stops."""
    missing = ", ".join(cfg.missing_settings)
    if cfg.app.store.is_sql:
        home = cfg.app.store.settings_home
        if missing:
            return Check(
                "settings",
                "fail",
                f"missing in {home}: {missing}",
                "Set them on /settings, or run `cyris settings push`.",
            )
        return Check("settings", "ok", _settings_ok(cfg, home))
    if missing:
        return Check(
            "settings",
            "fail",
            f"missing from cyris.toml: {missing}",
            "cyris.toml.example lists every key.",
        )
    return Check("settings", "ok", _settings_ok(cfg, "cyris.toml"))


def _settings_ok(cfg: Config, home: str) -> str:
    defaulted = cfg.defaulted_settings()
    if not defaulted:
        return f"all {len(GRADE_D_KEYS)} set in {home}"
    return (
        f"{len(GRADE_D_KEYS) - len(defaulted)} of {len(GRADE_D_KEYS)} set in {home}; "
        f"default for {', '.join(sorted(defaulted))}"
    )


def _check_file_settings(cfg: Config, config_path: Path | None) -> list[Check]:
    """Runtime settings a SQL deployment's cyris.toml still sets, and so ignores.

    A value the deployment does not read, sitting where a reader would edit it,
    is the 2026-08-25 failure: the edit takes, and nothing changes.
    """
    if not cfg.app.store.is_sql or config_path is None or not config_path.exists():
        return []
    import tomllib

    with open(config_path, "rb") as f:
        loaded = tomllib.load(f)
    ignored = [
        f"[{table}] {field}"
        for table, field in (key.split(".", 1) for key in sorted(GRADE_D_KEYS))
        if isinstance(loaded.get(table), dict) and field in loaded[table]
    ]
    if not ignored:
        return [Check("file settings", "ok", "cyris.toml sets no runtime settings")]
    where = "D1" if cfg.app.store.is_d1 else "the SQLite store"
    return [
        Check(
            "file settings",
            "fail",
            f"this deployment reads these from {where} and ignores "
            f"cyris.toml's {', '.join(ignored)}",
            f"Delete them from cyris.toml. If {where} lacks any, run `cyris settings push` first.",
        )
    ]


def _check_llm(cfg: Config) -> Check:
    # A missing key is the settings check's failure; this one only says why it stops.
    missing = {"llm_provider.provider", "llm_provider.model"} & set(cfg.missing_settings)
    llm = cfg.app.llm_provider
    if missing or llm is None:
        return Check("llm provider", "skip", "not set — see settings")
    if llm.provider == "none":
        return Check("llm provider", "ok", "none — digests list plain excerpts, by choice")
    if llm.provider == "openai_compatible" and not llm.base_url:
        return Check(
            "llm provider",
            "fail",
            "provider is openai_compatible but OPENAI_COMPATIBLE_BASE_URL is empty",
            "Set OPENAI_COMPATIBLE_BASE_URL (and OPENAI_COMPATIBLE_MODEL) on this deployment.",
        )
    if not llm.api_key and llm.provider != "openai_compatible":
        hint = f"Put {llm.api_key_env_var} in .env."
        if llm.provider == "workers_ai":
            hint += " CLOUDFLARE_EMBEDDING_API_TOKEN also works: the same Workers AI "
            hint += "permission covers text models."
        where = "on /settings" if cfg.app.store.is_sql else "in [llm_provider]"
        hint += f' Or set the provider to "none" {where} for plain-excerpt digests.'
        return Check(
            "llm provider",
            "fail",
            f"provider is {llm.provider} but {llm.api_key_env_var} is empty",
            hint,
        )
    if llm.provider in ("workers_ai", "ai_gateway") and not llm.account_id:
        return Check(
            "llm provider",
            "fail",
            f"{llm.provider} has a token but no account id — its REST path is per-account",
            "Put CLOUDFLARE_ACCOUNT_ID in .env.",
        )
    return Check("llm provider", "ok", f"{llm.provider} · {llm.model or 'default model'}")


# OpenAIClient always asks for `json_object`, which OpenAI refuses unless the
# messages mention JSON.
LLM_PROBE_PROMPT = 'Reply with JSON: {"ok": true}'


def _check_vote_similarity(cfg: Config) -> Check:
    from cyris.bootstrap import embedding_defaults

    vote = cfg.app.vote_similarity
    if vote is None or any(k.startswith("vote_similarity.") for k in cfg.missing_settings):
        return Check("vote similarity", "skip", "not set — see settings")
    if vote.threshold is not None:
        return Check(
            "vote similarity",
            "warn",
            f"[vote_similarity] threshold = {vote.threshold:g} is ignored (ADR-0017)",
            "Remove the threshold line from cyris.toml; the cutoff is relative to each window.",
        )
    if not vote.enabled:
        return Check("vote similarity", "skip", "off")
    model = vote.model or embedding_defaults(vote.provider)["model"]
    return Check("vote similarity", "ok", f"{vote.provider} · {model}")


# The clients allow 120-180 s per attempt and retry twice, which a run can afford
# and a Save cannot. A probe answers within seconds; this bound still leaves room
# for one retry after a 429.
LLM_PROBE_TIMEOUT_SECONDS = 30


async def probe_llm(llm_cfg) -> Check:
    """Ask the provider whether this model actually answers, with a real call.

    `_check_llm` reads config and stops there, which is right for a check that
    must stay free. This one costs a few tokens, and buys the one failure that
    reading config cannot see: a model id that does not exist. A typo survives
    every static check and then 404s in the middle of a digest run, after the
    fetch has already happened — so anything that *writes* the provider config
    should call this before saving, not after.
    """
    from cyris.adapters.ai_gateway_client import AIGatewayError
    from cyris.adapters.gemini_client import GeminiAPIError, OutputBudgetSpentError
    from cyris.bootstrap import build_llm

    if llm_cfg.provider == "none":
        return Check("llm probe", "skip", "none — no model to call")
    llm = build_llm(llm_cfg)
    if llm is None:
        if llm_cfg.provider == "openai_compatible":
            # Its key is optional, so an unbuilt client is one of the two the
            # reader fills: the address, or the model it serves.
            missing = (
                "OPENAI_COMPATIBLE_BASE_URL" if not llm_cfg.base_url else "OPENAI_COMPATIBLE_MODEL"
            )
            return Check(
                "llm probe",
                "fail",
                f"openai_compatible could not be built — {missing} is empty",
                f"Set {missing} on this deployment, then save again.",
            )
        return Check(
            "llm probe",
            "fail",
            f"{llm_cfg.provider or 'no provider'} could not be built — "
            f"{llm_cfg.api_key_env_var} is empty"
            + (
                f" ({llm_cfg.provider} also needs CLOUDFLARE_ACCOUNT_ID)"
                if llm_cfg.provider in ("workers_ai", "ai_gateway")
                else ""
            ),
            f"Set {llm_cfg.api_key_env_var} on this deployment, then save again.",
        )
    refused = (
        "Check the model name, or leave it empty to use the provider's default, "
        f"and that {llm_cfg.api_key_env_var} is a working key. Then save again."
    )
    try:
        # 16 was not enough: a reasoning model can spend the entire budget
        # thinking and return an empty candidate, which reads as a broken model.
        await asyncio.wait_for(
            llm.complete(LLM_PROBE_PROMPT, max_tokens=128), LLM_PROBE_TIMEOUT_SECONDS
        )
    except TimeoutError:
        return Check(
            "llm probe",
            "fail",
            f"{llm.model} did not answer within {LLM_PROBE_TIMEOUT_SECONDS:g} s",
            "The provider may be slow or rate-limiting this key. Wait a minute and save again.",
        )
    except GeminiAPIError as e:
        detail = e.message.replace(LLM_PROBE_PROMPT, "[probe text redacted]")
        return Check(
            "llm probe",
            "fail",
            f"{llm.model} refused: code={e.code}, status={e.status}, message={detail}",
            refused,
        )
    except AIGatewayError as e:
        detail = e.message.replace(LLM_PROBE_PROMPT, "[probe text redacted]")
        who = {
            "gateway": f"AI Gateway refused the request for {llm.model}",
            "model": f"AI Gateway refused the model {llm.model}",
        }.get(e.side, f"AI Gateway returned an error for {llm.model}")
        fix = {
            "gateway": (
                "Store this model's provider key under Provider Keys in the gateway named "
                f"default (alias default), and check that {llm_cfg.api_key_env_var} carries "
                "AI Gateway Run. Then save again."
            ),
            "model": (
                "Check the model name against Google's model list, written google/<model>. "
                "Then save again."
            ),
        }.get(e.side, refused)
        return Check(
            "llm probe",
            "fail",
            f"{who}: HTTP {e.status}, code={e.code}, message={detail}",
            fix,
        )
    except OutputBudgetSpentError:
        return Check(
            "llm probe",
            "ok",
            f"{llm_cfg.provider} · {llm.model} accepted the request; "
            "thinking used the probe's output budget",
        )
    except Exception as e:  # noqa: BLE001 - the provider's own words are the answer
        return Check("llm probe", "fail", f"{llm.model} refused: {str(e)[:300]}", refused)
    return Check("llm probe", "ok", f"{llm_cfg.provider} · {llm.model} answered")


EMBEDDING_PROBE_TEXT = "cyris embedding probe"
# The embedders back off on 429 for about 90 s in total, which a run can afford
# and a Save cannot. This bound still leaves room for one short retry.
EMBEDDING_PROBE_TIMEOUT_SECONDS = 15


def _provider_message(response: httpx.Response) -> str:
    """The provider's own words from an error body: Gemini's `error`, Cloudflare's `errors`."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:300]
    if isinstance(body, dict):
        if isinstance(body.get("error"), dict) and body["error"].get("message"):
            return str(body["error"]["message"])
        errors = body.get("errors")
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            return str(errors[0].get("message", errors[0]))
    return response.text[:300]


async def probe_embedder(provider: str, model: str, base_url: str = "") -> Check:
    """Ask the embedding provider whether this model answers, with one real call.

    What `probe_llm` is for the LLM: a model id typo passes every static check.
    Never raises. The key is scrubbed from the detail before it can reach a page
    or a log — no longer because Gemini carried it in the URL query (it rides in
    `x-goog-api-key` since 2026-09-21), but because a provider is free to quote
    what it rejected back at us, and this detail is rendered on `/settings`.
    """
    from cyris.bootstrap import EMBEDDING_ENV, embedding_defaults, make_embedder

    model = model or embedding_defaults(provider)["model"]
    env = EMBEDDING_ENV[provider]
    # The compatible embedder's key is optional, like the LLM's.
    unset = [
        name
        for name in env
        if not os.environ.get(name) and not (provider == "openai_compatible" and name == env[0])
    ]
    if unset:
        return Check(
            "embedding probe",
            "fail",
            f"{unset[0]} is not set",
            f"Set {unset[0]} on this deployment, or save with vote similarity off.",
        )
    key = os.environ.get(env[0], "")
    try:
        embedder = make_embedder(provider, model, base_url)
    except ValueError as e:
        # The compatible embedder without its address: the one refusal that
        # is this factory's own, naming exactly the environment name.
        return Check(
            "embedding probe",
            "fail",
            str(e),
            "Set OPENAI_COMPATIBLE_EMBEDDING_BASE_URL, then save again.",
        )
    fix = (
        "Check the embedding model name, or leave it empty to use the provider's default. "
        "Then save again, or save with vote similarity off."
    )
    try:
        [vector] = await asyncio.wait_for(
            embedder.embed([EMBEDDING_PROBE_TEXT]), EMBEDDING_PROBE_TIMEOUT_SECONDS
        )
    except TimeoutError:
        detail = f"{model} did not answer within {EMBEDDING_PROBE_TIMEOUT_SECONDS:g} s"
        fix = (
            "The provider may be rate-limiting this key. Wait a minute and save again, "
            "or save with vote similarity off."
        )
    except httpx.HTTPStatusError as e:
        # The body first: the status line alone says nothing about which model.
        detail = f"{model} refused: {e.response.status_code} {_provider_message(e.response)}"
    except Exception as e:  # noqa: BLE001 - the provider's own words are the answer
        detail = f"{model} refused: {str(e)[:300] or type(e).__name__}"
    else:
        return Check(
            "embedding probe", "ok", f"{provider} · {model} answered ({len(vector)} dimensions)"
        )
    return Check("embedding probe", "fail", detail.replace(key, "[redacted]"), fix)


DISCORD_PROBE_TIMEOUT_SECONDS = 10


async def probe_discord(url: str, transport: httpx.AsyncBaseTransport | None = None) -> Check:
    """Ask Discord whether this webhook exists, without posting to the channel.

    A GET on a webhook URL returns the webhook's own metadata; only a POST writes
    a message. So the only way to tell a live webhook from a deleted one is worth
    nothing if the check itself spams the channel — this is what a writer should
    call before storing a URL. It never raises: a probe that throws is a worse
    diagnostic than one that reports.
    """
    from cyris.adapters.notify import is_masked_webhook_url, parse_discord_webhook_url

    if is_masked_webhook_url(url):
        return Check(
            "discord probe",
            "fail",
            "that is the masked value, not a webhook URL",
            "A stored webhook is shown with its token hidden. Paste the whole URL again.",
        )

    if parse_discord_webhook_url(url) is None:
        return Check(
            "discord probe",
            "fail",
            "not a Discord webhook URL",
            "Copy the URL from the channel's Integrations -> Webhooks page.",
        )

    try:
        async with httpx.AsyncClient(
            transport=transport, timeout=DISCORD_PROBE_TIMEOUT_SECONDS
        ) as client:
            resp = await client.get(url)
        # Read the body before judging the status: Discord puts its own words in
        # there, and they say more than the code does. A 4xx is never retried —
        # the URL is wrong, and asking again only takes longer to say so.
        try:
            body = resp.json()
        except ValueError:
            body = {"message": resp.text[:200]}
        # A JSON array or bare string parses fine and then has no `.get` — an
        # intercepting proxy is enough to produce one, and this function's whole
        # promise is that it reports rather than throws.
        if not isinstance(body, dict):
            body = {"message": resp.text[:200]}
    except Exception as e:  # noqa: BLE001 - the transport's own words are the answer
        return Check("discord probe", "fail", f"could not reach Discord: {e}")

    if resp.status_code == httpx.codes.OK:
        name = body.get("name") or body.get("id") or "an unnamed webhook"
        return Check("discord probe", "ok", f"Discord knows it as {name}")

    message = body.get("message") or "no reason given"
    return Check(
        "discord probe",
        "fail",
        f"Discord answered {resp.status_code}: {message}",
        "Check the webhook still exists on the channel's Integrations page.",
    )


EGRESS_TRACE_URL = "https://cloudflare.com/cdn-cgi/trace"
EGRESS_PROBE_TIMEOUT_SECONDS = 5


async def probe_egress(transport: httpx.AsyncBaseTransport | None = None) -> dict[str, str]:
    """Where this process's requests leave from, as Cloudflare's edge sees them.

    Providers refuse by location, so an LLM probe's verdict means little without
    it. Never raises: `{"error": ...}` is the answer when the trace is unreachable.
    """
    try:
        async with httpx.AsyncClient(
            transport=transport, timeout=EGRESS_PROBE_TIMEOUT_SECONDS
        ) as client:
            resp = await client.get(EGRESS_TRACE_URL)
        trace = dict(line.split("=", 1) for line in resp.text.splitlines() if "=" in line)
        return {"colo": trace.get("colo", ""), "loc": trace.get("loc", "")}
    except Exception as e:  # noqa: BLE001 - the transport's own words are the answer
        return {"error": str(e)[:200]}


def _check_paths(cfg: Config) -> list[Check]:
    # With D1 the vault has no writer left — articles are a table and spend goes
    # to `usage_log` — so there is nothing to check. Probing anyway would *create*
    # the directory it asks about: a local-filesystem edge the cloud target spent
    # M0–M4 removing, re-added by the health check itself.
    if cfg.app.store.is_d1:
        return []

    checks = []
    agent_vault = cfg.app.agent_vault.path
    if _writable(agent_vault):
        checks.append(Check("agent vault", "ok", str(agent_vault)))
    else:
        checks.append(Check("agent vault", "fail", f"{agent_vault} is not writable"))
    return checks


def _check_build(cfg: Config, config_path: Path | None) -> list[Check]:
    """Ask what *this build* understands, not what the config asks for.

    The 2026-08-25→27 split: `cyris.toml` gained `[store] backend = "d1"` while
    the container ran an image whose config model had no `[store]` at all.
    Pydantic ignores unknown keys, so the setting was silently dropped and every
    run for two days wrote to the JSON store while `doctor` reported green.
    A config key this build cannot see is a failure, not a stray comment.
    """
    from cyris.bootstrap import build_store
    from cyris.config import AppConfig

    checks: list[Check] = []
    if config_path is not None and config_path.exists():
        import tomllib

        with open(config_path, "rb") as f:
            loaded = tomllib.load(f)
        unknown = [f"[{t}]" for t in sorted(set(loaded) - set(AppConfig.model_fields))]
        # One level down as well. A table this build moved elsewhere — `notify`
        # left `[general]` for the top level — stays nested under a table name
        # that is still valid, so comparing only the top level reports green
        # while the setting is silently dropped. That is the 2026-08-25 failure
        # again, one nesting level lower.
        for table in sorted(set(loaded) & set(AppConfig.model_fields)):
            body = loaded[table]
            annotation = AppConfig.model_fields[table].annotation
            # A runtime-settings table is `Table | None`, so look inside the union.
            model = next(
                (t for t in (annotation, *get_args(annotation)) if hasattr(t, "model_fields")),
                None,
            )
            fields = getattr(model, "model_fields", None)
            if not isinstance(body, dict) or fields is None:
                continue
            unknown += [f"[{table}] {key}" for key in sorted(set(body) - set(fields))]
        if unknown:
            checks.append(
                Check(
                    "build",
                    "fail",
                    f"this build ignores {', '.join(unknown)}",
                    "The config is newer than the code. Rebuild the image, or delete the keys.",
                )
            )
        else:
            checks.append(Check("build", "ok", f"understands every table in {config_path}"))

    # What the wiring actually resolved to, so a mismatch is readable rather
    # than inferred from behaviour two days later. A store that cannot be built
    # at all is `_check_store`'s answer to give, not this one's.
    try:
        resolved = type(build_store(cfg)).__name__
    except Exception:  # noqa: BLE001 - reported by _check_store, with its detail
        return checks
    expected = "D1ArticleStore" if cfg.app.store.is_sql else "ArticleStore"
    checks.append(
        Check(
            "store wiring",
            "ok" if resolved == expected else "fail",
            f"[store] backend = {cfg.app.store.backend!r} → {resolved}",
            "" if resolved == expected else f"Expected {expected}. The build is out of date.",
        )
    )
    return checks


def _check_store(cfg: Config) -> Check:
    from cyris.bootstrap import build_store

    backend = cfg.app.store.backend
    sql = cfg.app.store.is_sql
    try:
        counts = build_store(cfg).count_by_state()
    except Exception as e:  # noqa: BLE001 - any failure here is the answer
        if cfg.app.store.is_d1:
            fix = "Check [store] and CLOUDFLARE_API_TOKEN."
        elif sql:
            fix = "Check [store] sqlite_path and that its directory is writable."
        else:
            fix = ""
        return Check(f"article store ({backend})", "fail", str(e), fix)
    total = sum(counts.values())
    summary = ", ".join(f"{state} {n}" for state, n in sorted(counts.items())) or "empty"
    if not total and sql:
        # The tables are created on the way in, so an empty store no longer
        # distinguishes a first boot from a `database_id` (or `sqlite_path`)
        # pointing at the wrong database — and the run after this one would
        # write there, orphaning the real store. All this check observes is the
        # row count, so that is all it says: a store emptied by
        # `articles clean` is also legitimately empty, and telling it its tables
        # are new would send it to change the one setting that causes the
        # orphaning.
        return Check(
            f"article store ({backend})",
            "warn",
            "0 articles — nothing in this database",
            "Expected on a first run. Otherwise check "
            + ("[store] database_id" if cfg.app.store.is_d1 else "[store] sqlite_path")
            + " before the next run writes here.",
        )
    return Check(f"article store ({backend})", "ok", f"{total} articles — {summary}")


def _check_feed_health(cfg: Config) -> Check:
    """Name each RSS feed that keeps failing or has stopped producing articles.

    Never a failure: the run still digests every other source, and a feed can be
    quiet for reasons of its own. Until this existed a failing feed was one
    `console.warn` an hour in Workers Logs, gone after 7 days.
    """
    from datetime import UTC, datetime

    from cyris import bootstrap
    from cyris.adapters.store.feed_health import D1FeedHealth

    name = "feed health"
    try:
        client = bootstrap.build_d1_client(cfg)
        if client is None:
            return Check(name, "skip", "feed health lives only in D1, and this store is json")
        feeds = D1FeedHealth(client).read()
    except Exception as e:  # noqa: BLE001 - unreadable health is unknown, not broken
        return Check(name, "warn", f"could not read feed_health — {e}")
    if not feeds:
        return Check(name, "skip", "no RSS feed in D1 sources")
    now = datetime.now(UTC)
    unhealthy = {n: problems for n, h in feeds.items() if (problems := h.problems(now))}
    if not unhealthy:
        return Check(name, "ok", f"{len(feeds)} RSS feed(s), none failing or silent")
    listed = "; ".join(f"{n} ({', '.join(problems)})" for n, problems in unhealthy.items())
    return Check(
        name,
        "warn",
        f"{len(unhealthy)} of {len(feeds)} RSS feeds: {listed}",
        "Open the feed URL yourself; fix it or retire the source on /settings.",
    )


async def _check_workers(cfg: Config) -> list[Check]:
    checks: list[Check] = []

    if cfg.app.rss.worker_url and cfg.app.rss.token:
        from cyris.adapters.fetch.rss_worker_source import CloudflareRssSource

        source = CloudflareRssSource(cfg.app.rss.worker_url, cfg.app.rss.token)
        alive = await source.health_check()
        checks.append(
            Check(
                "rss buffer",
                "ok" if alive else "fail",
                cfg.app.rss.worker_url if alive else f"{cfg.app.rss.worker_url} did not answer",
                "" if alive else "Check the Worker is deployed and CYRIS_WORKER_TOKEN matches it.",
            )
        )
    elif cfg.app.rss.local_buffer and cfg.app.store.is_sql:
        from cyris import bootstrap
        from cyris.adapters.fetch.feed_buffer import STATUS

        try:
            row = bootstrap.build_d1_client(cfg).query(STATUS).rows[0]
        except Exception as e:  # noqa: BLE001 - a missing table is the answer
            checks.append(
                Check("rss buffer", "warn", f"local feed_buffer — {e}", "Run `cyris buffer`.")
            )
        else:
            if row["rows"]:
                checks.append(
                    Check(
                        "rss buffer",
                        "ok",
                        f"local feed_buffer: {row['rows']} entries, newest {row['newest']}",
                    )
                )
            else:
                checks.append(
                    Check(
                        "rss buffer",
                        "warn",
                        "local feed_buffer is empty",
                        "Run `cyris buffer` hourly (a run reads it, it does not poll).",
                    )
                )
    else:
        if cfg.app.store.is_d1:
            fix = (
                "A digest-time poll only sees each feed's current snapshot: measured, "
                "it found 95 of the 179 articles the buffer held. Deploy workers/rss/."
            )
        elif cfg.app.store.is_sql:
            fix = (
                "A digest-time poll only sees each feed's current snapshot: measured, "
                "it found 95 of the 179 articles the buffer held. Set "
                "`[rss] local_buffer = true` and run `cyris buffer` hourly."
            )
        else:
            fix = (
                "A digest-time poll only sees each feed's current snapshot: measured, "
                "it found 95 of the 179 articles the buffer held; the `sqlite` store "
                "has the local buffer to close it, and `json` this poll."
            )
        checks.append(
            Check(
                "rss buffer",
                "warn",
                "not configured — feeds are polled directly at digest time",
                fix,
            )
        )

    if cfg.app.newsletter.worker_url and cfg.app.newsletter.token:
        from cyris.adapters.fetch.newsletter_worker_source import CloudflareNewsletterSource

        source = CloudflareNewsletterSource(cfg.app.newsletter.worker_url, cfg.app.newsletter.token)
        alive = await source.health_check()
        checks.append(
            Check(
                "newsletter worker",
                "ok" if alive else "fail",
                cfg.app.newsletter.worker_url,
                "" if alive else "Check the Worker and CYRIS_WORKER_TOKEN.",
            )
        )
    else:
        checks.append(
            Check("newsletter worker", "skip", "not configured — email-only sources are off")
        )

    promote = cfg.app.promote
    if promote.worker_url and promote.token:
        checks.append(Check("promote worker", "ok", promote.worker_url))
    else:
        checks.append(Check("promote worker", "skip", "not configured — digest votes are off"))
    return checks


def _check_publish_token(cfg: Config) -> list[Check]:
    """Whether the digest can actually be published.

    The D1 token needs no check of its own: the article store check above runs a
    real query through it, which proves liveness and permission together. That is
    the standard here — ask the API the token is *for*, never a generic verify
    endpoint, which answers only for user tokens and calls a working
    account-owned token invalid.
    """
    if not cfg.app.promote.publish_enabled:
        return [Check("publishing", "skip", "disabled — the digest stays local")]

    from cyris.adapters.cloudflare import check_pages_access

    token = os.environ.get("CLOUDFLARE_API_TOKEN", "")
    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
    if not token:
        return [
            Check(
                "publishing",
                "fail",
                "no token",
                "Put a Pages-capable token in .env as CLOUDFLARE_API_TOKEN.",
            )
        ]
    if not account_id:
        return [Check("publishing", "fail", "CLOUDFLARE_ACCOUNT_ID is not set", "Put it in .env.")]

    ok, message = check_pages_access(account_id, cfg.app.promote.pages_project, token)
    return [
        Check(
            "publishing",
            "ok" if ok else "fail",
            message,
            "" if ok else "The token needs the Cloudflare Pages permission at Edit level.",
        )
    ]


def _check_digest_link(cfg: Config) -> Check:
    """Which host the Discord digest link will name, asked the way a run asks."""
    name = "digest link"
    if cfg.app.promote.custom_domain:
        return Check(name, "ok", f"{cfg.app.promote.custom_domain}, set explicitly")

    from cyris import bootstrap

    lookup = bootstrap.build_worker_domains()
    if lookup is None:
        return Check(name, "skip", "no app Worker to ask — CYRIS_APP_WORKER_NAME is unset")
    try:
        hosts = lookup()
    except Exception as e:  # noqa: BLE001 - every failure leaves the link on pages.dev
        return Check(
            name,
            "warn",
            f"links go to pages.dev: could not list the app Worker's domains — {e}",
            "Give CLOUDFLARE_API_TOKEN the Workers Scripts Read permission.",
        )
    if not hosts:
        return Check(
            name,
            "warn",
            "links go to pages.dev, where no vote buttons render: the app Worker has no "
            "custom domain",
        )
    return Check(name, "ok", f"{hosts[0]}, the app Worker's custom domain")


_ACCESS_TOKEN_VARS = ("CYRIS_ACCESS_CLIENT_ID", "CYRIS_ACCESS_CLIENT_SECRET")
_HEADER_VALUE = re.compile(r"[\x20-\x7e]+")


def _access_token(name: str) -> str:
    """The service-token value as sent: a pasted trailing space or newline is not part of it."""
    return os.environ.get(name, "").strip()


class _EndpointAbsentError(RuntimeError):
    """The deployment answered, and has no `/api/build`.

    Not the same failure as an unreachable host: the sign-in succeeded, so the
    deployment is alive and reachable — it is simply older than the endpoint,
    which is itself a date for it. Observed on 2026-09-15 against the image
    production had been running since 2026-09-14.
    """


class _ServiceTokenRefusedError(RuntimeError):
    """Access or the Worker turned the service token away before the container.

    Each status names a different layer, so each gets its own fix. The redirect
    is never followed: its Location belongs to Access, not to this deployment.
    """

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"/api/build answered {status}")
        self.status = status
        self.detail = detail


def _refusal_detail(response: httpx.Response) -> str:
    """The Worker's JSON `detail`, when the refusal carries one; "" otherwise."""
    try:
        body = response.json()
    except ValueError:
        return ""
    detail = body.get("detail") if isinstance(body, dict) else None
    return detail if isinstance(detail, str) else ""


def _refusal_fix(status: int, detail: str) -> str:
    if status == 302:
        return (
            "Access redirected the request to its login page: the service token is "
            "missing, or no Service Auth policy allows it on this application."
        )
    if status == 401:
        return (
            "Access refused the token: it is expired, or it does not match the Access "
            "application. Issue a new service token and update .env."
        )
    suffix = f" The Worker said: {detail}" if detail else ""
    return (
        "The Worker refused the Access JWT. Check CYRIS_ACCESS_TEAM_DOMAIN and "
        f"CYRIS_ACCESS_AUD on the deployment against the Access application.{suffix}"
    )


def _git(*args: str) -> str | None:
    """Run git in the working tree; None when there is no answer to be had."""
    import subprocess

    try:
        done = subprocess.run(
            ["git", *args], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


async def _fetch_build_sha(base: str) -> str:
    """Ask the deployment which image it starts, through Access with the service token.

    Every request wakes the `ui` instance, so this answers on demand rather than
    at the next cron hour — which is what keeps a deploy that never landed
    distinguishable from an hour that had nothing to fetch.
    """
    import httpx

    headers = {
        "CF-Access-Client-Id": _access_token("CYRIS_ACCESS_CLIENT_ID"),
        "CF-Access-Client-Secret": _access_token("CYRIS_ACCESS_CLIENT_SECRET"),
    }
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
        build = await client.get(f"{base}/api/build", headers=headers)
        if build.status_code == 404:
            raise _EndpointAbsentError(base)
        if build.status_code in (302, 401, 403):
            raise _ServiceTokenRefusedError(build.status_code, _refusal_detail(build))
        if build.status_code != 200:
            raise RuntimeError(f"/api/build answered {build.status_code}")
        return str(build.json().get("git_sha", "")).strip()


def _compare_build_sha(where: str, online: str) -> Check:
    """The comparison is the point: this command on a laptop reports the laptop."""
    name = "deployment image"
    if not online:
        return Check(
            name,
            "fail",
            f"{where} runs an image that cannot name its commit",
            "It was built without --build-arg GIT_SHA, so nothing can date what "
            "production runs. Release through .github/workflows/release-image.yml.",
        )
    short = online[:7]
    local = _git("rev-parse", "HEAD")
    if local is None:
        return Check(name, "ok", f"{where} runs {short} — no checkout here to compare it against")
    if local == online:
        return Check(name, "ok", f"{where} runs {short}, the commit this checkout is on")
    if _git("cat-file", "-e", f"{online}^{{commit}}") is None:
        return Check(
            name,
            "fail",
            f"{where} runs {short}, a commit this checkout does not have",
            "Fetch it, or check the deployment is built from this repository.",
        )
    ahead = _git("rev-list", "--count", f"{online}..HEAD") or "0"
    behind = _git("rev-list", "--count", f"HEAD..{online}") or "0"

    def commits(n: str) -> str:
        return f"{n} commit" if n == "1" else f"{n} commits"

    if behind != "0" and ahead != "0":
        detail = f"{where} runs {short} — diverged from HEAD by {ahead} and {behind} commits"
    elif behind != "0":
        detail = f"{where} runs {short}, {commits(behind)} ahead of HEAD"
    else:
        detail = f"{where} runs {short} — HEAD is {commits(ahead)} ahead of it"
    # Not a failure: a checkout ahead of production is the ordinary state of any
    # working day, and a check that is red every day is a check nobody reads.
    return Check(name, "warn", detail)


async def _check_deployment(url: str) -> Check:
    check, _online = await _deployment(url)
    return check


async def _deployment(url: str) -> tuple[Check, str]:
    """The image verdict, plus the sha it was judged on ("" when none was read)."""
    base = url.rstrip("/")
    # Before any request: a missing token is a fault in this machine's .env, and
    # answering it with the URL hint below would send the reader elsewhere.
    missing = [v for v in _ACCESS_TOKEN_VARS if not _access_token(v)]
    if missing:
        names = " and ".join(missing)
        return Check(
            "deployment image",
            "fail",
            f"cannot send a service token to {base} — {names} not set here",
            f"Put the Access service token in .env as {names}.",
        ), ""
    # Named by variable only: the value is what is wrong, and it must not be echoed.
    invalid = [v for v in _ACCESS_TOKEN_VARS if not _HEADER_VALUE.fullmatch(_access_token(v))]
    if invalid:
        names = " and ".join(invalid)
        return Check(
            "deployment image",
            "fail",
            f"cannot send a service token to {base} — {names} is not a single line of text",
            f"Copy the Access service token into .env again as {names}, on one line.",
        ), ""
    try:
        online = await _fetch_build_sha(base)
    except _EndpointAbsentError:
        # The URL hint below would be a wrong cause: this deployment answered us,
        # so the host is right and the image is simply older than the endpoint.
        # Say that, since it is the closest thing to a date it has.
        return Check(
            "deployment image",
            "fail",
            f"{base} signed in but serves no /api/build — that image predates the endpoint",
            "Deploy a build that carries it; until then production cannot name its commit.",
        ), ""
    except _ServiceTokenRefusedError as e:
        return Check(
            "deployment image",
            "fail",
            f"{base} refused the service token — {HTTPStatus(e.status).phrase}",
            _refusal_fix(e.status, e.detail),
        ), ""
    except httpx.LocalProtocolError:
        # Its message can carry the header value it refused, so it is never shown.
        return Check(
            "deployment image",
            "fail",
            f"{base} did not answer — the request could not be sent",
            "Check CYRIS_ACCESS_CLIENT_ID and CYRIS_ACCESS_CLIENT_SECRET in .env.",
        ), ""
    except Exception as e:  # noqa: BLE001 - every other failure is the same answer: unknown
        return Check(
            "deployment image",
            "fail",
            f"{base} did not answer — {e}",
            f"Check that {base} is the deployment's URL and that this machine can reach it.",
        ), ""
    return _compare_build_sha(base, online), online


def _check_last_run(cfg: Config, online_sha: str) -> Check:
    """Which sha production's last real run executed, beside the one it now starts.

    Never a failure: the run's sha lags a deploy until the next cron hour, so a
    mismatch is the ordinary state right after a release, not a broken deploy.
    """
    from cyris import bootstrap
    from cyris.adapters.store.runs import D1RunLog

    name = "last run"
    try:
        client = bootstrap.build_d1_client(cfg)
        if client is None:
            return Check(name, "skip", "run history lives only in D1, and this store is json")
        last = D1RunLog(client, "").last()
    except Exception as e:  # noqa: BLE001 - an unreadable history is unknown, not broken
        return Check(name, "warn", f"could not read digest_runs — {e}")
    if last is None:
        return Check(name, "skip", "no run recorded in this D1 yet")
    sha = last["build_sha"]
    detail = f"last run {sha[:7] or 'no sha'} at {last['finished_at']} ({last['status']})"
    if not sha:
        return Check(name, "warn", f"{detail} — the image that ran could not name its commit")
    if not online_sha:
        return Check(name, "warn", f"{detail} — the deployment's sha is unknown")
    if sha != online_sha:
        return Check(name, "warn", f"{detail} — deployment starts {online_sha[:7]}")
    return Check(name, "ok", detail)


def _check_output_sink(cfg: Config) -> Check:
    """Does the digest reach anywhere a person can read it?

    A run with neither sink still fetches, scores, summarizes and reports
    `status: ok` — and leaves nothing but store rows. Nothing downstream fails,
    which is why this belongs here: it is the failure a run cannot report.
    """
    if cfg.app.html_output.enabled:
        where = "published to Pages" if cfg.app.promote.publish_enabled else "written locally"
        return Check("digest output", "ok", where)
    # Not an either/or: `build_deps` constructs the publisher *inside* the
    # `html_output.enabled` branch, and `run_digest` gates the whole output block
    # on the writer existing. `publish_enabled` alone publishes nothing.
    return Check(
        "digest output",
        "fail",
        "nowhere — the run would finish ok and leave no digest",
        "Set [html_output] enabled = true. Publishing is built on top of it, so "
        "[promote] publish_enabled has no effect while it is false.",
    )


def _check_notifications(cfg: Config) -> Check:
    # Missing is the settings check's to report; reading the table first would
    # judge a value nobody set. Any missing notify key leaves the whole table None.
    if cfg.app.notify is None or "notify.discord_webhook_url" in cfg.missing_settings:
        return Check("discord", "skip", "not set — see settings")
    if cfg.app.notify.discord_webhook_url:
        return Check("discord", "ok", "webhook set")
    return Check(
        "discord",
        "skip",
        "off — runs finish without a message",
        "Set one on /settings to get a message per digest.",
    )


def _check_mail(cfg: Config) -> Check:
    if cfg.app.notify is None or "notify.email_to" in cfg.missing_settings:
        return Check("mail", "skip", "not set — see settings")
    notify = cfg.app.notify
    if not notify.email_to:
        return Check("mail", "skip", "off — runs send no mail")
    if not (os.environ.get("CLOUDFLARE_ACCOUNT_ID") and os.environ.get("CLOUDFLARE_API_TOKEN")):
        return Check(
            "mail",
            "warn",
            "an address is set, but CLOUDFLARE_ACCOUNT_ID or CLOUDFLARE_API_TOKEN is missing",
            "Set both; the token needs Email Sending: Edit.",
        )
    return Check("mail", "ok", f"to {notify.email_to} from {notify.email_from}")


async def run_checks(
    cfg: Config, config_path: Path | None = None, deployment_url: str = ""
) -> list[Check]:
    """Every check, in the order a reader would want to see them."""
    checks = [
        *_check_build(cfg, config_path),
        _check_config_file(cfg, config_path),
        _check_sources(cfg),
        _check_settings(cfg),
        *_check_file_settings(cfg, config_path),
        _check_llm(cfg),
        _check_vote_similarity(cfg),
        *_check_paths(cfg),
        _check_store(cfg),
        _check_feed_health(cfg),
    ]
    checks.extend(await _check_workers(cfg))
    checks.extend(_check_publish_token(cfg))
    checks.append(_check_output_sink(cfg))
    checks.append(_check_notifications(cfg))
    checks.append(_check_mail(cfg))
    checks.append(_check_digest_link(cfg))
    if deployment_url:
        image, online = await _deployment(deployment_url)
        checks.append(image)
        checks.append(_check_last_run(cfg, online))
    else:
        checks.append(
            Check(
                "deployment image",
                "skip",
                "not asked — this report is about the machine it ran on",
                "Pass --deployment https://<worker>.workers.dev to date the image "
                "production runs against this checkout.",
            )
        )
        checks.append(Check("last run", "skip", "not asked — pass --deployment to compare"))
    return checks
