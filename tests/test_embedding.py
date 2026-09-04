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


async def test_workers_ai_returns_unit_vectors_and_records_what_it_charged(patched_client):
    patched_client(lambda r: httpx.Response(200, json=workers_response([[3.0, 4.0]])))
    embedder = WorkersAIEmbedder("tok", "acct")

    [vector] = await embedder.embed(["今彩539開獎"])

    assert vector == pytest.approx([0.6, 0.8])  # normalised
    assert embedder.usage.input_tokens == 24
    assert embedder.usage.neurons == pytest.approx(0.0258)
    assert embedder.usage.requests == 1


async def test_a_repeated_text_is_embedded_once_per_call(patched_client):
    """No cache between calls (see the module docstring), but a run that asks for
    the same headline twice must not pay twice inside that run."""
    calls = []

    def handler(request):
        calls.append(json.loads(request.content)["text"])
        return httpx.Response(200, json=workers_response([[1.0, 0.0]]))

    patched_client(handler)
    embedder = WorkersAIEmbedder("tok", "acct")

    vectors = await embedder.embed(["同一個標題", "同一個標題"])

    assert calls == [["同一個標題"]]
    assert vectors == [[1.0, 0.0], [1.0, 0.0]]
    assert embedder.usage.requests == 1


async def test_a_200_carrying_success_false_is_an_error(patched_client):
    """Cloudflare answers 200 with success:false, so raise_for_status alone lets it pass."""
    patched_client(
        lambda r: httpx.Response(200, json={"success": False, "errors": [{"code": 10000}]})
    )
    embedder = WorkersAIEmbedder("tok", "acct")

    with pytest.raises(RuntimeError, match="refused"):
        await embedder.embed(["anything"])


async def test_gemini_asks_for_truncated_dimensions_when_told_to(patched_client):
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content)["requests"][0])
        return httpx.Response(200, json={"embeddings": [{"values": [0.0, 2.0]}]})

    patched_client(handler)
    embedder = GeminiEmbedder("k", output_dimensions=768)

    [vector] = await embedder.embed(["title"])

    assert seen["outputDimensionality"] == 768
    # The API returns non-unit vectors below 3072d; the adapter has to fix that.
    assert vector == pytest.approx([0.0, 1.0])


async def test_an_empty_input_never_reaches_the_api(patched_client):
    patched_client(lambda r: httpx.Response(500))
    embedder = WorkersAIEmbedder("tok", "acct")

    assert await embedder.embed([]) == []
    assert embedder.usage.requests == 0


def test_each_provider_carries_its_own_calibration(monkeypatch):
    """Switching provider without switching threshold is how this feature dies
    silently: bge-m3's cosines run lower, so 0.68 would suppress nothing."""
    from cyris.bootstrap import build_embedder, embedding_threshold
    from cyris.config import AppConfig, Config

    monkeypatch.setenv("CLOUDFLARE_EMBEDDING_API_TOKEN", "t")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a")
    monkeypatch.setenv("GEMINI_API_KEY", "g")

    cfg = Config(app=AppConfig(), sources={})
    cfg.app.vote_similarity.enabled = True

    cfg.app.vote_similarity.provider = "workers_ai"
    assert embedding_threshold(cfg) == 0.53
    assert type(build_embedder(cfg)).__name__ == "WorkersAIEmbedder"

    cfg.app.vote_similarity.provider = "gemini"
    assert embedding_threshold(cfg) == 0.68
    assert type(build_embedder(cfg)).__name__ == "GeminiEmbedder"


def test_a_configured_threshold_still_wins():
    from cyris.bootstrap import embedding_threshold
    from cyris.config import AppConfig, Config

    cfg = Config(app=AppConfig(), sources={})
    cfg.app.vote_similarity.threshold = 0.6

    assert embedding_threshold(cfg) == 0.6


def test_the_feature_off_means_no_embedder_at_all():
    from cyris.bootstrap import build_embedder
    from cyris.config import AppConfig, Config

    assert build_embedder(Config(app=AppConfig(), sources={})) is None


def test_module_default_models_match_provider_defaults() -> None:
    """The adapters' fallback models must equal provider_defaults.json's.

    `bootstrap.build_embedder` and `cyris embed-compare` both name the model from
    the JSON, so a constant here that drifts from it would only surface for a
    caller that omits the model — silently embedding with the wrong one.
    """
    from cyris.adapters.embedding import GEMINI_MODEL, WORKERS_AI_MODEL
    from cyris.bootstrap import embedding_defaults

    assert embedding_defaults("gemini")["model"] == GEMINI_MODEL
    assert embedding_defaults("workers_ai")["model"] == WORKERS_AI_MODEL
