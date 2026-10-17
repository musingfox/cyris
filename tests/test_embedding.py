"""Tests for the embedding adapters."""

import json
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from cyris.adapters.embedding import GeminiEmbedder, WorkersAIEmbedder
from cyris.entrypoints.cli import app

pytestmark = pytest.mark.integration

runner = CliRunner()


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


async def test_the_gemini_embedder_sends_its_key_as_a_header_not_a_query_string(patched_client):
    """Same rule the Gemini LLM client already follows (`gemini_client.py`).

    A key in `?key=` is printed by httpx's own INFO request log and embedded in
    every `HTTPStatusError`; a key in a header is in neither.
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"embeddings": [{"values": [3.0, 4.0]}]})

    patched_client(handler)
    await GeminiEmbedder("secret-gemini-key").embed(["今彩539開獎"])

    [request] = seen
    assert request.url.query == b""
    assert "secret-gemini-key" not in str(request.url)
    assert request.headers["x-goog-api-key"] == "secret-gemini-key"


async def test_workers_ai_returns_unit_vectors_and_records_what_it_charged(patched_client):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=workers_response([[3.0, 4.0]]))

    patched_client(handler)
    embedder = WorkersAIEmbedder("tok", "acct")

    [vector] = await embedder.embed(["今彩539開獎"])

    [request] = seen
    assert request.method == "POST"
    assert request.url.path == "/client/v4/accounts/acct/ai/run/@cf/baai/bge-m3"
    assert request.headers["Authorization"] == "Bearer tok"
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
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"success": False, "errors": [{"code": 10000}]})

    patched_client(handler)
    embedder = WorkersAIEmbedder("tok", "acct")

    with pytest.raises(RuntimeError, match="refused"):
        await embedder.embed(["anything"])

    assert len(seen) == 1


async def test_gemini_asks_for_truncated_dimensions_when_told_to(patched_client):
    seen = {}
    sent: list[httpx.Request] = []

    def handler(request):
        sent.append(request)
        seen.update(json.loads(request.content)["requests"][0])
        return httpx.Response(200, json={"embeddings": [{"values": [0.0, 2.0]}]})

    patched_client(handler)
    embedder = GeminiEmbedder("k", output_dimensions=768)

    [vector] = await embedder.embed(["title"])

    assert len(sent) == 1
    assert seen["outputDimensionality"] == 768
    # The API returns non-unit vectors below 3072d; the adapter has to fix that.
    assert vector == pytest.approx([0.0, 1.0])


async def test_an_empty_input_never_reaches_the_api(patched_client):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(500)

    patched_client(handler)
    embedder = WorkersAIEmbedder("tok", "acct")

    assert await embedder.embed([]) == []
    assert seen == []
    assert embedder.usage.requests == 0


def test_each_provider_builds_its_own_embedder(monkeypatch):
    from fakes import make_config

    from cyris.bootstrap import build_embedder

    monkeypatch.setenv("CLOUDFLARE_EMBEDDING_API_TOKEN", "t")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a")
    monkeypatch.setenv("GEMINI_API_KEY", "g")

    cfg = make_config()
    cfg.app.vote_similarity.enabled = True

    cfg.app.vote_similarity.provider = "workers_ai"
    assert type(build_embedder(cfg)).__name__ == "WorkersAIEmbedder"

    cfg.app.vote_similarity.provider = "gemini"
    assert type(build_embedder(cfg)).__name__ == "GeminiEmbedder"


def test_the_feature_off_means_no_embedder_at_all():
    from fakes import make_config

    from cyris.bootstrap import build_embedder

    assert build_embedder(make_config()) is None


def test_a_tracked_topic_needs_the_embedder_with_vote_similarity_off(monkeypatch, tmp_path):
    from fakes import make_config

    from cyris.bootstrap import build_deps
    from cyris.config import AgentVaultConfig
    from cyris.domain.models import TrackedTopic

    monkeypatch.setenv("GEMINI_API_KEY", "g")
    cfg = make_config(agent_vault=AgentVaultConfig(path=tmp_path))
    cfg.app.vote_similarity.provider = "gemini"
    cfg.tracked_topics = [
        TrackedTopic(name="A", description="B", threshold=0.7, model="gemini-embedding-001")
    ]

    deps = build_deps(cfg)

    assert type(deps.embedder).__name__ == "GeminiEmbedder"
    # The model a topic is matched against is the resolved one, never the empty setting.
    assert cfg.app.vote_similarity.model == ""
    assert deps.embedding_model == "gemini-embedding-001"


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


def test_every_embedder_carries_the_usage_the_port_declares() -> None:
    """`Embedder.usage` is on the Protocol because `embed-compare` reads it.

    A Protocol only lists what callers use, so an adapter missing a field would
    otherwise fail mid-comparison — after both providers have been paid for.
    """
    from cyris.service_layer.ports import EmbeddingUsage

    declared = set(EmbeddingUsage.__annotations__)
    for embedder in (
        GeminiEmbedder(api_key="k"),
        WorkersAIEmbedder(api_token="t", account_id="a"),
    ):
        assert declared <= set(vars(embedder.usage)), type(embedder).__name__
        assert callable(embedder.usage.as_dict)


def test_a_provider_that_reports_no_cost_says_none_rather_than_zero() -> None:
    """Gemini's batchEmbedContents returns bare vectors: None is the honest answer."""
    usage = GeminiEmbedder(api_key="k").usage.as_dict()

    assert usage["input_tokens"] is None
    assert usage["neurons"] is None


