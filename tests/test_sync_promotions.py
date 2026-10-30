"""Tests for promotion sync from the Cloudflare Worker."""

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import httpx
import pytest

from cyris.adapters.promotions import (
    PromotedArticle,
    pull_promotions,
    sync_promotions,
    topic_votes,
)
from cyris.adapters.store import ArticleStore
from cyris.adapters.store.json_topics import JsonTopicStore
from cyris.domain.models import Article, ArticleState, Lane, TopicRecord
from cyris.domain.triage import RejectReason

pytestmark = pytest.mark.integration

WORKER_URL = "https://promote.test.workers.dev"
TOKEN = "test-token"


def _mock_response(json_data, status_code=200):
    resp = MagicMock()
    resp.json.return_value = json_data
    resp.status_code = status_code
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=MagicMock(), response=resp
        )
    else:
        resp.raise_for_status.return_value = None
    return resp


@pytest.fixture
def store(tmp_path) -> ArticleStore:
    s = ArticleStore(tmp_path / "agent-vault")
    s.save(
        [
            Article(
                id=1,
                title="Stored Article",
                url="https://example.com/stored",
                content="Full text here",
                published_at=datetime.now(UTC),
                source_name="Source A",
                source_lane=Lane.READ,
            )
        ]
    )
    return s


def test_pull_promotions_parses_response():
    payload = [{"url": "https://example.com/a", "digest_date": "2026-07-10", "ts": "t"}]
    with patch("cyris.adapters.promotions.httpx.get", return_value=_mock_response(payload)) as get:
        result = pull_promotions(WORKER_URL, TOKEN)

    assert result == [
        PromotedArticle(url="https://example.com/a", digest_date="2026-07-10", ts="t")
    ]
    assert get.call_args.kwargs["headers"]["Authorization"] == f"Bearer {TOKEN}"


def test_pull_promotions_raises_on_error():
    with (
        patch("cyris.adapters.promotions.httpx.get", return_value=_mock_response({}, 401)),
        pytest.raises(httpx.HTTPStatusError),
    ):
        pull_promotions(WORKER_URL, TOKEN)


def test_sync_accepts_and_acks(store):
    payload = [{"url": "https://example.com/stored", "digest_date": "2026-07-10"}]
    with (
        patch("cyris.adapters.promotions.httpx.get", return_value=_mock_response(payload)),
        patch(
            "cyris.adapters.promotions.httpx.post", return_value=_mock_response({"ok": True})
        ) as post,
    ):
        synced = sync_promotions(WORKER_URL, TOKEN, store)

    assert synced == 1
    [article] = store.get_by_urls(["https://example.com/stored"])
    assert article.state == ArticleState.ACCEPTED
    assert post.call_args.kwargs["json"] == {"urls": ["https://example.com/stored"]}


def test_sync_legacy_deep_vote_accepts(store):
    """Digests published before the deep-read button was dropped still send its vote."""
    payload = [{"url": "https://example.com/stored", "vote": "deep"}]
    with (
        patch("cyris.adapters.promotions.httpx.get", return_value=_mock_response(payload)),
        patch("cyris.adapters.promotions.httpx.post", return_value=_mock_response({"ok": True})),
    ):
        synced = sync_promotions(WORKER_URL, TOKEN, store)

    assert synced == 1
    [article] = store.get_by_urls(["https://example.com/stored"])
    assert article.state == ArticleState.ACCEPTED


def test_reject_reason_supports_specific_human_feedback():
    assert RejectReason("not_interested") is RejectReason.NOT_INTERESTED
    assert RejectReason("already_known") is RejectReason.ALREADY_KNOWN
    assert RejectReason("manual_triage") is RejectReason.MANUAL_TRIAGE


def test_sync_routes_votes(store):
    """up accepts, down rejects, and both are stamped as human labels."""
    store.save(
        [
            Article(
                id=2,
                title="Disliked",
                url="https://example.com/nope",
                content="body",
                published_at=datetime.now(UTC),
                source_name="Source A",
                source_lane=Lane.READ,
            )
        ]
    )
    payload = [
        {"url": "https://example.com/stored", "vote": "up"},
        {
            "url": "https://example.com/nope",
            "vote": "down",
            "digest_date": "2026-08-28",
        },
    ]
    with (
        patch("cyris.adapters.promotions.httpx.get", return_value=_mock_response(payload)),
        patch("cyris.adapters.promotions.httpx.post", return_value=_mock_response({"ok": True})),
    ):
        synced = sync_promotions(WORKER_URL, TOKEN, store)

    assert synced == 2

    [up] = store.get_by_urls(["https://example.com/stored"])
    [down] = store.get_by_urls(["https://example.com/nope"])
    assert up.state == ArticleState.ACCEPTED
    assert down.state == ArticleState.REJECTED
    assert up.rejection_reason is None
    assert down.rejection_reason == RejectReason.NOT_INTERESTED
    assert up.triaged_at is not None and down.triaged_at is not None


