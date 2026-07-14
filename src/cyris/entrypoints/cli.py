"""CLI entry point for Cyris."""

import asyncio
import logging
import sys
from pathlib import Path
from typing import Annotated

import typer

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
    disable_learning: Annotated[
        bool, typer.Option("--disable-learning", help="Disable preference learning")
    ] = False,
    force: Annotated[
        bool,
        typer.Option(
            "--force", help="Re-process all articles in time window regardless of current state"
        ),
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging")] = False,
) -> None:
    """Run the full pipeline: fetch, score, route, and digest."""
    _setup_logging(verbose)

    from cyris.bootstrap import build_deps
    from cyris.config import load_config
    from cyris.service_layer.run_digest import RunOptions, run_digest

    try:
        cfg = load_config(config_path, sources_path)
        cfg.validate_required_keys()
    except (FileNotFoundError, ValueError) as e:
        logger.error("Configuration error: %s", e)
        raise typer.Exit(1) from e

    deps = build_deps(cfg, on_progress=typer.echo)
    options = RunOptions(
        period=period,
        dry_run=dry_run,
        force=force,
        enable_learning=not disable_learning,
    )
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

    from cyris.bootstrap import build_deps
    from cyris.config import load_config

    try:
        cfg = load_config(config_path, sources_path)
    except (FileNotFoundError, ValueError) as e:
        logger.error("Configuration error: %s", e)
        raise typer.Exit(1) from e

    deps = build_deps(cfg, on_progress=typer.echo)
    if deps.sync_promotions is None:
        typer.echo("Promotion sync not configured (set promote.worker_url + token).")
        raise typer.Exit(1)

    count = deps.sync_promotions()
    typer.echo(f"Synced {count} promoted article(s) to vault.")


@app.command("learn")
def learn(
    days: Annotated[int, typer.Option(help="Number of days to scan for feedback")] = 14,
    config_path: Annotated[Path, typer.Option("--config", help="Config file path")] = Path(
        "cyris.toml"
    ),
    sources_path: Annotated[Path, typer.Option("--sources", help="Sources file path")] = Path(
        "sources.yaml"
    ),
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging")] = False,
) -> None:
    """Analyze feedback and update preference profile."""
    _setup_logging(verbose)

    from cyris.bootstrap import build_deps
    from cyris.config import load_config
    from cyris.service_layer.learning import learn_from_triage

    try:
        cfg = load_config(config_path, sources_path)
        cfg.validate_required_keys()
    except (FileNotFoundError, ValueError) as e:
        logger.error("Configuration error: %s", e)
        raise typer.Exit(1) from e

    deps = build_deps(cfg, on_progress=typer.echo)

    try:
        report = asyncio.run(learn_from_triage(deps, days=days))
    except ValueError as e:
        typer.echo(f"Error: {e}")
        raise typer.Exit(1) from e

    profile = report.profile
    typer.echo("\nPreference Profile:")
    typer.echo(f"  Themes: {', '.join(profile.themes)}")
    typer.echo(f"  Signals: {', '.join(profile.signals[:3])}...")
    typer.echo(f"  Anti-signals: {', '.join(profile.anti_signals[:3])}...")


@app.command("schedule")
def schedule(
    action: Annotated[str, typer.Argument(help="install, uninstall, or status")],
    config_path: Annotated[Path, typer.Option("--config", help="Config file path")] = Path(
        "cyris.toml"
    ),
    sources_path: Annotated[Path, typer.Option("--sources", help="Sources file path")] = Path(
        "sources.yaml"
    ),
) -> None:
    """Manage launchd scheduled digest runs."""
    _setup_logging()

    from cyris.config import load_config
    from cyris.schedule.launchd import ScheduleManager

    manager = ScheduleManager(config_path, sources_path)
    promote = ScheduleManager(
        config_path,
        sources_path,
        label="com.cyris.promote-sync",
        subcommand="promote-sync",
        log_basename="promote-sync",
    )

    if action == "status":
        import json

        typer.echo(
            json.dumps({"digest": manager.status(), "promote_sync": promote.status()}, indent=2)
        )
        return

    if action == "install":
        try:
            cfg = load_config(config_path, sources_path)
        except (FileNotFoundError, ValueError) as e:
            logger.error("Configuration error: %s", e)
            raise typer.Exit(1) from e

        times = cfg.app.general.digest_schedule
        tz = cfg.app.general.timezone
        path = manager.install(times, tz)
        typer.echo(f"Installed: {path}")
        typer.echo(f"Schedule: {', '.join(times)} ({tz})")
        ppath = promote.install_interval(3600)
        typer.echo(f"Installed: {ppath}")
        typer.echo("Promote sync: every 3600s")
        return

    if action == "uninstall":
        removed = False
        for m in (manager, promote):
            try:
                m.uninstall()
                typer.echo(f"Uninstalled {m.label}.")
                removed = True
            except FileNotFoundError:
                pass
        if not removed:
            typer.echo("Not installed.")
            raise typer.Exit(1) from None
        return

    typer.echo(f"Unknown action: {action}. Use install, uninstall, or status.")
    raise typer.Exit(1)


