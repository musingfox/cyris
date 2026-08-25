"""Choosing a provider from the settings page, and the config write behind it."""

import pytest
from aiohttp.test_utils import TestClient, TestServer

from cyris.config import LLMProviderConfig, load_config, write_llm_provider
from cyris.entrypoints.triage_server import TriageServer

EXAMPLE = """\
# Cyris configuration
[general]
timezone = "Asia/Taipei"

[llm_provider]
# "anthropic" (ANTHROPIC_API_KEY), "gemini" (GEMINI_API_KEY)
provider = "anthropic"
model = "claude-sonnet-4-6"             # e.g. "gemini-2.5-flash" when provider = "gemini"

[digest]
max_articles_per_digest = 400
"""


@pytest.fixture
def config_file(tmp_path):
    path = tmp_path / "cyris.toml"
    path.write_text(EXAMPLE, encoding="utf-8")
    return path


class TestWriteLlmProvider:
    def test_replaces_both_values(self, config_file):
        write_llm_provider(config_file, "gemini", "gemini-3.7-flash")

        text = config_file.read_text()
        assert 'provider = "gemini"' in text
        assert 'model = "gemini-3.7-flash"' in text
        assert "claude-sonnet-4-6" not in text

    def test_keeps_the_comments_that_explain_the_file(self, config_file):
        """The reason this is a line edit and not a TOML round-trip."""
        write_llm_provider(config_file, "gemini", "gemini-3.7-flash")

        text = config_file.read_text()
        assert "# Cyris configuration" in text
        assert "(GEMINI_API_KEY)" in text
        assert 'e.g. "gemini-2.5-flash"' in text  # the trailing comment survives too

    def test_leaves_every_other_table_alone(self, config_file):
        write_llm_provider(config_file, "openai", "gpt-5.6-luna")

        text = config_file.read_text()
        assert 'timezone = "Asia/Taipei"' in text
        assert "max_articles_per_digest = 400" in text

    def test_the_result_still_loads(self, config_file, tmp_path, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        sources = tmp_path / "sources.yaml"
        sources.write_text("sources: []\n")

        write_llm_provider(config_file, "gemini", "gemini-3.7-flash")
        cfg = load_config(config_path=config_file, sources_path=sources)

        assert cfg.app.llm_provider.provider == "gemini"
        assert cfg.app.llm_provider.model == "gemini-3.7-flash"

    def test_an_empty_model_means_the_provider_default(self, config_file, tmp_path, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        sources = tmp_path / "sources.yaml"
        sources.write_text("sources: []\n")

        write_llm_provider(config_file, "gemini", "")
        cfg = load_config(config_path=config_file, sources_path=sources)

        assert cfg.app.llm_provider.model == ""  # bootstrap fills in the default

    def test_adds_keys_the_file_omitted(self, tmp_path):
        """Both keys are optional, so a minimal file has neither line to replace."""
        path = tmp_path / "cyris.toml"
        path.write_text("[llm_provider]\n\n[digest]\nmax_articles_per_digest = 5\n")

        write_llm_provider(path, "gemini", "gemini-3.7-flash")

        text = path.read_text()
        assert 'provider = "gemini"' in text
        assert 'model = "gemini-3.7-flash"' in text
        assert "max_articles_per_digest = 5" in text

    def test_a_table_at_the_end_of_the_file_still_gets_the_keys(self, tmp_path):
        path = tmp_path / "cyris.toml"
        path.write_text('[general]\ntimezone = "UTC"\n\n[llm_provider]\n')

        write_llm_provider(path, "openai", "gpt-5.6-luna")

        assert 'provider = "openai"' in path.read_text()

    def test_a_file_without_the_table_is_refused_rather_than_guessed_at(self, tmp_path):
        path = tmp_path / "cyris.toml"
        path.write_text('[general]\ntimezone = "UTC"\n')

        with pytest.raises(KeyError, match="llm_provider"):
            write_llm_provider(path, "gemini", "gemini-3.7-flash")


class FakeStore:
    """The settings routes never touch the store."""


async def _client(config_path=None, llm_provider=None):
    server = TriageServer(FakeStore(), config_path=config_path, llm_provider=llm_provider)
    client = TestClient(TestServer(server._app))
    await client.start_server()
    return client


class TestSettingsApi:
    async def test_reports_the_current_choice_and_what_else_is_available(
        self, config_file, monkeypatch
    ):
        monkeypatch.setenv("GEMINI_API_KEY", "g")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        client = await _client(config_file, LLMProviderConfig(provider="gemini", model="x"))

        data = await (await client.get("/api/settings")).json()
        await client.close()

        assert data["provider"] == "gemini"
        assert data["model"] == "x"
        assert data["writable"] is True
        by_name = {p["name"]: p for p in data["providers"]}
        assert by_name["gemini"]["configured"] is True
        assert by_name["openai"]["configured"] is False
        assert by_name["openai"]["env_var"] == "OPENAI_API_KEY"
        assert by_name["gemini"]["default_model"]  # something to fall back to

    async def test_an_unknown_provider_is_rejected_before_any_call(self, config_file):
        client = await _client(config_file, LLMProviderConfig(provider="gemini"))

        res = await client.post("/api/settings", json={"provider": "mistral", "model": "x"})
        body = await res.json()
        await client.close()

        assert res.status == 400
        assert "mistral" in body["error"]
        assert config_file.read_text().count("anthropic") == 2  # file untouched

    async def test_a_provider_with_no_key_is_rejected_and_names_the_variable(
        self, config_file, monkeypatch
    ):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        client = await _client(config_file, LLMProviderConfig(provider="gemini"))

        res = await client.post("/api/settings", json={"provider": "openai", "model": ""})
        body = await res.json()
        await client.close()

        assert res.status == 400
        assert "OPENAI_API_KEY" in body["error"]

    async def test_a_model_the_provider_refuses_never_reaches_the_file(
        self, config_file, monkeypatch
    ):
        """The failure this endpoint exists to prevent: a typo saved, and a
        digest run discovering it tomorrow morning after the fetch."""
        monkeypatch.setenv("GEMINI_API_KEY", "g")

        async def boom(*a, **kw):
            raise RuntimeError("models/gemini-3.7-flashh is not found")

        monkeypatch.setattr("cyris.adapters.gemini_client.GeminiClient.complete", boom)
        client = await _client(config_file, LLMProviderConfig(provider="gemini"))

        res = await client.post(
            "/api/settings", json={"provider": "gemini", "model": "gemini-3.7-flashh"}
        )
        body = await res.json()
        await client.close()

        assert res.status == 400
        assert "is not found" in body["error"]
        assert "gemini-3.7-flashh" not in config_file.read_text()
        assert 'provider = "anthropic"' in config_file.read_text()

    async def test_a_model_that_answers_is_written(self, config_file, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "g")

        async def ok(self, *a, **kw):
            from cyris.service_layer.ports import LLMResponse

            return LLMResponse(text="pong", input_tokens=1, output_tokens=1)

        monkeypatch.setattr("cyris.adapters.gemini_client.GeminiClient.complete", ok)
        client = await _client(config_file, LLMProviderConfig(provider="anthropic"))

        res = await client.post(
            "/api/settings", json={"provider": "gemini", "model": "gemini-3.7-flash"}
        )
        body = await res.json()
        await client.close()

        assert res.status == 200 and body["ok"] is True
        assert 'provider = "gemini"' in config_file.read_text()
        assert 'model = "gemini-3.7-flash"' in config_file.read_text()

    async def test_without_a_config_path_the_page_refuses_to_save(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "g")
        client = await _client(None, None)

        listing = await (await client.get("/api/settings")).json()
        res = await client.post("/api/settings", json={"provider": "gemini", "model": ""})
        await client.close()

        assert listing["writable"] is False
        assert res.status == 409
