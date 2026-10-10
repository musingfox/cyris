"""`build_deps` binds Clef to the Workers AI token, else the embedding token."""

from pathlib import Path

import httpx
import pytest
import respx
from fakes import make_config

from cyris import bootstrap
from cyris.config import AgentVaultConfig, Config

pytestmark = pytest.mark.integration

_CLEF = "https://api.cloudflare.com/client/v4/accounts/acct-clef/ai/run/@cf/cloudflare/clef-flash"
_ANSWER = {"success": True, "result": {"answers": {"q": {"noul": 0.5}}, "model": "clef-flash"}}


def _config(tmp_path: Path) -> Config:
    return make_config(agent_vault=AgentVaultConfig(path=tmp_path / "vault"))


def _env(monkeypatch: pytest.MonkeyPatch, **values: str | None) -> None:
    for name in (
        "CLOUDFLARE_AI_TOKEN",
        "CLOUDFLARE_EMBEDDING_API_TOKEN",
        "CLOUDFLARE_ACCOUNT_ID",
        "CLOUDFLARE_API_TOKEN",
    ):
        value = values.get(name)
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


@respx.mock
@pytest.mark.parametrize(
    ("ai", "expected"),
    [("tok-ai", "Bearer tok-ai"), (None, "Bearer tok-embed"), ("", "Bearer tok-embed")],
)
async def test_clef_authenticates_with_the_ai_token_else_the_embedding_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ai: str | None, expected: str
) -> None:
    _env(
        monkeypatch,
        CLOUDFLARE_AI_TOKEN=ai,
        CLOUDFLARE_EMBEDDING_API_TOKEN="tok-embed",
        CLOUDFLARE_ACCOUNT_ID="acct-clef",
        CLOUDFLARE_API_TOKEN="tok-account",
    )
    route = respx.post(_CLEF).mock(return_value=httpx.Response(200, json=_ANSWER))

    ask = bootstrap.build_deps(_config(tmp_path)).ask_clef
    assert ask is not None
    await ask({}, "I")

    assert route.call_count == 1
    assert route.calls.last.request.headers["Authorization"] == expected


def test_clef_is_unbound_without_an_ai_or_embedding_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env(monkeypatch, CLOUDFLARE_ACCOUNT_ID="acct-clef", CLOUDFLARE_API_TOKEN="tok-account")

    assert bootstrap.build_deps(_config(tmp_path)).ask_clef is None


def test_clef_is_unbound_without_the_account_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env(monkeypatch, CLOUDFLARE_AI_TOKEN="tok-ai")

    assert bootstrap.build_deps(_config(tmp_path)).ask_clef is None