@app.command("email-server")
def email_server(
    config_path: Annotated[Path, typer.Option("--config", help="Config file path")] = Path(
        "cyris.toml"
    ),
    sources_path: Annotated[Path, typer.Option("--sources", help="Sources file path")] = Path(
        "sources.yaml"
    ),
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging")] = False,
) -> None:
    """Start the newsletter email webhook server."""
    _setup_logging(verbose)

    import json
    from datetime import datetime

    from cyris.config import load_config
    from cyris.domain.models import Article
    from cyris.entrypoints.webhook_server import EmailWebhookServer

    try:
        cfg = load_config(config_path, sources_path)
    except (FileNotFoundError, ValueError) as e:
        logger.error("Configuration error: %s", e)
        raise typer.Exit(1) from e

    email_cfg = cfg.app.email
    if not email_cfg.webhook_secret:
        logger.error("CYRIS_EMAIL_WEBHOOK_SECRET is not set")
        raise typer.Exit(1)

    # Newsletter sources only
    newsletter_sources = {name: src for name, src in cfg.sources.items() if src.email_match}
    if not newsletter_sources:
        logger.error("No newsletter sources with email_match found in sources.yaml")
        raise typer.Exit(1)

    # Archive directory for received articles
    archive_dir = cfg.app.agent_vault.path / "daily" / "newsletters"
    archive_dir.mkdir(parents=True, exist_ok=True)

    async def on_received(articles: list[Article]) -> None:
        if not articles:
            return
        today = datetime.now().strftime("%Y-%m-%d")
        archive_file = archive_dir / f"{today}.json"

        existing: list[dict] = []
        if archive_file.exists():
            existing = json.loads(archive_file.read_text())
        existing.extend([a.model_dump(mode="json") for a in articles])
        archive_file.write_text(json.dumps(existing, ensure_ascii=False, indent=2))
        logger.info("Archived %d articles to %s", len(articles), archive_file)

    async def _run() -> None:
        server = EmailWebhookServer(
            host=email_cfg.webhook_host,
            port=email_cfg.webhook_port,
            path=email_cfg.webhook_path,
            webhook_secret=email_cfg.webhook_secret,
            sources=newsletter_sources,
            on_received=on_received,
        )
        await server.start()
        typer.echo(
            f"Listening on {email_cfg.webhook_host}:{email_cfg.webhook_port}"
            f"{email_cfg.webhook_path}"
        )
        typer.echo(f"Newsletter sources: {', '.join(newsletter_sources.keys())}")
        try:
            # Run until interrupted
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            pass
        finally:
            await server.stop()
            typer.echo("Shutting down")

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        typer.echo("Shutting down")


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

    from cyris.adapters.fetch.miniflux import MinifluxClient
    from cyris.adapters.store import ArticleStore
    from cyris.config import load_config
    from cyris.entrypoints.triage_server import TriageServer

    try:
        cfg = load_config(config_path, sources_path)
    except (FileNotFoundError, ValueError) as e:
        logger.error("Configuration error: %s", e)
        raise typer.Exit(1) from e

    store = ArticleStore(cfg.app.agent_vault.path)

    # Conditionally create MinifluxClient if API key is configured
    miniflux_client = None
    if cfg.app.miniflux.api_key:
        miniflux_client = MinifluxClient(cfg.app.miniflux.url, cfg.app.miniflux.api_key)

    async def _run() -> None:
        server = TriageServer(
            store,
            vault_path=cfg.app.obsidian.user_vault_path,
            miniflux_client=miniflux_client,
            host=host,
            port=port,
        )
        await server.start()
        typer.echo(f"Triage UI: http://{host}:{port}")
        typer.echo("Press Ctrl+C to stop")
        try:
            while True:
                await asyncio.sleep(3600)
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
    from cyris.adapters.store import ArticleStore
    from cyris.config import load_config
    from cyris.domain.models import ArticleState

    try:
        cfg = load_config(config_path, sources_path)
    except (FileNotFoundError, ValueError) as e:
        logger.error("Configuration error: %s", e)
        raise typer.Exit(1) from e

    store = ArticleStore(cfg.app.agent_vault.path)

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
    from cyris.adapters.output.article_export import ArticleExporter
    from cyris.adapters.store import ArticleStore
    from cyris.config import load_config

    try:
        cfg = load_config(config_path, sources_path)
    except (FileNotFoundError, ValueError) as e:
        logger.error("Configuration error: %s", e)
        raise typer.Exit(1) from e

    store = ArticleStore(cfg.app.agent_vault.path)

    # Update state and collect accepted URLs
    updated = 0
    accepted_urls = []
    for url in urls:
        if store.accept([url]):
            updated += 1
            accepted_urls.append(url)

    typer.echo(f"Accepted {updated} article(s).")

    # Export to vault if configured and articles were accepted
    vault_path = cfg.app.obsidian.user_vault_path
    if vault_path and updated > 0:
        try:
            articles = store.get_by_urls(accepted_urls)
            if articles:
                ArticleExporter().export_to_vault(articles, vault_path, folder="Reading")
                typer.echo(f"Exported {len(articles)} article(s) to vault.")
        except Exception as e:
            logger.warning("Failed to export articles: %s", e)


