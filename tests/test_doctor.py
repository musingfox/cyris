"""`cyris doctor` — the checks, and what each verdict tells the reader to do."""

from pathlib import Path

import pytest

from cyris.config import AppConfig, Config, LLMProviderConfig
from cyris.domain.models import SourceConfig, Tier
from cyris.service_layer import doctor


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any D1 a check reaches for is local sqlite, never the real API.

    Without this the backend="d1" cases hit api.cloudflare.com with a bogus
    token and pay the client's full retry backoff for it.
    """
    from fakes import SqliteD1

    db = SqliteD1()
    monkeypatch.setattr("cyris.adapters.store.d1.D1Client.__new__", lambda _cls, **_kw: db)
    return db


def _config(tmp_path: Path, **app_kwargs) -> Config:
    app = AppConfig(**app_kwargs)
    app.agent_vault.path = tmp_path / "agent-vault"
    return Config(
        app=app,
        sources={"Feed": SourceConfig(name="Feed", url="https://a.test/feed", tier=Tier.FILTER)},
    )


def _by_name(checks: list[doctor.Check], name: str) -> doctor.Check:
    return next(c for c in checks if c.name.startswith(name))


async def test_a_clean_local_setup_has_no_failures(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")
    cfg = _config(tmp_path, llm_provider=LLMProviderConfig(provider="anthropic"))

    checks = await doctor.run_checks(cfg)

    assert [c for c in checks if c.status == "fail"] == []


async def test_the_vault_check_is_gone_on_d1_and_creates_no_directory(tmp_path: Path) -> None:
    """The probe used to mkdir the very directory it asked about."""
    cfg = _config(tmp_path)
    cfg.app.store.backend = "d1"
    cfg.app.agent_vault.path = tmp_path / "never-created"

    checks = await doctor.run_checks(cfg)

    assert [c for c in checks if c.name == "agent vault"] == []
    assert not (tmp_path / "never-created").exists()


async def test_a_d1_store_with_unpushed_sources_warns(tmp_path: Path) -> None:
    """The Worker would be polling its bundled snapshot; that is not visible anywhere else."""
    cfg = _config(tmp_path)
    cfg.app.store.backend = "d1"
    cfg.sources_origin = "sources.yaml"

    check = _by_name(await doctor.run_checks(cfg), "sources")

    assert check.status == "warn"
    assert "cyris sources push" in check.fix


async def test_the_sources_check_names_where_they_came_from(tmp_path: Path) -> None:
    check = _by_name(await doctor.run_checks(_config(tmp_path)), "sources")

    assert "from sources.yaml" in check.detail


async def test_no_sources_is_a_failure(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    cfg.sources = {}

    assert _by_name(await doctor.run_checks(cfg), "sources").status == "fail"


async def test_a_provider_without_its_key_names_the_variable(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    cfg = _config(tmp_path, llm_provider=LLMProviderConfig(provider="gemini"))

    check = _by_name(await doctor.run_checks(cfg), "llm provider")

    assert check.status == "fail"
    assert "GEMINI_API_KEY" in check.fix


async def test_workers_ai_without_an_account_id_fails(tmp_path: Path, monkeypatch) -> None:
    """A token alone is not enough: the Workers AI REST path is per-account."""
    monkeypatch.setenv("CLOUDFLARE_AI_TOKEN", "ai-token")
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    cfg = _config(tmp_path, llm_provider=LLMProviderConfig(provider="workers_ai"))

    check = _by_name(await doctor.run_checks(cfg), "llm provider")

    assert check.status == "fail"
    assert "CLOUDFLARE_ACCOUNT_ID" in check.fix


async def test_no_provider_is_a_warning_not_a_failure(tmp_path: Path) -> None:
    """Degraded mode is a choice; it must not read as a broken deployment."""
    check = _by_name(await doctor.run_checks(_config(tmp_path)), "llm provider")

    assert check.status == "warn"


async def test_an_unwired_rss_buffer_warns_with_the_measured_cost(tmp_path: Path) -> None:
    check = _by_name(await doctor.run_checks(_config(tmp_path)), "rss buffer")

    assert check.status == "warn"
    assert "95 of the 179" in check.fix


async def test_a_working_account_token_is_not_called_invalid(tmp_path: Path, monkeypatch) -> None:
    """The bug this replaced: /user/tokens/verify rejects account-owned tokens,
    so doctor called a token invalid three lines under a check that had just
    used it successfully."""
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "account-owned")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct")
    monkeypatch.setattr(
        "cyris.adapters.cloudflare.check_pages_access",
        lambda *_a: (True, "can publish to cyris-digest"),
    )
    cfg = _config(tmp_path)
    cfg.app.promote.publish_enabled = True
    cfg.app.promote.pages_project = "cyris-digest"

    check = _by_name(await doctor.run_checks(cfg), "publishing")

    assert check.status == "ok"


async def test_a_token_without_pages_permission_says_so(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "d1-only")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct")
    monkeypatch.setattr(
        "cyris.adapters.cloudflare.check_pages_access",
        lambda *_a: (False, "Authentication error"),
    )
    cfg = _config(tmp_path)
    cfg.app.promote.publish_enabled = True
    cfg.app.promote.pages_project = "cyris-digest"

    check = _by_name(await doctor.run_checks(cfg), "publishing")

    assert check.status == "fail"
    assert "Pages permission" in check.fix


async def test_a_missing_publish_token_names_its_variable(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    cfg = _config(tmp_path)
    cfg.app.promote.publish_enabled = True

    check = _by_name(await doctor.run_checks(cfg), "publishing")

    assert check.status == "fail"
    assert "CLOUDFLARE_API_TOKEN" in check.fix


async def test_publishing_disabled_is_a_skip(tmp_path: Path) -> None:
    check = _by_name(await doctor.run_checks(_config(tmp_path)), "publishing")

    assert check.status == "skip"


async def test_an_unreachable_store_fails_rather_than_reading_as_empty(
    tmp_path: Path, monkeypatch
) -> None:
    from cyris.adapters.store.d1 import D1Error

    def explode(_cfg):
        raise D1Error("no such table: stored_articles")

    monkeypatch.setattr("cyris.bootstrap.build_store", explode)
    cfg = _config(tmp_path)
    cfg.app.store.backend = "d1"

    check = _by_name(await doctor.run_checks(cfg), "article store")

    assert check.status == "fail"
    assert "no such table" in check.detail


def test_the_command_renders_every_status_and_exits_nonzero_on_failure(monkeypatch) -> None:
    """The report renders by marker lookup, so an unmapped status would raise."""
    from typer.testing import CliRunner

    from cyris.entrypoints.cli import app

    async def fake_checks(_cfg, _path=None):
        return [
            doctor.Check("fine", "ok", "all good"),
            doctor.Check("partial", "warn", "degraded", "do this"),
            doctor.Check("absent", "skip", "not configured"),
            doctor.Check("broken", "fail", "it is broken", "fix it like this"),
        ]

    monkeypatch.setattr("cyris.service_layer.doctor.run_checks", fake_checks)
    monkeypatch.setattr("cyris.bootstrap.load_effective_config", lambda *a, **k: None)

    result = CliRunner().invoke(app, ["doctor"])

    assert result.exit_code == 1
    assert "✓ fine" in result.stdout
    assert "! partial" in result.stdout
    assert "– absent" in result.stdout
    assert "✗ broken" in result.stdout
    assert "fix it like this" in result.stdout
    assert "do this" in result.stdout
    # A passing check's hint would just be noise.
    assert "1 problem(s)" in result.stdout


def test_the_command_exits_zero_when_nothing_is_broken(monkeypatch) -> None:
    from typer.testing import CliRunner

    from cyris.entrypoints.cli import app

    async def fake_checks(_cfg, _path=None):
        return [doctor.Check("fine", "ok", "all good")]

    monkeypatch.setattr("cyris.service_layer.doctor.run_checks", fake_checks)
    monkeypatch.setattr("cyris.bootstrap.load_effective_config", lambda *a, **k: None)

    result = CliRunner().invoke(app, ["doctor"])

    assert result.exit_code == 0
    assert "Ready to run." in result.stdout


def test_the_command_does_not_claim_a_config_file_it_never_read(monkeypatch, tmp_path) -> None:
    """No green `config —` banner: the `config file` check is what reports this."""
    from typer.testing import CliRunner

    from cyris.entrypoints.cli import app

    async def fake_checks(cfg, _path=None):
        return [doctor._check_config_file(cfg, None)]

    monkeypatch.setattr("cyris.service_layer.doctor.run_checks", fake_checks)

    result = CliRunner().invoke(
        app,
        [
            "doctor",
            "--config",
            str(tmp_path / "nope.toml"),
            "--sources",
            str(tmp_path / "nope.yaml"),
        ],
    )

    assert "✓ config —" not in result.stdout
    assert "! config file — not found" in result.stdout


async def test_a_config_key_this_build_cannot_see_is_a_failure(tmp_path: Path) -> None:
    """The 2026-08-25→27 split: the config asked for D1, the image had no `[store]`.

    Pydantic ignores unknown tables, so the setting vanished and doctor stayed
    green for two days while every run wrote to the wrong store.
    """
    config_path = tmp_path / "cyris.toml"
    config_path.write_text('[general]\ntimezone = "Asia/Taipei"\n\n[from_the_future]\nx = 1\n')

    check = _by_name(await doctor.run_checks(_config(tmp_path), config_path), "build")

    assert check.status == "fail"
    assert "[from_the_future]" in check.detail


async def test_the_resolved_store_class_is_reported_not_the_configured_name(
    tmp_path: Path,
) -> None:
    check = _by_name(await doctor.run_checks(_config(tmp_path)), "store wiring")

    assert check.status == "ok"
    assert "ArticleStore" in check.detail


async def test_a_missing_config_file_on_d1_is_ok_and_names_the_environment(
    tmp_path: Path,
) -> None:
    """A container that ships no file is not a host that forgot the file."""
    cfg = _config(tmp_path)
    cfg.app.store.backend = "d1"
    # The premise of this case: the identity arrives as env, not as a file.
    cfg.app.store.database_id = "db"
    cfg.app.store.account_id = "acct"
    cfg.app.store.api_token = "tok"
    cfg.config_file_found = False

    checks = await doctor.run_checks(cfg)
    check = _by_name(checks, "config file")

    assert check.status == "ok"
    assert "environment" in check.detail.lower()
    assert {c.name for c in checks if c.status in ("warn", "fail")} <= {
        "sources",
        "llm provider",
        "rss buffer",
        # This fixture's D1 holds no articles, which is its own warning now.
        "article store (d1)",
    }


async def test_a_missing_config_file_on_json_warns_to_set_cyris_env(
    tmp_path: Path,
) -> None:
    cfg = _config(tmp_path)
    cfg.config_file_found = False

    check = _by_name(await doctor.run_checks(cfg), "config file")

    assert check.status == "warn"
    assert "CYRIS_" in check.fix


async def test_a_found_config_file_is_named_in_the_check(tmp_path: Path) -> None:
    config_path = tmp_path / "cyris.toml"
    config_path.write_text('[general]\ntimezone = "Asia/Taipei"\n')
    cfg = _config(tmp_path)
    cfg.config_file_found = True

    check = _by_name(await doctor.run_checks(cfg, config_path), "config file")

    assert check.status == "ok"
    assert str(config_path) in check.detail


async def test_settings_without_a_config_file_do_not_claim_cyris_toml(
    tmp_path: Path,
) -> None:
    cfg = _config(tmp_path)
    cfg.config_file_found = False
    cfg.settings_from_d1 = []

    check = _by_name(await doctor.run_checks(cfg), "settings")

    assert "cyris.toml" not in check.detail
