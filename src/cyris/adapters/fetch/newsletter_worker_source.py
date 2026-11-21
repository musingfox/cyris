"""FetchSources that pull forwarded newsletters from a queue, then ack what they processed.

`CloudflareNewsletterSource` pulls the Cloudflare Email Worker's KV: GET /newsletters -> parse
-> POST /ack, mirroring the promote loop. `TenantNewsletterSource` pulls a hosted tenant's own
`newsletters` table through the run's gateway, which the hosted Worker's email() handler fills
(ADR-0034), and acks by deleting there. Either way the sender is matched to a source via
`email_match`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

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

    # Whether an issue may take a link that can name its recipient as its article URL; see
    # `newsletter_article`. A hosted tenant's source turns it off.
    recipient_links = True

    def _holds_unmatched(self, item: dict) -> bool:
        """Whether a message no source matches stays queued rather than being acked."""
        return False

    def _hold(self, ids: list[str]) -> None:
        """Mark messages this run left queued; the KV queue has no such mark."""

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
        """The source whose `email_match` contains the sender's address, in any case.

        Case carries no meaning in an address, so both sides are lower-cased. A missing or blank
        From matches nothing: "" is a substring of every string, so the old `in` test filed
        such mail under the first source with an `email_match`.

        The test stays a substring one, not whole-address equality. The self-hosted /settings
        and `sources push` accept any string for `email_match`, so it may hold several
        addresses, `from:(a@x.test OR b@y.test)` or a display name; equality would match none
        of them and the mail would be acked as an unknown sender. The cost: sender
        `a@b.com` also matches `from:xa@b.com`. That is the same rule as the hosted class, whose
        route writes exactly one address.
        """
        sender = (from_email or "").strip().lower()
        if not sender:
            return None
        for source in sources.values():
            if not source.email_match:
                continue
            if sender in source.email_match.lower():
                return source
        return None

    @staticmethod
    def _match_forwarded(item: dict, sources: dict[str, SourceConfig]) -> SourceConfig | None:
        """Fallback for manually-forwarded mail whose From became the forwarder.

        Gmail's "Forward" button rewrites From to the user, leaving the original
        sender only in the body's forwarded header. Match a source whose
        email_match address appears in the body, in any case. (Filter auto-forwards keep the
        original From and match directly, so this only fires on manual forwards.)
        """
        body = f"{item.get('text', '')} {item.get('html', '')}".lower()
        for source in sources.values():
            if not source.email_match:
                continue
            addr = source.email_match.split(":", 1)[-1].strip().lower()
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

        Unmatched senders (no email_match source) are skipped and ACKed so they
        don't pile up, unless `_holds_unmatched` keeps one queued for a source added
        later. ACK runs after articles are built; a crash between
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
        held: set[str] = set()
        for item in queued:
            item_id = item.get("id")
            if item_id is not None:
                processed_ids.append(item_id)
            try:
                source = self._match_source(item.get("from", ""), sources) or self._match_forwarded(
                    item, sources
                )
                if source is None:
                    if item_id is not None and self._holds_unmatched(item):
                        held.add(item_id)
                        logger.info("Newsletter from unknown sender held: %s", item.get("from"))
                        continue
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
                art = newsletter_article(parsed, source, recipient_links=self.recipient_links)
                if art is not None:
                    articles.append(art)
            except Exception:
                logger.warning(
                    "Failed to process newsletter item id=%s from=%s",
                    item_id,
                    item.get("from"),
                    exc_info=True,
                )

        self._ack([item_id for item_id in processed_ids if item_id not in held])
        self._hold(sorted(held))
        logger.info(
            "Pulled %d newsletter(s), produced %d article(s), held %d",
            len(queued),
            len(articles),
            len(held),
        )
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


# A hosted tenant's queue as the Worker's `GET /newsletters` answers it, oldest first, with when
# each message arrived. `held_at` is written here and read by the Worker's store
# (workers/app/src/tenant_inbound.js), which evicts the oldest held message when the queue is full.
TENANT_QUEUE_SQL = (
    'SELECT id, sender AS "from", subject, html, text, date, received_at FROM newsletters '
    "ORDER BY received_at, id"
)

# How long a hosted tenant's run leaves a message no source matches in the queue, from when it
# arrived: the time a reader has to add the newsletter's source after subscribing at the address,
# and to read Gmail's forwarding confirmation (docs/hosted-tenants.md), before a run deletes it.
# The RSS buffer's 8 days, so mail and feeds wait as long. The queue's cap (`MAX_QUEUED` in
# workers/app/src/tenant_inbound.js) bounds what waits.
UNMATCHED_HOLD_DAYS = 8

# Ids a hosted ack deletes in one statement, far under the 100 bound values a statement takes.
TENANT_ACK_CHUNK = 50


class TenantNewsletterSource(CloudflareNewsletterSource):
    """Pull a hosted tenant's newsletters from its own Durable Object, through the run's gateway.

    The hosted Worker's email() handler stores each message sent to the tenant's random address
    in the tenant's `newsletters` table (`adapters/store/hosted_schema.sql`, ADR-0034). The
    gateway answers only this tenant, so the pull and its ack, a delete by id, reach no other
    tenant's queue. Matching, parsing and the ack-after-build order are the KV source's.

    A message no source matches stays queued for `UNMATCHED_HOLD_DAYS` from its arrival, so an
    issue that came before its tenant added the source reaches the first run after it is added;
    an older one is acked. A held message is stamped `held_at`, and the Worker evicts the oldest
    stamped one when the queue is full, so held mail never crowds out new mail. Senders match
    in any case.

    No link that can name its recipient becomes an article URL here (`recipient_links=False`):
    an ESP's "view in browser" link is the recipient's own copy and often carries a token that
    signs in as them, and a hosted issue's links are read by more than its recipient today (the
    operator reads every tenant's pages). The issue keeps its sender's canonical post link when
    that has no query left after the tracking strip, else its source's homepage.
    """

    recipient_links = False

    def __init__(
        self,
        d1: D1Queryable,
        ack: bool = True,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._d1 = d1
        self._ack_enabled = ack
        self._now = now

    @staticmethod
    def _match_source(from_email: str, sources: dict[str, SourceConfig]) -> SourceConfig | None:
        """The KV source's match, in any case: an address's case carries no meaning.

        The operator's route writes `email_match` in lower case, and the email() handler stores
        From as the sender wrote it (`Letter@News.example`). The KV source's own match is left
        as it is: changing it is a self-hosted change of its own.
        """
        sender = (from_email or "").strip().lower()
        if not sender:
            return None
        for source in sources.values():
            if source.email_match and sender in source.email_match.lower():
                return source
        return None

    @staticmethod
    def _match_forwarded(item: dict, sources: dict[str, SourceConfig]) -> SourceConfig | None:
        """The KV source's manual-forward fallback, in any case, for the same reason."""
        body = f"{item.get('text', '')} {item.get('html', '')}".lower()
        for source in sources.values():
            if not source.email_match:
                continue
            addr = source.email_match.split(":", 1)[-1].strip().lower()
            if addr and addr in body:
                return source
        return None

    def _hold(self, ids: list[str]) -> None:
        """Stamp each message this run left queued, so the Worker may evict it before new mail.

        Only a message a run has seen and found no source for is evictable: without the stamp,
        mail no source matches would fill the queue's cap for its whole hold, and every later
        issue would be dropped. A preview writes no stamp, as it deletes nothing.
        """
        if not ids or not self._ack_enabled:
            return
        stamp = self._now().astimezone(UTC).isoformat()
        try:
            for start in range(0, len(ids), TENANT_ACK_CHUNK):
                chunk = ids[start : start + TENANT_ACK_CHUNK]
                marks = ", ".join("?" for _ in chunk)
                self._d1.query(
                    f"UPDATE newsletters SET held_at = ? WHERE held_at IS NULL AND id IN ({marks})",
                    [stamp, *chunk],
                )
        except Exception:  # noqa: BLE001 - a failed stamp only leaves mail unevictable
            logger.warning("Tenant newsletter hold stamp failed", exc_info=True)

    def _holds_unmatched(self, item: dict) -> bool:
        try:
            arrived = datetime.fromisoformat(str(item.get("received_at")))
        except ValueError:
            return False
        if arrived.tzinfo is None:
            return False
        return self._now() - arrived < timedelta(days=UNMATCHED_HOLD_DAYS)

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
