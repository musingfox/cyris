"""CLI entry point for Cyris."""

import asyncio
import contextlib
import json
import logging
import os
import re
import signal
import sys
from collections import Counter
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Annotated

import typer

from cyris.config import ReasoningEffort
from cyris.domain.triage import RejectReason

app = typer.Typer(help="Cyris — AI-powered information digest agent", invoke_without_command=True)
logger = logging.getLogger("cyris")


@app.callback()
def main() -> None:
    """Cyris — AI-powered information digest agent."""


def _echo_d1_failure(error: Exception) -> None:
    """What to say when D1 itself is unusable — all three keys can land you here."""
    typer.echo(f"✗ d1 — {error}")
    typer.echo(
        "  Check [store] database_id, CLOUDFLARE_ACCOUNT_ID and "
        "CLOUDFLARE_API_TOKEN (needs D1 edit)."
    )


class _RedactCredentials(logging.Filter):
    """Mask a Discord webhook's posting token wherever a record carries it.

    Discord puts the token in the URL path, so unlike every other credential here
    it cannot be moved to a header — see `adapters/embedding.py`, which was the
    one that could. What is left is to catch it on the way out, and there are
    three ways it arrives: httpx's own `HTTP Request: POST <url>` line at INFO,
    an `HTTPStatusError` whose message embeds the URL, and the traceback
    `notify.send_discord` logs with `exc_info=True`. The last one never passes
    through the message, which is why `exc_text` is rewritten too.

    The container's stdout is Workers Logs for seven days.

    One path this does **not** cover, stated so it is not mistaken for covered:
    an exception that escapes a command is rendered by Typer's Rich traceback
    straight to stderr, never through logging. It prints source lines, not
    values — `pretty_exceptions_show_locals` is False by default (checked
    against typer 0.24.1) — so a runtime credential does not appear there
    today. Pinning that one keyword argument would not make the path safe
    either; what would is not letting the exception escape.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        from cyris.adapters.notify import redact_webhook_tokens

        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a logging bug must stay a logging bug
            # `%` arguments that do not match the format string. A filter runs in
            # `Handler.handle`, outside the try/except that wraps `emit`, so
            # raising here would abort a digest run over a wrong `%d` — where
            # before this class existed the same record only produced a note on
            # stderr. The record goes on to fail in `format` and reach
            # `Handler.handleError`, which prints `record.args` raw, so masking
            # the arguments is what keeps the swallow from becoming the leak.
            record.args = _redacted_args(record.args)
            return True

        if (masked := redact_webhook_tokens(message)) != message:
            record.msg, record.args = masked, ()
        if record.exc_info and record.exc_text is None:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact_webhook_tokens(record.exc_text)
        return True


def _redacted_args(args: object) -> object:
    """Mask every string in a record's `%` arguments, tuple or mapping."""
    from cyris.adapters.notify import redact_webhook_tokens

    mask = lambda value: redact_webhook_tokens(value) if isinstance(value, str) else value  # noqa: E731
    if isinstance(args, dict):
        return {key: mask(value) for key, value in args.items()}
    if isinstance(args, tuple):
        return tuple(mask(value) for value in args)
    return mask(args)


def _setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )
    # On the root handler, not on one logger: the leak is not httpx's to fix, and
    # the next library to log a URL would reopen it.
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, _RedactCredentials) for f in handler.filters):
            handler.addFilter(_RedactCredentials())


def _alert_could_not_start(cfg, error: Exception) -> None:
    """Alert the configured channels that a scheduled run stopped before `run_digest`.

    Only on a digest hour: the cron tick is hourly, and a broken configuration
    fails every tick. Settings that cannot say whether this hour is one, or
    where to send, alert nobody. Never raises: the run's own exit code stands.
    """
    from cyris import bootstrap
    from cyris.adapters import notify
    from cyris.service_layer.run_digest import failure_alert_text, send_alert
    from cyris.service_layer.schedule import due_period
    from cyris.utils.timezone import now_in_timezone

    try:
        tz = cfg.app.general.timezone
        now = now_in_timezone(tz)
        period = due_period(now, cfg.app.general.digest_schedule)
        if period is None:
            return
        subject = f"Digest run could not start: {period}, {now:%Y-%m-%d %H:%M} {tz}"
        asyncio.run(
            send_alert(
                cfg.app.notify,
                notify.send_discord_alert,
                bootstrap.build_send_email_alert(),
                subject,
                failure_alert_text(error),
            )
        )
    except Exception as e:
        logger.error("Could not send the alert that the run did not start: %s", e)


def _usage_error(message: str) -> None:
    typer.echo(message, err=True)
    raise typer.Exit(2)


