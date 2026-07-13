"""Pull promote-button clicks from the Cloudflare Worker into the local store."""

import logging
from pathlib import Path

import httpx
from pydantic import BaseModel

from cyris.adapters.output.article_export import ArticleExporter
from cyris.adapters.store import ArticleStore

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 15


class PromotedArticle(BaseModel):
    url: str
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
) -> int:
    """Pull promotions, mark articles accepted, export to vault, then ACK.

    Promotions whose URL is no longer in the store are logged and ACKed
    anyway. ACK happens only after a successful export, so a failure here
    leaves promotions queued for the next run.

    Returns:
        Number of articles exported to the vault.

    Raises:
        httpx.HTTPError: If the Worker is unreachable.
    """
    promotions = pull_promotions(worker_url, token)
    if not promotions:
        return 0

    urls = [p.url for p in promotions]
    found = store.get_by_urls(urls)

    missing = set(urls) - {a.url for a in found}
    for url in missing:
        logger.warning("Promoted URL not in store, acking anyway: %s", url)

    for article in found:
        store.accept([article.url])

    exported: list[Path] = []
    if found:
        exported = ArticleExporter().export_to_vault(found, vault_path, folder=folder)

    ack_promotions(worker_url, token, urls)
    logger.info("Synced %d promotion(s), exported %d article(s)", len(urls), len(exported))
    return len(exported)
