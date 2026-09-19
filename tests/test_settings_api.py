"""Choosing a provider and the digest hours from the settings page."""

import pytest
from aiohttp.test_utils import TestClient, TestServer
from fakes import TEST_SETTINGS

from cyris.config import GRADE_D_KEYS
from cyris.entrypoints.triage_server import TriageServer


class FakeSettings:
    """Stands in for `D1Settings`; records what the page decided to store."""

    def __init__(self) -> None:
        self.stored: dict = {}
        self.calls: list[dict] = []

    def set(self, values: dict) -> None:
        self.calls.append(dict(values))
        self.stored.update(values)


@pytest.fixture
def settings():
    return FakeSettings()


async def _client(settings=None, values=None):
    server = TriageServer(settings=settings, values=values)
    client = TestClient(TestServer(server._app))
    await client.start_server()
    return client


class TestSettingsApi:
    async def test_reports_the_current_choice_and_what_else_is_available(
        self, settings, monkeypatch
    ):
        monkeypatch.setenv("GEMINI_API_KEY", "g")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        client = await _client(
            settings, {"llm_provider.provider": "gemini", "llm_provider.model": "x"}
        )

        data = await (await client.get("/api/settings")).json()
        await client.close()

        assert data["values"]["llm_provider.provider"] == "gemini"
        assert data["values"]["llm_provider.model"] == "x"
        assert data["writable"] is True
        by_name = {p["name"]: p for p in data["providers"]}
        assert by_name["gemini"]["configured"] is True
        assert by_name["openai"]["configured"] is False
        assert by_name["openai"]["env_var"] == "OPENAI_API_KEY"
        assert by_name["gemini"]["default_model"]  # something to fall back to

    async def test_none_is_offered_last_after_the_live_providers(self, settings):
        from cyris.bootstrap import default_models

        client = await _client(settings)

        data = await (await client.get("/api/settings")).json()
        await client.close()

        names = [p["name"] for p in data["providers"]]
        assert names == [*default_models(), "none"]
        assert data["providers"][-1] == {
            "name": "none",
            "env_var": "",
            "default_model": "",
            "configured": True,
        }

    async def test_an_unknown_provider_is_rejected_before_any_call(self, settings):
        client = await _client(settings)

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
        client = await _client(settings)

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
        client = await _client(settings)

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
        client = await _client(settings)

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
        client = await _client(None)

        listing = await (await client.get("/api/settings")).json()
        res = await client.post("/api/settings", json={"provider": "gemini", "model": ""})
        await client.close()

        assert listing["writable"] is False
        assert res.status == 409


class TestProviderNone:
    @pytest.fixture(autouse=True)
    def no_probe(self, monkeypatch):
        async def probe(*a, **kw):
            raise AssertionError("provider none must not call a model")

        monkeypatch.setattr("cyris.diagnostics.doctor.probe_llm", probe)

    async def test_none_is_stored_without_a_probe(self, settings):
        client = await _client(settings, {})

        res = await client.post("/api/settings", json={"provider": "none", "model": ""})
        body = await res.json()
        data = await (await client.get("/api/settings")).json()
        await client.close()

        assert res.status == 200
        assert body == {
            "ok": True,
            "provider": "none",
            "model": "",
            "detail": "No model is called: digests list plain excerpts.",
            "note": "Saved. The next digest run picks this up.",
        }
        assert settings.calls == [{"llm_provider.provider": "none", "llm_provider.model": ""}]
        assert data["values"]["llm_provider.provider"] == "none"

    async def test_a_leftover_model_is_stored_empty(self, settings):
        client = await _client(settings)

        await client.post("/api/settings", json={"provider": "none", "model": "gemini-3.8-flash"})
        await client.close()

        assert settings.stored["llm_provider.model"] == ""

    async def test_an_empty_provider_is_not_none(self, settings):
        client = await _client(settings)

        res = await client.post("/api/settings", json={"provider": ""})
        body = await res.json()
        await client.close()

        assert res.status == 400
        assert body["error"] == "unknown provider ''"
        assert settings.calls == []

    async def test_without_a_settings_store_none_is_refused(self):
        client = await _client(None)

        res = await client.post("/api/settings", json={"provider": "none", "model": ""})
        await client.close()

        assert res.status == 409