def _is_issue_date(text: str) -> bool:
    """A real calendar date written YYYY-MM-DD, the shape an issue's key takes."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return False
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def _write_preview(path: Path, html: str) -> None:
    """The file a preview or a regeneration leaves, at the path the user named."""
    try:
        path.write_text(html, encoding="utf-8")
    except OSError as e:
        typer.echo(f"Could not write {path}: {e.strerror or e}", err=True)
        raise typer.Exit(1) from e


@app.command("run")
def run(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Preview without writing")] = False,
    output: Annotated[
        Path | None, typer.Option("--output", help="Dry run: write the preview page to PATH")
    ] = None,
    period: Annotated[str, typer.Option(help="Digest period: morning or evening")] = "morning",
    config_path: Annotated[Path, typer.Option("--config", help="Config file path")] = Path(
        "cyris.toml"
    ),
    sources_path: Annotated[Path, typer.Option("--sources", help="Sources file path")] = Path(
        "sources.yaml"
    ),
    force: Annotated[
        bool,
        typer.Option(
            "--force", help="Re-process all articles in time window regardless of current state"
        ),
    ] = False,
    if_due: Annotated[
        bool,
        typer.Option(
            "--if-due",
            help="Run only if this hour is on the digest schedule; picks --period from it",
        ),
    ] = False,
    date: Annotated[
        str | None,
        typer.Option(
            "--date",
            help="Issue date, YYYY-MM-DD; default today in the deployment's timezone",
        ),
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging")] = False,
) -> None:
    """Run the full pipeline: fetch, score, route, and digest."""
    if output is not None and not dry_run:
        _usage_error("--output is only available with --dry-run")
    if date is not None:
        # The tick dates its issue by the clock it fires on; naming another
        # date is a manual run's choice, such as the onboarding's first issue.
        if if_due:
            _usage_error("--date cannot be combined with --if-due")
        if not _is_issue_date(date):
            _usage_error(f"--date must be a real YYYY-MM-DD date, not {date!r}")
    _setup_logging(verbose)

    from cyris.bootstrap import build_deps, load_effective_config
    from cyris.service_layer.run_digest import RunOptions, RunReport, run_digest
    from cyris.service_layer.schedule import due_period
    from cyris.utils.timezone import now_in_timezone

    cfg = None
    try:
        cfg = load_effective_config(config_path, sources_path)
        cfg.validate_required_keys()
        cfg.require_complete_settings()
        cfg.require_sources()
    except (FileNotFoundError, ValueError) as e:
        logger.error("Configuration error: %s", e)
        if if_due and not dry_run and cfg is not None:
            _alert_could_not_start(cfg, e)
        raise typer.Exit(1) from e

    if cfg.awaiting_onboarding():
        # A new deployment has no source until onboarding picks some, and every
        # hourly tick would otherwise report the same failure. Ending here keeps
        # the exit status 0, builds no deps, calls no LLM and sends no alert.
        logger.info("%s", cfg.onboarding_wait())
        # The one caller that asked for a file gets nothing, as when not due.
        if output is not None:
            raise typer.Exit(1)
        return

    if if_due:
        # The schedule lives in D1 so changing it does not need a rebuild. The app
        # Worker's Cron Trigger asks the same question before waking the container
        # (workers/app/src/schedule.js), so a tick ends here only when the Worker
        # could not read the schedule, or on a `docker compose` install, whose
        # crontab is hourly and unconditional. Not due is a normal outcome.
        due = due_period(now_in_timezone(cfg.app.general.timezone), cfg.app.general.digest_schedule)
        if due is None:
            message = f"Not a digest hour ({', '.join(cfg.app.general.digest_schedule)})."
            if output is not None:
                typer.echo(message, err=True)
                raise typer.Exit(1)
            typer.echo(message)
            return
        period = due

    if output is not None:
        typer.echo(f"Store backend: {cfg.app.store.backend}")
    try:
        deps = build_deps(cfg, on_progress=typer.echo, dry_run=dry_run)
    except Exception as e:
        if if_due and not dry_run:
            _alert_could_not_start(cfg, e)
        raise
    options = RunOptions(period=period, dry_run=dry_run, force=force, date=date)

    # The Container stops a run with SIGTERM, whose default kills the process
    # without unwinding — `run_digest`'s `finally`, which records the run, would
    # never run. Cancelling the task unwinds it instead, and only at an await:
    # an exception raised from a signal handler could tear a sync D1 write,
    # the record in that `finally` included. 143 is 128 + SIGTERM, the status
    # a shell reports for the signal.
    async def _run() -> RunReport:
        loop = asyncio.get_running_loop()
        with contextlib.suppress(NotImplementedError):
            # Windows has no loop signal handlers, and nothing sends SIGTERM there.
            loop.add_signal_handler(signal.SIGTERM, asyncio.current_task().cancel)
        try:
            return await run_digest(deps, options)
        finally:
            with contextlib.suppress(NotImplementedError):
                loop.remove_signal_handler(signal.SIGTERM)

    try:
        report = asyncio.run(_run())
    except asyncio.CancelledError:
        raise typer.Exit(128 + signal.SIGTERM) from None
    if output is not None:
        if report.status != "ok" or not report.rendered:
            typer.echo(f"Nothing to preview: {report.status}", err=True)
            raise typer.Exit(1)
        _write_preview(output, report.rendered)
        typer.echo(f"Wrote {output}")
        return
    if report.rendered:
        typer.echo(report.rendered)


@app.command("buffer")
def buffer(
    config_path: Annotated[Path, typer.Option("--config", help="Config file path")] = Path(
        "cyris.toml"
    ),
    sources_path: Annotated[Path, typer.Option("--sources", help="Sources file path")] = Path(
        "sources.yaml"
    ),
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging")] = False,
) -> None:
    """Poll every feed into the local buffer a run then reads (`[rss] local_buffer`)."""
    _setup_logging(verbose)

    from cyris.bootstrap import build_feed_buffer, load_effective_config

    try:
        cfg = load_effective_config(config_path, sources_path)
        cfg.require_sources()
        feed_buffer = build_feed_buffer(cfg)
    except (FileNotFoundError, ValueError) as e:
        logger.error("Configuration error: %s", e)
        raise typer.Exit(1) from e
    if not (cfg.app.rss.local_buffer or cfg.app.store.is_d1):
        typer.echo(
            "Nothing reads this buffer: set [rss] local_buffer = true, or the run "
            "polls feeds at digest time.",
            err=True,
        )
    written, pruned = asyncio.run(feed_buffer.poll(cfg.sources, datetime.now(UTC)))
    typer.echo(f"{written} new entries, {pruned} pruned, {len(cfg.sources)} feeds")


@app.command("regenerate")
def regenerate(
    date: Annotated[str, typer.Argument(help="Issue date, YYYY-MM-DD")],
    period: Annotated[str, typer.Argument(help="morning or evening")],
    mode: Annotated[
        str, typer.Option("--mode", help="rerender: the stored page; rerun: the recorded inputs")
    ],
    output: Annotated[Path | None, typer.Option("--output", help="Write the page to PATH")] = None,
    publish: Annotated[
        bool, typer.Option("--publish", help="Rerun and publish over the live page")
    ] = False,
    config_path: Annotated[Path, typer.Option("--config", help="Config file path")] = Path(
        "cyris.toml"
    ),
    sources_path: Annotated[Path, typer.Option("--sources", help="Sources file path")] = Path(
        "sources.yaml"
    ),
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging")] = False,
) -> None:
    """Regenerate a stored digest: re-render its page, or rerun it over its recorded inputs."""
    from cyris.service_layer.schedule import PERIOD_ORDER

    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        _usage_error("DATE must be YYYY-MM-DD")
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        _usage_error("DATE must be YYYY-MM-DD")
    if period not in PERIOD_ORDER:
        _usage_error(f"PERIOD must be one of: {', '.join(PERIOD_ORDER)}")
    if mode not in ("rerender", "rerun"):
        _usage_error("MODE must be rerender or rerun")
    if (output is None) == (not publish):
        _usage_error("Give exactly one of --output PATH and --publish")
    if publish and mode == "rerender":
        _usage_error("--publish is only available with --mode rerun")

    _setup_logging(verbose)

    from cyris.bootstrap import build_deps, load_effective_config
    from cyris.service_layer.run_digest import RunOptions, rerender_digest, run_digest

    try:
        cfg = load_effective_config(config_path, sources_path)
    except (FileNotFoundError, ValueError) as e:
        logger.error("Configuration error: %s", e)
        raise typer.Exit(1) from e
    if not cfg.app.store.is_sql:
        # The stored page and the recorded inputs both live in the SQL store;
        # the json backend never wrote either.
        typer.echo(
            f"The store backend is {cfg.app.store.backend}; regenerate needs an SQL store",
            err=True,
        )
        raise typer.Exit(1)
    if not cfg.app.html_output.enabled:
        typer.echo("HTML output is disabled; regenerate needs [html_output] enabled", err=True)
        raise typer.Exit(1)

    rerun = mode == "rerun"
    deps = build_deps(cfg, on_progress=typer.echo if rerun else None, dry_run=True)
    if publish and deps.publish_site is None:
        typer.echo("Cannot publish: publishing is not configured", err=True)
        raise typer.Exit(1)

    if not rerun:
        report = rerender_digest(deps, date, period)
        if report.status == "not_found":
            typer.echo(f"No stored digest for {date} {period}", err=True)
            raise typer.Exit(1)
        _write_preview(output, report.rendered)
        typer.echo(f"Wrote {output}")
        return

    report = asyncio.run(
        run_digest(deps, RunOptions(period=period, dry_run=not publish, rerun_of=date))
    )
    if report.status == "no_inputs":
        typer.echo(f"No recorded inputs for {date} {period}", err=True)
        raise typer.Exit(1)
    if report.status == "not_latest":
        typer.echo(
            f"A later issue exists ({report.later[0]} {report.later[1]}); publishing is refused",
            err=True,
        )
        raise typer.Exit(1)
    if report.status == "publish_failed":
        typer.echo("Publish did not land: the live page differs from the render", err=True)
        raise typer.Exit(1)
    if report.status != "ok" or (not publish and not report.rendered):
        typer.echo(f"Nothing to regenerate: {report.status}", err=True)
        raise typer.Exit(1)
    if publish:
        typer.echo(f"Published {report.slug}")
        return
    _write_preview(output, report.rendered)
    typer.echo(f"Wrote {output}")


@app.command("promote-sync")
def promote_sync(
    config_path: Annotated[Path, typer.Option("--config", help="Config file path")] = Path(
        "cyris.toml"
    ),
    sources_path: Annotated[Path, typer.Option("--sources", help="Sources file path")] = Path(
        "sources.yaml"
    ),
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging")] = False,
) -> None:
    """Sync promote-button clicks from the cloud Worker into the article store (no fetch/LLM)."""
    _setup_logging(verbose)

    from cyris.bootstrap import build_promotion_sync, load_effective_config

    try:
        cfg = load_effective_config(config_path, sources_path)
    except (FileNotFoundError, ValueError) as e:
        logger.error("Configuration error: %s", e)
        raise typer.Exit(1) from e

    # Not through build_deps: this command starts on an empty D1, where the
    # runtime-settings tables build_deps reads do not exist yet.
    sync = build_promotion_sync(cfg)
    if sync is None:
        # The vote Worker is optional, and this command is the last one in the
        # container's `run` role — so its failure fails the tick. Not having
        # deployed one is a deployment shape, the way an hour that is not a
        # digest hour is a normal outcome of `run --if-due`, not a failure.
        typer.echo("Promotion sync not configured (set promote.worker_url + token).")
        return

    typer.echo(f"Synced {sync()} digest vote(s).")


@app.command("vote-sim")
def vote_sim(
    hours: Annotated[int, typer.Option("--hours", help="Window to judge")] = 24,
    show: Annotated[int, typer.Option("--show", help="Rows to print per side")] = 15,
    config_path: Annotated[Path, typer.Option("--config", help="Config file path")] = Path(
        "cyris.toml"
    ),
    sources_path: Annotated[Path, typer.Option("--sources", help="Sources file path")] = Path(
        "sources.yaml"
    ),
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging")] = False,
) -> None:
    """Preview what vote similarity would suppress, without running the pipeline.

    Read-only: judges the articles already in the store for the window and prints
    the diff, so the effect can be compared before `\\[vote_similarity] enabled`
    is turned on.
    """
    _setup_logging(verbose)

    from datetime import UTC, datetime, timedelta

    from cyris.bootstrap import build_deps, load_effective_config
    from cyris.domain.similarity import OUTLIER_MARGIN
    from cyris.service_layer.vote_similarity import judge_by_votes

    try:
        cfg = load_effective_config(config_path, sources_path)
        cfg.require_complete_settings()
    except (FileNotFoundError, ValueError) as e:
        logger.error("Configuration error: %s", e)
        raise typer.Exit(1) from e

    from cyris.bootstrap import build_embedder

    deps = build_deps(cfg)
    embedder = deps.embedder
    if embedder is None:
        # The preview has to work while the feature is still switched off — that
        # is the whole point of previewing it.
        cfg.app.vote_similarity.enabled = True
        embedder = build_embedder(cfg)
    now = datetime.now(UTC)
    candidates = deps.store.load_by_time_range(start=now - timedelta(hours=hours), end=now)

    report = asyncio.run(
        judge_by_votes(
            deps.store,
            embedder,
            candidates,
            max_seeds=cfg.app.vote_similarity.max_seeds,
        )
    )
    if not report.ran:
        typer.echo(f"Nothing to compare: {report.skipped_reason}")
        raise typer.Exit(1)

    by_url = {a.url: a for a in candidates}
    typer.echo(
        f"\n{len(candidates)} candidate(s) over {hours}h, judged against "
        f"{report.upvote_seeds} up / {report.downvote_seeds} down seed(s), "
        f"outlier margin {OUTLIER_MARGIN:g}\n"
    )
    typer.echo(f"WOULD SUPPRESS ({len(report.suppressed_urls)}):")
    for url in report.suppressed_urls[:show]:
        v = report.verdicts[url]
        a = by_url[url]
        typer.echo(f"  {v.down_similarity:.3f}  [{a.source_name[:18]:18}] {a.title[:52]}")
    if len(report.suppressed_urls) > show:
        typer.echo(f"  ... and {len(report.suppressed_urls) - show} more")

    ranked = sorted(report.verdicts.values(), key=lambda v: v.up_similarity, reverse=True)
    typer.echo(f"\nCLOSEST TO UPVOTES (top {show}):")
    for v in ranked[:show]:
        a = by_url[v.url]
        typer.echo(f"  {v.up_similarity:.3f}  [{a.source_name[:18]:18}] {a.title[:52]}")


@app.command("topic-sim")
def topic_sim(
    description: Annotated[
        str, typer.Argument(help="The topic's description, as it would be saved")
    ],
    hours: Annotated[int, typer.Option(help="Hours of stored articles to rank")] = 168,
    threshold: Annotated[
        float | None, typer.Option(help="Mark this cutoff in the list and count the hits above it")
    ] = None,
    show: Annotated[int, typer.Option(help="How many ranked titles to print")] = 60,
    config_path: Annotated[Path, typer.Option("--config", help="Config file path")] = Path(
        "cyris.toml"
    ),
    sources_path: Annotated[Path, typer.Option("--sources", help="Sources file path")] = Path(
        "sources.yaml"
    ),
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging")] = False,
) -> None:
    """Rank stored titles by cosine to a topic's description, to read its threshold off.

    Read-only, and no LLM call: it embeds the description and each title in the
    window once, with the embedder the next run uses, and prints them nearest
    first. A topic saved with the threshold you pick lists what sits above it.
    """
    _setup_logging(verbose)

    from datetime import UTC, datetime, timedelta

    from cyris.bootstrap import build_store, embedding_model, load_effective_config, make_embedder
    from cyris.service_layer.tracking import rank_titles

    try:
        cfg = load_effective_config(config_path, sources_path)
        cfg.require_complete_settings()
    except (FileNotFoundError, ValueError) as e:
        logger.error("Configuration error: %s", e)
        raise typer.Exit(1) from e

    vote = cfg.app.vote_similarity
    model = embedding_model(vote.provider, vote.model)
    now = datetime.now(UTC)
    candidates = build_store(cfg).load_by_time_range(start=now - timedelta(hours=hours), end=now)
    if not candidates:
        typer.echo(f"No stored article in the last {hours}h: nothing to rank.")
        raise typer.Exit(1)

    ranked = asyncio.run(
        rank_titles(
            make_embedder(vote.provider, vote.model, vote.base_url), description, candidates
        )
    )
    typer.echo(f"\n{len(ranked)} title(s) over {hours}h, by cosine to the description ({model})\n")
    cut_shown = threshold is None
    for article, score in ranked[:show]:
        if not cut_shown and score < threshold:
            typer.echo(f"  ---- {threshold:.2f} ----")
            cut_shown = True
        typer.echo(f"  {score:.3f}  [{article.source_name[:18]:18}] {article.title[:60]}")
    if len(ranked) > show:
        typer.echo(f"  ... and {len(ranked) - show} more")
    if threshold is not None:
        hit_count = sum(1 for _, score in ranked if score >= threshold)
        typer.echo(f"\nAt {threshold:.2f}: {hit_count} hit(s). Save the topic with model {model}.")


@app.command("embed-compare")
def embed_compare(
    hours: Annotated[int, typer.Option("--hours", help="Window to judge")] = 24,
    as_json: Annotated[
        bool,
        typer.Option(
            "--json", help="Emit the run as one JSON line on stdout; the report goes to stderr"
        ),
    ] = False,
    show: Annotated[int, typer.Option("--show", help="Disagreements to print")] = 15,
    config_path: Annotated[Path, typer.Option("--config", help="Config file path")] = Path(
        "cyris.toml"
    ),
    sources_path: Annotated[Path, typer.Option("--sources", help="Sources file path")] = Path(
        "sources.yaml"
    ),
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging")] = False,
) -> None:
    """Judge the same window with both embedding providers and report where they differ.

    The 2026-08-10 evaluation found zero disagreement across the whole store, but on a
    single wide-margin downvote class. This keeps the comparison running on real traffic
    and records what a one-off measurement cannot: cost and latency per provider.

    The per-arm disagreement key is `{arm}_only`, so a series started before
    2026-09-05 carries `workers_only` where this writes `workers_ai_only`: the
    name follows the arm now rather than being hardcoded. Nothing reads the old
    key — it was written by `--log`, removed in the same change — but a hand-kept
    file from before that date holds both spellings.

    Read-only, and it writes nothing: `--json >> parity.jsonl` is how a series is kept,
    which leaves where that file lives to whoever is keeping it.
    """
    _setup_logging(verbose)

    from datetime import UTC, datetime, timedelta

    from cyris.bootstrap import build_deps, load_effective_config
    from cyris.diagnostics.compare import (
        NothingToCompareError,
        build_embedding_arms,
        compare_embedders,
        margin,
    )

    try:
        # also loads .env into the environment
        cfg = load_effective_config(config_path, sources_path)
        cfg.require_complete_settings()
    except (FileNotFoundError, ValueError) as e:
        logger.error("Configuration error: %s", e)
        raise typer.Exit(1) from e

    deps = build_deps(cfg)
    now = datetime.now(UTC)
    candidates = deps.store.load_by_time_range(start=now - timedelta(hours=hours), end=now)
    try:
        arms = build_embedding_arms(
            account_id=os.environ.get("CLOUDFLARE_ACCOUNT_ID", ""),
            gemini_api_key=os.environ.get("GEMINI_API_KEY", ""),
            workers_api_token=os.environ.get("CLOUDFLARE_EMBEDDING_API_TOKEN", ""),
        )
        comparison = asyncio.run(
            compare_embedders(
                deps.store,
                arms,
                candidates,
                hours=hours,
                max_seeds=cfg.app.vote_similarity.max_seeds,
                checked_at=now,
            )
        )
    except NothingToCompareError as e:
        # stderr, always: with --json a redirected stdout is an append-only series,
        # and `>>` does not care that the run failed.
        typer.echo(f"Nothing to compare: {e}", err=True)
        raise typer.Exit(1) from e

    by_url = {a.url: a for a in candidates}
    first = comparison.arms[0].report
    # With --json stdout carries the row and nothing else, so the reader's report
    # goes to stderr and `>> parity.jsonl` stays a clean append.
    report = partial(typer.echo, err=as_json)
    report(
        f"\n{len(candidates)} candidate(s) over {hours}h, "
        f"{first.upvote_seeds} up / {first.downvote_seeds} down seed(s)\n"
    )
    for arm in comparison.arms:
        u = arm.usage.as_dict()
        m = margin(arm.report)
        cost = f", {u['input_tokens']} tokens, {u['neurons']} neurons" if u["neurons"] else ""
        report(
            f"  {arm.name:<11}  "
            f"suppresses {len(arm.report.suppressed_urls):>3}   "
            f"margin {m['kept_max']} -> {m['suppressed_min']}   "
            f"({u['embedded']} embedded in {u['requests']} req, "
            f"{arm.wall_seconds:.1f}s wall / {u['api_seconds']}s api{cost})"
        )
    only = comparison.only
    report(
        f"\n  agree on {len(comparison.agreed)}, "
        f"disagree on {sum(len(urls) for urls in only.values())}"
    )
    # Arm names come from the provider, so the label width is not a constant:
    # `workers_ai_only` is 15 and overflowed the 13 this used to pad to.
    width = max((len(label) for label in only), default=0)
    for label, urls in only.items():
        for url in urls[:show]:
            a = by_url[url]
            report(f"    {label:<{width}} [{a.source_name[:18]:18}] {a.title[:48]}")

    if as_json:
        typer.echo(json.dumps(comparison.log_row(), ensure_ascii=False))


def _build_arm(spec: str, reasoning_effort: ReasoningEffort):
    """Turn "provider:model" into a labelled LLM client, or explain why it cannot.

    Goes through the same `build_llm` the pipeline uses, so an arm picks up its
    key and account id from the environment exactly as a configured provider
    would, and a provider added later costs this command nothing. An empty model
    means that provider's default. The arm runs at the configured reasoning effort,
    so the comparison differs only by model.
    """
    from pydantic import ValidationError

    from cyris.bootstrap import build_llm
    from cyris.config import LLMProviderConfig

    provider, _, model = spec.partition(":")
    try:
        arm_cfg = LLMProviderConfig(
            provider=provider.strip(), model=model.strip(), reasoning_effort=reasoning_effort
        )
    except ValidationError as e:
        raise typer.BadParameter(f"--arm {spec!r}: unknown provider {provider.strip()!r}") from e
    if arm_cfg.provider == "none":
        raise typer.BadParameter(f"--arm {spec!r}: none is not a model to compare")

    llm = build_llm(arm_cfg)
    if llm is None:
        missing = (
            "CLOUDFLARE_ACCOUNT_ID"
            if arm_cfg.provider in ("workers_ai", "ai_gateway") and arm_cfg.api_key
            else arm_cfg.api_key_env_var
        )
        raise typer.BadParameter(f"--arm {spec!r}: {missing} is empty")
    return f"{arm_cfg.provider}:{llm.model}", llm


@app.command("llm-compare")
def llm_compare(
    arm: Annotated[
        list[str] | None,
        typer.Option("--arm", help="provider:model to compare against; repeat for a three-way"),
    ] = None,
    hours: Annotated[int, typer.Option("--hours", help="Window to digest")] = 24,
    period: Annotated[str, typer.Option("--period", help="morning or evening")] = "morning",
    config_path: Annotated[Path, typer.Option("--config", help="Config file path")] = Path(
        "cyris.toml"
    ),
    sources_path: Annotated[Path, typer.Option("--sources", help="Sources file path")] = Path(
        "sources.yaml"
    ),
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging")] = False,
) -> None:
    """Digest one window with several LLM providers, side by side.

    Cost and token counts are the easy half; the question this exists to answer is
    whether the summaries read as well, and only the rendered digests can answer
    that. Every arm reuses the scores already in the store, so the model is the
    only difference between them.

    Arm one is always the provider in cyris.toml. Each --arm adds another, as
    `provider:model` (an omitted model means that provider's default):

        cyris llm-compare --arm anthropic:claude-haiku-4-5
        cyris llm-compare --arm anthropic:claude-haiku-4-5 --arm openai:gpt-5.6-luna

    Read-only: no state updates, no usage_log row, no publish, no Discord, and no
    file of its own. Every arm's digest goes to stdout under its own heading and
    the numbers to stderr, so `cyris llm-compare --arm ... > compare.md` is the
    artifact you read. The configured provider is left alone — switching is a
    `cyris.toml` edit you make after reading it.
    """
    _setup_logging(verbose)

    from datetime import UTC, datetime, timedelta

    from cyris.bootstrap import build_deps, load_effective_config
    from cyris.diagnostics.compare import compare_llms

    if not arm:
        logger.error("Nothing to compare against. Pass at least one --arm provider:model.")
        raise typer.Exit(1)

    try:
        # also loads .env into the environment
        cfg = load_effective_config(config_path, sources_path)
        cfg.require_complete_settings()
        cfg.require_sources()
    except (FileNotFoundError, ValueError) as e:
        logger.error("Configuration error: %s", e)
        raise typer.Exit(1) from e

    deps = build_deps(cfg)
    if deps.llm is None:
        logger.error("No LLM provider configured, so there is nothing to compare against.")
        raise typer.Exit(1)
    if deps.html_writer is None:
        # The comparison's output *is* the rendered digests, so there is nothing
        # to fall back to. `[html_output] enabled = false` is a real configuration
        # — the D1 path publishes from memory — so this is a message, not a crash.
        logger.error("Set [html_output] enabled = true: this command prints rendered digests.")
        raise typer.Exit(1)

    # Built before any LLM call, so a typo in the fourth arm fails now rather than
    # after three digests have been paid for.
    arms = [(f"{cfg.app.llm_provider.provider}:{deps.llm.model}", deps.llm)]
    arms.extend(_build_arm(spec, cfg.app.llm_provider.reasoning_effort) for spec in arm)

    now = datetime.now(UTC)
    stored = deps.store.load_by_time_range(start=now - timedelta(hours=hours), end=now)
    if not stored:
        typer.echo(f"No articles in the last {hours}h. Run `cyris run` first.", err=True)
        raise typer.Exit(1)

    articles = [a.to_article() for a in stored]
    scores = {a.url: a.score for a in stored if a.score is not None}

    typer.echo(
        f"\n{len(articles)} article(s) over {hours}h, {len(scores)} already scored\n", err=True
    )

    rows = compare_llms(arms, articles, scores, cfg, period=period, render=deps.html_writer.render)

    width = max((len(row.label) for row in rows), default=0)
    for row in rows:
        u = row.content.usage
        cost = f", {row.neurons:.1f} neurons" if row.neurons is not None else ""
        typer.echo(
            f"  {row.label:<{width}} {row.content.articles_included:>3} included   "
            f"{len(row.content.news_clusters)} cluster(s), "
            f"{len(row.content.thematic_summaries)} theme(s), "
            f"{len(row.content.filtered_headlines)} headline(s)   "
            f"({u.input_tokens:,} in / {u.output_tokens:,} out over {u.api_calls} calls, "
            f"{row.wall_seconds:.1f}s{cost})",
            err=True,
        )

    for row in rows:
        typer.echo(f"\n\n# {row.label} — {row.content.date} {period}\n")
        typer.echo(row.rendered)

    if len(rows) > 1:
        typer.echo(
            "\nRead the digests on stdout. The numbers say what it costs; only the prose "
            "says whether the summaries are worth reading.",
            err=True,
        )


@app.command("doctor")
def doctor(
    config_path: Annotated[Path, typer.Option("--config", help="Config file path")] = Path(
        "cyris.toml"
    ),
    sources_path: Annotated[Path, typer.Option("--sources", help="Sources file path")] = Path(
        "sources.yaml"
    ),
    deployment: Annotated[
        str,
        typer.Option(
            "--deployment",
            help="Deployment URL to date against this checkout, e.g. https://cyris-app.you.workers.dev",
        ),
    ] = "",
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging")] = False,
) -> None:
    """Check the configuration before a run has to prove it at 08:00."""
    _setup_logging(verbose)
    if not verbose:
        # The report is the output here; a request log per check buries it.
        logging.getLogger("httpx").setLevel(logging.WARNING)

    from cyris.adapters.store.d1 import D1Error
    from cyris.bootstrap import load_effective_config
    from cyris.diagnostics.doctor import run_checks

    try:
        cfg = load_effective_config(config_path, sources_path)
    except (FileNotFoundError, ValueError) as e:
        typer.echo(f"✗ config — {e}")
        typer.echo("  Copy cyris.toml.example and sources.example.yaml, then fill them in.")
        raise typer.Exit(1) from e
    except D1Error as e:
        # The tables are created here on first boot, so reaching this means D1
        # itself is unusable. A traceback would bury that in a stack the reader
        # has to decode; this check line is the whole answer.
        _echo_d1_failure(e)
        raise typer.Exit(1) from e

    # No green banner here: it used to be true because a missing file raised.
    # It no longer does, and `_check_config_file` reports which file was read.
    marks = {"ok": "✓", "warn": "!", "fail": "✗", "skip": "–"}
    checks = asyncio.run(run_checks(cfg, config_path, deployment))
    for check in checks:
        typer.echo(f"{marks[check.status]} {check.name} — {check.detail}")
        if check.fix and check.status != "ok":
            typer.echo(f"  {check.fix}")

    failed = [c for c in checks if c.status == "fail"]
    if failed:
        typer.echo(f"\n{len(failed)} problem(s) would break a run.")
        raise typer.Exit(1)
    typer.echo("\nReady to run.")


config_app = typer.Typer(help="Show the configuration a run would use")
app.add_typer(config_app, name="config")


@config_app.command("show")
def config_show(
    config_path: Annotated[Path, typer.Option("--config", help="Config file path")] = Path(
        "cyris.toml"
    ),
    sources_path: Annotated[Path, typer.Option("--sources", help="Sources file path")] = Path(
        "sources.yaml"
    ),
) -> None:
    """Print every setting's effective value and where it came from. Secrets show only set."""
    from cyris.adapters.store.d1 import D1Error
    from cyris.bootstrap import load_effective_config
    from cyris.diagnostics.config_show import config_rows, render

    try:
        cfg = load_effective_config(config_path, sources_path)
    except (FileNotFoundError, ValueError) as e:
        typer.echo(f"✗ config — {e}", err=True)
        raise typer.Exit(1) from e
    except D1Error as e:
        _echo_d1_failure(e)
        raise typer.Exit(1) from e

    typer.echo(render(config_rows(cfg)))


def _llm_from_values(values: dict):
    """The LLM the reader's saved provider and model name, or None when none can be built."""
    from pydantic import ValidationError

    from cyris.bootstrap import build_llm
    from cyris.config import LLMProviderConfig

    effort_key = "llm_provider.reasoning_effort"
    effort = {"reasoning_effort": values[effort_key]} if effort_key in values else {}
    try:
        candidate = LLMProviderConfig(
            provider=values.get("llm_provider.provider") or "",
            model=values.get("llm_provider.model") or "",
            **effort,
        )
    except ValidationError:
        return None
    return build_llm(candidate)


def _local_run_starter(config_path: Path, sources_path: Path):
    """Start `cyris run` on this host, one at a time: the Worker's `run` instance.

    The app Worker starts a container per run and its lifecycle keeps one alive;
    locally that is a child process, and the check for a live one is the same
    promise — two runs at once would store over each other's issue.
    """
    import subprocess

    running: list[subprocess.Popen] = []

    def start(period: str | None, date: str | None) -> dict[str, str]:
        if running and running[0].poll() is None:
            raise RuntimeError("a run is already going; wait for it to finish")
        argv = [
            sys.executable,
            "-m",
            "cyris",
            "run",
            "--config",
            str(config_path),
            "--sources",
            str(sources_path),
        ]
        argv += ["--if-due"] if period is None else ["--period", period]
        if date is not None:
            argv += ["--date", date]
        running[:] = [subprocess.Popen(argv)]
        logger.info("Started run: %s", " ".join(argv))
        return {"started": "run", "at": datetime.now(UTC).isoformat()}

    return start


@app.command("triage-ui")
def triage_ui(
    port: Annotated[int, typer.Option(help="Server port")] = 8766,
    host: Annotated[str, typer.Option(help="Server host")] = "127.0.0.1",
    config_path: Annotated[Path, typer.Option("--config", help="Config file path")] = Path(
        "cyris.toml"
    ),
    sources_path: Annotated[Path, typer.Option("--sources", help="Sources file path")] = Path(
        "sources.yaml"
    ),
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging")] = False,
) -> None:
    """Start the settings web UI: LLM provider, digest hours, notifications, sources, topics;
    /labels, the blind-label page; and /interest, the interest map."""
    _setup_logging(verbose)

    from cyris.adapters.store.areas import D1AreaStore
    from cyris.adapters.store.blind_labels import D1BlindLabels
    from cyris.adapters.store.d1_store import D1ArticleStore
    from cyris.adapters.store.digests import D1DigestStore
    from cyris.adapters.store.feed_health import D1FeedHealth
    from cyris.adapters.store.interest import D1InterestReader
    from cyris.adapters.store.runs import D1RunLog
    from cyris.adapters.store.source_store import D1SourceStore
    from cyris.adapters.store.source_titles import D1SourceTitles
    from cyris.adapters.store.tracked_topics import D1TrackedTopicStore
    from cyris.bootstrap import (
        build_d1_client,
        build_feed_checker,
        build_onboarding_articles,
        build_settings,
        load_effective_config,
    )
    from cyris.entrypoints.triage_server import TriageServer

    try:
        cfg = load_effective_config(config_path, sources_path)
    except (FileNotFoundError, ValueError) as e:
        logger.error("Configuration error: %s", e)
        raise typer.Exit(1) from e

    d1 = build_d1_client(cfg)

    async def _run() -> None:
        server = TriageServer(
            host=host,
            port=port,
            settings=build_settings(cfg),
            values={**cfg.present_settings(), **cfg.defaulted_settings()},
            sources=cfg.sources,
            source_store=D1SourceStore(d1) if d1 else None,
            feed_health=D1FeedHealth(d1) if d1 else None,
            tracked_topics=cfg.tracked_topics,
            topic_store=D1TrackedTopicStore(d1) if d1 else None,
            blind_labels=D1BlindLabels(d1) if d1 else None,
            article_store=D1ArticleStore(d1) if d1 else None,
            area_store=D1AreaStore(d1) if d1 else None,
            source_titles=D1SourceTitles(d1) if d1 else None,
            llm_factory=_llm_from_values,
            onboarding_articles=build_onboarding_articles(cfg) if d1 else None,
            feed_checker=build_feed_checker() if d1 else None,
            digest_store=D1DigestStore(d1) if d1 else None,
            run_log=D1RunLog(d1, "") if d1 else None,
            interest_reader=D1InterestReader(d1).read if d1 else None,
            start_run=_local_run_starter(config_path, sources_path),
            archive_dir=cfg.app.html_output.output_dir,
        )
        await server.start()
        typer.echo(f"Settings:  http://{host}:{port}/settings")
        typer.echo(f"Labels:    http://{host}:{port}/labels")
        typer.echo(f"Interest:  http://{host}:{port}/interest")
        typer.echo(f"Archive:   http://{host}:{port}/")
        typer.echo("Press Ctrl+C to stop")

        # In the Container this process is PID 1, and Linux drops signals that
        # PID 1 has no handler for. Without these two lines the runtime's
        # SIGTERM is ignored, the instance never sleeps, and it bills all day.
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()

        # The platform's idle stop leaves no other trace, so these lines are
        # what tell a SIGTERM with a grace period from an outright kill.
        def on_signal(sig: signal.Signals) -> None:
            logger.info("triage-ui: %s, stopping", sig.name)
            stop.set()

        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, on_signal, sig)
        try:
            await stop.wait()
        except asyncio.CancelledError:
            pass
        finally:
            await server.stop()
            logger.info("triage-ui: stopped")

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        typer.echo("Shutting down")


