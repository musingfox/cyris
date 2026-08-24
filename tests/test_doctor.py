"""`cyris doctor` — the checks, and what each verdict tells the reader to do."""

from pathlib import Path

from cyris.config import AppConfig, Config, LLMProviderConfig, ObsidianConfig
from cyris.domain.models import SourceConfig, Tier
from cyris.service_layer import doctor


def _config(tmp_path: Path, **app_kwargs) -> Config:
    vault = tmp_path / "vault"
    (vault / "Digests").mkdir(parents=True)
    app = AppConfig(obsidian=ObsidianConfig(user_vault_path=vault), **app_kwargs)
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


async def test_no_provider_is_a_warning_not_a_failure(tmp_path: Path) -> None:
    """Degraded mode is a choice; it must not read as a broken deployment."""
    check = _by_name(await doctor.run_checks(_config(tmp_path)), "llm provider")

    assert check.status == "warn"


async def test_a_missing_vault_is_a_failure(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    cfg.app.obsidian.user_vault_path = tmp_path / "nowhere"

    assert _by_name(await doctor.run_checks(cfg), "obsidian vault").status == "fail"


async def test_an_unwired_rss_buffer_warns_with_the_measured_cost(tmp_path: Path) -> None:
    check = _by_name(await doctor.run_checks(_config(tmp_path)), "rss buffer")

    assert check.status == "warn"
    assert "95 of the 179" in check.fix


async def test_a_dead_token_is_reported_against_what_needs_it(tmp_path: Path, monkeypatch) -> None:
    """The failure this command exists for: an expired token nothing asks about."""
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "expired")
    monkeypatch.setattr(
        "cyris.adapters.cloudflare.verify_api_token", lambda _t: (False, "Invalid API Token")
    )
    cfg = _config(tmp_path)
    cfg.app.promote.publish_enabled = True

    check = _by_name(await doctor.run_checks(cfg), "cloudflare token")

    assert check.status == "fail"
    assert "Pages" in check.name
    assert "Invalid API Token" in check.detail


async def test_an_empty_token_names_the_variable_that_supplies_it(
    tmp_path: Path, monkeypatch
) -> None:
    """Publishing reads CLOUDFLARE_API_TOKEN; telling the reader to set the D1
    one instead sends them to fix the wrong line."""
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    cfg = _config(tmp_path)
    cfg.app.promote.publish_enabled = True

    check = _by_name(await doctor.run_checks(cfg), "cloudflare token")

    assert check.status == "fail"
    assert "CLOUDFLARE_API_TOKEN" in check.fix
    assert "CYRIS_D1_API_TOKEN" not in check.fix


async def test_an_empty_d1_token_names_its_own_variable(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    cfg.app.store.backend = "d1"
    cfg.app.store.api_token = ""

    check = _by_name(await doctor.run_checks(cfg), "cloudflare token")

    assert "CYRIS_D1_API_TOKEN" in check.fix


async def test_one_token_serving_two_purposes_is_verified_once(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "shared")
    monkeypatch.setattr(
        "cyris.adapters.cloudflare.verify_api_token",
        lambda token: (calls.append(token), (True, "active"))[1],
    )
    cfg = _config(tmp_path)
    cfg.app.promote.publish_enabled = True
    cfg.app.store.backend = "d1"
    cfg.app.store.api_token = "shared"

    checks = [c for c in await doctor.run_checks(cfg) if c.name.startswith("cloudflare token")]

    assert len(calls) == 1
    assert len(checks) == 1
    assert "Pages" in checks[0].name and "D1" in checks[0].name


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

    async def fake_checks(_cfg):
        return [
            doctor.Check("fine", "ok", "all good"),
            doctor.Check("partial", "warn", "degraded", "do this"),
            doctor.Check("absent", "skip", "not configured"),
            doctor.Check("broken", "fail", "it is broken", "fix it like this"),
        ]

    monkeypatch.setattr("cyris.service_layer.doctor.run_checks", fake_checks)
    monkeypatch.setattr("cyris.config.load_config", lambda *a, **k: None)

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

    async def fake_checks(_cfg):
        return [doctor.Check("fine", "ok", "all good")]

    monkeypatch.setattr("cyris.service_layer.doctor.run_checks", fake_checks)
    monkeypatch.setattr("cyris.config.load_config", lambda *a, **k: None)

    result = CliRunner().invoke(app, ["doctor"])

    assert result.exit_code == 0
    assert "Ready to run." in result.stdout
