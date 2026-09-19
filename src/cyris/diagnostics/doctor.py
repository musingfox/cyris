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

import httpx

from cyris.config import GRADE_D_KEYS, Config
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
    if cfg.app.store.is_d1:
        home, fix = "D1", "Add one on /settings, or run `cyris sources push`."
        if not cfg.sources:
            return Check("sources", "fail", "no sources in D1", fix)
    else:
        home, fix = "sources.yaml", "Add feeds to sources.yaml (see sources.example.yaml)."
    feeds = [s for s in cfg.sources.values() if s.url and s.type == "rss"]
    email = [s for s in cfg.sources.values() if s.email_match]
    summarize = [s for s in cfg.sources.values() if s.tier == Tier.SUMMARIZE]
    if not feeds and not email:
        return Check("sources", "fail", "no usable sources", fix)
    return Check(
        "sources",
        "ok",
        f"{len(feeds)} RSS, {len(email)} email-only, {len(summarize)} on the summarize tier "
        f"— from {home}",
    )


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
        "not found",
        "Set CYRIS_STORE_BACKEND (and the other CYRIS_ keys), or place a cyris.toml here.",
    )


def _check_settings(cfg: Config) -> Check:
    """Every runtime setting is in this deployment's one home, or the run stops."""
    missing = ", ".join(cfg.missing_settings)
    if cfg.app.store.is_d1:
        if missing:
            return Check(
                "settings",
                "fail",
                f"missing in D1: {missing}",
                "Set them on /settings, or run `cyris settings push`.",
            )
        return Check("settings", "ok", f"all {len(GRADE_D_KEYS)} set in D1")
    if missing:
        return Check(
            "settings",
            "fail",
            f"missing from cyris.toml: {missing}",
            "cyris.toml.example lists every key.",
        )
    return Check("settings", "ok", f"all {len(GRADE_D_KEYS)} set in cyris.toml")


def _check_file_settings(cfg: Config, config_path: Path | None) -> list[Check]:
    """Runtime settings a D1 deployment's cyris.toml still sets, and so ignores.

    A value the deployment does not read, sitting where a reader would edit it,
    is the 2026-08-25 failure: the edit takes, and nothing changes.
    """
    if not cfg.app.store.is_d1 or config_path is None or not config_path.exists():
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
    return [
        Check(
            "file settings",
            "fail",
            f"this deployment reads these from D1 and ignores cyris.toml's {', '.join(ignored)}",
            "Delete them from cyris.toml. If D1 lacks any, run `cyris settings push` first.",
        )
    ]


def _check_llm(cfg: Config) -> Check:
    # A missing key is the settings check's failure; this one only says why it stops.
    missing = {"llm_provider.provider", "llm_provider.model"} & set(cfg.missing_settings)
    if missing or not cfg.app.llm_provider.provider:
        return Check("llm provider", "skip", "not set — see settings")
    llm = cfg.app.llm_provider
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


# OpenAIClient always asks for `json_object`, which OpenAI refuses unless the
# messages mention JSON.
LLM_PROBE_PROMPT = 'Reply with JSON: {"ok": true}'


