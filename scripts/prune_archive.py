"""Remove named issues from the published archive: their stored rows, then the site.

Without `--yes` it prints what would go and changes nothing. With `--yes` it deletes the
issues' `digests` and `digest_inputs` rows, then redeploys the site without their pages.
The rows cannot be restored from cyris; back up D1 first.

Before anything is written it reads the live archive and refuses when it lists pages the
manifest lacks that were not named. A failure after the delete leaves the rows gone, and
re-running the same command completes the prune.

Nothing locks a prune against a publishing run, so run it away from the two digest hours.
A run that read the manifest before the prune saved its own deploys the pruned pages back
and saves them into the manifest again; re-running the same command with `--yes` removes
them (docs/operations.md, "Pruning old issues").

Usage:
    uv run python scripts/prune_archive.py 2026-10-01-morning 2026-10-01-evening
    uv run python scripts/prune_archive.py 2026-10-01-morning 2026-10-01-evening --yes

Reads the same `cyris.toml` and `sources.yaml` as `cyris regenerate`.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from cyris.adapters.output.html_digest import HtmlDigestWriter
from cyris.adapters.output.pages_manifest import D1PagesManifest
from cyris.adapters.output.pages_receipt import D1PagesDeployReceipt
from cyris.adapters.output.publish import (
    _DATED_HTML,
    _fetch_live_index,
    _issue,
    _slash,
    page_serves,
    publish_site,
    unexpected_live_pages,
)
from cyris.adapters.store.archive_meta import D1ArchiveMeta
from cyris.adapters.store.d1 import D1Error
from cyris.adapters.store.digest_inputs import D1DigestInputStore
from cyris.adapters.store.digests import D1DigestStore
from cyris.bootstrap import build_d1_client, load_effective_config
from cyris.service_layer.schedule import PERIOD_ORDER

logger = logging.getLogger(__name__)
SLUG = re.compile(r"(\d{4}-\d{2}-\d{2})-(.+)")


def _err(message: str) -> None:
    print(message, file=sys.stderr)


def _named_paths(date: str, period: str) -> set[str]:
    return {
        _slash(HtmlDigestWriter.digest_filename(date, period)),
        _slash(HtmlDigestWriter.raw_filename(date, period)),
    }


def _newest_slug(paths: set[str]) -> tuple[str, str] | None:
    """The (date, period) of the latest digest page in `paths`; raw pages do not count."""
    stems = [
        _issue(p).lstrip("/")
        for p in paths
        if _DATED_HTML.match(p) and _slash(p) == _issue(p) + ".html"
    ]
    if not stems:
        return None
    newest = max(stems, key=lambda stem: (stem[:10], _period_rank(stem[11:])))
    return newest[:10], newest[11:]


def _period_rank(period: str) -> int:
    return PERIOD_ORDER.index(period) if period in PERIOD_ORDER else -1


def _load_config(config_path: Path, sources_path: Path):
    try:
        cfg = load_effective_config(config_path, sources_path)
    except Exception as e:
        _err(f"Configuration error ({type(e).__name__}): {' '.join(str(e).split())}")
        return None
    app = cfg.app
    if app.store.backend != "d1":
        _err(f"The store backend is {app.store.backend}; prune needs d1")
    elif not app.html_output.enabled:
        _err("HTML output is disabled; prune needs [html_output] enabled")
    elif not app.promote.publish_enabled:
        _err("Publishing is disabled; prune needs [promote] publish_enabled")
    elif not app.promote.pages_project:
        _err("[promote] pages_project is empty; prune needs it")
    else:
        return cfg
    return None


def _redact(text: str, secret: str) -> str:
    return text.replace(secret, "<redacted>") if secret else text


class _RedactToken(logging.Filter):
    """Masks the token in every record, including the ones publish_site logs before it returns."""

    def __init__(self, token: str) -> None:
        super().__init__()
        self._token = token

    def filter(self, record: logging.LogRecord) -> bool:
        if self._token:
            message = record.getMessage()
            if self._token in message:
                record.msg = _redact(message, self._token)
                record.args = None
        return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prune named issues from the published archive")
    parser.add_argument("slugs", nargs="+", metavar="{date}-{period}")
    parser.add_argument("--config", type=Path, default=Path("cyris.toml"))
    parser.add_argument("--sources", type=Path, default=Path("sources.yaml"))
    parser.add_argument("--yes", action="store_true", help="Delete the rows and redeploy")
    args = parser.parse_args(argv)

    # basicConfig gives the root logger a handler, so the filter below sees records that
    # would otherwise reach logging's unfiltered last-resort handler.
    logging.basicConfig(format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    redact = _RedactToken(os.environ.get("CLOUDFLARE_API_TOKEN", ""))
    handlers = logging.getLogger().handlers
    for handler in handlers:
        handler.addFilter(redact)
    try:
        return _run(args, parser)
    finally:
        for handler in handlers:
            handler.removeFilter(redact)


def _run(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    issues = []
    for slug in args.slugs:
        match = SLUG.fullmatch(slug)
        if match is None or match.group(2) not in PERIOD_ORDER:
            parser.error(
                f"{slug!r} is not {{date}}-{{period}} with a period in: {', '.join(PERIOD_ORDER)}"
            )
        try:
            datetime.strptime(match.group(1), "%Y-%m-%d")
        except ValueError:
            parser.error(f"{slug!r} has no such date")
        if match.groups() not in issues:
            issues.append(match.groups())

    cfg = _load_config(args.config, args.sources)
    if cfg is None:
        return 1

    d1 = build_d1_client(cfg)
    digests = D1DigestStore(d1)
    digest_inputs = D1DigestInputStore(d1)
    manifest_store = D1PagesManifest(d1)
    manifest = {_slash(p) for p in manifest_store.load()}
    stored = set(digests.issues())
    named_paths = set().union(*(_named_paths(date, period) for date, period in issues))

    rows = [
        (
            f"{date}-{period}",
            int((date, period) in stored),
            int(digest_inputs.load(date, period) is not None),
            len(_named_paths(date, period) & manifest),
        )
        for date, period in issues
    ]
    pages_project = cfg.app.promote.pages_project
    if not any(count for row in rows for count in row[1:]):
        _print_counts(rows)
        live = _fetch_live_index(pages_project)
        if live is None:
            _err("Refusing to prune: the live archive could not be read")
            return 1
        still_listed = sorted(live & named_paths)
        if still_listed:
            _err(
                f"Nothing is stored to prune, but the live archive still lists "
                f"{len(still_listed)} named page(s), e.g. {', '.join(still_listed[:5])}. "
                "The last deploy may not be live yet: wait, then re-run the same command."
            )
            return 1
        print("Nothing to prune.")
        return 0

    remaining = manifest - named_paths
    newest = _newest_slug(remaining)
    if newest is None:
        _err("Refusing to prune: the prune would leave no dated issue, and would empty the archive")
        return 1

    live = _fetch_live_index(pages_project)
    if live is None:
        _err("Refusing to prune: the live archive could not be read")
        return 1
    unexpected = sorted(unexpected_live_pages(live, manifest, named_paths))
    if unexpected:
        _err(
            f"Refusing to prune: the live archive lists {len(unexpected)} page(s) the "
            f"manifest lacks and this command did not name, e.g. "
            f"{', '.join(unexpected[:5])}. Check [store] database_id"
        )
        return 1

    _print_counts(rows)
    if not args.yes:
        print("Dry run: nothing was written. Re-run with --yes to prune.")
        return 0

    print("Deleting stored rows...")
    try:
        for date, period in issues:
            digests.delete(date, period)
            digest_inputs.delete(date, period)
            print(f"{date}-{period}: rows deleted")
    except D1Error as e:
        _err(
            f"Could not delete the stored rows, nothing was deployed: {e}. Re-run the same command."
        )
        return 1

    writer = HtmlDigestWriter(Path(cfg.app.html_output.output_dir))
    try:
        archive_counts = D1ArchiveMeta(d1).article_counts()
    except Exception as e:
        logger.error("Failed to read the archive's article counts: %s", e)
        archive_counts = {}
    index = writer.render_index(sorted(p.lstrip("/") for p in remaining), counts=archive_counts)
    index_bytes = index.encode("utf-8")
    new_files = {
        "/index.html": index_bytes,
        **{f"/{name}": data for name, data in writer.site_assets().items()},
    }
    newest_date, newest_period = newest
    token = os.environ.get("CLOUDFLARE_API_TOKEN", "")

    def rerun_hint(reason: str) -> None:
        _err(f"{reason}. The stored rows are already deleted; re-run the same command to finish.")

    print("Deploying...")
    try:
        landed = publish_site(
            new_files,
            f"{newest_date}-{newest_period}",
            manifest_store=manifest_store,
            pages_project=pages_project,
            receipt_store=D1PagesDeployReceipt(d1),
            drop=frozenset(named_paths),
        )
    except Exception as e:
        rerun_hint(f"Deploy failed ({type(e).__name__}: {_redact(str(e), token)})")
        return 1
    if not landed:
        rerun_hint("Deploy did not land")
        return 1
    if not page_serves(pages_project, "/index.html", index_bytes):
        _err(
            "The live index does not serve the pruned archive. The stored rows are already "
            "deleted; wait for the site to update, then re-run the same command to check."
        )
        return 1
    print(f"Pruned {len(issues)} issue(s).")
    return 0


def _print_counts(rows: list[tuple[str, int, int, int]]) -> None:
    for slug, digests, inputs, paths in rows:
        print(f"{slug}: digests={digests} digest_inputs={inputs} manifest_paths={paths}")
    print(
        "total: digests={} digest_inputs={} manifest_paths={}".format(
            *(sum(row[i] for row in rows) for i in (1, 2, 3))
        )
    )


if __name__ == "__main__":
    sys.exit(main())
