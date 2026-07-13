"""Tests for promotion sync from the Cloudflare Worker."""

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import httpx
import pytest

from cyris.adapters.promotions import PromotedArticle, pull_promotions, sync_promotions
from cyris.adapters.store import ArticleStore
from cyris.domain.models import Article, ArticleState, Tier

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
    ):
        exported = sync_promotions(WORKER_URL, TOKEN, store, vault)

    assert exported == 1
    assert (vault / "Reading").exists()
    assert len(list((vault / "Reading").glob("*.md"))) == 1
    [article] = store.get_by_urls(["https://example.com/stored"])
    assert article.state == ArticleState.ACCEPTED
    assert post.call_args.kwargs["json"] == {"urls": ["https://example.com/stored"]}


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
        pytest.raises(ValueError),
    ):
        sync_promotions(WORKER_URL, TOKEN, store, missing_vault)

    post.assert_not_called()