async def probe_llm(llm_cfg) -> Check:
    """Ask the provider whether this model actually answers, with a real call.

    `_check_llm` reads config and stops there, which is right for a check that
    must stay free. This one costs a few tokens, and buys the one failure that
    reading config cannot see: a model id that does not exist. A typo survives
    every static check and then 404s in the middle of a digest run, after the
    fetch has already happened — so anything that *writes* the provider config
    should call this before saving, not after.
    """
    from cyris.adapters.gemini_client import GeminiAPIError
    from cyris.bootstrap import build_llm

    if llm_cfg.provider == "none":
        return Check("llm probe", "skip", "none — no model to call")
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
        await llm.complete(LLM_PROBE_PROMPT, max_tokens=128)
    except GeminiAPIError as e:
        detail = e.message.replace(LLM_PROBE_PROMPT, "[probe text redacted]")
        return Check(
            "llm probe",
            "fail",
            f"{llm.model} refused: code={e.code}, status={e.status}, message={detail}",
        )
    except Exception as e:  # noqa: BLE001 - the provider's own words are the answer
        return Check("llm probe", "fail", f"{llm.model} refused: {str(e)[:300]}")
    return Check("llm probe", "ok", f"{llm_cfg.provider} · {llm.model} answered")


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
            fields = getattr(AppConfig.model_fields[table].annotation, "model_fields", None)
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
        # real store. All this check observes is the row count, so that is all it
        # says: a store emptied by `articles clean` is also legitimately empty,
        # and telling it its tables are new would send it to change the one
        # setting that causes the orphaning.
        return Check(
            f"article store ({backend})",
            "warn",
            "0 articles — nothing in this database",
            "Expected on a first deploy. Otherwise check [store] database_id "
            "before the next run writes here.",
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


# Access cannot cover *.workers.dev, which is exactly why it is the hostname to
# ask: on the custom domain the request meets an Access login this command has no
# way to pass, and the failure reads as a dead deployment.
_WORKERS_DEV_HINT = (
    "Use the deployment's workers.dev hostname — a custom domain sits behind "
    "Cloudflare Access, which this command cannot log in to."
)


class _EndpointAbsentError(RuntimeError):
    """The deployment answered, and has no `/api/build`.

    Not the same failure as an unreachable host: the sign-in succeeded, so the
    deployment is alive and reachable — it is simply older than the endpoint,
    which is itself a date for it. Observed on 2026-09-15 against the image
    production had been running since 2026-09-14.
    """


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
    """Sign in to the deployment and ask which image it starts.

    Every request wakes the `ui` instance, so this answers on demand rather than
    at the next cron hour — which is what keeps a deploy that never landed
    distinguishable from an hour that had nothing to fetch.
    """
    import httpx

    token = os.environ["CYRIS_UI_TOKEN"]
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
        login = await client.post(f"{base}/login", data={"token": token})
        if login.status_code != 302:
            raise RuntimeError(f"/login answered {login.status_code}, not a session")
        # The cookie the 302 set is in the client's jar; the Worker checks it
        # before anything reaches the container.
        build = await client.get(f"{base}/api/build")
        if build.status_code == 404:
            raise _EndpointAbsentError(base)
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
    # answering it with the hostname hint below would send the reader elsewhere.
    if not os.environ.get("CYRIS_UI_TOKEN"):
        return Check(
            "deployment image",
            "fail",
            f"cannot sign in to {base} — CYRIS_UI_TOKEN is not set here",
            "Put the deployment's UI token in .env as CYRIS_UI_TOKEN.",
        ), ""
    try:
        online = await _fetch_build_sha(base)
    except _EndpointAbsentError:
        # The hostname hint below would be a wrong cause: this deployment signed
        # us in, so the host is right and the image is simply older than the
        # endpoint. Say that, since it is the closest thing to a date it has.
        return Check(
            "deployment image",
            "fail",
            f"{base} signed in but serves no /api/build — that image predates the endpoint",
            "Deploy a build that carries it; until then production cannot name its commit.",
        ), ""
    except Exception as e:  # noqa: BLE001 - every other failure is the same answer: unknown
        return Check(
            "deployment image", "fail", f"{base} did not answer — {e}", _WORKERS_DEV_HINT
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
    # judge a value nobody set.
    if "notify.discord_webhook_url" in cfg.missing_settings:
        return Check("discord", "skip", "not set — see settings")
    if cfg.app.notify.discord_webhook_url:
        return Check("discord", "ok", "webhook set")
    return Check(
        "discord",
        "skip",
        "off — runs finish without a message",
        "Set one on /settings to get a message per digest.",
    )


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
        *_check_paths(cfg),
        _check_store(cfg),
    ]
    checks.extend(await _check_workers(cfg))
    checks.extend(_check_publish_token(cfg))
    checks.append(_check_output_sink(cfg))
    checks.append(_check_notifications(cfg))
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