class TestEveryKeyReported:
    async def test_an_empty_home_reports_every_key_missing(self, settings):
        client = await _client(settings, {})

        data = await (await client.get("/api/settings")).json()
        await client.close()

        assert data["missing"] == sorted(GRADE_D_KEYS)
        assert data["values"] == dict.fromkeys(GRADE_D_KEYS)
        assert data["writable"] is True

    async def test_embedding_readiness_follows_the_environment(self, settings, monkeypatch):
        monkeypatch.setenv("CLOUDFLARE_EMBEDDING_API_TOKEN", "t")
        monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a")
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        client = await _client(settings, {})

        data = await (await client.get("/api/settings")).json()
        await client.close()

        by_name = {p["name"]: p for p in data["embedding_providers"]}
        assert set(by_name) == {"workers_ai", "gemini"}
        assert by_name["workers_ai"]["configured"] is True
        assert by_name["workers_ai"]["default_model"] == "@cf/baai/bge-m3"
        assert by_name["workers_ai"]["env_var"] == "CLOUDFLARE_EMBEDDING_API_TOKEN"
        assert by_name["gemini"]["configured"] is False
        assert by_name["gemini"]["env_var"] == "GEMINI_API_KEY"

    async def test_the_output_languages_are_offered(self, settings):
        client = await _client(settings, {})

        data = await (await client.get("/api/settings")).json()
        await client.close()

        assert {"zh-Hant", "en"} <= set(data["languages"])


class TestScheduleApi:
    async def test_the_current_schedule_is_reported(self, settings):
        client = await _client(settings, {"general.digest_schedule": ["08:00", "20:00"]})

        data = await (await client.get("/api/settings")).json()
        await client.close()

        assert data["values"]["general.digest_schedule"] == ["08:00", "20:00"]

    async def test_two_whole_hours_are_stored_earliest_first(self, settings):
        client = await _client(settings)

        res = await client.post("/api/settings/schedule", json={"times": ["21:00", "07:00"]})
        body = await res.json()
        await client.close()

        assert res.status == 200
        assert body["times"] == ["07:00", "21:00"]
        assert settings.stored == {"general.digest_schedule": ["07:00", "21:00"]}

    async def test_a_half_hour_is_refused_rather_than_rounded(self, settings):
        """The cron tick is hourly. Accepting 08:30 would fire at 08:00 and the
        reader would never learn why."""
        client = await _client(settings)

        res = await client.post("/api/settings/schedule", json={"times": ["08:30", "20:00"]})
        body = await res.json()
        await client.close()

        assert res.status == 400
        assert "whole hour" in body["error"]
        assert settings.stored == {}

    async def test_one_time_is_refused(self, settings):
        client = await _client(settings)

        res = await client.post("/api/settings/schedule", json={"times": ["08:00"]})
        await client.close()

        assert res.status == 400
        assert settings.stored == {}


class TestFeaturedCap:
    """`max_featured` is grade D — a reader preference with a writer, not a constant."""

    async def test_the_page_reports_the_cap_a_run_would_use(self, settings):
        client = await _client(settings, {"digest.max_featured": 3})

        data = await (await client.get("/api/settings")).json()
        await client.close()

        assert data["values"]["digest.max_featured"] == 3

    async def test_a_cap_of_zero_is_refused(self, settings):
        """A featured section of none is an empty band, not a preference."""
        client = await _client(settings)

        res = await client.post("/api/settings/values", json={"values": {"digest.max_featured": 0}})
        await client.close()

        assert res.status == 400
        assert settings.stored == {}

    async def test_the_old_digest_route_is_gone(self, settings):
        client = await _client(settings)

        res = await client.post("/api/settings/digest", json={"max_featured": 8})
        await client.close()

        assert res.status == 404
        assert settings.calls == []

    async def test_the_key_is_writable_and_reaches_the_config(self):
        """Storing a key the loader does not read would silently change nothing."""
        from cyris.adapters.store.settings import WRITABLE_KEYS
        from cyris.config import RawConfig, resolve_config

        assert "digest.max_featured" in WRITABLE_KEYS

        raw = RawConfig(toml={}, sources={}, config_file_found=False)
        cfg = resolve_config(raw, d1_settings={**TEST_SETTINGS, "digest.max_featured": 9})

        assert cfg.app.digest.max_featured == 9


