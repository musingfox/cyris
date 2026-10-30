"""Pull promote-button clicks from the Cloudflare Worker into the local store."""

import logging
from datetime import UTC, datetime, timedelta

import httpx
from pydantic import BaseModel

from cyris.adapters.store import ArticleStore
from cyris.domain.models import TopicVote
from cyris.domain.triage import RejectReason
from cyris.service_layer.ports import TopicRepository

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 15

# One click on a topic posts once per member URL, all at once, and the page gives up on
# a post after 10 seconds (VOTE_TIMEOUT_MS in _promote_script.html.j2), so the promote
# Worker stamps one click's posts within about 10 seconds of each other. A sync can ACK
# some of those posts before the rest land, so the next sync sees the same click again:
# a same-direction vote on the same topic within this window of a recorded one is that
# click, not a second vote. Sixty seconds leaves the 10 a wide margin; a reader voting
# the same way again within a minute means nothing new either.
ONE_CLICK_WINDOW = timedelta(seconds=60)


class PromotedArticle(BaseModel):
    url: str
    # "up" or "down". Legacy payloads (vote-less, or "deep") are anything-but-"down",
    # so they accept.
    #
    # The "deep" half is permanent, not a transition. The deep-read button shipped
    # 2026-07-30 (e235cc0) and was dropped 2026-08-08 (6931727), and the pages
    # published in between are still served: on 2026-09-05 the live archive answered
    # with 13 deep buttons across seven pages, 2026-07-31-morning through
    # 2026-08-06-evening. A published digest is never rewritten -- the deployed site
    # is the archive of record -- so this branch can only go when those seven pages
    # do. Re-check by grepping the live pages for data-vote="deep", not by counting
    # days since the removal.
    vote: str = "up"
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


def record_votes(
    store: ArticleStore, *, accepted: list[str], rejected: list[str], at: datetime
) -> None:
    """Write votes as human decisions: rejected, accepted, then stamped `triaged_at`.

    The one way a reader's verdict reaches the store. `/labels` writes its up and
    down answers through it too, so a blind label seeds vote similarity exactly
    as a digest vote does.
    """
    if rejected:
        store.reject(rejected, reason=RejectReason.NOT_INTERESTED)
    if accepted:
        store.accept(accepted)
    if rejected or accepted:
        store.update_triage_timestamp(rejected + accepted, at)


def topic_votes(
    promotions: list[PromotedArticle], topic_of: dict[str, str], now: datetime
) -> list[TopicVote]:
    """One vote per topic from its articles' votes, at the time it was clicked.

    A topic's vote group posts once per member URL, so one click arrives as several
    promotions a few milliseconds apart: the newest one's direction stands, timed by
    the first click of the agreeing run it ends, so a sync retried before its ACK
    records the same row again rather than a second vote.
    """
    by_topic: dict[str, list[tuple[datetime, int]]] = {}
    for p in promotions:
        topic = topic_of.get(p.url)
        if topic is None:
            continue
        clicked = datetime.fromisoformat(p.ts) if p.ts else now
        by_topic.setdefault(topic, []).append((clicked, -1 if p.vote == "down" else 1))
    votes = []
    for topic, clicks in by_topic.items():
        clicks.sort()
        direction = clicks[-1][1]
        # The run of agreeing clicks that ends the window: a click before the reader
        # last changed direction is not when this vote was cast.
        first = clicks[-1][0]
        for clicked, d in reversed(clicks):
            if d != direction:
                break
            first = clicked
        votes.append(TopicVote(topic_id=topic, direction=direction, clicked_at=first))
    return votes


def unrecorded(votes: list[TopicVote], recorded: list[TopicVote]) -> list[TopicVote]:
    """The votes no recorded vote already stands for (`ONE_CLICK_WINDOW`)."""
    return [
        v
        for v in votes
        if not any(
            r.topic_id == v.topic_id
            and r.direction == v.direction
            and abs(r.clicked_at - v.clicked_at) <= ONE_CLICK_WINDOW
            for r in recorded
        )
    ]


def sync_promotions(
    worker_url: str,
    token: str,
    store: ArticleStore,
    topics: TopicRepository | None = None,
) -> int:
    """Pull votes, apply them to the store, then ACK.

    Votes carry the only human-labelled signal in the pipeline, so every
    voted article gets a ``triaged_at`` stamp that tells it apart from the
    digest run's own accept/reject verdicts. A "down" vote rejects, anything
    else accepts.

    A voted URL that an issue put under a topic also writes one vote on that
    topic, at the click's time (`topic_votes`); a URL no topic holds, from an issue
    published before topics or from the raw page, is stamped and nothing more.

    Promotions whose URL is no longer in the store are logged and ACKed
    anyway. ACK happens only after the store is updated, so a failure here
    leaves promotions queued for the next run.

    Returns:
        Number of votes applied.

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
    now = datetime.now(UTC)
    record_votes(store, accepted=accepted, rejected=rejected, at=now)
    if topics is not None:
        voted = [p for p in promotions if p.url not in missing]
        votes = topic_votes(voted, topics.topics_of_urls([p.url for p in voted]), now)
        if votes:
            topics.record_topic_votes(unrecorded(votes, topics.topic_votes()))

    ack_promotions(worker_url, token, urls)
    logger.info("Synced %d vote(s) (%d down)", len(urls), len(rejected))
    return len(urls)