@app.command("serve")
def serve(
    host: Annotated[str, typer.Option(help="Interface to listen on")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Server port")] = 8767,
    config_path: Annotated[Path, typer.Option("--config", help="Config file path")] = Path(
        "cyris.toml"
    ),
    sources_path: Annotated[Path, typer.Option("--sources", help="Sources file path")] = Path(
        "sources.yaml"
    ),
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging")] = False,
) -> None:
    """Serve the digest pages over HTTP: the archive, each issue, its raw page."""
    _setup_logging(verbose)

    from cyris.bootstrap import load_effective_config
    from cyris.entrypoints.serve import run_serve

    try:
        cfg = load_effective_config(config_path, sources_path)
        output_dir = cfg.app.html_output.output_dir
        if not Path(output_dir).is_dir():
            raise ValueError(f"[html_output] output_dir is not a directory: {output_dir}")
    except (FileNotFoundError, ValueError) as e:
        logger.error("Configuration error: %s", e)
        raise typer.Exit(1) from e
    typer.echo(f"Archive:   http://{host}:{port}/")

    # The pages follow `digest.theme` live. A json deployment's home is
    # cyris.toml, fixed until the next start; the D1 store is read with the
    # Worker's own pause — its REST rate limit is shared with the pipeline and
    # Pages; a local sqlite file answers per request, and a save shows at the
    # very next view.
    if cfg.app.store.is_sql:
        import time

        from cyris.adapters.store.settings import D1Settings
        from cyris.bootstrap import build_d1_client

        settings = D1Settings(build_d1_client(cfg))
        ttl = 60 if cfg.app.store.is_d1 else 0
        held: dict[str, object] = {}

        def reader() -> str:
            now = time.monotonic()
            if held and now - float(held["read"]) < ttl:
                return str(held["theme"])
            theme = settings.all().get("digest.theme") or "default"
            held.clear()
            held.update(read=now, theme=theme)
            return theme

    else:
        start_theme = cfg.app.digest.theme if cfg.app.digest else "default"

        def reader() -> str:  # noqa: F811 — one reader per deployment kind
            return start_theme

    # As triage-ui's: stopped by SIGTERM or Ctrl+C, which a supervisor and a
    # shell each need to end a server that otherwise holds the port open.
    async def _run() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()

        def on_signal(sig: signal.Signals) -> None:
            logger.info("serve: %s, stopping", sig.name)
            stop.set()

        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError):  # Windows has no loop handlers
                loop.add_signal_handler(sig, on_signal, sig)
        try:
            await run_serve(host, port, output_dir, stop, theme=reader)
        finally:
            logger.info("serve: stopped")

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        typer.echo("Shutting down")


