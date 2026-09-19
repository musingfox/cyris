"""Grade-D settings in D1, and a D1 deployment that reads them from nowhere else."""

import logging

import pytest
from fakes import SqliteD1, seed_d1_settings, settings_toml

from cyris.adapters.store.settings import WRITABLE_KEYS, D1Settings
from cyris.config import (
    GRADE_D_KEYS,
    DigestConfig,
    GeneralConfig,
    LLMProviderConfig,
    NotifyConfig,
    RoutingConfig,
    VoteSimilarityConfig,
)


@pytest.fixture
def settings():
    return D1Settings(SqliteD1())


def test_a_stored_value_round_trips_through_json(settings):
    settings.set({"general.digest_schedule": ["07:00", "21:00"]})

    assert settings.all() == {"general.digest_schedule": ["07:00", "21:00"]}


def test_writing_the_same_key_twice_replaces_rather_than_duplicates(settings):
    settings.set({"llm_provider.provider": "gemini"})
    settings.set({"llm_provider.provider": "openai"})

    assert settings.all() == {"llm_provider.provider": "openai"}


def test_a_key_outside_the_grade_d_list_is_refused(settings):
    """The whitelist is the contract: an arbitrary dotted path would let the
    settings page write config no reader resolves."""
    with pytest.raises(ValueError, match="digest.bogus"):
        settings.set({"digest.bogus": 1})

    assert settings.all() == {}


def test_a_newly_writable_key_round_trips(settings):
    settings.set({"digest.output_language": "en"})

    assert settings.all() == {"digest.output_language": "en"}


def test_several_grade_d_keys_are_stored_in_one_set(settings):
    settings.set({"routing.score_threshold": 80, "vote_similarity.max_seeds": 50})

    assert settings.all() == {"routing.score_threshold": 80, "vote_similarity.max_seeds": 50}


def test_the_whitelist_is_exactly_the_twenty_grade_d_keys():
    assert len(GRADE_D_KEYS) == 20
    assert set(WRITABLE_KEYS) == set(GRADE_D_KEYS)


@pytest.mark.parametrize("key", GRADE_D_KEYS)
def test_every_grade_d_key_names_a_field_of_its_table(key):
    tables = {
        "general": GeneralConfig,
        "notify": NotifyConfig,
        "llm_provider": LLMProviderConfig,
        "digest": DigestConfig,
        "routing": RoutingConfig,
        "vote_similarity": VoteSimilarityConfig,
    }
    table, field = key.split(".", 1)

    assert field in tables[table].model_fields


def test_a_key_left_behind_by_an_older_build_is_ignored_not_applied(settings):
    settings._db.query(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
        ["obsidian.digest_folder", '"Digests"', "2026-08-01T00:00:00+00:00"],
    )

    assert settings.all() == {}


def test_the_discord_webhook_is_writable_from_the_settings_page(settings):
    settings.set({"notify.discord_webhook_url": "https://discord.com/api/webhooks/1/d1"})

    assert settings.all() == {"notify.discord_webhook_url": "https://discord.com/api/webhooks/1/d1"}


def test_a_misspelled_webhook_key_is_refused(settings):
    with pytest.raises(ValueError, match="notify.discord_webhook_urls"):
        settings.set({"notify.discord_webhook_urls": "x"})


ENV_WEBHOOK = "https://discord.com/api/webhooks/1/envTOKEN"


@pytest.fixture
def d1_deployment(tmp_path, monkeypatch):
    """A D1-backed deployment whose database is local sqlite; returns (db, load)."""
    from cyris import bootstrap
    from cyris.adapters.store import d1 as d1_module

    db = SqliteD1(with_schema=False)
    monkeypatch.setenv("CYRIS_STORE_BACKEND", "d1")
    monkeypatch.setenv("CYRIS_STORE_DATABASE_ID", "db")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "tok")
    monkeypatch.setattr(bootstrap, "build_d1_client", lambda _cfg: db)
    monkeypatch.setattr(d1_module, "D1Client", lambda **_kwargs: db)

    def load(toml: str = ""):
        config = tmp_path / "cyris.toml"
        config.write_text(toml)
        return bootstrap.load_effective_config(config, tmp_path / "sources.yaml")

    from cyris.adapters.store.d1 import apply_schema

    apply_schema(db)
    return db, load


def _row(db, key: str, raw: str) -> None:
    db.query(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
        [key, raw, "2026-09-19T00:00:00+00:00"],
    )


