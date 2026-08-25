"""Check a deployment before it has to prove itself at 08:00.

Everything here is read-only and cheap. The reason it exists: a Cloudflare API
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


def _check_paths(cfg: Config) -> list[Check]:
    vault = cfg.app.obsidian.user_vault_path
    digests = vault / cfg.app.obsidian.digest_folder
    checks = []
    if not vault.exists():
        checks.append(
            Check(
                "obsidian vault",
                "fail",
                f"{vault} does not exist",
                "Point [obsidian] user_vault_path at your vault, or CYRIS_VAULT_PATH.",
            )
        )
    elif not _writable(digests):
        checks.append(Check("obsidian vault", "fail", f"{digests} is not writable"))
    else:
        checks.append(Check("obsidian vault", "ok", str(digests)))

    agent_vault = cfg.app.agent_vault.path
    if _writable(agent_vault):
        checks.append(Check("agent vault", "ok", str(agent_vault)))
    else:
        checks.append(Check("agent vault", "fail", f"{agent_vault} is not writable"))
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
            "Check [store] and CYRIS_D1_API_TOKEN." if cfg.app.store.is_d1 else "",
        )
    total = sum(counts.values())
    summary = ", ".join(f"{state} {n}" for state, n in sorted(counts.items())) or "empty"
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
                "" if alive else "Check the Worker is deployed and CYRIS_RSS_TOKEN matches it.",
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
                "" if alive else "Check the Worker and CYRIS_NEWSLETTER_TOKEN.",
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


async def run_checks(cfg: Config) -> list[Check]:
    """Every check, in the order a reader would want to see them."""
    checks = [_check_sources(cfg), _check_llm(cfg), *_check_paths(cfg), _check_store(cfg)]
    checks.extend(await _check_workers(cfg))
    checks.extend(_check_publish_token(cfg))
    checks.append(_check_notifications(cfg))
    return checks