# Articles management sub-app
articles_app = typer.Typer(help="Manage article store")
app.add_typer(articles_app, name="articles")


@articles_app.command("list")
def articles_list(
    state: Annotated[str, typer.Option(help="Filter by state")] = "pending",
    limit: Annotated[int, typer.Option(help="Max articles")] = 20,
    offset: Annotated[int, typer.Option(help="Pagination offset")] = 0,
    output_format: Annotated[str, typer.Option("--format", help="Output format")] = "table",
    config_path: Annotated[Path, typer.Option("--config", help="Config file path")] = Path(
        "cyris.toml"
    ),
    sources_path: Annotated[Path, typer.Option("--sources", help="Sources file path")] = Path(
        "sources.yaml"
    ),
) -> None:
    """List articles from store."""
    from cyris.bootstrap import build_store, load_effective_config
    from cyris.domain.models import ArticleState

    try:
        cfg = load_effective_config(config_path, sources_path)
    except (FileNotFoundError, ValueError) as e:
        logger.error("Configuration error: %s", e)
        raise typer.Exit(1) from e

    store = build_store(cfg)

    # Parse state filter
    state_filter = None
    if state != "all":
        try:
            state_filter = ArticleState(state)
        except ValueError:
            typer.echo(f"Invalid state: {state}")
            raise typer.Exit(1) from None

    articles = store.list_articles(state=state_filter, limit=limit, offset=offset)

    if output_format == "json":
        import json

        data = [a.model_dump(mode="json") for a in articles]
        typer.echo(json.dumps(data, ensure_ascii=False, indent=2))
    elif output_format == "urls":
        for article in articles:
            typer.echo(article.url)
    else:  # table
        if not articles:
            typer.echo("No articles found.")
            return

        typer.echo(f"\nFound {len(articles)} articles:\n")
        for article in articles:
            title = article.title[:50] + "..." if len(article.title) > 50 else article.title
            date_str = article.first_seen_at.strftime("%Y-%m-%d")
            typer.echo(f"[{article.state.upper()}] {date_str} | {article.source_name}")
            typer.echo(f"  {title}")
            typer.echo(f"  {article.url}\n")


