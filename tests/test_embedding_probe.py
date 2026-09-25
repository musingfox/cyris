"""`probe_embedder`: one real embedding call, reported as a check, never raising."""

import asyncio

import httpx
import pytest

# Imported before any test patches httpx.AsyncClient: the LLM SDKs bootstrap pulls
# in subclass it at import time, and cannot subclass the patched factory.
import cyris.bootstrap  # noqa: F401
from cyris.diagnostics.doctor import probe_embedder

pytestmark = pytest.mark.integration


class Embeddings(list):
    """Every request the embedders send, answered by the handler a test installs."""

    def __init__(self) -> None:
        super().__init__()
        self.handler = lambda request: httpx.Response(500)

    def answer(self, handler) -> None:
        self.handler = handler


@pytest.fixture
def requests(monkeypatch) -> Embeddings:
    """Route the embedders' own httpx client here, as test_embedding does."""
    seen = Embeddings()
    real = httpx.AsyncClient

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return seen.handler(request)

    def factory(*args, **kwargs):
        return real(*args, **{**kwargs, "transport": httpx.MockTransport(handle)})

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return seen


@pytest.fixture(autouse=True)
def keys(monkeypatch) -> None:
    monkeypatch.setenv("CLOUDFLARE_EMBEDDING_API_TOKEN", "sentinel-cf-token")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct")
    monkeypatch.setenv("GEMINI_API_KEY", "sentinel-key-123")


async def test_workers_ai_answering_is_ok_with_its_dimensions(requests) -> None:
    requests.answer(
        lambda r: httpx.Response(200, json={"success": True, "result": {"data": [[0.1, 0.2]]}})
    )

    check = await probe_embedder("workers_ai", "")

    assert check.status == "ok"
    assert check.detail == "workers_ai · @cf/baai/bge-m3 answered (2 dimensions)"


async def test_a_refusal_carries_the_providers_words_and_not_the_key(requests) -> None:
    requests.answer(lambda r: httpx.Response(400, json={"error": {"message": "model not found"}}))

    check = await probe_embedder("gemini", "typo-model")

    assert check.status == "fail"
    assert check.detail.startswith("typo-model refused: 400 ")
    assert "model not found" in check.detail
    assert "sentinel-key-123" not in check.detail


async def test_a_missing_token_fails_without_a_request(requests, monkeypatch) -> None:
    monkeypatch.delenv("CLOUDFLARE_EMBEDDING_API_TOKEN")

    check = await probe_embedder("workers_ai", "")

    assert check.status == "fail"
    assert check.detail == "CLOUDFLARE_EMBEDDING_API_TOKEN is not set"
    assert requests == []


async def test_a_transport_error_is_a_failure_not_an_exception(requests) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    requests.answer(refuse)

    check = await probe_embedder("gemini", "")

    assert check.status == "fail"
    assert "sentinel-key-123" not in check.detail


async def test_a_rate_limited_embedder_fails_within_the_probe_bound(requests, monkeypatch) -> None:
    """The embedders back off on 429 for ~90 s; a Save must not wait that out."""
    monkeypatch.setattr("cyris.diagnostics.doctor.EMBEDDING_PROBE_TIMEOUT_SECONDS", 0.2)
    requests.answer(lambda r: httpx.Response(429, json={"error": {"message": "slow down"}}))

    check = await asyncio.wait_for(probe_embedder("gemini", ""), timeout=5)

    assert check.status == "fail"
    assert check.detail.startswith("gemini-embedding-001 did not answer within 0.2 s")
    assert "rate-limiting" in check.detail
    assert "save with vote similarity off" in check.detail
    assert "sentinel-key-123" not in check.detail


def test_the_probe_builds_its_embedder_the_way_a_run_does() -> None:
    """One provider switch: a probe that built its own could pass where a run fails."""
    import inspect

    from cyris.diagnostics import doctor

    source = inspect.getsource(doctor)
    assert "GeminiEmbedder(" not in source
    assert "WorkersAIEmbedder(" not in source
