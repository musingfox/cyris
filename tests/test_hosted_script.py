"""`scripts/hosted.sh`, the runbook's driver, run in a copy of the repo against stub commands.

The human pastes its commands and types every value it needs, so this holds what those
commands promise: nothing reaches Cloudflare until the account is confirmed by name, the hosted
config and never `.env` is what wrangler deploys, and every secret reaches wrangler or curl on
stdin, never in argv.
"""

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
ACCOUNT = "0123456789abcdef0123456789abcdef"
URL = "https://cyris-hosted.trial.workers.dev"
# The operator's Discord webhook: its URL is its credential.
OPERATOR_HOOK = "https://discord.com/api/webhooks/1/operator-SECRET"

# Records its argv and stdin, one JSON line per call, and answers as wrangler would.
BUN = """#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
stdin = "" if sys.stdin.isatty() else sys.stdin.read()
with open(os.environ["CALLS"], "a") as log:
    log.write(json.dumps({"cmd": "bun", "args": args, "stdin": stdin}) + "\\n")
if args[2:4] == ["whoami", "--json"]:
    print(json.dumps({"accounts": [{"id": "ACCOUNT", "name": "Trial account"}]}))
elif args[2:3] == ["deploy"]:
    print("Deployed cyris-hosted triggers\\n  URL")
""".replace("ACCOUNT", ACCOUNT).replace("URL", URL)