class TestD1SettingsResolveFromD1Only:
    def test_a_d1_row_is_the_value_whatever_the_file_says(self, d1_deployment):
        db, load = d1_deployment
        D1Settings(db).set({"digest.output_language": "zh-Hant"})

        cfg = load('[digest]\noutput_language = "en"\n')

        assert cfg.present_settings()["digest.output_language"] == "zh-Hant"

    def test_a_key_d1_lacks_is_missing_even_when_the_file_sets_it(self, d1_deployment):
        _db, load = d1_deployment

        cfg = load('[digest]\noutput_language = "en"\n')

        assert "digest.output_language" in cfg.missing_settings
        assert "digest.output_language" not in cfg.present_settings()

    def test_an_empty_webhook_row_is_off_whatever_the_environment_holds(
        self, d1_deployment, monkeypatch
    ):
        db, load = d1_deployment
        monkeypatch.setenv("CYRIS_DISCORD_WEBHOOK_URL", ENV_WEBHOOK)
        D1Settings(db).set({"notify.discord_webhook_url": ""})

        cfg = load()

        assert cfg.present_settings()["notify.discord_webhook_url"] == ""
        assert "notify.discord_webhook_url" not in cfg.missing_settings

    def test_the_environment_never_supplies_a_missing_webhook(self, d1_deployment, monkeypatch):
        _db, load = d1_deployment
        monkeypatch.setenv("CYRIS_DISCORD_WEBHOOK_URL", ENV_WEBHOOK)

        cfg = load()

        assert "notify.discord_webhook_url" in cfg.missing_settings
        assert "envTOKEN" not in cfg.model_dump_json()

    def test_the_api_key_follows_the_provider_d1_resolved(self, d1_deployment, monkeypatch):
        """Regression kept from the overlay: a provider D1 decides must still pick up
        its own key from the environment, or the run dies naming a variable that is
        in fact set (production, 2026-09-04)."""
        db, load = d1_deployment
        monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
        monkeypatch.setenv("GEMINI_API_KEY", "g")
        seed_d1_settings(db, **{"llm_provider.provider": "gemini", "llm_provider.model": ""})

        cfg = load('[llm_provider]\nprovider = "anthropic"\n')

        assert cfg.app.llm_provider.provider == "gemini"
        assert cfg.app.llm_provider.api_key == "g"

    def test_an_undecodable_row_is_missing_and_logged(self, d1_deployment, caplog):
        db, load = d1_deployment
        _row(db, "digest.max_featured", '"not json')

        with caplog.at_level(logging.WARNING):
            cfg = load()

        assert "digest.max_featured" in cfg.missing_settings
        assert any("digest.max_featured" in r.getMessage() for r in caplog.records)

    def test_a_row_the_rule_refuses_is_missing(self, d1_deployment):
        db, load = d1_deployment
        _row(db, "digest.max_featured", "0")

        cfg = load()

        assert "digest.max_featured" in cfg.missing_settings

    def test_a_null_provider_is_missing_not_none(self, d1_deployment):
        db, load = d1_deployment
        _row(db, "llm_provider.provider", "null")

        cfg = load()

        assert "llm_provider.provider" in cfg.missing_settings

    def test_provider_none_is_a_value(self, d1_deployment):
        db, load = d1_deployment
        D1Settings(db).set({"llm_provider.provider": "none"})

        cfg = load()

        assert "llm_provider.provider" not in cfg.missing_settings
        assert cfg.present_settings()["llm_provider.provider"] == "none"

    def test_a_fresh_database_misses_every_key_and_gets_its_tables(self, tmp_path, monkeypatch):
        from cyris import bootstrap
        from cyris.adapters.store import d1 as d1_module

        db = SqliteD1(with_schema=False)
        monkeypatch.setenv("CYRIS_STORE_BACKEND", "d1")
        monkeypatch.setenv("CYRIS_STORE_DATABASE_ID", "db")
        monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct")
        monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "tok")
        monkeypatch.setattr(bootstrap, "build_d1_client", lambda _cfg: db)
        monkeypatch.setattr(d1_module, "D1Client", lambda **_kwargs: db)

        cfg = bootstrap.load_effective_config(tmp_path / "nope.toml", tmp_path / "nope.yaml")

        assert cfg.missing_settings == sorted(GRADE_D_KEYS)
        assert db.query("SELECT name FROM sqlite_master WHERE name = 'settings'").rows

    def test_every_row_present_is_nothing_missing(self, d1_deployment):
        db, load = d1_deployment
        seed_d1_settings(db)

        assert load().missing_settings == []

    @pytest.mark.parametrize("key", GRADE_D_KEYS)
    def test_each_key_d1_lacks_is_missing_though_the_file_sets_it(self, d1_deployment, key):
        db, load = d1_deployment
        seed_d1_settings(db, omit=[key])

        cfg = load(settings_toml())

        assert cfg.missing_settings == [key]

    def test_the_overlay_is_gone(self):
        from cyris import bootstrap
        from cyris.adapters.store import settings
        from cyris.config import Config

        assert not hasattr(bootstrap, "_d1_value_survived")
        assert not hasattr(settings, "apply_to")
        assert "settings_from_d1" not in Config.model_fields
