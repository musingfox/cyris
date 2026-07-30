"""Tests for promotion sync from the Cloudflare Worker."""

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import httpx
import pytest

from cyris.adapters.promotions import PromotedArticle, pull_promotions, sync_promotions
from cyris.adapters.store import ArticleStore
from cyris.domain.models import Article, ArticleState, Tier
from cyris.learn.triage_feedback import collect_triage_feedback

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
                source_tier=Tier.SUMMARIZE,
            )
        ]
    )
    return s


@pytest.fixture
def vault(tmp_path):
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    return vault_path


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


def test_sync_exports_and_acks(store, vault):
    payload = [{"url": "https://example.com/stored", "digest_date": "2026-07-10"}]
    with (
        patch("cyris.adapters.promotions.httpx.get", return_value=_mock_response(payload)),
        patch(
            "cyris.adapters.promotions.httpx.post", return_value=_mock_response({"ok": True})
        ) as post,
        patch("cyris.adapters.promotions.fetch_full_markdown", return_value=None),
    ):
        exported = sync_promotions(WORKER_URL, TOKEN, store, vault)

    assert exported == 1
    assert (vault / "Reading").exists()
    assert len(list((vault / "Reading").glob("*.md"))) == 1
    [article] = store.get_by_urls(["https://example.com/stored"])
    assert article.state == ArticleState.ACCEPTED
    assert post.call_args.kwargs["json"] == {"urls": ["https://example.com/stored"]}


def test_sync_exports_full_text_markdown(store, vault):
    """When defuddle extraction succeeds, the exported note carries the clean markdown."""
    payload = [{"url": "https://example.com/stored"}]
    with (
        patch("cyris.adapters.promotions.httpx.get", return_value=_mock_response(payload)),
        patch("cyris.adapters.promotions.httpx.post", return_value=_mock_response({"ok": True})),
        patch(
            "cyris.adapters.promotions.fetch_full_markdown",
            return_value="Clean **markdown** body",
        ) as fetch_md,
    ):
        exported = sync_promotions(WORKER_URL, TOKEN, store, vault)

    assert exported == 1
    fetch_md.assert_called_once_with(
        "https://example.com/stored", "Full text here", "~/.bun/bin/bun"
    )
    [note] = (vault / "Reading").glob("*.md")
    text = note.read_text()
    assert "Clean **markdown** body" in text
    assert "Full text here" not in text


def test_sync_routes_votes(store, vault):
    """up accepts without exporting, down rejects, and both are stamped as human labels."""
    store.save(
        [
            Article(
                id=2,
                title="Disliked",
                url="https://example.com/nope",
                content="body",
                published_at=datetime.now(UTC),
                source_name="Source A",
                source_tier=Tier.SUMMARIZE,
            )
        ]
    )
    payload = [
        {"url": "https://example.com/stored", "vote": "up"},
        {"url": "https://example.com/nope", "vote": "down"},
    ]
    with (
        patch("cyris.adapters.promotions.httpx.get", return_value=_mock_response(payload)),
        patch("cyris.adapters.promotions.httpx.post", return_value=_mock_response({"ok": True})),
        patch("cyris.adapters.promotions.fetch_full_markdown") as fetch_md,
    ):
        exported = sync_promotions(WORKER_URL, TOKEN, store, vault)

    assert exported == 0
    assert not (vault / "Reading").exists()
    fetch_md.assert_not_called()

    [up] = store.get_by_urls(["https://example.com/stored"])
    [down] = store.get_by_urls(["https://example.com/nope"])
    assert up.state == ArticleState.ACCEPTED
    assert down.state == ArticleState.REJECTED
    assert up.triaged_at is not None and down.triaged_at is not None


def test_sync_missing_url_acks_without_export(store, vault):
    payload = [{"url": "https://example.com/unknown"}]
    with (
        patch("cyris.adapters.promotions.httpx.get", return_value=_mock_response(payload)),
        patch(
            "cyris.adapters.promotions.httpx.post", return_value=_mock_response({"ok": True})
        ) as post,
    ):
        exported = sync_promotions(WORKER_URL, TOKEN, store, vault)

    assert exported == 0
    assert not (vault / "Reading").exists()
    assert post.call_args.kwargs["json"] == {"urls": ["https://example.com/unknown"]}


def test_voted_articles_reach_learning_but_pipeline_verdicts_do_not(store, vault):
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
                source_tier=Tier.SUMMARIZE,
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
        sync_promotions(WORKER_URL, TOKEN, store, vault)

    feedback = collect_triage_feedback(store, days=14, min_triaged=1)
    assert [a.url for a in feedback.accepted_articles] == ["https://example.com/stored"]
    assert feedback.rejected_articles == []


def test_sync_empty_queue_skips_ack(store, vault):
    with (
        patch("cyris.adapters.promotions.httpx.get", return_value=_mock_response([])),
        patch("cyris.adapters.promotions.httpx.post") as post,
    ):
        exported = sync_promotions(WORKER_URL, TOKEN, store, vault)

    assert exported == 0
    post.assert_not_called()


def test_sync_export_failure_skips_ack(store, tmp_path):
    payload = [{"url": "https://example.com/stored"}]
    missing_vault = tmp_path / "no-such-vault"
    with (
        patch("cyris.adapters.promotions.httpx.get", return_value=_mock_response(payload)),
        patch("cyris.adapters.promotions.httpx.post") as post,
        patch("cyris.adapters.promotions.fetch_full_markdown", return_value=None),
        pytest.raises(ValueError),
    ):
        sync_promotions(WORKER_URL, TOKEN, store, missing_vault)

    post.assert_not_called()
