"""Choosing a provider and the digest hours from the settings page."""

import pytest
from aiohttp.test_utils import TestClient, TestServer

from cyris.config import LLMProviderConfig
from cyris.entrypoints.triage_server import TriageServer


class FakeSettings:
    """Stands in for `D1Settings`; records what the page decided to store."""

    def __init__(self) -> None:
        self.stored: dict = {}

    def set(self, values: dict) -> None:
        self.stored.update(values)


@pytest.fixture
def settings():
    return FakeSettings()


class FakeStore:
    """The settings routes never touch the store."""


async def _client(settings=None, llm_provider=None, schedule=None):
    server = TriageServer(
        FakeStore(), settings=settings, llm_provider=llm_provider, schedule=schedule
    )
    client = TestClient(TestServer(server._app))
    await client.start_server()
    return client


class TestSettingsApi:
    async def test_reports_the_current_choice_and_what_else_is_available(
        self, settings, monkeypatch
    ):
        monkeypatch.setenv("GEMINI_API_KEY", "g")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        client = await _client(settings, LLMProviderConfig(provider="gemini", model="x"))

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

    async def test_an_unknown_provider_is_rejected_before_any_call(self, settings):
        client = await _client(settings, LLMProviderConfig(provider="gemini"))

        res = await client.post("/api/settings", json={"provider": "mistral", "model": "x"})
        body = await res.json()
        await client.close()

        assert res.status == 400
        assert "mistral" in body["error"]
        assert settings.stored == {}  # nothing stored

    async def test_a_provider_with_no_key_is_rejected_and_names_the_variable(
        self, settings, monkeypatch
    ):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        client = await _client(settings, LLMProviderConfig(provider="gemini"))

        res = await client.post("/api/settings", json={"provider": "openai", "model": ""})
        body = await res.json()
        await client.close()

        assert res.status == 400
        assert "OPENAI_API_KEY" in body["error"]

    async def test_a_model_the_provider_refuses_is_never_stored(self, settings, monkeypatch):
        """The failure this endpoint exists to prevent: a typo saved, and a
        digest run discovering it tomorrow morning after the fetch."""
        monkeypatch.setenv("GEMINI_API_KEY", "g")

        async def boom(*a, **kw):
            raise RuntimeError("models/gemini-3.7-flashh is not found")

        monkeypatch.setattr("cyris.adapters.gemini_client.GeminiClient.complete", boom)
        client = await _client(settings, LLMProviderConfig(provider="gemini"))

        res = await client.post(
            "/api/settings", json={"provider": "gemini", "model": "gemini-3.7-flashh"}
        )
        body = await res.json()
        await client.close()

        assert res.status == 400
        assert "is not found" in body["error"]
        assert settings.stored == {}

    async def test_a_model_that_answers_is_stored(self, settings, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "g")

        async def ok(self, *a, **kw):
            from cyris.service_layer.ports import LLMResponse

            return LLMResponse(text="pong", input_tokens=1, output_tokens=1)

        monkeypatch.setattr("cyris.adapters.gemini_client.GeminiClient.complete", ok)
        client = await _client(settings, LLMProviderConfig(provider="anthropic"))

        res = await client.post(
            "/api/settings", json={"provider": "gemini", "model": "gemini-3.7-flash"}
        )
        body = await res.json()
        await client.close()

        assert res.status == 200 and body["ok"] is True
        assert settings.stored == {
            "llm_provider.provider": "gemini",
            "llm_provider.model": "gemini-3.7-flash",
        }

    async def test_without_a_settings_store_the_page_refuses_to_save(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "g")
        client = await _client(None, None)

        listing = await (await client.get("/api/settings")).json()
        res = await client.post("/api/settings", json={"provider": "gemini", "model": ""})
        await client.close()

        assert listing["writable"] is False
        assert res.status == 409


class TestScheduleApi:
    async def test_the_current_schedule_is_reported(self, settings):
        client = await _client(settings, None, schedule=["08:00", "20:00"])

        data = await (await client.get("/api/settings")).json()
        await client.close()

        assert data["schedule"] == ["08:00", "20:00"]

    async def test_two_whole_hours_are_stored_earliest_first(self, settings):
        client = await _client(settings, None, schedule=["08:00", "20:00"])

        res = await client.post("/api/settings/schedule", json={"times": ["21:00", "07:00"]})
        body = await res.json()
        await client.close()

        assert res.status == 200
        assert body["times"] == ["07:00", "21:00"]
        assert settings.stored == {"general.digest_schedule": ["07:00", "21:00"]}

    async def test_a_half_hour_is_refused_rather_than_rounded(self, settings):
        """The cron tick is hourly. Accepting 08:30 would fire at 08:00 and the
        reader would never learn why."""
        client = await _client(settings, None, schedule=["08:00", "20:00"])

        res = await client.post("/api/settings/schedule", json={"times": ["08:30", "20:00"]})
        body = await res.json()
        await client.close()

        assert res.status == 400
        assert "whole hour" in body["error"]
        assert settings.stored == {}

    async def test_one_time_is_refused(self, settings):
        client = await _client(settings, None, schedule=["08:00", "20:00"])

        res = await client.post("/api/settings/schedule", json={"times": ["08:00"]})
        await client.close()

        assert res.status == 400
        assert settings.stored == {}
