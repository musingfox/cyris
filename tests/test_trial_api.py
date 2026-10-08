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
    cf = Cloudflare()
    assert check(cf, "--email") == 0
    assert len(cf.requests) == 4
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
    cf = Cloudflare(**answers)
    assert check(cf, "--email") == 1
    assert len(cf.requests) == 4
    out = capsys.readouterr().out
    assert out.strip().splitlines() == [f"✗ the token lacks {missing}"]


def test_every_missing_permission_is_named_at_once(capsys) -> None:
    cf = Cloudflare(pages=AUTH, domains=AUTH)
    assert check(cf) == 1
    assert len(cf.requests) == 3
    out = capsys.readouterr().out
    assert "Cloudflare Pages → Edit" in out and "Workers Scripts → Read" in out


def test_mail_is_probed_only_when_asked() -> None:
    cf = Cloudflare(mail=AUTH)
    assert check(cf) == 0
    assert len(cf.requests) == 3
    assert not any("/email/" in r.url.path for r in cf.requests)


def test_the_mail_probe_sends_nothing_a_mail_could_be_made_of() -> None:
    cf = Cloudflare()
    check(cf, "--email")
    assert len(cf.requests) == 4
    mail = next(r for r in cf.requests if "/email/" in r.url.path)
    assert mail.content == b"{}"


def test_an_answer_that_is_not_json_is_not_a_grant(capsys) -> None:
    cf = Cloudflare(pages=httpx.Response(502, text="<html>bad gateway</html>"))
    assert check(cf) == 1
    assert len(cf.requests) == 3
    assert "Cloudflare Pages → Edit" in capsys.readouterr().out


def test_every_probe_targets_the_trial(capsys) -> None:
    cf = Cloudflare()
    check(cf, "--email")
    paths = {r.url.path for r in cf.requests}
    assert len(cf.requests) == len(paths)
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
    assert len(cf.requests) == 4
    assert {r.headers["authorization"] for r in cf.requests} == {f"Bearer {TOKEN}"}
    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err


def test_no_token_on_stdin_is_refused(capsys) -> None:
    cf = Cloudflare()
    assert trial_api.main(CHECK, io.StringIO(""), cf.client_for) == 1
    assert cf.requests == []
    assert "No token on stdin." in capsys.readouterr().err


def _rows(*rows: dict) -> dict:
    return {"success": True, "errors": [], "result": [{"results": list(rows), "success": True}]}


class D1:
    """Answers each digest_runs query with the next table state, then repeats the last."""

    def __init__(self, *states: dict) -> None:
        self.states = list(states)
        self.sql: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.sql.append(request.read().decode())
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return httpx.Response(200 if state.get("success") else 403, json=state)

    def client_for(self, token: str) -> httpx.Client:
        return httpx.Client(base_url=trial_api.API_ROOT, transport=httpx.MockTransport(self.handle))


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def __call__(self) -> float:
        return self.now


RUN_4 = {"id": 4, "status": "publish_failed", "period": "evening", "summary": "{}"}
RUN_5 = {
    "id": 5,
    "status": "ok",
    "period": "evening",
    "build_sha": "00fa8ab",
    "summary": '{"fetched": 6, "included": 6, "status": "ok", "sources": ["x"]}',
}
WAIT = ["wait-run", "--account-id", ACCOUNT, "--d1-id", D1_ID, "--app-worker", "cyris-app-t1"]


def wait(d1: D1, clock: Clock, *extra: str) -> int:
    return trial_api.main(
        [*WAIT, *extra], io.StringIO(TOKEN + "\n"), d1.client_for, clock.sleep, clock
    )


def test_the_wait_ends_at_the_first_row_newer_than_the_one_before(capsys) -> None:
    clock = Clock()
    d1 = D1(_rows(RUN_4), _rows(RUN_4), _rows(RUN_5))
    assert wait(d1, clock, "--after", "4", "--interval", "15") == 0
    assert len(d1.sql) == 3
    out = capsys.readouterr().out
    assert "run 5: status ok" in out
    assert clock.slept == [15, 15]
    assert out.count("no new run yet") == 2


def test_the_new_row_shows_its_columns_and_its_counts(capsys) -> None:
    d1 = D1(_rows(RUN_5))
    assert wait(d1, Clock(), "--after", "4") == 0
    assert len(d1.sql) == 1
    lines = capsys.readouterr().out.splitlines()
    for line in ("build_sha = 00fa8ab", "fetched = 6", "included = 6", "period = evening"):
        assert f"    {line}" in lines
    assert not any("sources" in ln or "summary" in ln for ln in lines)


def test_a_count_both_a_column_and_in_the_summary_shows_once(capsys) -> None:
    row = RUN_5 | {"fetched": 6, "summary": '{"fetched": 6, "wall_seconds": 1.3}'}
    d1 = D1(_rows(row))
    wait(d1, Clock(), "--after", "4")
    assert len(d1.sql) == 1
    lines = capsys.readouterr().out.splitlines()
    assert lines.count("    fetched = 6") == 1
    assert "    wall_seconds = 1.3" in lines


def test_a_failed_publish_points_at_the_pages_permission(capsys) -> None:
    failed = RUN_5 | {"status": "publish_failed"}
    d1 = D1(_rows(failed))
    assert wait(d1, Clock(), "--after", "4") == 0
    assert len(d1.sql) == 1
    out = capsys.readouterr().out
    assert "status publish_failed" in out
    assert "Cloudflare Pages → Edit" in out and "--from 7" in out


def test_an_ok_run_does_not_mention_pages(capsys) -> None:
    d1 = D1(_rows(RUN_5))
    wait(d1, Clock(), "--after", "4")
    assert len(d1.sql) == 1
    assert "Pages" not in capsys.readouterr().out


def test_the_wait_gives_up_at_its_timeout_and_names_workers_logs(capsys) -> None:
    clock = Clock()
    d1 = D1(_rows(RUN_4))
    assert wait(d1, clock, "--after", "4", "--timeout", "60", "--interval", "15") == 2
    assert len(d1.sql) == 5
    out = capsys.readouterr().out
    assert "No digest_runs row after 60s" in out
    assert f"https://dash.cloudflare.com/{ACCOUNT}/workers-and-pages → cyris-app-t1" in out
    assert clock.now == 60


def test_an_empty_table_counts_as_run_zero(capsys) -> None:
    latest = ["latest-run", "--account-id", ACCOUNT, "--d1-id", D1_ID]
    empty = D1(_rows())
    assert trial_api.main(latest, io.StringIO(TOKEN + "\n"), empty.client_for) == 0
    assert len(empty.sql) == 1
    assert capsys.readouterr().out.strip() == "0"
    d1 = D1(_rows(RUN_4))
    assert wait(d1, Clock(), "--after", "0") == 0
    assert len(d1.sql) == 1


def test_a_refused_query_fails_without_the_token(capsys) -> None:
    d1 = D1(D1_DENIED)
    assert wait(d1, Clock(), "--after", "4") == 1
    assert len(d1.sql) == 1
    captured = capsys.readouterr()
    assert "digest_runs query failed: errors [7403]" in captured.err
    assert TOKEN not in captured.out + captured.err


def test_the_wait_only_reads() -> None:
    d1 = D1(_rows(RUN_5))
    wait(d1, Clock(), "--after", "4")
    assert len(d1.sql) == 1
    assert d1.sql and all(s.startswith('{"sql":"SELECT * FROM digest_runs') for s in d1.sql)
