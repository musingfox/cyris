"""`cyris config show`: every graded setting, its effective value, and where that came from."""

import re
from pathlib import Path

import pytest
from fakes import SqliteD1, seed_d1_settings, settings_toml
from typer.testing import CliRunner

from cyris.adapters.store.feed_health import UNHEALTHY_FAILURE_STREAK
from cyris.bootstrap import default_model, embedding_defaults
from cyris.config import B_GRADE_ENV_VARS, GRADE_D_KEYS
from cyris.entrypoints.cli import app

pytestmark = pytest.mark.integration

runner = CliRunner()

# Long enough that a printed suffix or prefix could not be mistaken for the whole.
SECRET = "sk-fake-" + "q" * 32
WEBHOOK_TOKEN = "hookTOKEN" + "z" * 24
WEBHOOK = f"https://discord.com/api/webhooks/123/{WEBHOOK_TOKEN}"


def _show(tmp_path: Path, toml: str | None, *, color: bool = False):
    config_path = tmp_path / "cyris.toml"
    if toml is not None:
        config_path.write_text(toml)
    return runner.invoke(
        app,
        [
            "config",
            "show",
            "--config",
            str(config_path),
            "--sources",
            str(tmp_path / "sources.yaml"),
        ],
        color=color,
    )


def _rows(output: str) -> dict[str, tuple[str, str]]:
    """Each printed row as key → (value, source)."""
    rows = {}
    for line in output.splitlines():
        cells = re.split(r"\s{2,}", line.strip())
        if len(cells) == 3:
            rows[cells[0]] = (cells[1], cells[2])
    return rows


def test_a_d1_setting_is_labelled_d1_whatever_the_file_says(tmp_path: Path, d1: SqliteD1) -> None:
    seed_d1_settings(d1, **{"llm_provider.provider": "gemini", "llm_provider.model": "g-d1"})

    result = _show(tmp_path, settings_toml(**{"llm_provider.model": "g-file"}))

    assert result.exit_code == 0, result.output
    assert _rows(result.stdout)["llm_provider.model"] == ('"g-d1"', "D1 settings")


def test_a_missing_d1_setting_is_shown_missing_and_still_exits_0(
    tmp_path: Path, d1: SqliteD1
) -> None:
    seed_d1_settings(d1, omit=["digest.style_prompt"])

    result = _show(tmp_path, None)

    assert result.exit_code == 0, result.output
    assert _rows(result.stdout)["digest.style_prompt"] == ("-", "missing")


def test_a_json_deployment_takes_settings_from_cyris_toml(tmp_path: Path) -> None:
    result = _show(tmp_path, settings_toml(**{"digest.max_featured": 7}))

    assert result.exit_code == 0, result.output
    assert _rows(result.stdout)["digest.max_featured"] == ("7", "cyris.toml")


def test_every_grade_d_and_grade_b_key_gets_a_row(tmp_path: Path) -> None:
    result = _show(tmp_path, settings_toml())

    rows = _rows(result.stdout)
    assert set(GRADE_D_KEYS) <= rows.keys()
    assert set(B_GRADE_ENV_VARS) <= rows.keys()


def test_an_empty_llm_model_shows_the_provider_default(tmp_path: Path) -> None:
    toml = settings_toml(**{"llm_provider.provider": "gemini", "llm_provider.model": ""})

    result = _show(tmp_path, toml)

    value, source = _rows(result.stdout)["llm_provider.model"]
    assert value == f'"{default_model("gemini")}"'
    assert source.startswith("default")


def test_a_secret_from_dotenv_shows_only_that_it_is_set(
    tmp_path: Path, d1: SqliteD1, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Absent before the load, so the `.env` value is the one that lands, and
    # monkeypatch removes it again afterwards.
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    (tmp_path / ".env").write_text(f"GEMINI_API_KEY={SECRET}\n")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", SECRET[::-1])
    seed_d1_settings(d1, **{"notify.discord_webhook_url": WEBHOOK})

    result = _show(tmp_path, None)

    assert result.exit_code == 0, result.output
    rows = _rows(result.stdout)
    assert rows["GEMINI_API_KEY"] == ("set", ".env")
    assert rows["CLOUDFLARE_API_TOKEN"] == ("set", "environment")
    for printed in (result.stdout, result.stderr, result.output):
        assert SECRET not in printed
        assert SECRET[::-1] not in printed
        assert WEBHOOK_TOKEN not in printed


def test_a_secret_from_the_environment_is_labelled_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    result = _show(tmp_path, settings_toml())

    rows = _rows(result.stdout)
    assert rows["OPENAI_API_KEY"] == ("set", "environment")
    assert rows["ANTHROPIC_API_KEY"] == ("-", "unset")
    assert SECRET not in result.output


def test_a_file_identity_value_beats_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CYRIS_PROMOTE_PAGES_PROJECT", "from-env")
    monkeypatch.setenv("CYRIS_RSS_WORKER_URL", "https://rss.example")

    result = _show(tmp_path, '[promote]\npages_project = "from-file"\n\n' + settings_toml())

    rows = _rows(result.stdout)
    assert rows["promote.pages_project"] == ('"from-file"', "cyris.toml")
    assert rows["rss.worker_url"] == ('"https://rss.example"', "environment")
    assert rows["store.backend"] == ('"json"', "code")


def test_a_baked_threshold_is_labelled_code(tmp_path: Path) -> None:
    result = _show(tmp_path, settings_toml())

    rows = _rows(result.stdout)
    assert rows["feed_health.unhealthy_failure_streak"] == (str(UNHEALTHY_FAILURE_STREAK), "code")


def test_the_embedding_threshold_follows_the_calibrated_model(tmp_path: Path) -> None:
    toml = settings_toml(**{"vote_similarity.provider": "workers_ai", "vote_similarity.model": ""})

    result = _show(tmp_path, toml)

    value, source = _rows(result.stdout)["vote_similarity.threshold"]
    assert value == str(embedding_defaults("workers_ai")["threshold"])
    assert source.startswith("default")


def test_the_output_has_no_terminal_escapes_and_aligned_columns(tmp_path: Path) -> None:
    result = _show(tmp_path, settings_toml(), color=True)

    assert "\x1b" not in result.output
    header = next(line for line in result.stdout.splitlines() if line.startswith("KEY"))
    source_column = header.index("SOURCE")
    row = next(
        line for line in result.stdout.splitlines() if line.startswith("digest.max_featured")
    )
    assert row.index("cyris.toml") == source_column


def test_a_config_that_cannot_load_prints_the_error_and_exits_1(tmp_path: Path) -> None:
    result = _show(tmp_path, "[store\n")

    assert result.exit_code == 1
    assert "config" in result.output
