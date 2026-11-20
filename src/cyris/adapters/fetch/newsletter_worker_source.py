"""FetchSources that pull forwarded newsletters from a queue, then ack what they processed.

`CloudflareNewsletterSource` pulls the Cloudflare Email Worker's KV: GET /newsletters -> parse
-> POST /ack, mirroring the promote loop. `TenantNewsletterSource` pulls a hosted tenant's own
`newsletters` table through the run's gateway, which the hosted Worker's email() handler fills
(ADR-0034), and acks by deleting there. Either way the sender is matched to a source via
`email_match`.
"""

from __future__ import annotations

import logging
from datetime import datetime

import httpx

from cyris.adapters.fetch.email_parser import parse_newsletter
from cyris.adapters.fetch.keywords import private_reply_re
from cyris.adapters.fetch.newsletter import newsletter_article
from cyris.adapters.store.d1 import D1Queryable
from cyris.domain.models import Article, SourceConfig

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 15


class CloudflareNewsletterSource:
    """Pull newsletters queued in the Cloudflare Email Worker's KV."""

    # Whether an issue may take its labelled web-version link as its article URL; see
    # `newsletter_article`. A hosted tenant's source turns it off.
    web_version = True

    def __init__(self, worker_url: str, token: str, ack: bool = True) -> None:
        self._worker_url = worker_url.rstrip("/")
        self._token = token
        # A pull queue deletes what it hands over. `ack=False` is how a preview
        # reads the queue without emptying it; `bootstrap` sets it for a dry run.
        self._ack_enabled = ack

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
        # Stacked forwards then a reply, in the words keywords.json lists.
        # (non-str guarded to prevent escape ex from _is_private)
        return bool(private_reply_re().match(subject))

    async def fetch_articles(
        self,
        after: datetime,
        before: datetime,
        sources: dict[str, SourceConfig],
    ) -> list[Article]:
        """Pull queued newsletters, expand into Articles, then ACK the queue.

        Unmatched senders (no email_match source) are skipped but still ACKed so
        they don't pile up. ACK runs after articles are built; a crash between
        build and the caller's store-write loses at most this batch — acceptable
        for a personal newsletter inbox.
        """
        # A failed pull raises rather than returning []: `fetch_all_articles` skips the
        # source and lists it in `failed_sources`, which the failed-fetch alert reads.
        queued = self._pull()

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
                art = newsletter_article(parsed, source, web_version=self.web_version)
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

    def _pull(self) -> list[dict]:
        resp = httpx.get(
            f"{self._worker_url}/newsletters", headers=self._headers(), timeout=TIMEOUT_SECONDS
        )
        resp.raise_for_status()
        return resp.json()

    def _ack(self, ids: list[str]) -> None:
        if not ids:
            return
        if not self._ack_enabled:
            logger.info("Preview: leaving %d newsletter(s) queued", len(ids))
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


# A hosted tenant's queue as the Worker's `GET /newsletters` answers it, oldest first.
TENANT_QUEUE_SQL = (
    'SELECT id, sender AS "from", subject, html, text, date FROM newsletters '
    "ORDER BY received_at, id"
)

# Ids a hosted ack deletes in one statement, far under the 100 bound values a statement takes.
TENANT_ACK_CHUNK = 50


class TenantNewsletterSource(CloudflareNewsletterSource):
    """Pull a hosted tenant's newsletters from its own Durable Object, through the run's gateway.

    The hosted Worker's email() handler stores each message sent to the tenant's random address
    in the tenant's `newsletters` table (`adapters/store/hosted_schema.sql`, ADR-0034). The
    gateway answers only this tenant, so the pull and its ack, a delete by id, reach no other
    tenant's queue. Matching, parsing and the ack-after-build order are the KV source's.

    No web-version link becomes an article URL here: an ESP's "view in browser" link is the
    recipient's own copy and often carries a token that signs in as them, and a hosted issue's
    links are read by more than its recipient today (the operator reads every tenant's pages).
    The issue keeps its sender's canonical post link, else its source's homepage.
    """

    web_version = False

    def __init__(self, d1: D1Queryable, ack: bool = True) -> None:
        self._d1 = d1
        self._ack_enabled = ack

    def _pull(self) -> list[dict]:
        # A failed read raises, as the Worker's failed pull does, into `failed_sources`.
        return list(self._d1.query(TENANT_QUEUE_SQL, []).rows)

    def _ack(self, ids: list[str]) -> None:
        if not ids:
            return
        if not self._ack_enabled:
            logger.info("Preview: leaving %d newsletter(s) queued", len(ids))
            return
        try:
            for start in range(0, len(ids), TENANT_ACK_CHUNK):
                chunk = ids[start : start + TENANT_ACK_CHUNK]
                marks = ", ".join("?" for _ in chunk)
                self._d1.query(f"DELETE FROM newsletters WHERE id IN ({marks})", list(chunk))
        except Exception:  # noqa: BLE001 - a failed ack is logged, as the Worker's is
            logger.warning("Tenant newsletter ack failed", exc_info=True)

    async def health_check(self) -> bool:
        try:
            self._d1.query("SELECT count(*) AS n FROM newsletters", [])
        except Exception:  # noqa: BLE001 - any failure to read the queue is an unhealthy source
            return False
        return True