# Records its argv and the config it reads on stdin, and answers each route: a report, or the
# tenant's latest page, which the report names by its hash. NO_PAGE names a tenant whose page
# answers 404.
CURL = """#!/usr/bin/env python3
import hashlib, json, os, sys
args = sys.argv[1:]
config = sys.stdin.read()
with open(os.environ["CALLS"], "a") as log:
    log.write(json.dumps({"cmd": "curl", "args": args, "stdin": config}) + "\\n")
url = args[-1]
if "/hosted/tenants/" in url:
    tenant = url.split("/hosted/tenants/")[1].split("/")[0]
else:
    tenant = url.split("/t/")[1].split("/")[0]
if tenant == os.environ.get("UNREACHABLE"):
    # What curl does when it never gets an answer: no file, 000, and exit 7.
    sys.stderr.write("curl: (7) Failed to connect to cyris-hosted\\n")
    sys.stdout.write("000")
    sys.exit(7)
page = ('<html><head><meta name="cyris-issue" content="2026-11-18-morning"></head>'
        f'<body>{tenant}</body></html>').encode()
out = args[args.index("-o") + 1]
if "/t/" in url:
    if tenant == os.environ.get("NO_PAGE"):
        with open(out, "w") as body:
            body.write('{"error": "no issue yet"}')
        sys.stdout.write("404")
        sys.exit(0)
    with open(out, "wb") as body:
        body.write(page)
    sys.stdout.write("200")
    sys.exit(0)
sources = {"alpha": ["Hacker News"], "bravo": ["Aeon"]}[tenant]
report = {
    "tenant": tenant,
    "sources": sources,
    "stored_articles": [{"source_name": sources[0], "articles": 2}],
    "digest_runs": [{"id": 1, "status": "ok", "dry_run": 0}],
    "digests": [
        {"date": "2026-11-18", "period": "morning", "saved_at": "2026-11-18T08:05:00+00:00"}
    ],
    "latest_page": {"path": "/2026-11-18-morning.html", "sha256": hashlib.sha256(page).hexdigest()},
}
with open(out, "w") as body:
    json.dump(report, body)
sys.stdout.write("200")
"""


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """The files the script reads, in a tree of their own, with stub commands first on PATH."""
    for rel in (
        "scripts/hosted.sh",
        "scripts/hosted_check.py",
        "wrangler.hosted.toml",
        "workers/app/src/hosted_seed.json",
    ):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, tmp_path / rel)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("bun", BUN), ("curl", CURL)):
        path = bin_dir / name
        path.write_text(body, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return tmp_path


def _run(
    repo: Path, args: list[str], typed: list[str], **env: str
) -> tuple[subprocess.CompletedProcess, list[dict]]:
    calls = repo / "calls.jsonl"
    done = subprocess.run(
        ["bash", str(repo / "scripts/hosted.sh"), *args],
        input="".join(f"{line}\n" for line in typed),
        capture_output=True,
        text=True,
        env={
            "PATH": f"{repo / 'bin'}:{os.environ['PATH']}",
            "CALLS": str(calls),
            "TMPDIR": str(repo),
            "CLOUDFLARE_ACCOUNT_ID": "f" * 32,
            **env,
        },
        timeout=60,
    )
    lines = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
    return done, [json.loads(line) for line in lines]


def test_nothing_is_deployed_until_the_account_is_confirmed_by_name(repo: Path) -> None:
    done, calls = _run(repo, ["deploy"], [ACCOUNT, "no"])

    assert done.returncode == 1
    assert "account name: Trial account" in done.stdout
    assert [c["args"][2] for c in calls] == ["whoami"]


def test_deploy_ships_the_hosted_config_without_dotenv_and_keeps_its_url(repo: Path) -> None:
    done, calls = _run(repo, ["deploy"], [ACCOUNT, "yes"])

    assert done.returncode == 0, done.stderr
    [deploy] = [c for c in calls if c["args"][2] == "deploy"]
    assert deploy["args"] == [
        "x",
        "wrangler",
        "deploy",
        "--config",
        "wrangler.hosted.toml",
        "--env-file",
        "/dev/null",
    ]
    state = repo / ".env.hosted"
    assert state.read_text(encoding="utf-8") == f"HOSTED_URL={URL}\n"
    assert stat.S_IMODE(state.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    ("args", "typed", "whoami"),
    [
        (["deploy"], [ACCOUNT, "yes"], True),
        (["deploy"], [ACCOUNT, "no"], True),
        (
            ["secrets"],
            [ACCOUNT, "yes", "team.cloudflareaccess.com", "AUD-1", "sk-ant-SECRET", OPERATOR_HOOK],
            True,
        ),
        (["containers"], [ACCOUNT], False),
        (["tail"], [ACCOUNT], False),
        (["delete"], [ACCOUNT, "yes"], True),
    ],
)
def test_every_wrangler_call_reads_the_hosted_config_and_never_dotenv(
    repo: Path, args: list[str], typed: list[str], whoami: bool
) -> None:
    """The confirmation's whoami too: a token from `.env` would make it check another credential.

    A checkout's root `.env` holds production's CLOUDFLARE_API_TOKEN; wrangler reads it unless
    told `--env-file /dev/null`, so a whoami without it would name the account that token sees
    while deploy acted as the logged-in user.
    """
    (repo / ".env").write_text("CLOUDFLARE_API_TOKEN=production-token\n", encoding="utf-8")
    (repo / ".env.hosted").write_text(f"HOSTED_URL={URL}\n", encoding="utf-8")

    _, calls = _run(repo, args, typed)

    wrangler = [c["args"] for c in calls if c["cmd"] == "bun"]
    assert wrangler
    assert any(a[2] == "whoami" for a in wrangler) is whoami
    for argv in wrangler:
        assert argv[:2] == ["x", "wrangler"], argv
        assert argv[-4:] == ["--config", "wrangler.hosted.toml", "--env-file", "/dev/null"], argv


def test_the_account_typed_is_the_one_wrangler_acts_on_whatever_the_environment_held(
    repo: Path,
) -> None:
    done, _ = _run(repo, ["deploy"], ["not-an-id"])

    assert done.returncode == 1
    assert "ACCOUNT_ID must be 32 hex characters" in done.stderr


def test_each_secret_reaches_wrangler_on_stdin_and_never_in_argv(repo: Path) -> None:
    (repo / ".env.hosted").write_text(f"HOSTED_URL={URL}\n", encoding="utf-8")
    typed = [ACCOUNT, "yes", "team.cloudflareaccess.com", "AUD-1", "sk-ant-SECRET", OPERATOR_HOOK]

    done, calls = _run(repo, ["secrets"], typed)

    assert done.returncode == 0, done.stderr
    puts = [c for c in calls if c["args"][2:4] == ["secret", "put"]]
    assert [(c["args"][4], c["stdin"]) for c in puts] == [
        ("CYRIS_ACCESS_TEAM_DOMAIN", "team.cloudflareaccess.com"),
        ("CYRIS_ACCESS_AUD", "AUD-1"),
        ("ANTHROPIC_API_KEY", "sk-ant-SECRET"),
        # Where every tenant's failed run is reported (ADR-0035).
        ("CYRIS_HOSTED_OPERATOR_WEBHOOK", OPERATOR_HOOK),
        # The host deploy kept: every tenant's links point at it.
        ("CYRIS_STORE_HOSTED_DOMAIN", "cyris-hosted.trial.workers.dev"),
    ]
    assert all(
        c["args"][5:] == ["--config", "wrangler.hosted.toml", "--env-file", "/dev/null"]
        for c in puts
    )
    for secret in ("sk-ant-SECRET", OPERATOR_HOOK):
        assert not any(secret in " ".join(c["args"]) for c in calls)
        assert secret not in done.stdout + done.stderr


def test_secrets_refuses_an_operator_webhook_that_is_not_https(repo: Path) -> None:
    (repo / ".env.hosted").write_text(f"HOSTED_URL={URL}\n", encoding="utf-8")
    typed = [ACCOUNT, "yes", "team.cloudflareaccess.com", "AUD-1", "sk-ant-SECRET", "hook"]

    done, calls = _run(repo, ["secrets"], typed)

    assert done.returncode == 1
    assert "CYRIS_HOSTED_OPERATOR_WEBHOOK must be an https:// URL" in done.stderr
    assert [c for c in calls if c["args"][2:4] == ["secret", "put"]] == []


def test_secrets_needs_the_url_deploy_kept_before_asking_for_anything(repo: Path) -> None:
    done, calls = _run(repo, ["secrets"], [ACCOUNT, "yes"])

    assert done.returncode == 1
    assert "no HOSTED_URL in .env.hosted; run: scripts/hosted.sh deploy" in done.stderr
    assert calls == []


def test_check_reads_every_tenant_and_its_page_with_the_token_on_stdin_and_judges_them(
    repo: Path,
) -> None:
    (repo / ".env.hosted").write_text(f"HOSTED_URL={URL}\n", encoding="utf-8")

    done, calls = _run(repo, ["check"], ["client-id.access", "client-SECRET"])

    assert done.returncode == 0, done.stderr
    curls = [c for c in calls if c["cmd"] == "curl"]
    assert [c["args"][-1] for c in curls] == [
        f"{URL}/hosted/tenants/alpha/report",
        f"{URL}/t/alpha/",
        f"{URL}/hosted/tenants/bravo/report",
        f"{URL}/t/bravo/",
    ]
    assert "GET /t/alpha/ -> HTTP 200" in done.stderr
    # The page is judged, not printed.
    assert "<meta" not in done.stdout
    for c in curls:
        assert "CF-Access-Client-Secret: client-SECRET" in c["stdin"]
        assert "client-SECRET" not in " ".join(c["args"])
    assert "PASS alpha" in done.stdout
    assert "PASS bravo" in done.stdout


def test_a_run_names_a_period_the_worker_takes(repo: Path) -> None:
    (repo / ".env.hosted").write_text(f"HOSTED_URL={URL}\n", encoding="utf-8")

    refused, calls = _run(repo, ["run", "noon"], [])
    done, calls = _run(repo, ["run", "evening"], ["client-id.access", "client-SECRET"])

    assert refused.returncode == 2
    assert done.returncode == 0, done.stderr
    assert [c["args"][-1] for c in calls if c["cmd"] == "curl"] == [
        f"{URL}/hosted/tenants/alpha/run?period=evening",
        f"{URL}/hosted/tenants/bravo/run?period=evening",
    ]


def test_poll_polls_each_seeded_tenant_with_the_token_on_stdin(repo: Path) -> None:
    """The tick's RSS poll on request, so the operator can see a buffer fill and measure one."""
    (repo / ".env.hosted").write_text(f"HOSTED_URL={URL}\n", encoding="utf-8")

    done, calls = _run(repo, ["poll"], ["client-id.access", "client-SECRET"])

    assert done.returncode == 0, done.stderr
    curls = [c for c in calls if c["cmd"] == "curl"]
    assert [c["args"][-1] for c in curls] == [
        f"{URL}/hosted/tenants/alpha/poll",
        f"{URL}/hosted/tenants/bravo/poll",
    ]
    for c in curls:
        assert c["args"][c["args"].index("-X") + 1] == "POST"
        assert "CF-Access-Client-Secret: client-SECRET" in c["stdin"]
        assert "client-SECRET" not in " ".join(c["args"])


def test_register_registers_each_seeded_tenant_with_the_token_on_stdin(repo: Path) -> None:
    """The seed route seeds only a registered tenant, so the runbook registers both first."""
    (repo / ".env.hosted").write_text(f"HOSTED_URL={URL}\n", encoding="utf-8")

    done, calls = _run(repo, ["register"], ["client-id.access", "client-SECRET"])

    assert done.returncode == 0, done.stderr
    curls = [c for c in calls if c["cmd"] == "curl"]
    assert [c["args"][-1] for c in curls] == [
        f"{URL}/hosted/tenants/alpha/register",
        f"{URL}/hosted/tenants/bravo/register",
    ]
    for c in curls:
        assert c["args"][c["args"].index("-X") + 1] == "POST"
        assert "CF-Access-Client-Secret: client-SECRET" in c["stdin"]
        assert "client-SECRET" not in " ".join(c["args"])


@pytest.mark.parametrize(
    ("args", "route"),
    [
        (["register"], "register"),
        (["seed"], "seed"),
        (["run", "morning"], "run?period=morning"),
        (["poll"], "poll"),
        (["check"], "report"),
    ],
)
def test_a_tenant_that_never_answers_fails_alone_and_the_next_is_still_asked(
    repo: Path, args: list[str], route: str
) -> None:
    """A curl that gets no answer writes no file: the loop must not end on it, nor pass."""
    (repo / ".env.hosted").write_text(f"HOSTED_URL={URL}\n", encoding="utf-8")

    done, calls = _run(repo, args, ["client-id.access", "client-SECRET"], UNREACHABLE="alpha")

    assert done.returncode == 1
    # check also fetches the page of each tenant whose report came back.
    page = [f"{URL}/t/bravo/"] if args == ["check"] else []
    assert [c["args"][-1] for c in calls if c["cmd"] == "curl"] == [
        f"{URL}/hosted/tenants/alpha/{route}",
        f"{URL}/hosted/tenants/bravo/{route}",
        *page,
    ]
    assert "-> HTTP 000" in done.stderr
    assert "FAILED alpha:" in done.stderr
    assert "failed for: alpha" in done.stderr
    assert "FAILED bravo" not in done.stderr


def test_a_tenant_whose_page_does_not_answer_200_fails_the_check(repo: Path) -> None:
    (repo / ".env.hosted").write_text(f"HOSTED_URL={URL}\n", encoding="utf-8")

    done, _ = _run(repo, ["check"], ["client-id.access", "client-SECRET"], NO_PAGE="bravo")

    assert done.returncode == 1
    assert "PASS alpha" in done.stdout
    assert "FAIL bravo: its latest page did not answer 200" in done.stdout
    assert "FAILED bravo: its latest page answered HTTP 404" in done.stderr
    assert "failed for: bravo" in done.stderr


def test_check_still_judges_the_reports_that_came_back(repo: Path) -> None:
    (repo / ".env.hosted").write_text(f"HOSTED_URL={URL}\n", encoding="utf-8")

    done, _ = _run(repo, ["check"], ["client-id.access", "client-SECRET"], UNREACHABLE="bravo")

    assert done.returncode == 1
    assert "PASS alpha" in done.stdout
    assert "failed for: bravo" in done.stderr
