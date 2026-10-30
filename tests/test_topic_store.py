from datetime import UTC, datetime, timedelta

import pytest
from fakes import SqliteD1

from cyris.adapters.store.json_topics import JsonTopicStore
from cyris.adapters.store.topics import D1TopicStore
from cyris.domain.models import TopicKind, TopicRecord, TopicTag, TopicVote

pytestmark = pytest.mark.unit

T0 = datetime(2026, 10, 30, 0, 0, tzinfo=UTC)


@pytest.fixture(params=["d1", "json"])
def store(request, tmp_path):
    if request.param == "d1":
        return D1TopicStore(SqliteD1())
    return JsonTopicStore(tmp_path)


def _topic(tid: str, *urls: str, tags: tuple[tuple[str, TopicKind], ...] = ()) -> TopicRecord:
    return TopicRecord(
        id=tid, name=tid.upper(), urls=list(urls), tags=[TopicTag(tag=t, kind=k) for t, k in tags]
    )


def test_a_saved_topic_answers_for_each_of_its_urls(store) -> None:
    store.save_topics("2026-10-30", "morning", [_topic("a", "u1", "u2"), _topic("b", "u3")], T0)

    assert store.topics_of_urls(["u1", "u2", "u3", "unknown"]) == {
        "u1": "a",
        "u2": "a",
        "u3": "b",
    }


def test_the_newest_save_holding_a_url_names_its_topic(store) -> None:
    store.save_topics("2026-10-30", "morning", [_topic("old", "u1")], T0)
    store.save_topics("2026-10-30", "evening", [_topic("new", "u1")], T0 + timedelta(hours=12))

    assert store.topics_of_urls(["u1"]) == {"u1": "new"}


def test_a_rerun_of_an_issue_replaces_its_members_and_drops_the_orphan(store) -> None:
    store.save_topics("2026-10-30", "morning", [_topic("gone", "u1")], T0)
    store.save_topics("2026-10-30", "morning", [_topic("kept", "u1")], T0 + timedelta(minutes=5))

    assert store.topics_of_urls(["u1"]) == {"u1": "kept"}


def test_a_voted_topic_survives_a_rerun_that_drops_it(store) -> None:
    store.save_topics("2026-10-30", "morning", [_topic("voted", "u1")], T0)
    store.record_topic_votes([TopicVote(topic_id="voted", direction=1, clicked_at=T0)])
    store.save_topics("2026-10-30", "morning", [], T0 + timedelta(minutes=5))

    assert store.topics_of_urls(["u1"]) == {}
    # Recorded again: the vote is still on file, so its topic was kept for it.
    assert store.record_topic_votes([TopicVote(topic_id="voted", direction=1, clicked_at=T0)]) == 0


def test_a_vote_is_recorded_once_per_topic_and_click_time(store) -> None:
    vote = TopicVote(topic_id="a", direction=-1, clicked_at=T0)
    later = TopicVote(topic_id="a", direction=1, clicked_at=T0 + timedelta(seconds=1))

    assert store.record_topic_votes([vote, vote]) == 1
    assert store.record_topic_votes([vote, later]) == 1


def test_d1_keeps_the_four_kinds_of_data_in_their_tables() -> None:
    db = SqliteD1()
    store = D1TopicStore(db)
    tags = (("anthropic", TopicKind.ENTITY), ("ai", TopicKind.DOMAIN))
    store.save_topics("2026-10-30", "morning", [_topic("a", "u1", tags=tags)], T0)
    store.save_topics(
        "2026-10-31", "morning", [_topic("a", "u9", tags=tags)], T0 + timedelta(days=1)
    )
    store.record_topic_votes([TopicVote(topic_id="a", direction=1, clicked_at=T0)])

    topic = db.query("SELECT name, first_seen, last_seen FROM topics").rows
    assert topic == [
        {
            "name": "A",
            "first_seen": "2026-10-30T00:00:00.000000+00:00",
            "last_seen": "2026-10-31T00:00:00.000000+00:00",
        }
    ]
    members = db.query(
        "SELECT article_url, digest_date FROM topic_members ORDER BY article_url"
    ).rows
    assert members == [
        {"article_url": "u1", "digest_date": "2026-10-30"},
        {"article_url": "u9", "digest_date": "2026-10-31"},
    ]
    assert db.query("SELECT tag, kind FROM topic_tags ORDER BY tag").rows == [
        {"tag": "ai", "kind": "domain"},
        {"tag": "anthropic", "kind": "entity"},
    ]
    assert db.query("SELECT direction, clicked_at FROM topic_votes").rows == [
        {"direction": 1, "clicked_at": "2026-10-30T00:00:00.000000+00:00"}
    ]


def test_d1_chunks_an_issue_larger_than_one_statement_can_bind() -> None:
    store = D1TopicStore(SqliteD1())
    topics = [_topic(f"t{i}", f"u{i}") for i in range(120)]

    store.save_topics("2026-10-30", "morning", topics, T0)

    assert len(store.topics_of_urls([f"u{i}" for i in range(120)])) == 120
    assert len(store.topics_by_ids([f"t{i}" for i in range(120)])) == 120


def test_recent_topics_are_those_last_seen_in_the_window_with_their_tags(store) -> None:
    tags = (("kmt", TopicKind.ENTITY), ("taiwan", TopicKind.REGION))
    store.save_topics("2026-10-20", "morning", [_topic("old", "u1")], T0 - timedelta(days=10))
    store.save_topics("2026-10-30", "morning", [_topic("now", "u2", tags=tags)], T0)
    store.save_topics("2026-10-31", "morning", [_topic("later", "u3")], T0 + timedelta(days=1))

    (topic,) = store.recent_topics(T0 - timedelta(days=7), T0)

    assert (topic.id, topic.name, topic.last_seen) == ("now", "NOW", T0)
    assert [(t.tag, t.kind) for t in topic.tags] == [
        ("kmt", TopicKind.ENTITY),
        ("taiwan", TopicKind.REGION),
    ]


def test_a_rerun_that_tags_a_topic_differently_replaces_its_tags(store) -> None:
    before = (("kmt", TopicKind.ENTITY), ("taiwan", TopicKind.REGION))
    after = (("dpp", TopicKind.ENTITY),)
    store.save_topics("2026-10-30", "morning", [_topic("a", "u1", tags=before)], T0)
    store.save_topics(
        "2026-10-30", "morning", [_topic("a", "u1", tags=after)], T0 + timedelta(minutes=5)
    )

    (topic,) = store.topics_by_ids(["a"])

    assert [(t.tag, t.kind) for t in topic.tags] == [("dpp", TopicKind.ENTITY)]


def test_topics_by_ids_names_only_stored_topics(store) -> None:
    store.save_topics("2026-10-30", "morning", [_topic("a", "u1")], T0)

    assert [t.id for t in store.topics_by_ids(["a", "missing"])] == ["a"]


def test_every_recorded_vote_reads_back(store) -> None:
    votes = [
        TopicVote(topic_id="a", direction=1, clicked_at=T0),
        TopicVote(topic_id="b", direction=-1, clicked_at=T0 + timedelta(hours=1)),
    ]
    store.record_topic_votes(votes)

    assert sorted(store.topic_votes(), key=lambda v: v.topic_id) == votes
