"""Grade-D settings in D1, and the read order that makes them trustworthy."""

import pytest
from fakes import SqliteD1

from cyris.adapters.store.settings import D1Settings, apply_to
from cyris.config import AppConfig, Config


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


def test_a_key_with_no_writer_is_refused(settings):
    """The whitelist is the contract: an arbitrary dotted path would let the
    settings page write config the overlay has no idea how to apply."""
    with pytest.raises(ValueError, match="digest.max_articles_per_digest"):
        settings.set({"digest.max_articles_per_digest": 400})

    assert settings.all() == {}


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