@articles_app.command("reject")
def articles_reject(
    urls: Annotated[list[str], typer.Argument(help="Article URLs to reject")],
    reason: Annotated[str, typer.Option(help="Rejection reason")] = "manual",
    config_path: Annotated[Path, typer.Option("--config", help="Config file path")] = Path(
        "cyris.toml"
    ),
    sources_path: Annotated[Path, typer.Option("--sources", help="Sources file path")] = Path(
        "sources.yaml"
    ),
) -> None:
    """Reject articles by URL."""
    from cyris.adapters.store import ArticleStore
    from cyris.config import load_config

    try:
        cfg = load_config(config_path, sources_path)
    except (FileNotFoundError, ValueError) as e:
        logger.error("Configuration error: %s", e)
        raise typer.Exit(1) from e

    store = ArticleStore(cfg.app.agent_vault.path)

    updated = store.reject(urls, reason=reason)

    typer.echo(f"Rejected {updated} article(s).")


@articles_app.command("export")
def articles_export(
    state: Annotated[str, typer.Option(help="State filter")] = "accepted",
    folder: Annotated[str, typer.Option(help="Vault subfolder")] = "Reading",
    limit: Annotated[int, typer.Option(help="Max articles")] = 100,
    config_path: Annotated[Path, typer.Option("--config", help="Config file path")] = Path(
        "cyris.toml"
    ),
    sources_path: Annotated[Path, typer.Option("--sources", help="Sources file path")] = Path(
        "sources.yaml"
    ),
) -> None:
    """Export articles to user vault."""
    from cyris.adapters.output.article_export import ArticleExporter
    from cyris.adapters.store import ArticleStore
    from cyris.config import load_config
    from cyris.domain.models import ArticleState

    try:
        cfg = load_config(config_path, sources_path)
    except (FileNotFoundError, ValueError) as e:
        logger.error("Configuration error: %s", e)
        raise typer.Exit(1) from e

    store = ArticleStore(cfg.app.agent_vault.path)

    # Parse state filter
    try:
        state_filter = ArticleState(state)
    except ValueError:
        typer.echo(f"Invalid state: {state}")
        raise typer.Exit(1) from None

    articles = store.list_articles(state=state_filter, limit=limit)

    if not articles:
        typer.echo("No articles to export.")
        return

    exporter = ArticleExporter()
    paths = exporter.export_to_vault(articles, cfg.app.obsidian.user_vault_path, folder=folder)

    typer.echo(f"Exported {len(paths)} article(s) to {cfg.app.obsidian.user_vault_path / folder}")


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
    from cyris.adapters.store import ArticleStore
    from cyris.config import load_config
    from cyris.domain.models import ArticleState

    try:
        cfg = load_config(config_path, sources_path)
    except (FileNotFoundError, ValueError) as e:
        logger.error("Configuration error: %s", e)
        raise typer.Exit(1) from e

    # Parse state filter
    try:
        state_filter = ArticleState(state)
    except ValueError:
        typer.echo(f"Invalid state: {state}")
        raise typer.Exit(1) from None

    store = ArticleStore(cfg.app.agent_vault.path)

    # Confirm deletion
    if not confirm:
        proceed = typer.confirm(
            f"Delete all {state_filter.upper()} articles older than {older_than} days?"
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

    from cyris.adapters.store import ArticleStore
    from cyris.config import load_config
    from cyris.domain.models import ArticleState
    from cyris.service_layer.scoring import score_in_batches

    try:
        cfg = load_config(config_path, sources_path)
        cfg.validate_required_keys()
    except (FileNotFoundError, ValueError) as e:
        logger.error("Configuration error: %s", e)
        raise typer.Exit(1) from e

    store = ArticleStore(cfg.app.agent_vault.path)

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
        from cyris.learn.profile import load_latest_profile

        llm = build_llm(cfg.app.llm_provider)

        # Load preference profile
        preference_profile = load_latest_profile(cfg.app.agent_vault.path)
        if preference_profile:
            logger.info(
                "Loaded preference profile for scoring (sample_size=%d)",
                preference_profile.sample_size,
            )

        total_updated = 0

        def persist(url_to_score_lang: dict) -> None:
            nonlocal total_updated
            total_updated += store.update_scores(url_to_score_lang)

        total_usage = await score_in_batches(
            scorable,
            llm,
            preference_profile=preference_profile,
            snippet_length=cfg.app.digest.scoring_snippet_length,
            progress=typer.echo,
            persist=persist,
        )

        typer.echo(
            f"\nScored {total_updated} articles. "
            f"API: {total_usage.api_calls} calls, "
            f"${total_usage.estimated_cost:.4f}"
        )

    asyncio.run(_run())


@articles_app.command("triage")
def articles_triage(
    digest_limit: Annotated[int, typer.Option(help="Max recent digests to scan")] = 7,
    export_folder: Annotated[str, typer.Option(help="Vault subfolder for export")] = "Reading",
    config_path: Annotated[Path, typer.Option("--config")] = Path("cyris.toml"),
    sources_path: Annotated[Path, typer.Option("--sources")] = Path("sources.yaml"),
) -> None:
    """Process digest feedback to accept articles and export to vault."""
    from cyris.adapters.output.article_export import ArticleExporter
    from cyris.adapters.store import ArticleStore
    from cyris.config import load_config
    from cyris.service_layer.triage import export_accepted, process_feedback, scan_digests

    try:
        cfg = load_config(config_path, sources_path)
    except (FileNotFoundError, ValueError) as e:
        logger.error("Configuration error: %s", e)
        raise typer.Exit(1) from e

    # Initialize store and exporter
    store = ArticleStore(cfg.app.agent_vault.path)
    exporter = ArticleExporter()
    user_vault_path = cfg.app.obsidian.user_vault_path

    # Step 1: Scan digests
    try:
        feedback_list = scan_digests(
            user_vault_path, cfg.app.obsidian.digest_folder, limit=digest_limit
        )
        typer.echo(f"掃描了 {len(feedback_list)} 個 digest 檔案")
    except ValueError as e:
        typer.echo(f"錯誤: {e}")
        typer.echo("請確認您的 digest 資料夾路徑正確，且內含 digest 檔案。")
        raise typer.Exit(0) from e

    # Step 2: Process feedback
    triage_result = process_feedback(feedback_list, store)
    typer.echo(f"已接受 {triage_result.accepted_count} 篇文章")

    if triage_result.skipped_count > 0:
        typer.echo(f"跳過 {triage_result.skipped_count} 篇文章 (未找到或缺少 URL)")

    # Step 3: Export accepted articles
    if triage_result.accepted_count > 0:
        export_paths = export_accepted(
            store, exporter, user_vault_path, triage_result.accepted_urls, folder=export_folder
        )
        typer.echo(f"已匯出 {len(export_paths)} 篇文章至 {export_folder}/")
    else:
        typer.echo("沒有文章需要匯出。")