class _WindowEmbedder:
    """Three downvotes on [1, 0]; each candidate's cosine to them is written in its title."""

    COSINES = {f"C{i}": 0.40 + 0.01 * i for i in range(11)} | {"Out": 0.95}

    def __init__(self) -> None:
        self.usage = None

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            if text.startswith("S"):
                out.append([1.0, 0.0])
            else:
                c = self.COSINES[text]
                out.append([c, (1 - c * c) ** 0.5])
        return out


def _vote_sim_world(tmp_path: Path, monkeypatch) -> Path:
    from datetime import UTC, datetime, timedelta

    from fakes import settings_toml

    from cyris.adapters.store import ArticleStore
    from cyris.domain.models import Article, Tier

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("cyris.bootstrap.build_embedder", lambda cfg: _WindowEmbedder())
    (tmp_path / "cyris.toml").write_text(
        settings_toml(**{"vote_similarity.enabled": True, "vote_similarity.provider": "gemini"})
    )
    sources = tmp_path / "sources.yaml"
    sources.write_bytes((Path(__file__).parent.parent / "sources.example.yaml").read_bytes())
    now = datetime.now(UTC)
    titles = [f"S{i}" for i in range(3)] + list(_WindowEmbedder.COSINES)
    articles = [
        Article(
            id=i,
            title=title,
            url=f"https://example.com/{title}",
            content="c",
            published_at=now - timedelta(hours=1),
            source_name="Src",
            source_tier=Tier.SUMMARIZE,
        )
        for i, title in enumerate(titles)
    ]
    store = ArticleStore(tmp_path / "agent-vault")
    store.save(articles)
    seeds = [f"https://example.com/S{i}" for i in range(3)]
    store.reject(seeds, "not_interested")
    store.update_triage_timestamp(seeds, now)
    return sources


def test_vote_sim_marks_the_one_outlier_it_would_suppress(tmp_path: Path, monkeypatch) -> None:
    sources = _vote_sim_world(tmp_path, monkeypatch)

    result = runner.invoke(app, ["vote-sim", "--sources", str(sources)])

    assert result.exit_code == 0, result.output
    section = result.output.split("WOULD SUPPRESS")[1].split("CLOSEST TO UPVOTES")[0]
    rows = [line for line in section.splitlines() if line.startswith("  ")]
    assert len(rows) == 1 and rows[0].endswith("Out")
    assert "outlier margin 3.5" in result.output
    assert "threshold" not in result.output


def test_vote_sim_refuses_a_threshold_option(tmp_path: Path, monkeypatch) -> None:
    sources = _vote_sim_world(tmp_path, monkeypatch)

    result = runner.invoke(app, ["vote-sim", "--threshold", "0.5", "--sources", str(sources)])

    assert result.exit_code == 2
