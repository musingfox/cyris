"""Tests for the embedding adapters."""

import json

import httpx
import pytest

from cyris.adapters.embedding import GeminiEmbedder, WorkersAIEmbedder


def workers_response(vectors: list[list[float]], tokens: int = 24, neurons: float = 0.0258):
    return {
        "success": True,
        "result": {
            "data": vectors,
            "shape": [len(vectors), len(vectors[0])],
            "meta": {
                "cost_metric_name_1": "input_tokens",
                "cost_metric_value_1": tokens,
                "neurons": neurons,
            },
        },
    }


def stub(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


@pytest.fixture
def patched_client(monkeypatch):
    """Route the adapters' httpx client at a handler the test controls."""

    def install(handler):
        real = httpx.AsyncClient

        def factory(*args, **kwargs):
            return real(*args, **{**kwargs, "transport": stub(handler)})

        monkeypatch.setattr(httpx, "AsyncClient", factory)

    return install


async def test_workers_ai_returns_unit_vectors_and_records_what_it_charged(
    tmp_path, patched_client
):
    patched_client(lambda r: httpx.Response(200, json=workers_response([[3.0, 4.0]])))
    embedder = WorkersAIEmbedder("tok", "acct", tmp_path / "c.json")

    [vector] = await embedder.embed(["今彩539開獎"])

    assert vector == pytest.approx([0.6, 0.8])  # normalised
    assert embedder.usage.input_tokens == 24
    assert embedder.usage.neurons == pytest.approx(0.0258)
    assert embedder.usage.requests == 1


async def test_a_cached_text_never_reaches_the_api_again(tmp_path, patched_client):
    calls = []

    def handler(request):
        calls.append(json.loads(request.content)["text"])
        return httpx.Response(200, json=workers_response([[1.0, 0.0]]))

    patched_client(handler)
    cache = tmp_path / "c.json"
    await WorkersAIEmbedder("tok", "acct", cache).embed(["同一個標題"])

    second = WorkersAIEmbedder("tok", "acct", cache)
    [vector] = await second.embed(["同一個標題"])

    assert calls == [["同一個標題"]], "the second run re-fetched a text it had on disk"
    assert vector == [1.0, 0.0]
    assert second.usage.requests == 0


async def test_a_200_carrying_success_false_is_an_error(tmp_path, patched_client):
    """Cloudflare answers 200 with success:false, so raise_for_status alone lets it pass."""
    patched_client(
        lambda r: httpx.Response(200, json={"success": False, "errors": [{"code": 10000}]})
    )
    embedder = WorkersAIEmbedder("tok", "acct", tmp_path / "c.json")

    with pytest.raises(RuntimeError, match="refused"):
        await embedder.embed(["anything"])


async def test_gemini_asks_for_truncated_dimensions_when_told_to(tmp_path, patched_client):
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content)["requests"][0])
        return httpx.Response(200, json={"embeddings": [{"values": [0.0, 2.0]}]})

    patched_client(handler)
    embedder = GeminiEmbedder("k", tmp_path / "c.json", output_dimensions=768)

    [vector] = await embedder.embed(["title"])

    assert seen["outputDimensionality"] == 768
    # The API returns non-unit vectors below 3072d; the adapter has to fix that.
    assert vector == pytest.approx([0.0, 1.0])


async def test_an_unreadable_cache_starts_empty_rather_than_crashing(tmp_path, patched_client):
    cache = tmp_path / "c.json"
    cache.write_text("{ truncated", encoding="utf-8")
    patched_client(lambda r: httpx.Response(200, json=workers_response([[1.0, 0.0]])))

    [vector] = await WorkersAIEmbedder("tok", "acct", cache).embed(["t"])

    assert vector == [1.0, 0.0]
