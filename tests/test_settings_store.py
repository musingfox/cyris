"""Grade-D settings in D1, and the read order that makes them trustworthy."""

import pytest
from fakes import SqliteD1

from cyris.adapters.store.settings import WRITABLE_KEYS, D1Settings, apply_to
from cyris.config import (
    GRADE_D_KEYS,
    AppConfig,
    Config,
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


def test_d1_wins_over_the_file():
    """The whole point of the milestone: one resolution order, everywhere. A host
    run and a container run reading different settings is the 08-25→27 split."""
    cfg = Config(app=AppConfig(), sources={})
    cfg.app.llm_provider.provider = "anthropic"
    cfg.app.general.digest_schedule = ["08:00", "20:00"]

    applied = apply_to(
        cfg, {"llm_provider.provider": "openai", "general.digest_schedule": ["07:00", "21:00"]}
    )

    assert cfg.app.llm_provider.provider == "openai"
    assert cfg.app.general.digest_schedule == ["07:00", "21:00"]
    assert sorted(applied) == ["general.digest_schedule", "llm_provider.provider"]


def test_a_key_d1_does_not_hold_keeps_its_file_value():
    cfg = Config(app=AppConfig(), sources={})
    cfg.app.general.digest_schedule = ["08:00", "20:00"]

    apply_to(cfg, {"llm_provider.provider": "openai"})

    assert cfg.app.general.digest_schedule == ["08:00", "20:00"]


def test_a_provider_from_d1_still_picks_up_its_api_key(monkeypatch):
    """Regression: `apply_to` used to `setattr` straight into the table, which
    skips the model validators. With no `cyris.toml` in the image the provider
    is unset at construction, so `inject_api_key` never ran; D1 then flipped the
    provider and the key stayed empty — the run died naming an environment
    variable that was in fact set. Deployed 2026-09-04, caught in production."""
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key-from-env")
    cfg = Config(app=AppConfig.model_validate({}), sources={})
    assert cfg.app.llm_provider.provider is None
    assert cfg.app.llm_provider.api_key == ""

    apply_to(cfg, {"llm_provider.provider": "gemini"})

    assert cfg.app.llm_provider.provider == "gemini"
    assert cfg.app.llm_provider.api_key == "gemini-key-from-env"
    cfg.validate_required_keys()


def test_the_discord_webhook_is_writable_from_the_settings_page(settings):
    settings.set({"notify.discord_webhook_url": "https://discord.com/api/webhooks/1/d1"})

    assert settings.all() == {"notify.discord_webhook_url": "https://discord.com/api/webhooks/1/d1"}


def test_a_misspelled_webhook_key_is_refused(settings):
    with pytest.raises(ValueError, match="notify.discord_webhook_urls"):
        settings.set({"notify.discord_webhook_urls": "x"})


def test_a_stored_webhook_becomes_the_one_the_run_uses():
    cfg = Config(app=AppConfig(), sources={})

    applied = apply_to(cfg, {"notify.discord_webhook_url": "https://discord.com/api/webhooks/1/d1"})

    assert cfg.app.notify.discord_webhook_url == "https://discord.com/api/webhooks/1/d1"
    assert "notify.discord_webhook_url" in applied


def test_the_stored_webhook_outranks_the_file():
    cfg = Config(
        app=AppConfig.model_validate(
            {"notify": {"discord_webhook_url": "https://discord.com/api/webhooks/1/file"}}
        ),
        sources={},
    )

    apply_to(cfg, {"notify.discord_webhook_url": "https://discord.com/api/webhooks/1/d1"})

    assert cfg.app.notify.discord_webhook_url == "https://discord.com/api/webhooks/1/d1"
