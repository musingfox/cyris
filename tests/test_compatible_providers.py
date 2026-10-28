"""The openai_compatible providers: a reader-named endpoint, tested as one.

One key idea across both adapters: the address and the model are the reader's,
the key is optional (a LAN or localhost endpoint may answer with none), and a
missing address or model is refused with its own name — never a silent fall back
to some default that would call a server that is not theirs.
"""

import httpx
import pytest
import respx

from cyris.adapters.embedding import OpenAICompatibleEmbedder
from cyris.adapters.openai_client import OpenAIClient
from cyris.bootstrap import build_llm
from cyris.config import LLMProviderConfig, VoteSimilarityConfig
from cyris.diagnostics.doctor import probe_embedder, probe_llm

pytestmark = pytest.mark.unit

BASE = "http://llm.example:8000/v1"


def _cfg(**kwargs) -> LLMProviderConfig:
    return LLMProviderConfig(provider="openai_compatible", model="m", **kwargs)


class TestTheCompatibleLLM:
    def test_without_an_address_nothing_is_built(self, monkeypatch):
        monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "k")

        assert build_llm(_cfg(base_url="")) is None

    def test_a_named_address_builds_a_client_pointing_at_it(self, monkeypatch):
        monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "k")

        built = build_llm(_cfg(base_url=BASE))

        assert built.model == "m"
        assert built._url == f"{BASE}/chat/completions"
        # A reader-named endpoint keeps its own reasoning behaviour: the
        # effort goes out only when the reader chose one.
        assert built._reasoning_effort is None

    def test_without_a_model_nothing_is_built(self, monkeypatch):
        monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "k")

        no_model = LLMProviderConfig(provider="openai_compatible", model="", base_url=BASE)
        assert build_llm(no_model) is None

    async def test_the_probes_failure_names_what_is_missing(self, monkeypatch):
        monkeypatch.delenv("OPENAI_COMPATIBLE_API_KEY", raising=False)

        assert "OPENAI_COMPATIBLE_BASE_URL is empty" in (await probe_llm(_cfg(base_url=""))).detail
        assert (
            "OPENAI_COMPATIBLE_MODEL is empty"
            in (
                await probe_llm(
                    LLMProviderConfig(provider="openai_compatible", model="", base_url=BASE)
                )
            ).detail
        )

    async def test_it_answers_the_readers_endpoint_with_or_without_a_key(self):
        def ok(*args, **kwargs):
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": '{"ok": true}'}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 2},
                },
            )

        for api_key in ("", "k"):
            async with respx.mock:
                route = respx.post(f"{BASE}/chat/completions").mock(side_effect=ok)
                client = OpenAIClient(api_key, model="m", base_url=BASE)
                response = await client.complete("Hi.")

                assert route.call_count == 1
                assert response.text == '{"ok": true}'
                assert route.calls[0].request.url == f"{BASE}/chat/completions"
                # A keyless endpoint carries no Bearer at all, not an empty one.
                if api_key:
                    assert route.calls[0].request.headers["authorization"] == "Bearer k"
                else:
                    assert "authorization" not in route.calls[0].request.headers

    def test_the_official_address_is_untouched(self):
        # No base_url keeps the module constant; only the compatible choice moves.
        assert "api.openai.com" in OpenAIClient("k", model="m")._url


class TestTheCompatibleEmbedder:
    def test_without_an_address_it_is_refused_by_name(self, monkeypatch):
        monkeypatch.delenv("OPENAI_COMPATIBLE_API_KEY", raising=False)

        from cyris.bootstrap import make_embedder

        with pytest.raises(ValueError, match="OPENAI_COMPATIBLE_EMBEDDING_BASE_URL"):
            make_embedder("openai_compatible", "m", "")

    async def test_the_probe_refuses_a_missing_address_without_touching_the_store(
        self, monkeypatch
    ):
        monkeypatch.delenv("OPENAI_COMPATIBLE_API_KEY", raising=False)

        check = await probe_embedder("openai_compatible", "m", "")

        assert check.status == "fail"
        assert "OPENAI_COMPATIBLE_EMBEDDING_BASE_URL" in check.detail

    async def test_it_answers_the_readers_endpoint_and_reports_the_batch(self, monkeypatch):
        monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "k")
        monkeypatch.delenv("CLOUDFLARE_EMBEDDING_API_TOKEN", raising=False)
        monkeypatch.setattr("cyris.adapters.embedding.OpenAICompatibleEmbedder.BATCH_SIZE", 1)

        async with respx.mock:
            route = respx.post(f"{BASE}/embeddings").mock(
                side_effect=lambda request: httpx.Response(
                    200, json={"data": [{"embedding": [0.6, 0.8]}], "usage": {"prompt_tokens": 3}}
                )
            )
            embedder = OpenAICompatibleEmbedder(api_key="k", model="m", base_url=BASE)
            vectors = await embedder.embed(["a", "b"])

        # One request per batch, unit-length vectors, every text answered.
        assert route.call_count == 2
        assert len(vectors) == 2
        assert abs(sum(v * v for v in vectors[0]) - 1.0) < 1e-6
        assert embedder.usage.input_tokens == 6

    def test_the_embedder_reads_its_own_key_not_the_llms(self, monkeypatch):
        from cyris.bootstrap import make_embedder

        monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "llm-key")
        monkeypatch.setenv("OPENAI_COMPATIBLE_EMBEDDING_API_KEY", "emb-key")

        embedder = make_embedder("openai_compatible", "m", BASE)

        assert embedder._api_key == "emb-key"

    def test_a_config_row_answers_the_new_choices(self, monkeypatch):
        monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "k")

        vote = VoteSimilarityConfig(
            enabled=True, provider="openai_compatible", model="m", base_url=BASE, max_seeds=1
        )
        assert vote.provider == "openai_compatible" and vote.base_url == BASE

        # The LLM's own config carries its address apart from the embedder's.
        assert _cfg(base_url=BASE).base_url == BASE