@articles_app.command("accept")
def articles_accept(
    urls: Annotated[list[str], typer.Argument(help="Article URLs to accept")],
    config_path: Annotated[Path, typer.Option("--config", help="Config file path")] = Path(
        "cyris.toml"
    ),
    sources_path: Annotated[Path, typer.Option("--sources", help="Sources file path")] = Path(
        "sources.yaml"
    ),
) -> None:
    """Accept articles by URL."""
    from cyris.bootstrap import build_store, load_effective_config

    try:
        cfg = load_effective_config(config_path, sources_path)
    except (FileNotFoundError, ValueError) as e:
        logger.error("Configuration error: %s", e)
        raise typer.Exit(1) from e

    store = build_store(cfg)

    # Update state and collect accepted URLs
    updated = 0
    accepted_urls = []
    for url in urls:
        if store.accept([url]):
            updated += 1
            accepted_urls.append(url)

    if accepted_urls:
        store.update_triage_timestamp(accepted_urls, datetime.now(UTC))

    typer.echo(f"Accepted {updated} article(s).")


@articles_app.command("reject")
def articles_reject(
    urls: Annotated[list[str], typer.Argument(help="Article URLs to reject")],
    reason: Annotated[
        str,
        typer.Option(
            help="Rejection reason (canonical: not_interested | already_known; free text allowed)"
        ),
    ] = RejectReason.NOT_INTERESTED,
    config_path: Annotated[Path, typer.Option("--config", help="Config file path")] = Path(
        "cyris.toml"
    ),
    sources_path: Annotated[Path, typer.Option("--sources", help="Sources file path")] = Path(
        "sources.yaml"
    ),
) -> None:
    """Reject articles by URL."""
    from cyris.bootstrap import build_store, load_effective_config

    try:
        cfg = load_effective_config(config_path, sources_path)
    except (FileNotFoundError, ValueError) as e:
        logger.error("Configuration error: %s", e)
        raise typer.Exit(1) from e

    store = build_store(cfg)

    updated = store.reject(urls, reason=reason)
    if updated:
        store.update_triage_timestamp(urls, datetime.now(UTC))

    typer.echo(f"Rejected {updated} article(s).")