def test_sync_missing_url_acks_anyway(store):
    payload = [{"url": "https://example.com/unknown"}]
    with (
        patch("cyris.adapters.promotions.httpx.get", return_value=_mock_response(payload)),
        patch(
            "cyris.adapters.promotions.httpx.post", return_value=_mock_response({"ok": True})
        ) as post,
    ):
        synced = sync_promotions(WORKER_URL, TOKEN, store)

    assert synced == 1
    assert post.call_args.kwargs["json"] == {"urls": ["https://example.com/unknown"]}


def test_voted_articles_reach_learning_but_pipeline_verdicts_do_not(store):
    """The whole point of the buttons: a vote becomes a label, a pipeline verdict does not."""
    store.save(
        [
            Article(
                id=3,
                title="Machine Accepted",
                url="https://example.com/machine",
                content="body",
                published_at=datetime.now(UTC),
                source_name="Source A",
                source_lane=Lane.READ,
            )
        ]
    )
    # Exactly what a digest run writes — same state, no human stamp.
    store.update_states(
        {"https://example.com/machine": (ArticleState.ACCEPTED, None)},
        digest_date=datetime.now(UTC).strftime("%Y-%m-%d"),
    )

    payload = [{"url": "https://example.com/stored", "vote": "up"}]
    with (
        patch("cyris.adapters.promotions.httpx.get", return_value=_mock_response(payload)),
        patch("cyris.adapters.promotions.httpx.post", return_value=_mock_response({"ok": True})),
    ):
        sync_promotions(WORKER_URL, TOKEN, store)

    # Only the vote leaves a human stamp; the digest's own verdict does not.
    # That stamp is what vote similarity seeds from — see `vote_similarity._voted`.
    stamped = [a for a in store.list_articles(state=ArticleState.ACCEPTED) if a.triaged_at]
    assert [a.url for a in stamped] == ["https://example.com/stored"]


def test_sync_empty_queue_skips_ack(store):
    with (
        patch("cyris.adapters.promotions.httpx.get", return_value=_mock_response([])),
        patch("cyris.adapters.promotions.httpx.post") as post,
    ):
        synced = sync_promotions(WORKER_URL, TOKEN, store)

    assert synced == 0
    post.assert_not_called()


def _sand_store(tmp_path) -> tuple[ArticleStore, JsonTopicStore]:
    articles = ArticleStore(tmp_path / "agent-vault")
    articles.save(
        [
            Article(
                id=i,
                title=f"Member {i}",
                url=f"https://news.example/{i}",
                content="",
                published_at=datetime.now(UTC),
                source_name="Wire",
                source_lane=Lane.SKIM,
            )
            for i in range(4)
        ]
    )
    topics = JsonTopicStore(tmp_path / "agent-vault")
    sand = TopicRecord(id="sand", name="Sand", urls=[f"https://news.example/{i}" for i in range(3)])
    topics.save_topics("2026-10-30", "morning", [sand], datetime(2026, 10, 30, tzinfo=UTC))
    return articles, topics


def _sync(payload, articles, topics) -> None:
    with (
        patch("cyris.adapters.promotions.httpx.get", return_value=_mock_response(payload)),
        patch("cyris.adapters.promotions.httpx.post", return_value=_mock_response({"ok": True})),
    ):
        sync_promotions(WORKER_URL, TOKEN, articles, topics)


def test_a_sand_vote_is_one_topic_vote_at_its_click_and_stamps_every_member(tmp_path):
    articles, topics = _sand_store(tmp_path)
    payload = [
        {"url": f"https://news.example/{i}", "vote": "down", "ts": f"2026-10-30T09:00:00.00{i}Z"}
        for i in range(3)
    ]

    _sync(payload, articles, topics)

    stored = articles.get_by_urls([f"https://news.example/{i}" for i in range(3)])
    assert all(a.state == ArticleState.REJECTED and a.triaged_at for a in stored)
    data = json.loads((tmp_path / "agent-vault" / "topics.json").read_text())
    assert data["votes"] == [
        {"topic_id": "sand", "direction": -1, "clicked_at": "2026-10-30T09:00:00.000000+00:00"}
    ]


def test_a_vote_on_a_url_no_topic_holds_only_stamps_it(tmp_path):
    articles, topics = _sand_store(tmp_path)
    payload = [{"url": "https://news.example/3", "vote": "up", "ts": "2026-10-30T09:00:00Z"}]

    _sync(payload, articles, topics)

    (stored,) = articles.get_by_urls(["https://news.example/3"])
    assert stored.state == ArticleState.ACCEPTED and stored.triaged_at
    data = json.loads((tmp_path / "agent-vault" / "topics.json").read_text())
    assert data["votes"] == []


def test_the_newest_click_on_a_topic_sets_its_direction():
    promotions = [
        PromotedArticle(url="a", vote="up", ts="2026-10-30T09:00:00Z"),
        PromotedArticle(url="b", vote="down", ts="2026-10-30T09:05:00Z"),
    ]

    (vote,) = topic_votes(promotions, {"a": "t", "b": "t"}, datetime.now(UTC))

    assert (vote.direction, vote.clicked_at) == (-1, datetime(2026, 10, 30, 9, 5, tzinfo=UTC))
