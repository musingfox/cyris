"""A hosted tenant's run takes its provider, model, notify targets and article cap from the policy.

A tenant's reader writes its settings on the hosted Worker, which refuses these keys (ADR-0040),
but the run container can write any row of its own database through its gateway. So the run's
settings load pins them, whatever the rows say: the provider and model the hosted policy names,
every notify target blank, and an article cap above the policy's clamped to it. A self-hosted
load reads its rows as it always has.
"""

from pathlib import Path

import pytest
from fakes import seed_d1_settings
from test_hosted_run_wiring import _files, _hosted_env, _through
from test_tenant_gateway import FakeGateway

from cyris import bootstrap
from cyris.adapters.hosted import hosted_policy, pin_tenant_settings
from cyris.adapters.store.d1 import apply_schema
from cyris.adapters.store.local_d1 import LocalD1Client

pytestmark = pytest.mark.integration

POLICY = hosted_policy()

# What a tenant's own container could write over its seed.
REWRITTEN = {
    "llm_provider.provider": "gemini",
    "llm_provider.model": "gemini-3-pro",
    "notify.discord_webhook_url": "https://discord.com/api/webhooks/1/tok",
    "notify.email_to": "anyone@a.test",
    "notify.email_from": "cyris@a.test",
}


def _hosted_load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **rows):
    _hosted_env(monkeypatch)
    gateway = FakeGateway("alpha")
    seed_d1_settings(gateway.db, **rows)
    _through(gateway, monkeypatch)
    return bootstrap.load_effective_config(*_files(tmp_path))


def test_a_tenants_run_takes_the_policys_model_and_no_notify_target_whatever_its_rows_say(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _hosted_load(tmp_path, monkeypatch, **REWRITTEN)

    assert cfg.app.llm_provider.provider == POLICY["provider"]
    assert cfg.app.llm_provider.model == POLICY["model"]
    assert cfg.app.notify.discord_webhook_url == ""
    assert cfg.app.notify.email_to == ""
    assert cfg.app.notify.email_from == ""
    assert cfg.missing_settings == []


def test_a_tenants_article_cap_above_the_policys_is_the_policys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cap = POLICY["max_articles_per_digest"]

    cfg = _hosted_load(tmp_path, monkeypatch, **{"digest.max_articles_per_digest": cap * 10})

    assert cfg.app.digest.max_articles_per_digest == cap


def test_a_tenants_article_cap_within_the_policys_and_its_other_settings_are_its_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _hosted_load(
        tmp_path,
        monkeypatch,
        **{"digest.max_articles_per_digest": 150, "digest.style_prompt": "Short."},
    )

    assert cfg.app.digest.max_articles_per_digest == 150
    assert cfg.app.digest.style_prompt == "Short."


# The tuning keys a tenant writes on its settings page (ADR-0040's update note), each with the
# value a save puts in its row and the config field a run reads it from.
TUNING = {
    "digest.vote_half_life_days": (30, ("digest", "vote_half_life_days")),
    "digest.topic_lifetime_days": (14, ("digest", "topic_lifetime_days")),
    "vote_similarity.max_seeds": (321, ("vote_similarity", "max_seeds")),
    "digest.scoring_snippet_length": (1500, ("digest", "scoring_snippet_length")),
    "digest.summarize_snippet_length": (2000, ("digest", "summarize_snippet_length")),
    "digest.filter_snippet_length": (300, ("digest", "filter_snippet_length")),
}


def test_every_tuning_key_the_policy_opens_is_one_this_file_runs() -> None:
    opened = {key for key in POLICY["writable_settings"] if key in TUNING}

    assert opened == set(TUNING)


@pytest.mark.parametrize("key", TUNING)
def test_a_saved_tuning_key_reaches_the_tenants_run_config(
    key: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value, (table, field) = TUNING[key]

    cfg = _hosted_load(tmp_path, monkeypatch, **{key: value})

    assert getattr(getattr(cfg.app, table), field) == value
    assert cfg.missing_settings == []


def test_a_cap_the_rule_refuses_is_left_for_the_load_to_report() -> None:
    assert (
        pin_tenant_settings({"digest.max_articles_per_digest": "many"})[
            "digest.max_articles_per_digest"
        ]
        == "many"
    )
    assert "digest.max_articles_per_digest" not in pin_tenant_settings({})


def test_a_self_hosted_load_reads_its_rows_as_they_are(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CYRIS_STORE_TENANT_ID", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    path = tmp_path / "state.sqlite3"
    config = tmp_path / "cyris.toml"
    config.write_text(f'[store]\nbackend = "sqlite"\nsqlite_path = "{path}"\n', encoding="utf-8")
    local = LocalD1Client(path)
    apply_schema(local)
    seed_d1_settings(local, **{**REWRITTEN, "digest.max_articles_per_digest": 5000})

    cfg = bootstrap.load_effective_config(config, tmp_path / "sources.yaml")

    assert cfg.app.llm_provider.provider == "gemini"
    assert cfg.app.llm_provider.model == "gemini-3-pro"
    assert cfg.app.notify.discord_webhook_url == REWRITTEN["notify.discord_webhook_url"]
    assert cfg.app.digest.max_articles_per_digest == 5000
