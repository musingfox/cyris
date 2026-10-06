"""scripts/trial_api.py: what it concludes from Cloudflare's answers.

Cloudflare is a fake transport that answers each path the way the real API answered
the first trial's tokens on 2026-10-06.
"""

import importlib.util
import io
import sys
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("trial_api", ROOT / "scripts/trial_api.py")
trial_api = importlib.util.module_from_spec(_spec)
sys.modules["trial_api"] = trial_api
_spec.loader.exec_module(trial_api)

ACCOUNT = "a" * 32
D1_ID = "0948e71f-2374-4e4e-8f30-44890139011b"
TOKEN = "trial-token-SECRET"
OK = {"success": True, "errors": [], "result": []}
AUTH = {"success": False, "errors": [{"code": 10000, "message": "Authentication error"}]}
D1_DENIED = {"success": False, "errors": [{"code": 7403, "message": "not authorized"}]}
MAIL_SCHEMA = {
    "success": False,
    "errors": [{"code": 10001, "message": "email.sending.error.invalid_request_schema"}],
}
CHECK = [
    "check-token",
    "--account-id",
    ACCOUNT,
    "--d1-id",
    D1_ID,
    "--pages-project",
    "cyris-app-t1-0123456789abcdef",
    "--app-worker",
    "cyris-app-t1",
]


class Cloudflare:
    def __init__(self, **answers) -> None:
        self.answers = {"d1": OK, "pages": OK, "domains": OK, "mail": MAIL_SCHEMA} | answers
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        key = next(
            k
            for k, part in [
                ("d1", "/d1/database/"),
                ("pages", "/upload-token"),
                ("domains", "/workers/domains"),
                ("mail", "/email/sending/send"),
            ]
            if part in path
        )
        answer = self.answers[key]
        if isinstance(answer, httpx.Response):
            return answer
        return httpx.Response(200 if answer.get("success") else 403, json=answer)

    def client_for(self, token: str) -> httpx.Client:
        return httpx.Client(
            base_url=trial_api.API_ROOT,
            headers={"Authorization": f"Bearer {token}"},
            transport=httpx.MockTransport(self.handle),
        )


def check(cf: Cloudflare, *extra: str) -> int:
    return trial_api.main([*CHECK, *extra], io.StringIO(TOKEN + "\n"), cf.client_for)


def test_a_token_with_every_permission_passes(capsys) -> None:
    assert check(Cloudflare(), "--email") == 0
    assert "✓ the token has every permission the trial needs" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("answers", "missing"),
    [
        ({"pages": AUTH}, "Cloudflare Pages → Edit"),
        ({"d1": D1_DENIED}, "D1 → Edit"),
        ({"domains": AUTH}, "Workers Scripts → Read"),
        ({"mail": AUTH}, "Email Sending → Edit"),
    ],
)
def test_each_missing_permission_is_named(capsys, answers: dict, missing: str) -> None:
    assert check(Cloudflare(**answers), "--email") == 1
    out = capsys.readouterr().out
    assert out.strip().splitlines() == [f"✗ the token lacks {missing}"]


def test_every_missing_permission_is_named_at_once(capsys) -> None:
    assert check(Cloudflare(pages=AUTH, domains=AUTH)) == 1
    out = capsys.readouterr().out
    assert "Cloudflare Pages → Edit" in out and "Workers Scripts → Read" in out


def test_mail_is_probed_only_when_asked() -> None:
    cf = Cloudflare(mail=AUTH)
    assert check(cf) == 0
    assert not any("/email/" in r.url.path for r in cf.requests)


def test_the_mail_probe_sends_nothing_a_mail_could_be_made_of() -> None:
    cf = Cloudflare()
    check(cf, "--email")
    mail = next(r for r in cf.requests if "/email/" in r.url.path)
    assert mail.content == b"{}"


def test_an_answer_that_is_not_json_is_not_a_grant(capsys) -> None:
    assert check(Cloudflare(pages=httpx.Response(502, text="<html>bad gateway</html>"))) == 1
    assert "Cloudflare Pages → Edit" in capsys.readouterr().out


def test_every_probe_targets_the_trial(capsys) -> None:
    cf = Cloudflare()
    check(cf, "--email")
    paths = {r.url.path for r in cf.requests}
    base = f"/client/v4/accounts/{ACCOUNT}"
    assert paths == {
        f"{base}/d1/database/{D1_ID}/query",
        f"{base}/pages/projects/cyris-app-t1-0123456789abcdef/upload-token",
        f"{base}/workers/domains",
        f"{base}/email/sending/send",
    }
    domains = next(r for r in cf.requests if r.url.path.endswith("/workers/domains"))
    assert domains.url.params["service"] == "cyris-app-t1"


def test_the_token_is_sent_and_never_printed(capsys) -> None:
    cf = Cloudflare(pages=AUTH)
    check(cf, "--email")
    assert {r.headers["authorization"] for r in cf.requests} == {f"Bearer {TOKEN}"}
    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err


def test_no_token_on_stdin_is_refused(capsys) -> None:
    assert trial_api.main(CHECK, io.StringIO(""), Cloudflare().client_for) == 1
    assert "No token on stdin." in capsys.readouterr().err
