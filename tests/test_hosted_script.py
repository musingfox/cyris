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

# Records its argv and the config it reads on stdin, and answers each route with a report.
CURL = """#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
config = sys.stdin.read()
with open(os.environ["CALLS"], "a") as log:
    log.write(json.dumps({"cmd": "curl", "args": args, "stdin": config}) + "\\n")
url = args[-1]
tenant = url.split("/hosted/tenants/")[1].split("/")[0]
sources = {"alpha": ["Hacker News"], "bravo": ["Aeon"]}[tenant]
report = {
    "tenant": tenant,
    "sources": sources,
    "stored_articles": [{"source_name": sources[0], "articles": 2}],
    "digest_runs": [{"id": 1, "status": "ok", "dry_run": 0}],
    "digests": [{"date": "2026-11-18", "period": "morning"}],
}
with open(args[args.index("-o") + 1], "w") as out:
    json.dump(report, out)
sys.stdout.write("200")
"""


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """The files the script reads, in a tree of their own, with stub commands first on PATH."""
    for rel in ("scripts/hosted.sh", "scripts/hosted_check.py", "wrangler.hosted.toml"):
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
    repo: Path, args: list[str], typed: list[str]
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


def test_the_account_typed_is_the_one_wrangler_acts_on_whatever_the_environment_held(
    repo: Path,
) -> None:
    done, _ = _run(repo, ["deploy"], ["not-an-id"])

    assert done.returncode == 1
    assert "ACCOUNT_ID must be 32 hex characters" in done.stderr


def test_each_secret_reaches_wrangler_on_stdin_and_never_in_argv(repo: Path) -> None:
    typed = [ACCOUNT, "yes", "team.cloudflareaccess.com", "AUD-1", "sk-ant-SECRET"]

    done, calls = _run(repo, ["secrets"], typed)

    assert done.returncode == 0, done.stderr
    puts = [c for c in calls if c["args"][2:4] == ["secret", "put"]]
    assert [(c["args"][4], c["stdin"]) for c in puts] == [
        ("CYRIS_ACCESS_TEAM_DOMAIN", "team.cloudflareaccess.com"),
        ("CYRIS_ACCESS_AUD", "AUD-1"),
        ("ANTHROPIC_API_KEY", "sk-ant-SECRET"),
    ]
    assert all(
        c["args"][5:] == ["--config", "wrangler.hosted.toml", "--env-file", "/dev/null"]
        for c in puts
    )
    assert not any("sk-ant-SECRET" in " ".join(c["args"]) for c in calls)
    assert "sk-ant-SECRET" not in done.stdout + done.stderr


def test_check_reads_every_tenant_with_the_token_on_stdin_and_judges_them(repo: Path) -> None:
    (repo / ".env.hosted").write_text(f"HOSTED_URL={URL}\n", encoding="utf-8")

    done, calls = _run(repo, ["check"], ["client-id.access", "client-SECRET"])

    assert done.returncode == 0, done.stderr
    curls = [c for c in calls if c["cmd"] == "curl"]
    assert [c["args"][-1] for c in curls] == [
        f"{URL}/hosted/tenants/alpha/report",
        f"{URL}/hosted/tenants/bravo/report",
    ]
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
