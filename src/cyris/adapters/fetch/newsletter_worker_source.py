"""FetchSources that pull forwarded newsletters from a queue, then ack what they processed.

`CloudflareNewsletterSource` pulls the Cloudflare Email Worker's KV: GET /newsletters -> parse
-> POST /ack, mirroring the promote loop. `TenantNewsletterSource` pulls a hosted tenant's own
`newsletters` table through the run's gateway, which the hosted Worker's email() handler fills
(ADR-0034), and acks by deleting there. Either way the sender is matched to a source via
`email_match`, or a source is made for a sender none names (ADR-0050).
"""

from __future__ import annotations

import html
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

import httpx

from cyris.adapters.fetch.email_parser import parse_newsletter
from cyris.adapters.fetch.keywords import (
    forwarded_from_line_re,
    forwarded_header_line_re,
    forwarded_marker_re,
    system_senders,
)
from cyris.adapters.fetch.newsletter import newsletter_article
from cyris.adapters.store.d1 import D1Queryable
from cyris.domain.models import (
    NEWSLETTER_SOURCE_TYPE,
    SOURCE_REVIEW_DECLINED,
    SOURCE_REVIEW_NEW,
    Article,
    Lane,
    SourceConfig,
)

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 15

# New sources one run may create from unknown senders. Each new source is another stream of
# articles the LLM reads every run, and a hosted tenant's container writes its `sources` rows
# through its gateway, past the Worker's own source-cap check (workers/app/src/tenant_buffer.js),
# so a flood of new senders must not add sources faster than this.
MAX_NEW_SOURCES_PER_RUN = 10

# How long a run leaves mail it could not file in the queue, from when it arrived, before it acks
# it: on both editions, mail kept for a later run (a forward whose block is unread, a sender a cap,
# a failed write or a taken name left without its source); on a hosted tenant also mail no source
# matches. The time a reader has to add the newsletter's source after subscribing at the address,
# and to read Gmail's forwarding confirmation (docs/hosted-tenants.md). The RSS buffer's 8 days,
# so mail and feeds wait as long. A hosted queue's cap (`MAX_QUEUED` in
# workers/app/src/tenant_inbound.js) bounds what waits there; the KV queue has none but this.
UNMATCHED_HOLD_DAYS = 8

_BRACKETED_ADDRESS = re.compile(r"<([^<>@\s]+@[^<>\s]+)>")
_ADDRESS = re.compile(r"[\w.!#$%&'*+/=?^`{|}~-]+@[\w-]+(?:\.[\w-]+)+")
_HTML_BREAK = re.compile(r"<(?:br|/p|/div|/tr|/li)\b[^>]*>", re.IGNORECASE)
_HTML_TAG = re.compile(r"<[^>]+>")


def _address(text: str | None) -> str | None:
    """The first address in `text`, lower-cased and whole."""
    text = text or ""
    found = _BRACKETED_ADDRESS.search(text) or _ADDRESS.search(text)
    if found is None:
        return None
    address = (found.group(1) if found.re is _BRACKETED_ADDRESS else found.group(0)).lower()
    local, _, domain = address.rpartition("@")
    return address if local and domain else None


def _address_key(address: str) -> str:
    """`address` with its local part's `+tag` dropped: the key a made source is named by."""
    local, _, domain = address.rpartition("@")
    return f"{local.split('+', 1)[0] or local}@{domain}"


def _instant(text: object) -> datetime | None:
    """`text` as an aware datetime, or None for anything else."""
    try:
        at = datetime.fromisoformat(str(text))
    except ValueError:
        return None
    return at if at.tzinfo is not None else None


def _plain_body(item: dict) -> str:
    text = item.get("text") or ""
    if isinstance(text, str) and text.strip():
        return text
    raw = item.get("html") or ""
    if not isinstance(raw, str):
        return ""
    return html.unescape(_HTML_TAG.sub(" ", _HTML_BREAK.sub("\n", raw)))


def own_address_keys(*texts: str) -> frozenset[str]:
    """The `+tag`-less key of every address in `texts`: the reader's own, compared by key."""
    return frozenset(
        _address_key(address.lower()) for text in texts for address in _ADDRESS.findall(text or "")
    )


class Forward(NamedTuple):
    """What a forward's body quotes: the original sender, and the original message's text."""

    sender: str
    text: str


