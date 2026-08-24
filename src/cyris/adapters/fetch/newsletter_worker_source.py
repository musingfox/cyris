"""FetchSource that pulls forwarded newsletters from the Cloudflare Email Worker.

Mirrors the promote loop's pull/ack pattern: GET /newsletters -> parse -> fetch
linked articles -> POST /ack. Sender is matched to a source via `email_match`.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime

import httpx

from cyris.adapters.fetch.email_parser import parse_newsletter
from cyris.adapters.fetch.newsletter import newsletter_article
from cyris.domain.models import Article, SourceConfig

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 15


class CloudflareNewsletterSource:
    """Pull newsletters queued in the Cloudflare Email Worker's KV."""

    def __init__(self, worker_url: str, token: str) -> None:
        self._worker_url = worker_url.rstrip("/")
        self._token = token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    @staticmethod
    def _match_source(from_email: str, sources: dict[str, SourceConfig]) -> SourceConfig | None:
        for source in sources.values():
            if source.email_match and from_email in source.email_match:
                return source
        return None

    @staticmethod
    def _match_forwarded(item: dict, sources: dict[str, SourceConfig]) -> SourceConfig | None:
        """Fallback for manually-forwarded mail whose From became the forwarder.

        Gmail's "Forward" button rewrites From to the user, leaving the original
        sender only in the body's forwarded header. Match a source whose
        email_match address appears in the body. (Filter auto-forwards keep the
        original From and match directly, so this only fires on manual forwards.)
        """
        body = f"{item.get('text', '')} {item.get('html', '')}"
        for source in sources.values():
            if not source.email_match:
                continue
            addr = source.email_match.split(":", 1)[-1].strip()
            if addr and addr in body:
                return source
        return None

    @staticmethod
    def _is_private_reply(item: dict) -> bool:
        """Private reply (or Fwd of reply) -> ACK but no ingest. Normal Fwd of issue ok."""
        subject = item.get("subject")
        if not isinstance(subject, str):
            return False
        subject = subject.strip()
        if not subject:
            return False
        # support Fw:Re, Fwd:Fwd:Re, 回覆:; tolerate ws
        # (non-str guarded to prevent escape ex from _is_private)
        return bool(
            re.match(r"^(?:(?:Fwd?|Fw|轉寄)[:：]\s*)*(?:Re|回覆)[:：]\s*", subject, re.IGNORECASE)
        )

    async def fetch_articles(
        self,
        after: datetime,
        before: datetime,
        sources: dict[str, SourceConfig],
        aliases: dict[str, str] | None = None,
        limit: int = 200,
    ) -> list[Article]:
        """Pull queued newsletters, expand into Articles, then ACK the queue.

        Unmatched senders (no email_match source) are skipped but still ACKed so
        they don't pile up. ACK runs after articles are built; a crash between
        build and the caller's store-write loses at most this batch — acceptable
        for a personal newsletter inbox.
        """
        try:
            resp = httpx.get(
                f"{self._worker_url}/newsletters", headers=self._headers(), timeout=TIMEOUT_SECONDS
            )
            resp.raise_for_status()
            queued = resp.json()
        except httpx.HTTPError:
            logger.warning("Newsletter worker pull failed", exc_info=True)
            return []

        if not queued:
            return []

        articles: list[Article] = []
        processed_ids: list[str] = []
        for item in queued:
            item_id = item.get("id")
            if item_id is not None:
                processed_ids.append(item_id)
            try:
                source = self._match_source(item.get("from", ""), sources) or self._match_forwarded(
                    item, sources
                )
                if source is None:
                    logger.info("Newsletter from unknown sender skipped: %s", item.get("from"))
                    continue
                if self._is_private_reply(item):
                    # private reply (or fwd of reply): do not ingest, but acked to avoid pileup
                    continue
                payload = {
                    "from": item.get("from", ""),
                    "subject": item.get("subject", ""),
                    "html": item.get("html", ""),
                    "text": item.get("text", ""),
                    "headers": {"Date": item.get("date", "")},
                }
                parsed = parse_newsletter(payload, source.name)
                art = newsletter_article(parsed, source)
                if art is not None:
                    articles.append(art)
            except Exception:
                logger.warning(
                    "Failed to process newsletter item id=%s from=%s",
                    item_id,
                    item.get("from"),
                    exc_info=True,
                )

        self._ack(processed_ids)
        logger.info("Pulled %d newsletter(s), produced %d article(s)", len(queued), len(articles))
        return articles

    def _ack(self, ids: list[str]) -> None:
        if not ids:
            return
        try:
            resp = httpx.post(
                f"{self._worker_url}/ack",
                json={"ids": ids},
                headers=self._headers(),
                timeout=TIMEOUT_SECONDS,
            )
            resp.raise_for_status()
        except httpx.HTTPError:
            logger.warning("Newsletter worker ack failed", exc_info=True)

    async def health_check(self) -> bool:
        try:
            resp = httpx.get(
                f"{self._worker_url}/newsletters", headers=self._headers(), timeout=TIMEOUT_SECONDS
            )
            return resp.status_code == 200
        except httpx.HTTPError:
            return False
