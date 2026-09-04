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

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from cyris.config import Config
from cyris.domain.models import Tier

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
    feeds = [s for s in cfg.sources.values() if s.url and s.type == "rss"]
    email = [s for s in cfg.sources.values() if s.email_match]
    summarize = [s for s in cfg.sources.values() if s.tier == Tier.SUMMARIZE]
    detail = (
        f"{len(feeds)} RSS, {len(email)} email-only, {len(summarize)} on the summarize tier "
        f"— from {cfg.sources_origin}"
    )
    if not feeds and not email:
        return Check(
            "sources",
            "fail",
            "no usable sources",
            "Add feeds to sources.yaml (see sources.example.yaml).",
        )
    if cfg.app.store.is_d1 and cfg.sources_origin != "d1":
        return Check(
            "sources",
            "warn",
            detail,
            "The store is on D1 but the sources table is empty or unreachable, so the "
            "RSS Worker is polling its bundled snapshot. Run `cyris sources push`.",
        )
    return Check("sources", "ok", detail)


def _check_config_file(cfg: Config, config_path: Path | None) -> Check:
    """Where this run's file came from, so a silent-default host is visible."""
    if cfg.config_file_found:
        where = str(config_path) if config_path is not None else "cyris.toml"
        return Check("config file", "ok", where)
    if cfg.app.store.is_d1:
        return Check("config file", "ok", "not found — running from the environment")
    return Check(
        "config file",
        "warn",
        "not found — running on baked defaults",
        "Set CYRIS_STORE_BACKEND (and the other CYRIS_ keys), or place a cyris.toml here.",
    )


def _check_settings_origin(cfg: Config) -> Check:
    """Which home the grade-D keys came from, so a split is visible on sight."""
    if not cfg.settings_from_d1:
        home = "defaults" if not cfg.config_file_found else "cyris.toml"
        return Check("settings", "ok", f"{home} — D1 holds no overrides")
    return Check("settings", "ok", f"D1 overrides {', '.join(sorted(cfg.settings_from_d1))}")


def _check_llm(cfg: Config) -> Check:
    llm = cfg.app.llm_provider
    if not llm.provider:
        return Check(
            "llm provider",
            "warn",
            "not configured — the digest will fall back to plain excerpts",
            'Set [llm_provider] provider to "anthropic", "gemini", "openai" or '
            '"workers_ai" in cyris.toml.',
        )
    if not llm.api_key:
        hint = f"Put {llm.api_key_env_var} in .env."
        if llm.provider == "workers_ai":
            hint += " CLOUDFLARE_EMBEDDING_API_TOKEN also works: the same Workers AI "
            hint += "permission covers text models."
        return Check(
            "llm provider",
            "fail",
            f"provider is {llm.provider} but {llm.api_key_env_var} is empty",
            hint,
        )
    if llm.provider == "workers_ai" and not llm.account_id:
        return Check(
            "llm provider",
            "fail",
            "workers_ai has a token but no account id — its REST path is per-account",
            "Put CLOUDFLARE_ACCOUNT_ID in .env.",
        )
    return Check("llm provider", "ok", f"{llm.provider} · {llm.model or 'default model'}")


async def probe_llm(llm_cfg) -> Check:
    """Ask the provider whether this model actually answers, with a real call.

    `_check_llm` reads config and stops there, which is right for a check that
    must stay free. This one costs a few tokens, and buys the one failure that
    reading config cannot see: a model id that does not exist. A typo survives
    every static check and then 404s in the middle of a digest run, after the
    fetch has already happened — so anything that *writes* the provider config
    should call this before saving, not after.
    """
    from cyris.bootstrap import build_llm

    llm = build_llm(llm_cfg)
    if llm is None:
        return Check(
            "llm probe",
            "fail",
            f"{llm_cfg.provider or 'no provider'} could not be built — "
            f"{llm_cfg.api_key_env_var} is empty"
            + (
                " (workers_ai also needs CLOUDFLARE_ACCOUNT_ID)"
                if llm_cfg.provider == "workers_ai"
                else ""
            ),
        )
    try:
        # 16 was not enough: a reasoning model can spend the entire budget
        # thinking and return an empty candidate, which reads as a broken model.
        await llm.complete("ping", max_tokens=128)
    except Exception as e:  # noqa: BLE001 - the provider's own words are the answer
        return Check("llm probe", "fail", f"{llm.model} refused: {str(e)[:300]}")
    return Check("llm probe", "ok", f"{llm_cfg.provider} · {llm.model} answered")


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
            tables = set(tomllib.load(f))
        unknown = sorted(tables - set(AppConfig.model_fields))
        if unknown:
            checks.append(
                Check(
                    "build",
                    "fail",
                    f"this build ignores {', '.join('[' + t + ']' for t in unknown)}",
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
    expected = "D1ArticleStore" if cfg.app.store.is_d1 else "ArticleStore"
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
    try:
        counts = build_store(cfg).count_by_state()
    except Exception as e:  # noqa: BLE001 - any failure here is the answer
        return Check(
            f"article store ({backend})",
            "fail",
            str(e),
            "Check [store] and CLOUDFLARE_API_TOKEN." if cfg.app.store.is_d1 else "",
        )
    total = sum(counts.values())
    summary = ", ".join(f"{state} {n}" for state, n in sorted(counts.items())) or "empty"
    if not total and cfg.app.store.is_d1:
        # The tables are created on the way in, so an empty store no longer
        # distinguishes a first boot from a `database_id` pointing at the wrong
        # database — and the run after this one would write there, orphaning the
        # real store. Say it once rather than let the reader find out from a
        # digest with no history.
        return Check(
            f"article store ({backend})",
            "warn",
            "0 articles — this database was empty and its tables were just created",
            "Expected on a first deploy. If not, check [store] database_id.",
        )
    return Check(f"article store ({backend})", "ok", f"{total} articles — {summary}")


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
    else:
        checks.append(
            Check(
                "rss buffer",
                "warn",
                "not configured — feeds are polled directly at digest time",
                "A digest-time poll only sees each feed's current snapshot: measured, "
                "it found 95 of the 179 articles the buffer held. Deploy workers/rss/.",
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


def _check_notifications(cfg: Config) -> Check:
    if cfg.app.general.notify.discord_webhook_url:
        return Check("discord", "ok", "webhook configured")
    return Check(
        "discord",
        "skip",
        "no webhook — runs finish silently",
        "Set CYRIS_DISCORD_WEBHOOK_URL to get a message per digest.",
    )


async def run_checks(cfg: Config, config_path: Path | None = None) -> list[Check]:
    """Every check, in the order a reader would want to see them."""
    checks = [
        *_check_build(cfg, config_path),
        _check_config_file(cfg, config_path),
        _check_sources(cfg),
        _check_settings_origin(cfg),
        _check_llm(cfg),
        *_check_paths(cfg),
        _check_store(cfg),
    ]
    checks.extend(await _check_workers(cfg))
    checks.extend(_check_publish_token(cfg))
    checks.append(_check_notifications(cfg))
    return checks
