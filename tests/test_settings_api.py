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


async def _client(
    settings=None, llm_provider=None, schedule=None, max_featured=5, notify_webhook=""
):
    server = TriageServer(
        FakeStore(),
        settings=settings,
        llm_provider=llm_provider,
        schedule=schedule,
        max_featured=max_featured,
        notify_webhook=notify_webhook,
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


class TestFeaturedCap:
    """`max_featured` is grade D — a reader preference with a writer, not a constant."""

    async def test_the_page_reports_the_cap_a_run_would_use(self, settings):
        client = await _client(settings, None, max_featured=3)

        data = await (await client.get("/api/settings")).json()
        await client.close()

        assert data["max_featured"] == 3

    async def test_a_new_cap_is_stored_under_the_key_the_config_reads(self, settings):
        client = await _client(settings, None, max_featured=5)

        res = await client.post("/api/settings/digest", json={"max_featured": 8})
        body = await res.json()
        await client.close()

        assert res.status == 200
        assert body["max_featured"] == 8
        assert settings.stored == {"digest.max_featured": 8}

    async def test_a_cap_of_zero_is_refused(self, settings):
        """A featured section of none is an empty band, not a preference."""
        client = await _client(settings, None)

        res = await client.post("/api/settings/digest", json={"max_featured": 0})
        await client.close()

        assert res.status == 400
        assert settings.stored == {}

    async def test_the_key_is_writable_and_reaches_the_config(self):
        """Storing a key the overlay does not apply would silently change nothing."""
        from cyris.adapters.store.settings import WRITABLE_KEYS, apply_to
        from cyris.config import AppConfig, Config

        assert "digest.max_featured" in WRITABLE_KEYS

        cfg = Config(app=AppConfig(), sources={})
        apply_to(cfg, {"digest.max_featured": 9})

        assert cfg.app.digest.max_featured == 9


class TestNotifySettingsForm:
    async def test_the_page_has_a_notify_form_and_webhook_field(self):
        client = await _client()
        body = await (await client.get("/settings")).text()
        await client.close()

        assert 'id="notify-form"' in body
        assert 'id="discord-webhook"' in body

    async def test_the_hint_names_the_env_fallback(self):
        client = await _client()
        body = await (await client.get("/settings")).text()
        await client.close()

        assert "CYRIS_DISCORD_WEBHOOK_URL" in body
        assert "cannot turn notifications off" in body

    async def test_the_form_posts_to_the_notify_route(self):
        client = await _client()
        body = await (await client.get("/settings")).text()
        await client.close()

        assert "/api/settings/notify" in body

    async def test_the_form_has_its_own_result_div(self):
        client = await _client()
        body = await (await client.get("/settings")).text()
        await client.close()

        assert "notify-result" in body


class TestNotifyWebhookMaskedInSettingsPayload:
    async def test_the_current_webhook_is_returned_with_its_token_replaced(self, settings):
        client = await _client(
            settings, notify_webhook="https://discord.com/api/webhooks/123/abcTOKEN"
        )

        res = await client.get("/api/settings")
        text = await res.text()
        await client.close()

        import json

        body = json.loads(text)
        assert body["notify_webhook"] == "https://discord.com/api/webhooks/123/••••"
        assert "abcTOKEN" not in text

    async def test_an_empty_webhook_is_reported_as_empty(self, settings):
        client = await _client(settings)

        data = await (await client.get("/api/settings")).json()
        await client.close()

        assert data["notify_webhook"] == ""


class TestNotifyWebhookWrite:
    async def test_a_live_webhook_is_stored_and_returned_masked(self, settings, monkeypatch):
        async def ok(url, transport=None):
            from cyris.diagnostics.doctor import Check

            return Check("discord probe", "ok", "Discord knows it as test")

        monkeypatch.setattr("cyris.entrypoints.triage_server.probe_discord", ok)
        client = await _client(settings)

        res = await client.post(
            "/api/settings/notify",
            json={"discord_webhook_url": "https://discord.com/api/webhooks/1/tok"},
        )
        body = await res.json()
        await client.close()

        assert res.status == 200 and body["ok"] is True
        assert body["discord_webhook_url"] == "https://discord.com/api/webhooks/1/••••"
        # The probe's own sentence rides back: it names the webhook Discord
        # answered for, which is the only confirmation that the URL reached the
        # channel the reader meant.
        assert body["detail"] == "Discord knows it as test"
        assert settings.stored == {
            "notify.discord_webhook_url": "https://discord.com/api/webhooks/1/tok"
        }

    async def test_a_webhook_discord_refuses_is_never_stored(self, settings, monkeypatch):
        async def nope(url, transport=None):
            from cyris.diagnostics.doctor import Check

            return Check("discord probe", "fail", "404 Unknown Webhook")

        monkeypatch.setattr("cyris.entrypoints.triage_server.probe_discord", nope)
        client = await _client(settings)

        res = await client.post(
            "/api/settings/notify",
            json={"discord_webhook_url": "https://discord.com/api/webhooks/1/tok"},
        )
        body = await res.json()
        await client.close()

        assert res.status == 400
        assert body["error"] == "404 Unknown Webhook"
        assert settings.stored == {}

    async def test_whitespace_alone_is_refused_without_asking_discord(self, settings, monkeypatch):
        called = []

        async def probe(url, transport=None):
            called.append(url)
            from cyris.diagnostics.doctor import Check

            return Check("discord probe", "ok", "should not run")

        monkeypatch.setattr("cyris.entrypoints.triage_server.probe_discord", probe)
        client = await _client(settings)

        res = await client.post("/api/settings/notify", json={"discord_webhook_url": "   "})
        body = await res.json()
        await client.close()

        assert res.status == 400
        assert "cannot be turned off from /settings" in body["error"]
        assert "remove the CYRIS_DISCORD_WEBHOOK_URL Worker secret" in body["error"]
        assert "[notify]" in body["error"]
        assert settings.stored == {}
        assert called == []

    async def test_without_a_settings_store_the_page_refuses_to_save(self):
        client = await _client(None)

        res = await client.post(
            "/api/settings/notify",
            json={"discord_webhook_url": "https://discord.com/api/webhooks/1/tok"},
        )
        body = await res.json()
        await client.close()

        assert res.status == 409
        assert body["error"] == "this deployment has no settings store to write"

    async def test_a_body_that_is_not_json_is_refused(self, settings):
        client = await _client(settings)

        res = await client.post(
            "/api/settings/notify", data="not-json", headers={"Content-Type": "text/plain"}
        )
        body = await res.json()
        await client.close()

        assert res.status == 400
        assert body["error"] == "invalid JSON"

    async def test_a_store_failure_is_returned_as_500(self, monkeypatch):
        class Boom:
            def set(self, values):
                raise RuntimeError("d1 down")

        async def ok(url, transport=None):
            from cyris.diagnostics.doctor import Check

            return Check("discord probe", "ok", "Discord knows it as test")

        monkeypatch.setattr("cyris.entrypoints.triage_server.probe_discord", ok)
        client = await _client(Boom())

        res = await client.post(
            "/api/settings/notify",
            json={"discord_webhook_url": "https://discord.com/api/webhooks/1/tok"},
        )
        body = await res.json()
        await client.close()

        assert res.status == 500
        assert "d1 down" in body["error"]

    async def test_a_successful_save_is_what_the_next_get_reports(self, settings, monkeypatch):
        async def ok(url, transport=None):
            from cyris.diagnostics.doctor import Check

            return Check("discord probe", "ok", "Discord knows it as test")

        monkeypatch.setattr("cyris.entrypoints.triage_server.probe_discord", ok)
        client = await _client(settings)

        await client.post(
            "/api/settings/notify",
            json={"discord_webhook_url": "https://discord.com/api/webhooks/1/tok"},
        )
        data = await (await client.get("/api/settings")).json()
        await client.close()

        assert data["notify_webhook"] == "https://discord.com/api/webhooks/1/••••"


class TestLlmDiagnostics:
    """Asking the Container's own egress whether a provider answers, without saving.

    The Worker has its own probe, but the Container leaves from a different place,
    and that is where providers refused by location.
    """

    @pytest.fixture(autouse=True)
    def egress(self, monkeypatch):
        async def fake_egress():
            return {"colo": "SEA", "loc": "US"}

        monkeypatch.setattr("cyris.diagnostics.doctor.probe_egress", fake_egress)

    async def test_a_provider_that_answers_reports_ok_with_the_egress(self, settings, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "o")

        async def ok(self, *a, **kw):
            from cyris.service_layer.ports import LLMResponse

            return LLMResponse(text="pong", input_tokens=1, output_tokens=1)

        monkeypatch.setattr("cyris.adapters.openai_client.OpenAIClient.complete", ok)
        client = await _client(settings, LLMProviderConfig(provider="gemini"))

        res = await client.post(
            "/api/diagnostics/llm", json={"provider": "openai", "model": "gpt-5-mini"}
        )
        body = await res.json()
        await client.close()

        assert res.status == 200
        assert body["ok"] is True
        assert body["provider"] == "openai"
        assert body["egress"] == {"colo": "SEA", "loc": "US"}
        assert settings.stored == {}

    async def test_a_refusal_carries_the_provider_words_and_the_egress(self, settings, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "g")

        async def refused(*a, **kw):
            raise RuntimeError("User location is not supported for the API use")

        monkeypatch.setattr("cyris.adapters.gemini_client.GeminiClient.complete", refused)
        client = await _client(settings, LLMProviderConfig(provider="gemini"))

        res = await client.post(
            "/api/diagnostics/llm", json={"provider": "gemini", "model": "gemini-3.7-flash"}
        )
        body = await res.json()
        await client.close()

        assert res.status == 502
        assert body["ok"] is False
        assert "User location is not supported" in body["detail"]
        assert body["egress"] == {"colo": "SEA", "loc": "US"}
        assert settings.stored == {}

    async def test_it_works_without_a_settings_store(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "g")

        async def ok(self, *a, **kw):
            from cyris.service_layer.ports import LLMResponse

            return LLMResponse(text="pong", input_tokens=1, output_tokens=1)

        monkeypatch.setattr("cyris.adapters.gemini_client.GeminiClient.complete", ok)
        client = await _client(None, None)

        res = await client.post("/api/diagnostics/llm", json={"provider": "gemini"})
        await client.close()

        assert res.status == 200

    async def test_an_unknown_provider_is_rejected_before_any_call(self, settings):
        client = await _client(settings, None)

        res = await client.post("/api/diagnostics/llm", json={"provider": "mistral"})
        body = await res.json()
        await client.close()

        assert res.status == 400
        assert "mistral" in body["error"]
