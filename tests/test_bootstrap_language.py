"""The composition root hands the current output language to every issue surface (ADR-0027)."""

from pathlib import Path

import pytest
from fakes import SqliteD1, make_config

from cyris import bootstrap
from cyris.config import AgentVaultConfig, Config

pytestmark = pytest.mark.integration


def _config(tmp_path: Path, language: str) -> Config:
    return make_config(
        agent_vault=AgentVaultConfig(path=tmp_path / "vault"),
        store={"backend": "d1", "database_id": "db", "account_id": "acct", "api_token": "tok"},
        html_output={"enabled": True, "output_dir": str(tmp_path / "html")},
        digest={"output_language": language},
    )


@pytest.mark.parametrize("language", ["en", "zh-Hant"])
def test_the_page_mail_and_discord_get_the_current_language(
    language, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(bootstrap, "build_d1_client", lambda _cfg: SqliteD1())
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "tok")

    deps = bootstrap.build_deps(_config(tmp_path, language))

    assert deps.html_writer.language == language
    assert deps.send_discord.keywords["language"] == language
    assert deps.send_email.keywords["language"] == language