class TestPlainValues:
    async def test_several_plain_keys_are_stored_in_one_write(self, settings):
        client = await _client(settings, {})

        res = await client.post(
            "/api/settings/values",
            json={"values": {"general.timezone": "Europe/Berlin", "digest.max_featured": 3}},
        )
        body = await res.json()
        data = await (await client.get("/api/settings")).json()
        await client.close()

        assert res.status == 200
        assert body == {
            "ok": True,
            "values": {"general.timezone": "Europe/Berlin", "digest.max_featured": 3},
            "note": "Effective next run.",
        }
        assert settings.calls == [{"general.timezone": "Europe/Berlin", "digest.max_featured": 3}]
        assert data["values"]["general.timezone"] == "Europe/Berlin"
        assert data["values"]["digest.max_featured"] == 3
        assert {"general.timezone", "digest.max_featured"}.isdisjoint(data["missing"])

    async def test_one_invalid_value_stores_nothing(self, settings):
        client = await _client(settings)

        res = await client.post(
            "/api/settings/values",
            json={"values": {"digest.max_featured": 3, "routing.score_threshold": 150}},
        )
        body = await res.json()
        await client.close()

        assert res.status == 400
        assert body["error"].startswith("routing.score_threshold: ")
        assert settings.calls == []

    async def test_a_key_with_its_own_form_is_refused(self, settings):
        client = await _client(settings)

        res = await client.post(
            "/api/settings/values", json={"values": {"llm_provider.model": "x"}}
        )
        body = await res.json()
        await client.close()

        assert res.status == 400
        assert body["error"] == "llm_provider.model is saved from its own form"
        assert settings.calls == []

    async def test_the_webhook_is_not_a_plain_value(self, settings):
        client = await _client(settings)

        res = await client.post(
            "/api/settings/values", json={"values": {"notify.discord_webhook_url": ""}}
        )
        await client.close()

        assert res.status == 400
        assert settings.calls == []

    @pytest.mark.parametrize("body", [{"values": {}}, {"values": []}, {}], ids=str)
    async def test_nothing_to_store_is_refused(self, settings, body):
        client = await _client(settings)

        res = await client.post("/api/settings/values", json=body)
        await client.close()

        assert res.status == 400
        assert settings.calls == []

    async def test_invalid_json_is_refused(self, settings):
        client = await _client(settings)

        res = await client.post(
            "/api/settings/values", data="not-json", headers={"Content-Type": "text/plain"}
        )
        await client.close()

        assert res.status == 400

    async def test_without_a_settings_store_the_page_refuses_to_save(self):
        client = await _client(None)

        res = await client.post("/api/settings/values", json={"values": {"digest.max_featured": 3}})
        await client.close()

        assert res.status == 409

    async def test_a_store_failure_is_returned_as_500(self):
        class Boom:
            def set(self, values):
                raise RuntimeError("D1 down")

        client = await _client(Boom())

        res = await client.post("/api/settings/values", json={"values": {"digest.max_featured": 3}})
        body = await res.json()
        await client.close()

        assert res.status == 500
        assert "D1 down" in body["error"]


