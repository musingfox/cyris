"""`build_deps` binds the run recorder to D1, stamped with the image's sha."""

from pathlib import Path

import httpx
import pytest
import respx
from fakes import SqliteD1, make_config

from cyris import bootstrap
from cyris.config import AgentVaultConfig, Config
from cyris.domain.models import DigestContent, UsageStats

pytestmark = pytest.mark.integration

_SUMMARY = {"status": "ok", "period": "morning", "dry_run": False}


def _d1_config(tmp_path: Path) -> Config:
    cfg = make_config(agent_vault=AgentVaultConfig(path=tmp_path / "vault"))
    cfg.app.store.backend = "d1"
    cfg.app.store.database_id = "db"
    cfg.app.store.account_id = "acct"
    cfg.app.store.api_token = "tok"
    return cfg


@pytest.fixture
def db(monkeypatch) -> SqliteD1:
    db = SqliteD1()
    monkeypatch.setattr(bootstrap, "build_d1_client", lambda _cfg: db)
    return db


def test_a_d1_run_row_carries_the_image_sha(tmp_path: Path, monkeypatch, db) -> None:
    monkeypatch.setenv("CYRIS_GIT_SHA", "deadbeef")

    bootstrap.build_deps(_d1_config(tmp_path)).record_run(_SUMMARY)

    assert db.query("SELECT build_sha FROM digest_runs").rows == [{"build_sha": "deadbeef"}]


def test_an_image_built_without_a_sha_records_an_empty_one(tmp_path: Path, monkeypatch, db) -> None:
    monkeypatch.delenv("CYRIS_GIT_SHA", raising=False)

    bootstrap.build_deps(_d1_config(tmp_path)).record_run(_SUMMARY)

    assert db.query("SELECT build_sha FROM digest_runs").rows == [{"build_sha": ""}]


def test_a_json_backend_records_no_run(tmp_path: Path) -> None:
    cfg = make_config(agent_vault=AgentVaultConfig(path=tmp_path / "vault"))

    assert bootstrap.build_deps(cfg).record_run is None


def test_a_d1_deployment_stores_its_digest_in_its_own_d1(tmp_path: Path, db) -> None:
    content = DigestContent(
        date="2026-09-24",
        period="morning",
        sources_processed=1,
        articles_received=1,
        articles_included=1,
        usage=UsageStats(),
    )

    bootstrap.build_deps(_d1_config(tmp_path)).digest_store.save(content, raw_page=True)

    assert db.query("SELECT date, period FROM digests").rows == [
        {"date": content.date, "period": content.period}
    ]


def test_a_json_backend_has_no_digest_store(tmp_path: Path) -> None:
    cfg = make_config(agent_vault=AgentVaultConfig(path=tmp_path / "vault"))

    assert bootstrap.build_deps(cfg).digest_store is None


def _json_config(tmp_path: Path) -> Config:
    return make_config(agent_vault=AgentVaultConfig(path=tmp_path / "vault"))


def _alert_mail_env(monkeypatch: pytest.MonkeyPatch, *, account: str = "", token: str = "") -> None:
    if account:
        monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", account)
    else:
        monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    if token:
        monkeypatch.setenv("CLOUDFLARE_API_TOKEN", token)
    else:
        monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    monkeypatch.setenv("CLOUDFLARE_AI_TOKEN", "tok-decoy-ai")
    monkeypatch.setenv("CLOUDFLARE_EMBEDDING_API_TOKEN", "tok-decoy-embed")


_ALERT_SEND = "https://api.cloudflare.com/client/v4/accounts/acct-alert/email/sending/send"


@respx.mock
async def test_failure_mail_uses_the_account_token_not_the_ai_or_embedding_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _alert_mail_env(monkeypatch, account="acct-alert", token="tok-alert")
    route = respx.post(_ALERT_SEND).mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True,
                "errors": [],
                "messages": [],
                "result": {
                    "delivered": ["me@example.org"],
                    "permanent_bounces": [],
                    "queued": [],
                },
            },
        )
    )

    await bootstrap.build_deps(_json_config(tmp_path)).send_email_alert(
        "me@example.org", "d@example.org", "S", "B"
    )

    assert route.call_count == 1
    assert route.calls.last.request.headers["Authorization"] == "Bearer tok-alert"


def test_failure_mail_is_unbound_without_the_api_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _alert_mail_env(monkeypatch, account="acct-alert")

    assert bootstrap.build_deps(_json_config(tmp_path)).send_email_alert is None


def test_failure_mail_is_unbound_without_the_account_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _alert_mail_env(monkeypatch, token="tok-alert")

    assert bootstrap.build_deps(_json_config(tmp_path)).send_email_alert is None


def test_failure_mail_is_one_field_and_ports_gain_no_sender_list() -> None:
    ports = Path("src/cyris/service_layer/ports.py").read_text()
    fields = bootstrap.Deps.__dataclass_fields__

    assert "send_alert" not in ports
    assert "send_email_alert" not in ports
    assert "senders" not in fields
    assert fields["send_email_alert"].default is None