# How far around a From line a quoted header block's other lines are looked for.
_BLOCK_REACH = 3
_QUOTE = re.compile(r"^[ \t]*>[ \t]?")


def _opens_block(lines: list[str], at: int) -> bool:
    """Whether the From line `lines[at]` heads a quoted header block, not a sentence.

    A marker line just above it says so, as a Gmail or Apple Mail forward opens; so does another
    header line just below it, as an Outlook forward, which has no marker, lists Sent and Subject.
    """
    above = [line for line in lines[max(0, at - _BLOCK_REACH) : at] if line.strip(" \t>")]
    if above and forwarded_marker_re().match(above[-1]):
        return True
    return any(
        forwarded_header_line_re().match(line) for line in lines[at + 1 : at + 1 + _BLOCK_REACH]
    )


def read_forward(body: str, own: frozenset[str]) -> Forward | None:
    """The first quoted header block in `body` whose sender is not the reader's, or None.

    Read in every language `forwarded_blocks` lists, whatever the subject says, since a mailbox's
    forward rule writes its own prefix or none. A block whose sender is one of `own` is the
    reader's earlier forward of their own mail, so the next block is read. The text is what
    follows the block's header lines, unquoted one level when the block was quoted, so the
    reader's note and the client's wrapper stay out of the article.
    """
    lines = body.splitlines()
    for at, line in enumerate(lines):
        found = forwarded_from_line_re().match(line)
        if found is None or not _opens_block(lines, at):
            continue
        sender = _address(found.group(1))
        if sender is None or _address_key(sender) in own:
            continue
        end = at + 1
        while end < len(lines) and forwarded_header_line_re().match(lines[end]):
            end += 1
        rest = lines[end:]
        if _QUOTE.match(line):
            rest = [_QUOTE.sub("", text, count=1) for text in rest]
        return Forward(sender, "\n".join(rest).strip())
    return None


def _addresses(source: SourceConfig) -> set[str]:
    """Each address `source.email_match` names, lower-cased and whole."""
    return {a.lower() for a in _ADDRESS.findall(source.email_match or "")}


def _match_address(address: str, sources: dict[str, SourceConfig]) -> SourceConfig | None:
    """The source that names `address` exactly, else one that names it `+tag` aside.

    Exact first, so a source made for `list+ai@x.test` takes that list's mail while one for
    `list@x.test` takes the rest. There is no substring match: every address an `email_match`
    names is found whole, `from:(a@x.test OR b@y.test)` included, so a substring could only add
    a wrong hit, `a@b.com` filed under `from:xa@b.com`.
    """
    named = [(source, _addresses(source)) for source in sources.values()]
    for source, addresses in named:
        if address in addresses:
            return source
    key = _address_key(address)
    for source, addresses in named:
        if key in {_address_key(a) for a in addresses}:
            return source
    return None


class _Filing(NamedTuple):
    """Where one mail goes: its source or none, the forward its sender was read from, or kept."""

    source: SourceConfig | None
    forward: Forward | None = None
    keep: bool = False


