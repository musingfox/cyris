"""Tests for the embedding adapters."""

import json
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from cyris.adapters.embedding import DETAIL_CHARS, GeminiEmbedder, WorkersAIEmbedder
from cyris.entrypoints.cli import app
from cyris.service_layer.ports import EmbeddingRejectedError

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

    # Captured once: a second `install` in one test must replace the handler, not wrap the
    # first factory, whose transport would then win.
    real = httpx.AsyncClient

    def install(handler):
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

    from fakes import seeded_sqlite

    from cyris.adapters.store.d1_store import D1ArticleStore
    from cyris.domain.models import Article, Lane

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("cyris.bootstrap.build_embedder", lambda cfg: _WindowEmbedder())
    state = tmp_path / "state.sqlite3"
    (tmp_path / "cyris.toml").write_text(f'[store]\nsqlite_path = "{state}"\n')
    client = seeded_sqlite(
        state, **{"vote_similarity.enabled": True, "vote_similarity.provider": "gemini"}
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
            source_lane=Lane.READ,
        )
        for i, title in enumerate(titles)
    ]
    store = D1ArticleStore(client)
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


@pytest.mark.parametrize("status", [400, 401, 403, 404])
async def test_a_refusing_4xx_raises_the_typed_error_without_the_url(patched_client, status):
    """The grading reads `status_code`, never the message, and the message has no URL."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text='{"error": "API key not valid"}')

    patched_client(handler)
    for embedder in (GeminiEmbedder("k"), WorkersAIEmbedder("tok", "acct-secret-id")):
        with pytest.raises(EmbeddingRejectedError) as raised:
            await embedder.embed(["anything"])
        assert raised.value.status_code == status
        assert raised.value.detail == "API key not valid"
        assert "acct-secret-id" not in str(raised.value)
        assert "https://" not in str(raised.value)


@pytest.mark.parametrize("status", [500, 503])
async def test_a_5xx_stays_the_plain_http_error(patched_client, status):
    patched_client(lambda request: httpx.Response(status))

    for embedder in (GeminiEmbedder("k"), WorkersAIEmbedder("tok", "acct")):
        with pytest.raises(httpx.HTTPStatusError):
            await embedder.embed(["anything"])


async def test_a_429_is_retried_and_never_the_typed_rejection(patched_client, backoff_sleeps):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(429)

    patched_client(handler)
    for embedder in (GeminiEmbedder("k"), WorkersAIEmbedder("tok", "acct")):
        with pytest.raises(RuntimeError, match="rate-limited"):
            await embedder.embed(["anything"])

    assert len(calls) == 10


async def test_a_refusal_that_quotes_the_key_back_has_it_scrubbed(patched_client):
    """The detail reaches Discord, mail and the run summary, so the adapter scrubs it."""
    for embedder, secret in (
        (GeminiEmbedder("sentinel-gemini-key"), "sentinel-gemini-key"),
        (WorkersAIEmbedder("sentinel-cf-token", "acct"), "sentinel-cf-token"),
    ):
        patched_client(
            lambda request, secret=secret: httpx.Response(
                401, json={"error": {"message": f"bad credential {secret}"}}
            )
        )
        with pytest.raises(EmbeddingRejectedError) as raised:
            await embedder.embed(["anything"])
        assert secret not in raised.value.detail
        assert secret not in str(raised.value)
        assert raised.value.detail == "bad credential [redacted]"


async def test_the_detail_is_the_providers_message_for_either_error_shape(patched_client):
    gemini = {"error": {"code": 400, "message": "API key not valid. Please pass a valid API key."}}
    cloudflare = {"success": False, "errors": [{"code": 10000, "message": "Authentication error"}]}
    for embedder, body, status, message in (
        (GeminiEmbedder("k"), gemini, 400, "API key not valid. Please pass a valid API key."),
        (WorkersAIEmbedder("tok", "acct"), cloudflare, 403, "Authentication error"),
    ):
        patched_client(lambda request, body=body, status=status: httpx.Response(status, json=body))
        with pytest.raises(EmbeddingRejectedError) as raised:
            await embedder.embed(["anything"])
        assert raised.value.status_code == status
        assert raised.value.detail == message


@pytest.mark.parametrize("secret", ["", "k", "short"])
async def test_a_secret_too_short_to_be_a_key_is_never_scrubbed(patched_client, secret):
    """Replacing "k" would turn "key" into "[redacted]ey"; an empty secret matches everywhere."""
    patched_client(
        lambda request: httpx.Response(
            401, json={"error": {"message": "API key not valid. Please pass a valid API key."}}
        )
    )

    with pytest.raises(EmbeddingRejectedError) as raised:
        await GeminiEmbedder(secret).embed(["anything"])

    assert raised.value.detail == "API key not valid. Please pass a valid API key."


_REAL_LENGTH_KEY = "AIzaSyD-9tSrke72PouQMnMX-a7eZSW0jkFMBWY"  # 39 characters, a fake


def _body_with_the_key_straddling_the_trim(shape: str, key: str) -> httpx.Response:
    """A 401 whose text carries `key` starting 10 characters before `DETAIL_CHARS`."""
    start = DETAIL_CHARS - 10
    if shape == "non-json":
        return httpx.Response(401, text="x" * start + key)
    opening = '{"detail": "'
    return httpx.Response(401, text=opening + "x" * (start - len(opening)) + key + '"}')


@pytest.mark.parametrize("shape", ["non-json", "json-without-error"])
async def test_a_key_straddling_the_trim_leaves_no_prefix_behind(patched_client, shape):
    """Trimmed before the scrub, a key cut at character 300 would keep its first ten."""
    assert len(_REAL_LENGTH_KEY) == 39
    patched_client(lambda request: _body_with_the_key_straddling_the_trim(shape, _REAL_LENGTH_KEY))
    for embedder in (
        GeminiEmbedder(_REAL_LENGTH_KEY),
        WorkersAIEmbedder(_REAL_LENGTH_KEY, "acct"),
    ):
        with pytest.raises(EmbeddingRejectedError) as raised:
            await embedder.embed(["anything"])
        assert "[redacted]" in raised.value.detail
        assert len(raised.value.detail) <= DETAIL_CHARS
        for size in range(10, len(_REAL_LENGTH_KEY) + 1):
            assert _REAL_LENGTH_KEY[:size] not in str(raised.value)
