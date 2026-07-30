"""Pull promote-button clicks from the Cloudflare Worker into the local store."""

import logging
from datetime import UTC, datetime
from pathlib import Path

import httpx
from pydantic import BaseModel

from cyris.adapters.fetch.defuddle import DEFAULT_BUN_PATH, fetch_full_markdown
from cyris.adapters.output.article_export import ArticleExporter
from cyris.adapters.store import ArticleStore
from cyris.domain.triage import RejectReason

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 15


class PromotedArticle(BaseModel):
    url: str
    # "deep" (default, also what vote-less legacy payloads mean), "up" or "down".
    vote: str = "deep"
    digest_date: str | None = None
    ts: str | None = None


def pull_promotions(worker_url: str, token: str) -> list[PromotedArticle]:
    """Fetch pending promotions from the Worker.

    Raises:
        httpx.HTTPError: On network failure or non-2xx response.
    """
    resp = httpx.get(
        f"{worker_url}/promotions",
        headers={"Authorization": f"Bearer {token}"},
        timeout=TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    return [PromotedArticle.model_validate(item) for item in resp.json()]


def ack_promotions(worker_url: str, token: str, urls: list[str]) -> None:
    """Acknowledge processed promotions so the Worker deletes them.

    Raises:
        httpx.HTTPError: On network failure or non-2xx response.
    """
    resp = httpx.post(
        f"{worker_url}/ack",
        json={"urls": urls},
        headers={"Authorization": f"Bearer {token}"},
        timeout=TIMEOUT_SECONDS,
    )
    resp.raise_for_status()


def sync_promotions(
    worker_url: str,
    token: str,
    store: ArticleStore,
    vault_path: Path,
    folder: str = "Reading",
    bun_path: str = DEFAULT_BUN_PATH,
) -> int:
    """Pull votes, apply them to the store, export deep-read picks, then ACK.

    Votes carry the only human-labelled signal in the pipeline, so every
    voted article gets a ``triaged_at`` stamp that tells it apart from the
    digest run's own accept/reject verdicts. A "down" vote rejects, "up"
    and "deep" accept, and only "deep" exports to the vault.

    Before export each article is re-fetched and cleaned to full-text
    markdown via defuddle; on failure the stored feed content is exported
    as-is. Promotions whose URL is no longer in the store are logged and
    ACKed anyway. ACK happens only after a successful export, so a failure
    here leaves promotions queued for the next run.

    Returns:
        Number of articles exported to the vault.

    Raises:
        httpx.HTTPError: If the Worker is unreachable.
    """
    promotions = pull_promotions(worker_url, token)
    if not promotions:
        return 0

    urls = [p.url for p in promotions]
    vote_by_url = {p.url: p.vote for p in promotions}
    found = store.get_by_urls(urls)

    missing = set(urls) - {a.url for a in found}
    for url in missing:
        logger.warning("Promoted URL not in store, acking anyway: %s", url)

    rejected = [a.url for a in found if vote_by_url[a.url] == "down"]
    accepted = [a.url for a in found if vote_by_url[a.url] != "down"]
    if rejected:
        store.reject(rejected, reason=RejectReason.MANUAL_TRIAGE)
    if accepted:
        store.accept(accepted)
    if found:
        store.update_triage_timestamp([a.url for a in found], datetime.now(UTC))

    exported: list[Path] = []
    to_export = [a for a in found if vote_by_url[a.url] == "deep"]
    if to_export:
        display = []
        for article in to_export:
            markdown = fetch_full_markdown(article.url, article.content, bun_path)
            display.append(
                article.model_copy(update={"content": markdown}) if markdown else article
            )
        exported = ArticleExporter().export_to_vault(display, vault_path, folder=folder)

    ack_promotions(worker_url, token, urls)
    logger.info(
        "Synced %d vote(s) (%d down), exported %d article(s)",
        len(urls),
        len(rejected),
        len(exported),
    )
    return len(exported)