class CloudflareNewsletterSource:
    """Pull newsletters queued in the Cloudflare Email Worker's KV."""

    # Whether an issue may take a link that can name its recipient as its article URL; see
    # `newsletter_article`. A hosted tenant's source turns it off.
    recipient_links = True

    def _holds_unmatched(self, item: dict) -> bool:
        """Whether a message no source matches stays queued rather than being acked."""
        return False

    def _keeps(self, item: dict) -> bool:
        """Whether a message this run could not file is left queued for a later run.

        Only for `UNMATCHED_HOLD_DAYS` from its arrival: a message no run can file, such as a
        forward in a wording `forwarded_blocks` does not list, would otherwise be read again by
        every run, and the queue would only grow. A message with no time to count from, neither
        a `received_at` nor a readable `date`, stays queued: an ack would lose it unseen.
        """
        return self._arrived(item) is None or self._within_hold(item)

    def _arrived(self, item: dict) -> datetime | None:
        """When the message reached the queue: the newsletter Worker's `received_at`, or for
        one queued before the Worker wrote it, its `date`, which is the arrival time when the
        message had no Date header."""
        return _instant(item.get("received_at") or item.get("date"))

    def _within_hold(self, item: dict) -> bool:
        arrived = self._arrived(item)
        return arrived is not None and self._now() - arrived < timedelta(days=UNMATCHED_HOLD_DAYS)

    def _hold(self, ids: list[str]) -> None:
        """Mark messages this run left queued; the KV queue has no such mark."""

    def _source_cap(self) -> int | None:
        """How many sources this deployment may hold in all, or None for no cap."""
        return None

    def __init__(
        self,
        worker_url: str,
        token: str,
        ack: bool = True,
        create_source: Callable[[SourceConfig], bool] | None = None,
        own_addresses: frozenset[str] = frozenset(),
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._worker_url = worker_url.rstrip("/")
        self._now = now
        self._token = token
        # A pull queue deletes what it hands over. `ack=False` is how a preview
        # reads the queue without emptying it; `bootstrap` sets it for a dry run.
        self._ack_enabled = ack
        # Writes a new source and says whether it did. None is a preview's: the source an unknown
        # sender would get is resolved as a run resolves it, and written nowhere.
        self._create_source = create_source
        # The reader's own addresses, by `own_address_keys`: mail from one is a forward.
        self._own = own_addresses
        # The URL of each article this source read from such a forward: a run pulls once.
        self.forwarded: set[str] = set()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    def _file(self, item: dict, sources: dict[str, SourceConfig], created: list[str]) -> _Filing:
        """Where this mail goes: a source, made for it when its sender is new and may be.

        A From that is the reader's is a forward, whatever its subject, and is told before any
        source is matched, so even a source that names the reader's address never takes a
        forward: its quoted sender (`read_forward`) is matched by `_match_address`, exactly
        and then `+tag` aside, or made a source, and a forward whose sender cannot be read is
        kept queued, so no source is ever made for the reader's own address. Any other From is
        matched the same way or makes a source for its key. Mail from anyone else is never read
        for a block, so a newsletter that quotes a forward stays its own sender's. A preview
        files every mail as a run does and writes nothing.
        """
        sender = item.get("from")
        address = _address(sender) if isinstance(sender, str) else None
        if address is None:
            return _Filing(None)
        if _address_key(address) not in self._own:
            found = _match_address(address, sources)
            if found is not None:
                return _Filing(found)
            return self._new_source(item, address, sources, created)
        forward = read_forward(_plain_body(item), self._own)
        if forward is None:
            return _Filing(None, keep=True)
        found = _match_address(forward.sender, sources)
        if found is not None:
            return _Filing(found, forward)
        return self._new_source(item, forward.sender, sources, created)._replace(forward=forward)

    def _new_source(
        self, item: dict, address: str, sources: dict[str, SourceConfig], created: list[str]
    ) -> _Filing:
        """A newsletter source for `address`, written and added to `sources`, or none.

        A system sender never gets one, so its mail is an unknown sender's. A subject is never
        read: one that starts "Re:" is a guess, not certain content, so its mail makes a source
        like any other. One a cap, a failed write or a name another source already holds
        refused is kept queued, as lenient as an unread forward: a later run, under the cap, with
        the store back or the name freed, makes it.
        """
        key = _address_key(address)
        if address in system_senders():
            return _Filing(None)
        if key in sources:
            # A row of that name whose `email_match` names another address, as a failed write.
            logger.info("Newsletter source not created, its name is taken: %s", key)
            return _Filing(None, keep=True)
        if len(created) >= MAX_NEW_SOURCES_PER_RUN:
            logger.info("Newsletter source not created, %d made this run: %s", len(created), key)
            return _Filing(None, keep=True)
        cap = self._source_cap()
        # A stopped source reads nothing, so it holds no place under the cap.
        held = sum(1 for s in sources.values() if s.review != SOURCE_REVIEW_DECLINED)
        if cap is not None and held >= cap:
            logger.info("Newsletter source not created, %d sources held: %s", held, key)
            return _Filing(None, keep=True)
        source = SourceConfig(
            name=key,
            type=NEWSLETTER_SOURCE_TYPE,
            lane=Lane.READ,
            email_match=f"from:{key}",
            review=SOURCE_REVIEW_NEW,
        )
        if self._create_source is None:
            logger.info("Preview: newsletter source would be created: %s", key)
        else:
            try:
                written = self._create_source(source)
            except Exception:  # noqa: BLE001 - a failed write keeps the mail for a later run
                logger.warning("Newsletter source write failed: %s", key, exc_info=True)
                return _Filing(None, keep=True)
            if not written:
                return _Filing(None, keep=True)
            logger.info("Newsletter source created: %s", key)
        sources[key] = source
        created.append(key)
        return _Filing(source)

    async def fetch_articles(
        self,
        after: datetime,
        before: datetime,
        sources: dict[str, SourceConfig],
    ) -> list[Article]:
        """Pull queued newsletters, expand into Articles, then ACK the queue.

        Mail no source matches and none can be made for is skipped and ACKed so it doesn't
        pile up, unless `_holds_unmatched` keeps it queued for a source added later. Mail a
        later run may yet file (a forward whose sender is unread, a sender a cap or a failed
        write left without its source) is left queued while `_keeps` says so. Mail
        from a stopped source is acked unread. ACK runs after articles are built; a crash between
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
        # The run's own copy, which the sources this run creates join; the caller's stays as given.
        run_sources = dict(sources)
        created: list[str] = []
        for item in queued:
            item_id = item.get("id")
            if item_id is not None:
                processed_ids.append(item_id)
            try:
                filing = self._file(item, run_sources, created)
                source = filing.source
                if filing.keep:
                    if item_id is not None and self._keeps(item):
                        held.add(item_id)
                        logger.info("Newsletter kept for a later run: %s", item.get("from"))
                        continue
                    logger.info("Newsletter kept past its hold, acked: %s", item.get("from"))
                    continue
                if source is None:
                    if item_id is not None and self._holds_unmatched(item):
                        held.add(item_id)
                        logger.info("Newsletter from unknown sender held: %s", item.get("from"))
                        continue
                    logger.info("Newsletter from unknown sender skipped: %s", item.get("from"))
                    continue
                if source.review == SOURCE_REVIEW_DECLINED:
                    # Acked, never held: the reader deleted this sender's source.
                    logger.info("Newsletter from declined source acked: %s", source.name)
                    continue
                forward = filing.forward
                payload = {
                    # A forward's issue is its quoted sender's, from their message on.
                    "from": forward.sender if forward else item.get("from", ""),
                    "subject": item.get("subject", ""),
                    "html": item.get("html", ""),
                    "text": forward.text if forward else item.get("text", ""),
                    "headers": {"Date": item.get("date", "")},
                }
                parsed = parse_newsletter(payload, source.name)
                art = newsletter_article(parsed, source, recipient_links=self.recipient_links)
                if art is not None:
                    articles.append(art)
                    if forward is not None:
                        self.forwarded.add(art.url)
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

# Ids a hosted ack deletes in one statement, far under the 100 bound values a statement takes.
TENANT_ACK_CHUNK = 50


class TenantNewsletterSource(CloudflareNewsletterSource):
    """Pull a hosted tenant's newsletters from its own Durable Object, through the run's gateway.

    The hosted Worker's email() handler stores each message sent to the tenant's random address
    in the tenant's `newsletters` table (`adapters/store/hosted_schema.sql`, ADR-0034). The
    gateway answers only this tenant, so the pull and its ack, a delete by id, reach no other
    tenant's queue. Matching, parsing and the ack-after-build order are the KV source's.

    A message no source matches and none is made for (a system sender, a forward whose block we
    cannot read, a sender past the run's or the tenant's cap) stays queued for
    `UNMATCHED_HOLD_DAYS` from its arrival, so an issue that came before its source reaches the
    first run after it is added; an older one is acked. A held message is stamped `held_at`, and
    the Worker evicts the oldest stamped one when the queue is full, so held mail never crowds out
    new mail.

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
        create_source: Callable[[SourceConfig], bool] | None = None,
        own_addresses: frozenset[str] = frozenset(),
    ) -> None:
        self._d1 = d1
        self._ack_enabled = ack
        self._now = now
        self._create_source = create_source
        self._own = own_addresses
        self.forwarded = set()

    def _source_cap(self) -> int | None:
        """The hosted policy's cap, which the Worker holds at a save and a run's write skips."""
        from cyris.adapters.hosted import hosted_policy

        return hosted_policy()["max_sources"]

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

    def _arrived(self, item: dict) -> datetime | None:
        """The Worker's `received_at`, which every row of a tenant's queue has."""
        return _instant(item.get("received_at"))

    def _holds_unmatched(self, item: dict) -> bool:
        return self._within_hold(item)

    def _keeps(self, item: dict) -> bool:
        """Kept as unmatched mail is held: every row carries `received_at`, and a full queue
        evicts what is held, so the hold bounds it."""
        return self._holds_unmatched(item)

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