class TestVoteSimilarity:
    ON = {"enabled": True, "provider": "workers_ai", "model": "", "max_seeds": 200}

    @pytest.fixture
    def probe(self, monkeypatch):
        """Answers the embedder probe with `probe.result`, recording each call."""
        from cyris.diagnostics.doctor import Check

        calls: list[tuple] = []

        async def fake(provider, model):
            calls.append((provider, model))
            return fake.result

        fake.result = Check("embedding probe", "ok", "workers_ai · @cf/baai/bge-m3 answered")
        fake.calls = calls
        monkeypatch.setattr("cyris.entrypoints.triage_server.probe_embedder", fake)
        return fake

    async def test_turning_it_on_stores_all_four_after_the_probe(self, settings, probe):
        client = await _client(settings, {})

        res = await client.post("/api/settings/vote-similarity", json=self.ON)
        body = await res.json()
        data = await (await client.get("/api/settings")).json()
        await client.close()

        assert res.status == 200
        assert settings.calls == [
            {
                "vote_similarity.enabled": True,
                "vote_similarity.provider": "workers_ai",
                "vote_similarity.model": "",
                "vote_similarity.max_seeds": 200,
            }
        ]
        assert body["detail"] == "workers_ai · @cf/baai/bge-m3 answered"
        assert body["note"] == "Saved. The next digest run picks this up."
        assert probe.calls == [("workers_ai", "")]
        assert data["values"]["vote_similarity.max_seeds"] == 200

    async def test_an_embedder_that_refuses_is_never_turned_on(self, settings, probe):
        from cyris.diagnostics.doctor import Check

        probe.result = Check("embedding probe", "fail", "@cf/x refused: 404 no such model")
        client = await _client(settings)

        res = await client.post("/api/settings/vote-similarity", json=self.ON)
        body = await res.json()
        await client.close()

        assert res.status == 400
        assert body["error"] == "@cf/x refused: 404 no such model"
        assert settings.calls == []

    async def test_off_is_stored_without_a_probe(self, settings, probe):
        client = await _client(settings)

        res = await client.post(
            "/api/settings/vote-similarity",
            json={"enabled": False, "provider": "gemini", "model": "typo-model", "max_seeds": 50},
        )
        body = await res.json()
        await client.close()

        assert res.status == 200
        assert probe.calls == []
        assert body["detail"] == (
            "Not checked: vote similarity is off. Turning it on checks the embedder first."
        )
        assert settings.stored["vote_similarity.model"] == "typo-model"

    async def test_an_unknown_embedding_provider_is_refused_before_the_probe(self, settings, probe):
        client = await _client(settings)

        res = await client.post(
            "/api/settings/vote-similarity", json={**self.ON, "provider": "openai"}
        )
        body = await res.json()
        await client.close()

        assert res.status == 400
        assert body["error"].startswith("vote_similarity.provider: ")
        assert probe.calls == []
        assert settings.calls == []

    async def test_every_field_is_required(self, settings, probe):
        client = await _client(settings)

        body = {k: v for k, v in self.ON.items() if k != "max_seeds"}
        res = await client.post("/api/settings/vote-similarity", json=body)
        answer = await res.json()
        await client.close()

        assert res.status == 400
        assert "max_seeds" in answer["error"]
        assert settings.calls == []

    async def test_without_a_settings_store_the_page_refuses_to_save(self, probe):
        client = await _client(None)

        res = await client.post("/api/settings/vote-similarity", json=self.ON)
        await client.close()

        assert res.status == 409

    async def test_a_refusal_never_carries_the_key(self, settings, monkeypatch):
        """The real probe, against a provider that echoes the key in its refusal."""
        import httpx

        import cyris.bootstrap  # noqa: F401 - its SDK imports must precede the patch

        monkeypatch.setenv("GEMINI_API_KEY", "sentinel-key-123")
        real = httpx.AsyncClient

        def refuse(request):
            return httpx.Response(
                400, json={"error": {"message": "API key sentinel-key-123 not valid"}}
            )

        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            lambda *a, **kw: real(*a, **{**kw, "transport": httpx.MockTransport(refuse)}),
        )
        client = await _client(settings)

        res = await client.post(
            "/api/settings/vote-similarity",
            json={"enabled": True, "provider": "gemini", "model": "", "max_seeds": 50},
        )
        text = await res.text()
        await client.close()

        assert res.status == 400
        assert "sentinel-key-123" not in text
        assert settings.calls == []


class TestNotifySettingsForm:
    async def test_the_page_has_a_notify_form_and_webhook_field(self):
        client = await _client()
        body = await (await client.get("/settings")).text()
        await client.close()

        assert 'id="notify-form"' in body
        assert 'id="discord-webhook"' in body

    async def test_the_hint_names_no_env_fallback_and_says_turn_off_clears_it(self):
        from cyris.entrypoints.triage_server import render_settings_page

        page = render_settings_page()

        assert "CYRIS_DISCORD_WEBHOOK_URL" not in page
        assert "Turn off clears it: runs then finish without a message." in page
        assert 'id="notify-off"' in page

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


def _leaked(text: str, literals: list[str]) -> list[str]:
    return [literal for literal in literals if literal in text]


