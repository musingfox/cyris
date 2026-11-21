"""A hosted tenant's queued votes, applied at the start of its run (ADR-0039)."""

from datetime import UTC, datetime

import pytest
from fakes import SqliteD1

from cyris.adapters.hosted_votes import sync_hosted_votes
from cyris.adapters.store.d1_store import D1ArticleStore
from cyris.adapters.store.tenant_gateway import apply_hosted_schema
from cyris.adapters.store.topics import D1TopicStore
from cyris.domain.models import Article, ArticleState, Lane, TopicRecord

pytestmark = pytest.mark.integration

URLS = [f"https://news.example/{i}" for i in range(4)]
QUEUE = "INSERT INTO hosted_votes (url, vote, ts) VALUES (?, ?, ?)"


@pytest.fixture
def tenant():
    """A tenant's database with four stored articles, three of them one topic."""
    db = SqliteD1()
    apply_hosted_schema(db)
    articles = D1ArticleStore(db)
    articles.save(
        [
            Article(
                id=i,
                title=f"Article {i}",
                url=url,
                content="text",
                published_at=datetime(2026, 11, 20, tzinfo=UTC),
                source_name="Source",
                source_lane=Lane.READ,
            )
            for i, url in enumerate(URLS)
        ]
    )
    topics = D1TopicStore(db)
    topics.save_topics(
        "2026-11-20",
        "morning",
        [TopicRecord(id="sand", name="Sand", urls=URLS[:3])],
        datetime(2026, 11, 20, tzinfo=UTC),
    )
    return db, articles, topics


def _stamped(db) -> dict[str, tuple[str, bool]]:
    rows = db.query("SELECT url, state, triaged_at FROM stored_articles").rows
    return {r["url"]: (r["state"], r["triaged_at"] is not None) for r in rows}


def test_a_queued_vote_stamps_its_article_as_a_human_decision_and_leaves_the_queue(tenant):
    db, articles, topics = tenant
    db.query(QUEUE, [URLS[3], "down", "2026-11-20T09:00:00+00:00"])

    result = sync_hosted_votes(db, articles, topics)

    assert result.votes == 1
    assert _stamped(db)[URLS[3]] == (str(ArticleState.REJECTED), True)
    assert db.query("SELECT count(*) AS n FROM hosted_votes").rows[0]["n"] == 0


def test_only_a_voted_article_is_stamped(tenant):
    db, articles, topics = tenant
    db.query(QUEUE, [URLS[3], "up", "2026-11-20T09:00:00+00:00"])

    sync_hosted_votes(db, articles, topics)

    assert [url for url, (_, stamped) in _stamped(db).items() if stamped] == [URLS[3]]


def test_no_queued_vote_stamps_nothing(tenant):
    db, articles, topics = tenant

    result = sync_hosted_votes(db, articles, topics)

    assert result.votes == 0
    assert not any(stamped for _, stamped in _stamped(db).values())
    assert topics.topic_votes() == []


def test_a_topic_click_is_one_topic_vote_through_the_one_write_rule(tenant):
    """The same `apply_promotions` as `promote-sync`: three posts of one click are one vote."""
    db, articles, topics = tenant
    for i, url in enumerate(URLS[:3]):
        db.query(QUEUE, [url, "up", f"2026-11-20T09:00:00.00{i}+00:00"])

    sync_hosted_votes(db, articles, topics)

    assert [(v.topic_id, v.direction) for v in topics.topic_votes()] == [("sand", 1)]
    assert all(stamped for url, (_, stamped) in _stamped(db).items() if url in URLS[:3])


def test_a_reader_who_changes_their_mind_ends_on_the_later_vote(tenant):
    db, articles, topics = tenant
    db.query(QUEUE, [URLS[3], "up", "2026-11-20T09:00:00+00:00"])
    db.query(QUEUE, [URLS[3], "down", "2026-11-20T09:05:00+00:00"])

    sync_hosted_votes(db, articles, topics)

    assert _stamped(db)[URLS[3]] == (str(ArticleState.REJECTED), True)


def test_a_vote_queued_while_the_run_applies_the_others_stays_queued(tenant):
    db, articles, topics = tenant
    db.query(QUEUE, [URLS[3], "up", "2026-11-20T09:00:00+00:00"])
    record = articles.record_verdicts

    def record_then_a_late_vote(**kwargs):
        found = record(**kwargs)
        db.query(QUEUE, [URLS[0], "up", "2026-11-20T09:00:01+00:00"])
        return found

    articles.record_verdicts = record_then_a_late_vote

    sync_hosted_votes(db, articles, topics)

    assert [r["url"] for r in db.query("SELECT url FROM hosted_votes").rows] == [URLS[0]]


def test_a_failed_write_leaves_every_vote_queued_for_the_next_run(tenant):
    db, articles, topics = tenant
    db.query(QUEUE, [URLS[3], "up", "2026-11-20T09:00:00+00:00"])

    def fails(**_kwargs):
        raise RuntimeError("gateway down")

    articles.record_verdicts = fails

    with pytest.raises(RuntimeError):
        sync_hosted_votes(db, articles, topics)

    assert db.query("SELECT count(*) AS n FROM hosted_votes").rows[0]["n"] == 1
    assert not any(stamped for _, stamped in _stamped(db).values())
