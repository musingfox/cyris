"""CLI entry point for Cyris."""

import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from cyris.domain.triage import RejectReason

app = typer.Typer(help="Cyris — AI-powered information digest agent", invoke_without_command=True)
logger = logging.getLogger("cyris")


@app.callback()
def main() -> None:
    """Cyris — AI-powered information digest agent."""


def _setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )


@app.command("run")
def run(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Preview without writing")] = False,
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
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging")] = False,
) -> None:
    """Run the full pipeline: fetch, score, route, and digest."""
    _setup_logging(verbose)

    from cyris.bootstrap import build_deps, load_effective_config
    from cyris.service_layer.run_digest import RunOptions, run_digest
    from cyris.service_layer.schedule import due_period
    from cyris.utils.timezone import now_in_timezone

    try:
        cfg = load_effective_config(config_path, sources_path)
        cfg.validate_required_keys()
    except (FileNotFoundError, ValueError) as e:
        logger.error("Configuration error: %s", e)
        raise typer.Exit(1) from e

    if if_due:
        # The cron tick is hourly and unconditional; the schedule lives in D1 so
        # changing it does not need a rebuild. Not due is a normal outcome, not
        # a failure — 22 of every 24 ticks end here.
        due = due_period(now_in_timezone(cfg.app.general.timezone), cfg.app.general.digest_schedule)
        if due is None:
            typer.echo(f"Not a digest hour ({', '.join(cfg.app.general.digest_schedule)}).")
            return
        period = due

    deps = build_deps(cfg, on_progress=typer.echo)
    options = RunOptions(period=period, dry_run=dry_run, force=force)
    report = asyncio.run(run_digest(deps, options))
    if report.rendered:
        typer.echo(report.rendered)


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
    """Sync promote-button clicks from the cloud Worker to the vault (no fetch/LLM)."""
    _setup_logging(verbose)

    from cyris.bootstrap import build_deps, load_effective_config

    try:
        cfg = load_effective_config(config_path, sources_path)
    except (FileNotFoundError, ValueError) as e:
        logger.error("Configuration error: %s", e)
        raise typer.Exit(1) from e

    deps = build_deps(cfg, on_progress=typer.echo)
    if deps.sync_promotions is None:
        typer.echo("Promotion sync not configured (set promote.worker_url + token).")
        raise typer.Exit(1)

    count = deps.sync_promotions()
    typer.echo(f"Synced {count} digest vote(s).")