class TestSettingsPageCarriesNoCredential:
    SECRETS = ["abcTOKEN", "cyris-probe-sentinel-key"]

    def test_a_leaked_literal_is_named(self):
        page = '<input value="https://discord.com/api/webhooks/123/abcTOKEN">'
        assert _leaked(page, ["abcTOKEN"]) == ["abcTOKEN"]

    @pytest.mark.parametrize("path", ["/settings", "/static/settings.js"])
    async def test_the_served_page_holds_no_key_and_no_webhook_token(
        self, settings, monkeypatch, path
    ):
        monkeypatch.setenv("GEMINI_API_KEY", "cyris-probe-sentinel-key")
        client = await _client(
            settings,
            {
                "llm_provider.provider": "gemini",
                "notify.discord_webhook_url": "https://discord.com/api/webhooks/123/abcTOKEN",
            },
        )

        response = await client.get(path)
        body = await response.text()
        await client.close()

        assert response.status == 200
        assert _leaked(body, self.SECRETS) == []


class TestNotifyWebhookMaskedInSettingsPayload:
    async def test_the_current_webhook_is_returned_with_its_token_replaced(self, settings):
        client = await _client(
            settings,
            {"notify.discord_webhook_url": "https://discord.com/api/webhooks/123/abcTOKEN"},
        )

        res = await client.get("/api/settings")
        text = await res.text()
        await client.close()

        import json

        body = json.loads(text)
        assert body["values"]["notify.discord_webhook_url"] == (
            "https://discord.com/api/webhooks/123/••••"
        )
        assert "abcTOKEN" not in text

    async def test_an_empty_webhook_is_reported_as_off_not_missing(self, settings):
        client = await _client(settings, {"notify.discord_webhook_url": ""})

        data = await (await client.get("/api/settings")).json()
        await client.close()

        assert data["values"]["notify.discord_webhook_url"] == ""
        assert "notify.discord_webhook_url" not in data["missing"]


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
        assert body["error"] == (
            "Paste a Discord webhook URL, or press Turn off to stop notifications."
        )
        assert "CYRIS_DISCORD_WEBHOOK_URL" not in await res.text()
        assert settings.stored == {}
        assert called == []

    async def test_turning_off_stores_empty_without_asking_discord(self, settings, monkeypatch):
        async def probe(url, transport=None):
            raise AssertionError("turning notifications off must not call Discord")

        monkeypatch.setattr("cyris.entrypoints.triage_server.probe_discord", probe)
        client = await _client(
            settings, {"notify.discord_webhook_url": "https://discord.com/api/webhooks/1/tok"}
        )

        res = await client.post("/api/settings/notify", json={"off": True})
        body = await res.json()
        data = await (await client.get("/api/settings")).json()
        await client.close()

        assert res.status == 200
        assert body == {
            "ok": True,
            "discord_webhook_url": "",
            "note": "Notifications are off. The next run finishes without a message.",
        }
        assert settings.calls == [{"notify.discord_webhook_url": ""}]
        assert data["values"]["notify.discord_webhook_url"] == ""

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

        assert data["values"]["notify.discord_webhook_url"] == (
            "https://discord.com/api/webhooks/1/••••"
        )


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
        client = await _client(settings)

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
        client = await _client(settings)

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
        client = await _client(None)

        res = await client.post("/api/diagnostics/llm", json={"provider": "gemini"})
        await client.close()

        assert res.status == 200

    async def test_an_unknown_provider_is_rejected_before_any_call(self, settings):
        client = await _client(settings)

        res = await client.post("/api/diagnostics/llm", json={"provider": "mistral"})
        body = await res.json()
        await client.close()

        assert res.status == 400
        assert "mistral" in body["error"]


SETTINGS_ROUTES = [
    "/api/settings",
    "/api/settings/schedule",
    "/api/settings/values",
    "/api/settings/vote-similarity",
    "/api/settings/notify",
]


class TestEverySettingsWriteGuardsAlike:
    @pytest.mark.parametrize("route", SETTINGS_ROUTES)
    async def test_without_a_store_every_route_refuses(self, route):
        client = await _client(None)

        res = await client.post(route, json={})
        body = await res.json()
        await client.close()

        assert (res.status, body["error"]) == (
            409,
            "this deployment has no settings store to write",
        )

    @pytest.mark.parametrize("route", SETTINGS_ROUTES)
    async def test_a_body_that_is_not_an_object_is_refused(self, settings, route):
        client = await _client(settings)

        res = await client.post(route, json=["not", "an", "object"])
        body = await res.json()
        await client.close()

        assert (res.status, body["error"]) == (400, "invalid JSON")
        assert settings.calls == []