@articles_app.command("clean")
def articles_clean(
    state: Annotated[str, typer.Option(help="State to delete")] = "rejected",
    older_than: Annotated[int, typer.Option(help="Days old")] = 30,
    confirm: Annotated[bool, typer.Option(help="Skip confirmation")] = False,
    config_path: Annotated[Path, typer.Option("--config", help="Config file path")] = Path(
        "cyris.toml"
    ),
    sources_path: Annotated[Path, typer.Option("--sources", help="Sources file path")] = Path(
        "sources.yaml"
    ),
) -> None:
    """Delete old articles by state."""
    from cyris.bootstrap import build_store, load_effective_config
    from cyris.domain.models import ArticleState

    try:
        cfg = load_effective_config(config_path, sources_path)
    except (FileNotFoundError, ValueError) as e:
        logger.error("Configuration error: %s", e)
        raise typer.Exit(1) from e

    # Parse state filter
    try:
        state_filter = ArticleState(state)
    except ValueError:
        typer.echo(f"Invalid state: {state}")
        raise typer.Exit(1) from None

    store = build_store(cfg)

    # Confirm deletion
    if not confirm:
        proceed = typer.confirm(
            f"Delete all {state_filter.upper()} articles older than {older_than} days? "
            "(human-triaged rows are kept)"
        )
        if not proceed:
            typer.echo("Cancelled.")
            raise typer.Exit(0) from None

    deleted = store.delete_articles(state=state_filter, older_than_days=older_than)
    typer.echo(f"Deleted {deleted} article(s).")


@articles_app.command("score")
def articles_score(
    force: Annotated[bool, typer.Option("--force", help="Rescore all articles")] = False,
    limit: Annotated[int, typer.Option(help="Max articles to score")] = 200,
    config_path: Annotated[Path, typer.Option("--config", help="Config file path")] = Path(
        "cyris.toml"
    ),
    sources_path: Annotated[Path, typer.Option("--sources", help="Sources file path")] = Path(
        "sources.yaml"
    ),
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging")] = False,
) -> None:
    """Score non-news articles via AI for triage ranking."""
    _setup_logging(verbose)

    from cyris.adapters.store.areas import D1AreaStore
    from cyris.bootstrap import build_d1_client, build_store, load_effective_config
    from cyris.domain.areas import AreaCatalog
    from cyris.domain.models import ArticleState
    from cyris.service_layer.run_digest import proposal_counts
    from cyris.service_layer.scoring import score_in_batches, select_scorable

    try:
        cfg = load_effective_config(config_path, sources_path)
        cfg.validate_required_keys()
        cfg.require_complete_settings()
    except (FileNotFoundError, ValueError) as e:
        logger.error("Configuration error: %s", e)
        raise typer.Exit(1) from e

    d1 = build_d1_client(cfg)
    store = build_store(cfg, d1)

    # Same wiring as run_digest: the prompt asks for tags on every call, so
    # persist them when D1 is available instead of paying for them and
    # discarding them. JSON-only deployments keep the current behavior.
    tag_store = None
    if d1 is not None:
        from cyris.adapters.store.tags import D1TagStore

        tag_store = D1TagStore(d1)

    # Get articles to score
    if force:
        articles = store.list_articles(state=None, limit=limit)
    else:
        articles = store.list_articles(state=ArticleState.PENDING, limit=limit)

    scorable = select_scorable(articles, force=force)

    if not scorable:
        typer.echo("No articles to score.")
        return

    typer.echo(f"Scoring {len(scorable)} articles...")

    async def _run() -> None:
        from cyris.bootstrap import build_llm

        llm = build_llm(cfg.app.llm_provider)

        total_updated = 0

        def persist(url_to_score_lang: dict) -> None:
            nonlocal total_updated
            total_updated += store.update_scores(url_to_score_lang)

        persist_tags = None
        if tag_store is not None:

            def persist_tags(url_to_tags) -> None:
                try:
                    tag_store.save(url_to_tags)
                except Exception as e:
                    logger.warning("Failed to persist scoring tags: %s", e)

        catalog = AreaCatalog(cfg.areas)
        total_usage, picks = await score_in_batches(
            scorable,
            llm,
            snippet_length=cfg.app.digest.scoring_snippet_length,
            progress=typer.echo,
            persist=persist,
            persist_tags=persist_tags,
            area_names=catalog.names(),
        )
        if d1 is not None:
            try:
                counts = proposal_counts(picks, catalog)
                if counts:
                    D1AreaStore(d1).record_proposals(counts, None)
            except Exception as e:
                logger.warning("Recording area proposals failed: %s", type(e).__name__)

        cost = total_usage.estimated_cost
        typer.echo(
            f"\nScored {total_updated} articles. "
            f"API: {total_usage.api_calls} calls, "
            + (f"${cost:.4f}" if cost is not None else f"cost unknown for {total_usage.model}")
        )

    asyncio.run(_run())


labels_app = typer.Typer(help="Blind labels on the filter's candidates, kept in D1")
app.add_typer(labels_app, name="labels")


def _blind_labels(config_path: Path, sources_path: Path):
    """The D1 blind-label store, or exit naming why there is none."""
    from cyris.adapters.store.blind_labels import D1BlindLabels
    from cyris.bootstrap import build_d1_client, load_effective_config

    try:
        cfg = load_effective_config(config_path, sources_path)
        d1 = build_d1_client(cfg)
    except (FileNotFoundError, ValueError) as e:
        typer.echo(f"Configuration error: {e}")
        raise typer.Exit(1) from e
    if d1 is None:
        typer.echo('Blind labels live in D1: set [store] backend = "d1".')
        raise typer.Exit(1)
    return cfg, D1BlindLabels(d1)


@labels_app.command("draw")
def labels_draw(
    since: Annotated[str, typer.Option(help="First-seen cutoff of the pool, with a UTC offset")],
    seed: Annotated[int, typer.Option(help="Seed for the draw and its order")],
    size: Annotated[int, typer.Option(help="Items to draw")] = 100,
    min_rejected: Annotated[
        int, typer.Option(help="Fewest pipeline-rejected items the sample may hold")
    ] = 30,
    replace: Annotated[
        bool, typer.Option(help="Discard the kept sample and its answers first")
    ] = False,
    config_path: Annotated[Path, typer.Option("--config", help="Config file path")] = Path(
        "cyris.toml"
    ),
    sources_path: Annotated[Path, typer.Option("--sources", help="Sources file path")] = Path(
        "sources.yaml"
    ),
) -> None:
    """Draw the sample /labels shows: filter candidates since a cutoff, stratified by
    news and by the pipeline's verdict, that no human has judged yet."""
    from cyris.diagnostics.blind_labels import STRATA, draw_sample, stratum_of

    try:
        cutoff = datetime.fromisoformat(since)
    except ValueError as e:
        typer.echo(f"--since {since} is not an ISO 8601 time, such as 2026-01-02T10:00Z.")
        raise typer.Exit(1) from e
    if cutoff.tzinfo is None:
        typer.echo(f"--since {since} has no UTC offset: write it as {since}Z.")
        raise typer.Exit(1)

    _, labels = _blind_labels(config_path, sources_path)
    if (kept := labels.size()) and not replace:
        answered, _ = labels.progress()
        typer.echo(
            f"A sample is already drawn: {kept} items, {answered} answered. "
            "Pass --replace to discard it and its answers."
        )
        raise typer.Exit(1)

    pool = labels.pool(cutoff)
    try:
        rows = draw_sample(pool, size=size, min_rejected=min_rejected, seed=seed)
    except ValueError as e:
        typer.echo(f"Nothing drawn: {e}.")
        raise typer.Exit(1) from e
    labels.replace_sample(rows, seed=seed, since=cutoff, drawn_at=datetime.now(UTC))

    pooled = Counter(stratum_of(r.news, r.state) for r in pool)
    drawn = Counter(stratum_of(r.news, r.pipeline_state) for r in rows)
    typer.echo(f"Drew {len(rows)} of {len(pool)} filter candidates first seen since {since}.")
    for news, rejected in STRATA:
        name = f"{'news' if news else 'non-news'} · {'rejected' if rejected else 'kept'}"
        typer.echo(f"  {name:20} {drawn[(news, rejected)]:4} of {pooled[(news, rejected)]}")
    typer.echo("Label them at /labels on this deployment's app Worker.")