@app.command("vote-sim")
def vote_sim(
    hours: Annotated[int, typer.Option("--hours", help="Window to judge")] = 24,
    threshold: Annotated[
        float | None, typer.Option("--threshold", help="Override the configured cutoff")
    ] = None,
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
    the diff, so the effect can be compared before `[vote_similarity] enabled`
    is turned on.
    """
    _setup_logging(verbose)

    from datetime import UTC, datetime, timedelta

    from cyris.bootstrap import build_deps, embedding_threshold, load_effective_config
    from cyris.service_layer.vote_similarity import judge_by_votes

    try:
        cfg = load_effective_config(config_path, sources_path)
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
            threshold=threshold if threshold is not None else embedding_threshold(cfg),
            max_seeds=cfg.app.vote_similarity.max_seeds,
        )
    )
    if not report.ran:
        typer.echo(f"Nothing to compare: {report.skipped_reason}")
        raise typer.Exit(1)

    by_url = {a.url: a for a in candidates}
    cut = threshold if threshold is not None else embedding_threshold(cfg)
    typer.echo(
        f"\n{len(candidates)} candidate(s) over {hours}h, judged against "
        f"{report.upvote_seeds} up / {report.downvote_seeds} down seed(s) at "
        f"threshold {cut:.2f}\n"
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


@app.command("embed-compare")
def embed_compare(
    hours: Annotated[int, typer.Option("--hours", help="Window to judge")] = 24,
    threshold: Annotated[
        float | None, typer.Option("--threshold", help="Gemini cutoff (default: configured)")
    ] = None,
    workers_threshold: Annotated[
        float, typer.Option("--workers-threshold", help="bge-m3 cutoff; its cosines run lower")
    ] = 0.53,
    log: Annotated[
        Path | None, typer.Option("--log", help="Append one JSON line per run to this file")
    ] = None,
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

    Read-only. Neither result reaches the digest.
    """
    _setup_logging(verbose)

    from datetime import UTC, datetime, timedelta

    from cyris.adapters.embedding import GeminiEmbedder, WorkersAIEmbedder
    from cyris.bootstrap import _EMBEDDING_DEFAULTS, build_deps, load_effective_config
    from cyris.service_layer.vote_similarity import judge_by_votes

    try:
        # also loads .env into the environment
        cfg = load_effective_config(config_path, sources_path)
    except (FileNotFoundError, ValueError) as e:
        logger.error("Configuration error: %s", e)
        raise typer.Exit(1) from e

    account = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
    token = os.environ.get("CLOUDFLARE_EMBEDDING_API_TOKEN", "")
    if not (account and token):
        logger.error(
            "Needs CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_EMBEDDING_API_TOKEN "
            "(the token must carry Workers AI -> Read; the wrangler one does not)."
        )
        raise typer.Exit(1)

    deps = build_deps(cfg)
    arms = {
        "gemini": (
            GeminiEmbedder(api_key=os.environ.get("GEMINI_API_KEY", "")),
            threshold if threshold is not None else _EMBEDDING_DEFAULTS["gemini"]["threshold"],
        ),
        "workers_ai": (
            WorkersAIEmbedder(api_token=token, account_id=account),
            workers_threshold,
        ),
    }

    now = datetime.now(UTC)
    candidates = deps.store.load_by_time_range(start=now - timedelta(hours=hours), end=now)
    results = {}
    for name, (embedder, arm_cut) in arms.items():
        # Wall-clock, not just api_seconds: Gemini sleeps 1.5s between batches of 50
        # where bge-m3 batches 100 with no pause, and that gap is the throughput
        # difference the per-request timer cannot see.
        started = time.monotonic()
        report = asyncio.run(
            judge_by_votes(
                deps.store,
                embedder,
                candidates,
                threshold=arm_cut,
                max_seeds=cfg.app.vote_similarity.max_seeds,
            )
        )
        elapsed = time.monotonic() - started
        if not report.ran:
            typer.echo(f"Nothing to compare: {report.skipped_reason}")
            raise typer.Exit(1)
        results[name] = (report, arm_cut, embedder.usage, elapsed)

    g_report = results["gemini"][0]
    sets = {n: set(r.suppressed_urls) for n, (r, *_) in results.items()}
    only = {
        "gemini_only": sorted(sets["gemini"] - sets["workers_ai"]),
        "workers_only": sorted(sets["workers_ai"] - sets["gemini"]),
    }

    def margin(report) -> dict[str, float | None]:
        """Where this window's boundary actually fell, per arm.

        The thresholds are pinned constants calibrated against two downvote seeds. As
        the seed set grows they drift, and by different amounts because the two cosine
        scales differ — so the first disagreement this log records could just as easily
        be threshold staleness as a model difference. Recording each side of the
        boundary is what lets the two be told apart later.
        """
        cut_side = [v.down_similarity for v in report.verdicts.values() if v.suppressed]
        keep_side = [v.down_similarity for v in report.verdicts.values() if not v.suppressed]
        return {
            "suppressed_min": round(min(cut_side), 4) if cut_side else None,
            "kept_max": round(max(keep_side), 4) if keep_side else None,
        }

    by_url = {a.url: a for a in candidates}
    typer.echo(
        f"\n{len(candidates)} candidate(s) over {hours}h, "
        f"{g_report.upvote_seeds} up / {g_report.downvote_seeds} down seed(s)\n"
    )
    for name, (report, arm_cut, usage, elapsed) in results.items():
        u = usage.as_dict()
        m = margin(report)
        cost = f", {u['input_tokens']} tokens, {u['neurons']} neurons" if u["neurons"] else ""
        typer.echo(
            f"  {name:<11} @ {arm_cut:.2f}  suppresses {len(report.suppressed_urls):>3}   "
            f"margin {m['kept_max']} -> {m['suppressed_min']}   "
            f"({u['embedded']} embedded in {u['requests']} req, "
            f"{elapsed:.1f}s wall / {u['api_seconds']}s api{cost})"
        )
    typer.echo(
        f"\n  agree on {len(sets['gemini'] & sets['workers_ai'])}, "
        f"disagree on {len(only['gemini_only']) + len(only['workers_only'])}"
    )
    for label, urls in only.items():
        for url in urls[:show]:
            a = by_url[url]
            typer.echo(f"    {label:<13} [{a.source_name[:18]:18}] {a.title[:48]}")

    if log:
        row = {
            "checked_at": now.isoformat(),
            "hours": hours,
            "candidates": len(candidates),
            "seeds": {"up": g_report.upvote_seeds, "down": g_report.downvote_seeds},
            "agree": len(sets["gemini"] & sets["workers_ai"]),
            **only,
            **{
                name: {
                    "threshold": arm_cut,
                    "suppressed": len(report.suppressed_urls),
                    "wall_seconds": round(elapsed, 2),
                    **margin(report),
                    **usage.as_dict(),
                }
                for name, (report, arm_cut, usage, elapsed) in results.items()
            },
        }
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        typer.echo(f"\nLogged to {log}")


def _build_arm(spec: str):
    """Turn "provider:model" into a labelled LLM client, or explain why it cannot.

    Goes through the same `build_llm` the pipeline uses, so an arm picks up its
    key and account id from the environment exactly as a configured provider
    would, and a provider added later costs this command nothing. An empty model
    means that provider's default.
    """
    from pydantic import ValidationError

    from cyris.bootstrap import build_llm
    from cyris.config import LLMProviderConfig

    provider, _, model = spec.partition(":")
    try:
        arm_cfg = LLMProviderConfig(provider=provider.strip(), model=model.strip())
    except ValidationError as e:
        raise typer.BadParameter(f"--arm {spec!r}: unknown provider {provider.strip()!r}") from e

    llm = build_llm(arm_cfg)
    if llm is None:
        missing = (
            "CLOUDFLARE_ACCOUNT_ID"
            if arm_cfg.provider == "workers_ai" and arm_cfg.api_key
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
    out: Annotated[
        Path | None, typer.Option("--out", help="Where to write each arm's digest")
    ] = None,
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

    Read-only: no state updates, no usage_log row, no publish, no Discord. Nothing
    reaches the digest, and the configured provider is left alone — switching is a
    `cyris.toml` edit you make after reading the output.
    """
    _setup_logging(verbose)

    from datetime import UTC, datetime, timedelta

    from cyris.bootstrap import build_deps, load_effective_config
    from cyris.service_layer.digest_pipeline import DigestPipeline

    if not arm:
        logger.error("Nothing to compare against. Pass at least one --arm provider:model.")
        raise typer.Exit(1)

    try:
        # also loads .env into the environment
        cfg = load_effective_config(config_path, sources_path)
    except (FileNotFoundError, ValueError) as e:
        logger.error("Configuration error: %s", e)
        raise typer.Exit(1) from e

    deps = build_deps(cfg)
    if deps.llm is None:
        logger.error("No LLM provider configured, so there is nothing to compare against.")
        raise typer.Exit(1)

    # Built before any LLM call, so a typo in the fourth arm fails now rather than
    # after three digests have been paid for.
    arms = [(f"{cfg.app.llm_provider.provider}:{deps.llm.model}", deps.llm)]
    arms.extend(_build_arm(spec) for spec in arm)

    now = datetime.now(UTC)
    stored = deps.store.load_by_time_range(start=now - timedelta(hours=hours), end=now)
    if not stored:
        typer.echo(f"No articles in the last {hours}h. Run `cyris run` first.")
        raise typer.Exit(1)

    articles = [a.to_article() for a in stored]
    scores = {a.url: a.score for a in stored if a.score is not None}

    out_dir = out or cfg.app.agent_vault.path / "llm-compare"
    out_dir.mkdir(parents=True, exist_ok=True)
    typer.echo(f"\n{len(articles)} article(s) over {hours}h, {len(scores)} already scored\n")

    rows = []
    for label, llm in arms:
        pipeline = DigestPipeline(
            llm,
            max_digest_output=cfg.app.digest.max_articles_per_digest_output,
            summarize_snippet_length=cfg.app.digest.summarize_snippet_length,
            filter_snippet_length=cfg.app.digest.filter_snippet_length,
            score_threshold=cfg.app.routing.summarize_score_threshold,
            output_language=cfg.app.digest.output_language,
            style_prompt=cfg.app.digest.style_prompt,
        )
        started = time.monotonic()
        try:
            result = asyncio.run(
                pipeline.process(
                    articles,
                    cfg.sources,
                    period=period,
                    timezone=cfg.app.general.timezone,
                    article_scores=scores,
                )
            )
        except Exception as e:
            logger.error("%s failed: %s", label, e)
            continue
        elapsed = time.monotonic() - started

        content = result.content
        # The pipeline swallows LLM failures on purpose — `cyris run` would rather
        # ship excerpts than nothing. For a comparison that is the wrong trade: an
        # arm that never reached its model still renders a plausible digest, and
        # printing it as a row invites reading excerpt fallback as this model's
        # work. Zero calls is the tell, and it is not a result.
        if content.usage.api_calls == 0:
            logger.error(
                "%s made no API calls — every request failed and the pipeline fell back to "
                "excerpts. Re-run with --verbose for the provider's reason. Not comparable.",
                label,
            )
            continue

        path = out_dir / f"{content.date}-{period}-{label.replace(':', '_').replace('/', '_')}.md"
        path.write_text(deps.writer.render(content), encoding="utf-8")
        rows.append((label, content, elapsed, getattr(llm, "neurons", None), path))

    width = max((len(r[0]) for r in rows), default=0)
    for label, content, elapsed, neurons, path in rows:
        u = content.usage
        cost = f", {neurons:.1f} neurons" if neurons is not None else ""
        typer.echo(
            f"  {label:<{width}} {content.articles_included:>3} included   "
            f"{len(content.news_clusters)} cluster(s), "
            f"{len(content.thematic_summaries)} theme(s), "
            f"{len(content.filtered_headlines)} headline(s)   "
            f"({u.input_tokens:,} in / {u.output_tokens:,} out over {u.api_calls} calls, "
            f"{elapsed:.1f}s{cost})"
        )
        typer.echo(f"  {'':<{width}} {path}")

    if len(rows) > 1:
        typer.echo("\nRead the files. The numbers say what it costs; only the prose says")
        typer.echo("whether the summaries are worth reading.")


@app.command("doctor")
def doctor(
    config_path: Annotated[Path, typer.Option("--config", help="Config file path")] = Path(
        "cyris.toml"
    ),
    sources_path: Annotated[Path, typer.Option("--sources", help="Sources file path")] = Path(
        "sources.yaml"
    ),
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging")] = False,
) -> None:
    """Check the configuration before a run has to prove it at 08:00."""
    _setup_logging(verbose)
    if not verbose:
        # The report is the output here; a request log per check buries it.
        logging.getLogger("httpx").setLevel(logging.WARNING)

    from cyris.bootstrap import load_effective_config
    from cyris.service_layer.doctor import run_checks

    try:
        cfg = load_effective_config(config_path, sources_path)
    except (FileNotFoundError, ValueError) as e:
        typer.echo(f"✗ config — {e}")
        typer.echo("  Copy cyris.toml.example and sources.example.yaml, then fill them in.")
        raise typer.Exit(1) from e
    typer.echo(f"✓ config — {config_path} + {sources_path}")

    marks = {"ok": "✓", "warn": "!", "fail": "✗", "skip": "–"}
    checks = asyncio.run(run_checks(cfg, config_path))
    for check in checks:
        typer.echo(f"{marks[check.status]} {check.name} — {check.detail}")
        if check.fix and check.status != "ok":
            typer.echo(f"  {check.fix}")

    failed = [c for c in checks if c.status == "fail"]
    if failed:
        typer.echo(f"\n{len(failed)} problem(s) would break a run.")
        raise typer.Exit(1)
    typer.echo("\nReady to run.")


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
    """Start the triage web UI for article classification."""
    _setup_logging(verbose)

    from cyris.adapters.store.source_store import D1SourceStore
    from cyris.bootstrap import build_d1_client, build_settings, build_store, load_effective_config
    from cyris.entrypoints.triage_server import TriageServer

    try:
        cfg = load_effective_config(config_path, sources_path)
    except (FileNotFoundError, ValueError) as e:
        logger.error("Configuration error: %s", e)
        raise typer.Exit(1) from e

    store = build_store(cfg)
    d1 = build_d1_client(cfg)

    async def _run() -> None:
        server = TriageServer(
            store,
            host=host,
            port=port,
            settings=build_settings(cfg),
            llm_provider=cfg.app.llm_provider,
            schedule=cfg.app.general.digest_schedule,
            sources=cfg.sources,
            sources_origin=cfg.sources_origin,
            source_store=D1SourceStore(d1) if d1 else None,
        )
        await server.start()
        typer.echo(f"Triage UI: http://{host}:{port}")
        typer.echo(f"Settings:  http://{host}:{port}/settings")
        typer.echo("Press Ctrl+C to stop")

        # In the Container this process is PID 1, and Linux drops signals that
        # PID 1 has no handler for. Without these two lines the runtime's
        # SIGTERM is ignored, the instance never sleeps, and it bills all day.
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)
        try:
            await stop.wait()
        except asyncio.CancelledError:
            pass
        finally:
            await server.stop()

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

    from cyris.bootstrap import build_d1_client, build_store, load_effective_config
    from cyris.domain.models import ArticleState
    from cyris.service_layer.scoring import score_in_batches

    try:
        cfg = load_effective_config(config_path, sources_path)
        cfg.validate_required_keys()
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

    # Filter: only non-news articles, and only unscored (unless --force)
    scorable = []
    for a in articles:
        if "news" in a.source_tags:
            continue
        if not force and a.score is not None:
            continue
        scorable.append(a)

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

        total_usage = await score_in_batches(
            scorable,
            llm,
            snippet_length=cfg.app.digest.scoring_snippet_length,
            progress=typer.echo,
            persist=persist,
            persist_tags=persist_tags,
        )

        cost = total_usage.estimated_cost
        typer.echo(
            f"\nScored {total_updated} articles. "
            f"API: {total_usage.api_calls} calls, "
            + (f"${cost:.4f}" if cost is not None else f"cost unknown for {total_usage.model}")
        )

    asyncio.run(_run())


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


def _load_both_stores(config_path: Path, sources_path: Path):
    """Return (json_store, d1_store) regardless of which one [store] selects."""
    from cyris.adapters.store import ArticleStore
    from cyris.adapters.store.d1_store import D1ArticleStore
    from cyris.bootstrap import build_d1_client, load_effective_config

    try:
        cfg = load_effective_config(config_path, sources_path)
    except (FileNotFoundError, ValueError) as e:
        logger.error("Configuration error: %s", e)
        raise typer.Exit(1) from e

    if not cfg.app.store.database_id or not cfg.app.store.api_token:
        typer.echo("No D1 configured: set [store] database_id and CLOUDFLARE_API_TOKEN.")
        raise typer.Exit(1)

    # Both stores are needed here whichever backend is live, so D1 is built
    # directly rather than through the [store] backend switch.
    cfg.app.store.backend = "d1"
    return ArticleStore(cfg.app.agent_vault.path), D1ArticleStore(build_d1_client(cfg))


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
    """Compare both stores article by article. Silence means they agree."""
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


sources_app = typer.Typer(help="Keep source definitions in D1 so adding a feed is not a rebuild")
app.add_typer(sources_app, name="sources")


def _source_store(config_path: Path, sources_path: Path):
    """Return (yaml_sources, D1SourceStore) or exit with what is missing."""
    from cyris.adapters.store.source_store import D1SourceStore
    from cyris.bootstrap import build_d1_client, load_effective_config

    try:
        cfg = load_effective_config(config_path, sources_path)
    except (FileNotFoundError, ValueError) as e:
        logger.error("Configuration error: %s", e)
        raise typer.Exit(1) from e

    if not cfg.app.store.database_id or not cfg.app.store.api_token:
        typer.echo("No D1 configured: set [store] database_id and CLOUDFLARE_API_TOKEN.")
        raise typer.Exit(1)

    cfg.app.store.backend = "d1"
    return cfg, D1SourceStore(build_d1_client(cfg))


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

    # load_config already prefers D1 when it has rows, so read the file directly
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
        typer.echo("D1 has no sources; both readers fall back to sources.yaml.")
        return
    for source in sources.values():
        typer.echo(f"{source.tier.value:<10} {source.type:<11} {source.name} — {source.url or '—'}")
    typer.echo(f"\n{len(sources)} sources in D1.")