def _ratio(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


@labels_app.command("report")
def labels_report(
    resamples: Annotated[int, typer.Option(help="Bootstrap resamples for the AUC's interval")] = (
        1000
    ),
    bootstrap_seed: Annotated[int, typer.Option(help="Seed for the bootstrap")] = 0,
    config_path: Annotated[Path, typer.Option("--config", help="Config file path")] = Path(
        "cyris.toml"
    ),
    sources_path: Annotated[Path, typer.Option("--sources", help="Sources file path")] = Path(
        "sources.yaml"
    ),
) -> None:
    """Score the filter and the embedding preference against the blind labels.

    Reads D1 and embeds titles with the configured embedder; writes nothing.
    """
    from cyris.bootstrap import embedding_model, make_embedder
    from cyris.diagnostics.blind_labels import (
        STRATA,
        auc,
        bootstrap_auc,
        filter_score,
        preference_scores,
        stratum_of,
    )

    cfg, labels = _blind_labels(config_path, sources_path)
    try:
        cfg.require_complete_settings()
    except ValueError as e:
        typer.echo(f"Configuration error: {e}")
        raise typer.Exit(1) from e

    rows = labels.labeled()
    if not rows:
        typer.echo("No item is labeled up or down yet: answer some on /labels first.")
        raise typer.Exit(1)
    answered, total = labels.progress()
    seed, since = labels.drawn()
    ups = sum(r.up for r in rows)
    typer.echo(
        f"Blind labels: {len(rows)} labeled ({ups} up, {len(rows) - ups} down), "
        f"{answered - len(rows)} skipped, {total - answered} unanswered of {total}."
    )
    typer.echo(f"Drawn with seed {seed} from filter candidates first seen since {since}.\n")

    typer.echo(f"  {'stratum':20} {'pool':>6} {'labeled':>8} {'up':>4} {'down':>5}")
    for news, rejected in STRATA:
        cell = [r for r in rows if stratum_of(r.news, r.pipeline_state) == (news, rejected)]
        pool = str(cell[0].stratum_size) if cell else "-"
        up = sum(r.up for r in cell)
        name = f"{'news' if news else 'non-news'} · {'rejected' if rejected else 'kept'}"
        typer.echo(f"  {name:20} {pool:>6} {len(cell):>8} {up:>4} {len(cell) - up:>5}")
    kept = Counter(r.pipeline_state for r in rows if not stratum_of(r.news, r.pipeline_state)[1])
    typer.echo(
        f"Kept at draw: {kept['accepted']} accepted, {kept['pending']} pending "
        "(pending: cut by a cap, suppressed, or not yet judged).\n"
    )

    typer.echo("The filter against the labels: an upvote is the positive, and the filter")
    typer.echo("keeps what it did not reject. Each stratum is weighted by its pool share.")
    typer.echo(f"{'':10} {'labeled':>8} {'precision':>10} {'recall':>7}")
    for name, group in (
        ("news", [r for r in rows if r.news]),
        ("non-news", [r for r in rows if not r.news]),
        ("overall", rows),
    ):
        score = filter_score(group)
        typer.echo(
            f"{name:10} {score.labeled:>8} {_ratio(score.precision):>10} {_ratio(score.recall):>7}"
        )

    vote = cfg.app.vote_similarity
    votes = labels.human_votes()
    embedder = make_embedder(vote.provider, vote.model, vote.base_url)
    scores = asyncio.run(preference_scores(embedder, {r.url: r.title for r in rows}, votes))
    scored = [r for r in rows if r.url in scores]
    values = [scores[r.url] for r in scored]
    positives = [r.up for r in scored]
    up_seeds = sum(v.up for v in votes)
    typer.echo(
        "\nEmbedding preference: cosine of the title to the nearest upvote minus the nearest "
        f"downvote, {embedding_model(vote.provider, vote.model)}."
    )
    typer.echo(
        f"Seeds: {up_seeds} up, {len(votes) - up_seeds} down human votes, the sample left out."
    )
    point = auc(values, positives)
    interval = bootstrap_auc(values, positives, resamples=resamples, seed=bootstrap_seed)
    if point is None:
        typer.echo(f"AUC n/a: the {len(scored)} scored items are not both up and down.")
    elif interval is None:
        typer.echo(
            f"AUC {point:.3f}, no 95% interval: fewer than 2 of the {resamples} bootstrap "
            f"resamples drew both an up and a down label. Over {len(scored)} labeled items, "
            "unweighted."
        )
    else:
        typer.echo(
            f"AUC {point:.3f}, 95% CI {interval[0]:.3f} to {interval[1]:.3f} "
            f"({resamples} bootstrap resamples), over {len(scored)} labeled items, unweighted."
        )
    typer.echo(f"Embedding spend: {json.dumps(embedder.usage.as_dict())}")


store_app = typer.Typer(help="Move the article store between backends")
app.add_typer(store_app, name="store")

_ALL_ARTICLES = 1_000_000  # the store holds thousands; this means "everything"


def _newest_by_url(articles: list) -> dict:
    """One article per URL, the most recently seen one.

    The JSON store can hold the same URL twice — its dedup scan only looks back
    8 days, so a re-published article lands again in a later partition. D1's URL
    primary key keeps one row, so comparing needs both sides to pick the same
    one, or every such URL reads as a mismatch when nothing is wrong.
    """
    newest: dict = {}
    for article in articles:
        seen = newest.get(article.url)
        if seen is None or article.first_seen_at > seen.first_seen_at:
            newest[article.url] = article
    return newest


def _force_d1_client(config_path: Path, sources_path: Path):
    """Return (cfg, SQL client) for the commands that talk to the store's tables.

    `store migrate` and `sources push` are how an empty D1 is filled for the
    first time, and they run while `[store] backend` is still json — so
    `load_effective_config` built no client and created no tables. Overriding
    the backend here is what makes the schema apply on this path too; without
    it the first INSERT dies with `no such table`. A `sqlite` deployment has a
    client already, and this returns it untouched.
    """
    from cyris.adapters.store.d1 import D1Error, apply_schema
    from cyris.bootstrap import build_d1_client, load_effective_config

    try:
        # Already applies the schema when the backend is d1, so an unusable
        # database surfaces here rather than at the first INSERT.
        cfg = load_effective_config(config_path, sources_path)
    except (FileNotFoundError, ValueError) as e:
        logger.error("Configuration error: %s", e)
        raise typer.Exit(1) from e
    except D1Error as e:
        _echo_d1_failure(e)
        raise typer.Exit(1) from e

    already_applied = cfg.app.store.is_d1  # load_effective_config built a client
    if cfg.app.store.is_sql:
        # sqlite: `load_effective_config` built the client and applied the schema
        # already. Forcing it to "d1" here would ask for D1 credentials the
        # deployment does not have.
        return cfg, build_d1_client(cfg)
    cfg.app.store.backend = "d1"
    # After the override, so it asks the same question `build_d1_client` will —
    # the old check omitted CLOUDFLARE_ACCOUNT_ID and let it raise past the try.
    missing = cfg.missing_store_keys()
    if missing:
        typer.echo(f"No D1 configured: set {', '.join(missing)}.")
        raise typer.Exit(1)

    client = build_d1_client(cfg)
    if not already_applied:
        # Creating the tables is the first thing a new deployment does here, so
        # an unusable D1 surfaces on this line rather than at the first INSERT.
        # Same answer `doctor` gives, for the same error.
        try:
            apply_schema(client)
        except D1Error as e:
            _echo_d1_failure(e)
            raise typer.Exit(1) from e
    return cfg, client


def _load_both_stores(config_path: Path, sources_path: Path):
    """Return (json_store, d1_store) regardless of which one [store] selects."""
    from cyris.adapters.store import ArticleStore
    from cyris.adapters.store.d1_store import D1ArticleStore

    cfg, client = _force_d1_client(config_path, sources_path)
    return ArticleStore(cfg.app.agent_vault.path), D1ArticleStore(client)


@store_app.command("migrate")
def store_migrate(
    config_path: Annotated[Path, typer.Option("--config", help="Config file path")] = Path(
        "cyris.toml"
    ),
    sources_path: Annotated[Path, typer.Option("--sources", help="Sources file path")] = Path(
        "sources.yaml"
    ),
) -> None:
    """Copy the local JSON store into D1. Safe to re-run; never overwrites."""
    json_store, d1_store = _load_both_stores(config_path, sources_path)

    articles = json_store.list_articles(state=None, limit=_ALL_ARTICLES)
    typer.echo(f"Read {len(articles)} articles from the local store.")

    # A full store is thousands of statements; without this it looks like a hang.
    def progress(done: int, total: int) -> None:
        if done % 50 == 0 or done == total:
            typer.echo(f"  {done}/{total} batches")

    imported = d1_store.import_articles(articles, on_progress=progress)
    typer.echo(f"Inserted {imported} into D1 ({len(articles) - imported} already there).")


@store_app.command("diff")
def store_diff(
    config_path: Annotated[Path, typer.Option("--config", help="Config file path")] = Path(
        "cyris.toml"
    ),
    sources_path: Annotated[Path, typer.Option("--sources", help="Sources file path")] = Path(
        "sources.yaml"
    ),
) -> None:
    """Compare both stores article by article; agreement prints only the counts."""
    json_store, d1_store = _load_both_stores(config_path, sources_path)

    local = _newest_by_url(json_store.list_articles(state=None, limit=_ALL_ARTICLES))
    remote = _newest_by_url(d1_store.list_articles(state=None, limit=_ALL_ARTICLES))
    typer.echo(f"local: {len(local)} urls    d1: {len(remote)} urls")

    only_local = sorted(local.keys() - remote.keys())
    only_remote = sorted(remote.keys() - local.keys())
    for label, urls in (("only local", only_local), ("only d1", only_remote)):
        typer.echo(f"\n{label} ({len(urls)}):")
        for url in urls[:20]:
            typer.echo(f"  {url}")
        if len(urls) > 20:
            typer.echo(f"  … and {len(urls) - 20} more")

    # State, score and the triage stamp are what the pipeline acts on, so those
    # are what a mismatch would corrupt — content drift is not a thing here.
    fields = ("state", "score", "language", "triaged_at", "digest_date", "rejection_reason")
    mismatched = 0
    for url in sorted(local.keys() & remote.keys()):
        differences = [
            f"{f}: {getattr(local[url], f)!r} vs {getattr(remote[url], f)!r}"
            for f in fields
            if getattr(local[url], f) != getattr(remote[url], f)
        ]
        if differences:
            mismatched += 1
            if mismatched <= 20:
                typer.echo(f"\n{url}\n  " + "\n  ".join(differences))
    typer.echo(f"\nshared: {len(local.keys() & remote.keys())}, differing: {mismatched}")


settings_app = typer.Typer(help="Fill D1's runtime settings from a cyris.toml")
app.add_typer(settings_app, name="settings")


@settings_app.command("push")
def settings_push(
    config_path: Annotated[Path, typer.Option("--config", help="Config file path")] = Path(
        "cyris.toml"
    ),
    sources_path: Annotated[Path, typer.Option("--sources", help="Sources file path")] = Path(
        "sources.yaml"
    ),
) -> None:
    """Copy each runtime setting D1 lacks from cyris.toml. Never overwrites a row."""
    import json
    import tomllib

    from cyris.adapters.notify import mask_discord_webhook_url
    from cyris.adapters.store.settings import D1Settings
    from cyris.config import GRADE_D_KEYS, REQUIRED_KEYS, validate_setting

    # The file itself, not cfg.app: a D1 deployment's load ignores these keys.
    raw = tomllib.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    from_file: dict = {}
    for key in GRADE_D_KEYS:
        table, field = key.split(".", 1)
        body = raw.get(table)
        if not isinstance(body, dict) or field not in body:
            continue
        try:
            from_file[key] = validate_setting(key, body[field])
        except ValueError as e:
            typer.echo(f"{config_path}: {key}: {e}", err=True)
            raise typer.Exit(1) from e

    cfg, client = _force_d1_client(config_path, sources_path)
    if cfg.app.store.is_d1:
        typer.echo(f"D1 database {cfg.app.store.database_id}")
    else:
        from cyris.bootstrap import sqlite_path

        typer.echo(f"SQLite store {sqlite_path(cfg)}")
    settings = D1Settings(client)
    rows = settings.all()
    in_d1: dict = {}
    for key, value in rows.items():
        try:
            in_d1[key] = validate_setting(key, value)
        except ValueError:
            continue  # an invalid row counts as missing, so the file may replace it

    def shown(key: str, value) -> str:
        if key == "notify.discord_webhook_url":
            value = mask_discord_webhook_url(value)
        return json.dumps(value, ensure_ascii=False)

    added: dict = {}
    kept = missing = 0
    for key in sorted(GRADE_D_KEYS):
        if key in in_d1:
            kept += 1
            note = ""
            if key not in from_file:
                note = " (not in file)"
            elif from_file[key] != in_d1[key]:
                note = f" (file has {shown(key, from_file[key])})"
            typer.echo(f"  {'kept':<9}{key}{note}")
        elif key in from_file:
            added[key] = from_file[key]
            typer.echo(f"  {'added':<9}{key} = {shown(key, from_file[key])}")
        elif key not in REQUIRED_KEYS and key not in rows:
            typer.echo(f"  {'default':<9}{key} — in neither D1 nor {config_path}")
        else:
            missing += 1
            typer.echo(f"  {'missing':<9}{key} — in neither D1 nor {config_path}")
    if added:
        settings.set(added)
    typer.echo(
        f"Added {len(added)} of {len(GRADE_D_KEYS)} settings from {config_path}; "
        f"{kept} kept; {missing} still missing."
    )


sources_app = typer.Typer(help="Keep source definitions in D1 so adding a feed is not a rebuild")
app.add_typer(sources_app, name="sources")


def _source_store(config_path: Path, sources_path: Path):
    """Return (yaml_sources, D1SourceStore) or exit with what is missing."""
    from cyris.adapters.store.source_store import D1SourceStore

    cfg, client = _force_d1_client(config_path, sources_path)
    return cfg, D1SourceStore(client)


@sources_app.command("push")
def sources_push(
    config_path: Annotated[Path, typer.Option("--config", help="Config file path")] = Path(
        "cyris.toml"
    ),
    sources_path: Annotated[Path, typer.Option("--sources", help="Sources file path")] = Path(
        "sources.yaml"
    ),
) -> None:
    """Make D1 match sources.yaml exactly, removals included."""
    cfg, store = _source_store(config_path, sources_path)

    # A D1 deployment's config holds the table's sources, so read the file directly
    # or a push would just write D1's current contents back to itself.
    import yaml as _yaml

    from cyris.config import SourcesConfig

    raw = _yaml.safe_load(sources_path.read_text(encoding="utf-8")) or {}
    from_file = {s.name: s for s in SourcesConfig.model_validate(raw).sources}

    before = set(store.list_sources())
    written = store.replace_all(from_file)
    removed = sorted(before - set(from_file))

    typer.echo(f"Pushed {written} sources from {sources_path}.")
    for name in removed:
        typer.echo(f"  removed: {name}")


@sources_app.command("list")
def sources_list(
    config_path: Annotated[Path, typer.Option("--config", help="Config file path")] = Path(
        "cyris.toml"
    ),
    sources_path: Annotated[Path, typer.Option("--sources", help="Sources file path")] = Path(
        "sources.yaml"
    ),
) -> None:
    """Show what D1 currently serves to the pipeline and the RSS Worker."""
    _cfg, store = _source_store(config_path, sources_path)

    sources = store.list_sources()
    if not sources:
        typer.echo(
            "D1 has no sources; runs stop until you add one on /settings "
            "or run `cyris sources push`."
        )
        return
    for source in sources.values():
        typer.echo(f"{source.lane.value:<10} {source.type:<11} {source.name} — {source.url or '—'}")
    typer.echo(f"\n{len(sources)} sources in D1.")


schedule_app = typer.Typer(help="Schedule runs on this host")
app.add_typer(schedule_app, name="schedule")


@schedule_app.command("install-systemd")
def schedule_install_systemd(
    repo: Annotated[Path, typer.Option("--repo", help="The clone to run from")] = Path("."),
    dest: Annotated[
        Path | None,
        typer.Option("--dest", help="Write the units here instead of installing them"),
    ] = None,
    buffer: Annotated[
        bool, typer.Option("--buffer", help="Also poll feeds hourly into [rss] local_buffer")
    ] = False,
) -> None:
    """Install the hourly tick, and optionally the hourly feed poll, as user units.

    The Cron Trigger is the Cloudflare deployment's clock: `run --if-due` every
    hour, which does nothing outside the two digest hours. These units are the
    same tick on this host, with the repository as the working directory so the
    default `cyris.toml` and `sources.yaml` are the ones it reads.
    """
    import subprocess

    repo = repo.resolve()
    python = sys.executable
    units: dict[str, str] = {
        "cyris-run.service": (
            "[Unit]\n"
            "Description=cyris digest tick (a run only on a digest hour)\n"
            "Wants=network-online.target\n"
            "After=network-online.target\n"
            "\n"
            "[Service]\n"
            "Type=oneshot\n"
            f"WorkingDirectory={repo}\n"
            f"ExecStart={python} -m cyris run --if-due\n"
        ),
        "cyris-run.timer": (
            "[Unit]\n"
            "Description=cyris digest tick, hourly\n"
            "\n"
            "[Timer]\n"
            "OnCalendar=*-*-* *:02:00\n"
            "Persistent=true\n"
            "\n"
            "[Install]\n"
            "WantedBy=timers.target\n"
        ),
    }
    if buffer:
        # The poll runs first so the tick's run finds this hour's items; the
        # buffer is a window, not a queue, so two minutes early is enough.
        units["cyris-buffer.service"] = (
            "[Unit]\n"
            "Description=cyris feed buffer poll\n"
            "Wants=network-online.target\n"
            "After=network-online.target\n"
            "\n"
            "[Service]\n"
            "Type=oneshot\n"
            f"WorkingDirectory={repo}\n"
            f"ExecStart={python} -m cyris buffer\n"
        )
        units["cyris-buffer.timer"] = (
            "[Unit]\n"
            "Description=cyris feed buffer poll, hourly\n"
            "\n"
            "[Timer]\n"
            "OnCalendar=*-*-* *:00:00\n"
            "Persistent=true\n"
            "\n"
            "[Install]\n"
            "WantedBy=timers.target\n"
        )

    target = dest or Path.home() / ".config" / "systemd" / "user"
    target.mkdir(parents=True, exist_ok=True)
    for name, text in units.items():
        (target / name).write_text(text, encoding="utf-8")
        typer.echo(f"Wrote {target / name}")
    if dest is not None:
        return

    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    for name in ("cyris-run.timer", "cyris-buffer.timer"):
        if name in units:
            subprocess.run(["systemctl", "--user", "enable", "--now", name], check=True)
    linger = subprocess.run(
        ["loginctl", "show-user", os.environ.get("USER", ""), "-p", "Linger"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    if linger != "Linger=yes":
        typer.echo(
            "\nThe timers stop when you log out: run `loginctl enable-linger` once "
            "to keep them going."
        )
    typer.echo("\nScheduled. `systemctl --user list-timers cyris-*` shows the next ticks.")
