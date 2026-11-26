"""`scripts/hosted.sh`, the runbook's driver, run in a copy of the repo against stub commands.

The human pastes its commands and types every value it needs, so this holds what those
commands promise: nothing reaches Cloudflare until the account is confirmed by name, the hosted
config and never `.env` is what wrangler deploys, and every secret reaches wrangler or curl on
stdin, never in argv.
"""

import hashlib
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
SECRETS_TYPED = [
    ACCOUNT,
    "yes",
    "team.cloudflareaccess.com",
    "AUD-1",
    "client-id.access,owner@example.com",
    "sk-ant-SECRET",
    "cf-embed-SECRET",
    OPERATOR_HOOK,
    "cyris@mail.example",
]

# Records its argv and stdin, one JSON line per call, and answers as wrangler would.
BUN = """#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
stdin = "" if sys.stdin.isatty() else sys.stdin.read()
with open(os.environ["CALLS"], "a") as log:
    seen = {k: v for k, v in os.environ.items() if k.startswith("CLOUDFLARE")}
    log.write(json.dumps({"cmd": "bun", "args": args, "stdin": stdin, "env": seen}) + "\\n")
if args[2:4] == ["secret", "list"]:
    # The names the Worker has set, as wrangler lists them; LIST_FAILS is a wrangler that cannot.
    if os.environ.get("LIST_FAILS"):
        sys.stderr.write("not logged in\\n")
        sys.exit(1)
    names = json.loads(os.environ.get("SET_SECRETS", "[]"))
    print(json.dumps([{"name": n, "type": "secret_text"} for n in names]))
elif args[2:4] == ["whoami", "--json"]:
    print(json.dumps({"accounts": [{"id": "ACCOUNT", "name": "Trial account"}]}))
elif args[2:3] == ["deploy"]:
    print("Deployed cyris-hosted triggers\\n  URL")
""".replace("ACCOUNT", ACCOUNT).replace("URL", URL)

# Records where it ran and what it was given, and answers as `cf` would from the JSON the test
# puts in CF_ORG (the Zero Trust organization), CF_APPS (the Access applications) and CF_TOKENS
# (the service tokens); CF_FAILS makes every call fail with that message.
CF = """#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["CALLS"], "a") as log:
    log.write(json.dumps({
        "cmd": "cf", "args": args, "stdin": "", "cwd": os.getcwd(),
        "dotenv_here": os.path.exists(".env"),
        "token_set": "CLOUDFLARE_API_TOKEN" in os.environ,
        "account": os.environ.get("CLOUDFLARE_ACCOUNT_ID"),
    }) + "\\n")
if os.environ.get("CF_FAILS"):
    sys.stderr.write(os.environ["CF_FAILS"] + "\\n")
    sys.exit(1)
answers = {
    "organization": "CF_ORG",
    "applications": "CF_APPS",
    "service-tokens": "CF_TOKENS",
}
for word, var in answers.items():
    if word in args:
        if var not in os.environ:
            sys.stderr.write("no such thing in this account\\n")
            sys.exit(1)
        print(os.environ[var])
        sys.exit(0)
sys.exit(2)
"""

# Records its argv and the config it reads on stdin, and answers each route: a report, the
# tenant's latest page, which the report names by its hash, or the JSON body it was sent back.
# NO_PAGE names a tenant whose page answers 404. FULL_REPORT adds what the combined hosted Worker's
# report also holds: the registry's runs and the tenant's queued newsletters.
CURL = """#!/usr/bin/env python3
import hashlib, json, os, sys
args = sys.argv[1:]
config = sys.stdin.read()
with open(os.environ["CALLS"], "a") as log:
    log.write(json.dumps({"cmd": "curl", "args": args, "stdin": config}) + "\\n")
url = args[-1]
# /hosted/spend names no tenant: it is every tenant's.
tenant = url.split("/hosted/tenants/")[1].split("/")[0] if "/hosted/tenants/" in url else ""
if tenant == os.environ.get("UNREACHABLE"):
    # What curl does when it never gets an answer: no file, 000, and exit 7.
    sys.stderr.write("curl: (7) Failed to connect to cyris-hosted\\n")
    sys.stdout.write("000")
    sys.exit(7)
page = ('<html><head><meta name="cyris-issue" content="2026-11-18-morning"></head>'
        f'<body>{tenant}</body></html>').encode()
out = args[args.index("-o") + 1]
if "/hosted/spend" in url or "/budget?" in url:
    with open(out, "w") as body:
        body.write('{"answered": true}')
    sys.stdout.write("200")
    sys.exit(0)
answered = ("/invite", "/revoke", "/members", "/hosted/backup")
if url.endswith(answered) or "/restore?" in url or "/register" in url:
    with open(out, "w") as body:
        body.write('{"answered": true}')
    sys.stdout.write("200")
    sys.exit(0)
if url.endswith("/page"):
    if tenant == os.environ.get("NO_PAGE"):
        with open(out, "w") as body:
            body.write('{"error": "no issue yet"}')
        sys.stdout.write("404")
        sys.exit(0)
    with open(out, "wb") as body:
        body.write(page)
    sys.stdout.write("200")
    sys.exit(0)
if "/run?" in url:
    # A run the registry queued.
    with open(out, "w") as body:
        json.dump({"queued": {"tenant_id": tenant, "state": "queued"}}, body)
    sys.stdout.write("202")
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
if os.environ.get("FULL_REPORT"):
    report["runs"] = [{"id": 1, "tenant_id": tenant, "period": "morning", "date": "2026-11-18",
                       "origin": "tick", "state": "done", "attempts": 1, "outcome": "exit 0"}]
    report["newsletters"] = [{"id": "n1", "sender": "letter@news.example", "subject": "Issue 1",
                              "date": "2026-11-18", "received_at": "2026-11-18T07:00:00Z",
                              "held_at": None}]
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
    for name, body in (("bun", BUN), ("curl", CURL), ("cf", CF)):
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
            [
                ACCOUNT,
                "yes",
                "team.cloudflareaccess.com",
                "AUD-1",
                "client-id.access",
                "sk-ant-SECRET",
                "cf-embed-SECRET",
                OPERATOR_HOOK,
                "cyris@mail.example",
            ],
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

    done, calls = _run(repo, ["secrets"], SECRETS_TYPED)

    assert done.returncode == 0, done.stderr
    puts = [c for c in calls if c["args"][2:4] == ["secret", "put"]]
    assert [(c["args"][4], c["stdin"]) for c in puts] == [
        ("CYRIS_ACCESS_TEAM_DOMAIN", "team.cloudflareaccess.com"),
        ("CYRIS_ACCESS_AUD", "AUD-1"),
        # Who the operator routes admit of whoever Access signs in (ADR-0037).
        ("CYRIS_HOSTED_OPERATORS", "client-id.access,owner@example.com"),
        ("ANTHROPIC_API_KEY", "sk-ant-SECRET"),
        # The tenants' vote similarity embeds on the operator's Workers AI (ADR-0049), with the
        # account id the operator confirmed.
        ("CLOUDFLARE_EMBEDDING_API_TOKEN", "cf-embed-SECRET"),
        ("CLOUDFLARE_ACCOUNT_ID", ACCOUNT),
        # Where every tenant's failed run is reported (ADR-0035).
        ("CYRIS_HOSTED_OPERATOR_WEBHOOK", OPERATOR_HOOK),
        # The sign-in mail's sender (ADR-0033).
        ("CYRIS_HOSTED_MAIL_FROM", "cyris@mail.example"),
        # The host deploy kept: every tenant's links point at it.
        ("CYRIS_STORE_HOSTED_DOMAIN", "cyris-hosted.trial.workers.dev"),
    ]
    assert all(
        c["args"][5:] == ["--config", "wrangler.hosted.toml", "--env-file", "/dev/null"]
        for c in puts
    )
    for secret in ("sk-ant-SECRET", "cf-embed-SECRET", OPERATOR_HOOK):
        assert not any(secret in " ".join(c["args"]) for c in calls)
        assert secret not in done.stdout + done.stderr


def test_secrets_asks_for_nothing_after_the_mail_sender_and_sets_no_backup_secret(
    repo: Path,
) -> None:
    """The daily backup writes R2 through a binding (ADR-0045): it has no credential to set."""
    (repo / ".env.hosted").write_text(f"HOSTED_URL={URL}\n", encoding="utf-8")

    done, calls = _run(repo, ["secrets"], SECRETS_TYPED)

    assert done.returncode == 0, done.stderr
    names = [c["args"][4] for c in calls if c["args"][2:4] == ["secret", "put"]]
    assert "CYRIS_STORE_HOSTED_DOMAIN" in names
    assert [n for n in names if "B2" in n or "BACKUP" in n] == []


def test_restore_loads_one_export_into_a_new_id_and_register_names_it_paused(
    repo: Path,
) -> None:
    (repo / ".env.hosted").write_text(f"HOSTED_URL={URL}\n", encoding="utf-8")
    token = ["client-id.access", "client-SECRET"]

    restored, restore_calls = _run(repo, ["restore", "2026-11-22", "alpha", "alpha-copy"], token)
    (repo / "calls.jsonl").unlink()
    registered, register_calls = _run(repo, ["register", "alpha-copy", "paused"], token)
    (repo / "calls.jsonl").unlink()
    backed_up, backup_calls = _run(repo, ["backup"], token)

    assert (restored.returncode, registered.returncode, backed_up.returncode) == (0, 0, 0)
    curls = [c for c in restore_calls + register_calls + backup_calls if c["cmd"] == "curl"]
    assert [(c["args"][c["args"].index("-X") + 1], c["args"][-1]) for c in curls] == [
        ("POST", f"{URL}/hosted/tenants/alpha-copy/restore?date=2026-11-22&from=alpha"),
        ("POST", f"{URL}/hosted/tenants/alpha-copy/register?status=paused"),
        ("POST", f"{URL}/hosted/backup"),
    ]
    for c in curls:
        assert "CF-Access-Client-Secret: client-SECRET" in c["stdin"]
        assert "client-SECRET" not in " ".join(c["args"])


@pytest.mark.parametrize(
    "args",
    [
        ["restore"],
        ["restore", "2026-11-22", "alpha"],
        ["restore", "22-11-2026", "alpha", "alpha-copy"],
        ["restore", "2026-11-22", "alpha", "Alpha Copy"],
        ["register", "alpha", "deleted"],
        ["register", "a/b"],
    ],
)
def test_restore_and_register_refuse_bad_arguments_before_asking_for_anything(
    repo: Path, args: list[str]
) -> None:
    (repo / ".env.hosted").write_text(f"HOSTED_URL={URL}\n", encoding="utf-8")

    done, calls = _run(repo, args, [])

    assert done.returncode == 2
    assert "usage: " in done.stderr and "deploy|secrets" not in done.stderr
    assert calls == []


def test_secrets_refuses_an_operator_webhook_that_is_not_https(repo: Path) -> None:
    (repo / ".env.hosted").write_text(f"HOSTED_URL={URL}\n", encoding="utf-8")
    typed = [
        ACCOUNT,
        "yes",
        "team.cloudflareaccess.com",
        "AUD-1",
        "client-id.access",
        "sk-ant-SECRET",
        "cf-embed-SECRET",
        "hook",
    ]

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
        f"{URL}/hosted/tenants/alpha/page",
        f"{URL}/hosted/tenants/bravo/report",
        f"{URL}/hosted/tenants/bravo/page",
    ]
    # /t/ takes a tenant session, which the operator never holds: the page is read under /hosted/.
    assert "GET /hosted/tenants/alpha/page -> HTTP 200" in done.stderr
    # The page is judged, not printed.
    assert "<meta" not in done.stdout
    for c in curls:
        assert "CF-Access-Client-Secret: client-SECRET" in c["stdin"]
        assert "client-SECRET" not in " ".join(c["args"])
    assert "PASS alpha" in done.stdout
    assert "PASS bravo" in done.stdout


@pytest.mark.parametrize("failing", [{}, {"NO_PAGE": "bravo"}, {"UNREACHABLE": "alpha"}])
def test_check_leaves_no_report_or_page_behind_and_prints_each_pages_hash(
    repo: Path, failing: dict[str, str]
) -> None:
    """A tenant's report and page are its content: none outlives the check, pass or fail."""
    (repo / ".env.hosted").write_text(f"HOSTED_URL={URL}\n", encoding="utf-8")

    done, _ = _run(repo, ["check"], ["client-id.access", "client-SECRET"], **failing)

    assert list(repo.glob("cyris-hosted-report.*")) == []
    assert "reports and pages deleted" in done.stderr
    for tenant in ("alpha", "bravo"):
        page = (
            '<html><head><meta name="cyris-issue" content="2026-11-18-morning"></head>'
            f"<body>{tenant}</body></html>"
        ).encode()
        fetched = tenant not in failing.values()
        assert (f"{hashlib.sha256(page).hexdigest()} {tenant}" in done.stdout) is fetched


def test_a_token_in_the_environment_is_used_on_stdin_without_asking(repo: Path) -> None:
    (repo / ".env.hosted").write_text(f"HOSTED_URL={URL}\n", encoding="utf-8")

    done, calls = _run(
        repo,
        ["run"],
        [],
        CYRIS_ACCESS_CLIENT_ID="client-id.access",
        CYRIS_ACCESS_CLIENT_SECRET="client-SECRET",
    )

    assert done.returncode == 0, done.stderr
    assert "CF-Access-Client-Id of a service token" not in done.stdout + done.stderr
    curls = [c for c in calls if c["cmd"] == "curl"]
    assert len(curls) == 2
    for c in curls:
        assert "CF-Access-Client-Secret: client-SECRET" in c["stdin"]
        assert not any("client-SECRET" in a for a in c["args"])


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


def test_a_run_the_registry_queued_is_not_a_failure(repo: Path) -> None:
    """The run route answers 202: the run waits in the registry's queue (ADR-0032)."""
    (repo / ".env.hosted").write_text(f"HOSTED_URL={URL}\n", encoding="utf-8")

    done, _ = _run(repo, ["run", "morning"], ["client-id.access", "client-SECRET"])

    assert done.returncode == 0, done.stderr
    assert "run?period=morning -> HTTP 202" in done.stderr
    assert "FAILED" not in done.stderr


def test_schedule_sets_one_tenants_schedule_encoded_with_the_token_on_stdin(repo: Path) -> None:
    (repo / ".env.hosted").write_text(f"HOSTED_URL={URL}\n", encoding="utf-8")

    refused, calls = _run(repo, ["schedule", "alpha", "07:00"], [])
    done, calls = _run(
        repo,
        ["schedule", "alpha", "07:00", "Etc/GMT+8", "4"],
        ["client-id.access", "client-SECRET"],
    )

    assert refused.returncode == 2
    assert done.returncode == 0, done.stderr
    [curl] = [c for c in calls if c["cmd"] == "curl"]
    assert curl["args"][-1] == (
        f"{URL}/hosted/tenants/alpha/schedule?times=07%3A00&timezone=Etc%2FGMT%2B8&daily_run_cap=4"
    )
    assert curl["args"][curl["args"].index("-X") + 1] == "POST"
    assert "CF-Access-Client-Secret: client-SECRET" in curl["stdin"]
    assert "client-SECRET" not in " ".join(curl["args"])


def test_schedule_names_two_times_only_with_the_maximum_that_allows_them(repo: Path) -> None:
    """One time a day unless the operator allows two (ADR-0042): the maximum rides beside them."""
    (repo / ".env.hosted").write_text(f"HOSTED_URL={URL}\n", encoding="utf-8")

    done, calls = _run(
        repo,
        ["schedule", "alpha", "07:00,19:00", "Asia/Taipei", "6", "2"],
        ["client-id.access", "client-SECRET"],
    )

    assert done.returncode == 0, done.stderr
    [curl] = [c for c in calls if c["cmd"] == "curl"]
    assert curl["args"][-1] == (
        f"{URL}/hosted/tenants/alpha/schedule"
        "?times=07%3A00%2C19%3A00&timezone=Asia%2FTaipei&daily_run_cap=6&max_digest_times=2"
    )


def test_spend_lists_every_tenants_month_and_budget_sets_one_allowance(repo: Path) -> None:
    """The operator's two AI-allowance routes (ADR-0038), signed in as every other call is."""
    (repo / ".env.hosted").write_text(f"HOSTED_URL={URL}\n", encoding="utf-8")
    token = ["client-id.access", "client-SECRET"]
    # Refused first: the call log accumulates across runs in one repo.
    refused = [
        _run(repo, args, [])
        for args in (["spend", "11-2026"], ["budget", "alpha"], ["budget", "alpha", "-1"])
    ]

    listed, now = _run(repo, ["spend"], token)
    named, november = _run(repo, ["spend", "2026-11"], token)
    set_, budget = _run(repo, ["budget", "alpha", "3.5"], token)

    for done in (listed, named, set_):
        assert done.returncode == 0, done.stderr
    # The call log accumulates across runs in one repo: each run's own call is its last.
    curls = [[[c for c in calls if c["cmd"] == "curl"][-1]] for calls in (now, november, budget)]
    assert [c["args"][-1] for [c] in curls] == [
        f"{URL}/hosted/spend",
        f"{URL}/hosted/spend?month=2026-11",
        f"{URL}/hosted/tenants/alpha/budget?usd=3.5",
    ]
    assert [c["args"][c["args"].index("-X") + 1] for [c] in curls] == ["GET", "GET", "POST"]
    for [curl] in curls:
        assert "CF-Access-Client-Secret: client-SECRET" in curl["stdin"]
        assert "client-SECRET" not in " ".join(curl["args"])
    for done, sent in refused:
        assert done.returncode == 2
        assert sent == []


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


@pytest.mark.parametrize(
    ("args", "method", "route"),
    [
        (["address"], "GET", "address"),
        (["address", "rotate"], "POST", "rotate-address"),
        (["address", "revoke"], "POST", "revoke-address"),
    ],
)
def test_address_reads_rotates_or_revokes_each_tenants_newsletter_address(
    repo: Path, args: list[str], method: str, route: str
) -> None:
    (repo / ".env.hosted").write_text(f"HOSTED_URL={URL}\n", encoding="utf-8")

    done, calls = _run(repo, args, ["client-id.access", "client-SECRET"])

    assert done.returncode == 0, done.stderr
    curls = [c for c in calls if c["cmd"] == "curl"]
    assert [c["args"][-1] for c in curls] == [
        f"{URL}/hosted/tenants/alpha/{route}",
        f"{URL}/hosted/tenants/bravo/{route}",
    ]
    for c in curls:
        assert c["args"][c["args"].index("-X") + 1] == method
        assert "CF-Access-Client-Secret: client-SECRET" in c["stdin"]
        assert "client-SECRET" not in " ".join(c["args"])


def test_address_refuses_a_verb_it_does_not_know_before_asking_for_anything(repo: Path) -> None:
    (repo / ".env.hosted").write_text(f"HOSTED_URL={URL}\n", encoding="utf-8")

    done, calls = _run(repo, ["address", "delete"], [])

    assert done.returncode == 2
    assert [c for c in calls if c["cmd"] == "curl"] == []


def test_newsletter_source_adds_one_tenants_source_with_its_query_encoded(repo: Path) -> None:
    (repo / ".env.hosted").write_text(f"HOSTED_URL={URL}\n", encoding="utf-8")

    done, calls = _run(
        repo,
        [
            "newsletter-source",
            "bravo",
            "The Letter & Co",
            "letter@news.example",
            "https://news.example",
        ],
        ["client-id.access", "client-SECRET"],
    )

    assert done.returncode == 0, done.stderr
    [curl] = [c for c in calls if c["cmd"] == "curl"]
    assert curl["args"][-1] == (
        f"{URL}/hosted/tenants/bravo/newsletter-source?name=The+Letter+%26+Co"
        "&from=letter%40news.example&homepage=https%3A%2F%2Fnews.example"
    )
    assert curl["args"][curl["args"].index("-X") + 1] == "POST"
    assert "client-SECRET" not in " ".join(curl["args"])


def test_newsletter_source_needs_a_tenant_a_name_and_a_sender_before_asking(repo: Path) -> None:
    (repo / ".env.hosted").write_text(f"HOSTED_URL={URL}\n", encoding="utf-8")

    done, calls = _run(repo, ["newsletter-source", "alpha", "The Letter"], [])

    assert done.returncode == 2
    assert [c for c in calls if c["cmd"] == "curl"] == []


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
    page = [f"{URL}/hosted/tenants/bravo/page"] if args == ["check"] else []
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


@pytest.mark.parametrize("action", ["invite", "revoke"])
def test_invite_and_revoke_send_the_address_in_a_json_body_on_stdin(
    repo: Path, action: str
) -> None:
    """The address goes in the body, so it is in no URL the Worker's logs keep, and no argv."""
    (repo / ".env.hosted").write_text(f"HOSTED_URL={URL}\n", encoding="utf-8")

    done, calls = _run(
        repo, [action, "alpha", 'ana"q@example.com'], ["client-id.access", "client-SECRET"]
    )

    assert done.returncode == 0, done.stderr
    [curl] = [c for c in calls if c["cmd"] == "curl"]
    assert curl["args"][-1] == f"{URL}/hosted/tenants/alpha/{action}"
    assert curl["args"][curl["args"].index("-X") + 1] == "POST"
    assert 'header = "Content-Type: application/json"' in curl["stdin"]
    # curl's config unescapes this back to {"email": "ana\"q@example.com"}.
    assert 'data = "{\\"email\\": \\"ana\\\\\\"q@example.com\\"}"' in curl["stdin"]
    assert "ana" not in " ".join(curl["args"])
    assert "CF-Access-Client-Secret: client-SECRET" in curl["stdin"]


def test_members_lists_one_tenant_and_every_subcommand_names_its_arguments(repo: Path) -> None:
    (repo / ".env.hosted").write_text(f"HOSTED_URL={URL}\n", encoding="utf-8")

    done, calls = _run(repo, ["members", "bravo"], ["client-id.access", "client-SECRET"])
    missing = [_run(repo, args, [])[0] for args in (["invite", "alpha"], ["revoke"], ["members"])]

    assert done.returncode == 0, done.stderr
    assert [c["args"][-1] for c in calls if c["cmd"] == "curl"] == [
        f"{URL}/hosted/tenants/bravo/members"
    ]
    assert [m.returncode for m in missing] == [2, 2, 2]
    assert "usage: " in missing[0].stderr and "invite TENANT EMAIL" in missing[0].stderr


def test_every_cards_subcommand_is_in_one_script_and_check_judges_the_full_report(
    repo: Path,
) -> None:
    """The schedule, newsletter and sign-in cards' subcommands sit beside the rest, and `check`
    reads each page through the operator's page route and judges the report the combined Worker
    answers, its runs and newsletters included."""
    usage = _run(repo, ["no-such-command"], [])[0]
    # Each subcommand's own refusal of missing arguments, so none of them fell to the usage line.
    refused = {
        tuple(args): (_run(repo, args, []), says)
        for args, says in (
            (["schedule", "alpha"], "schedule TENANT HH:00[,HH:00] TIMEZONE"),
            (["newsletter-source", "alpha", "Letter"], "newsletter-source takes TENANT NAME FROM"),
            (["address", "no-such-verb"], "address takes show, rotate or revoke"),
            (["invite", "alpha"], "invite TENANT EMAIL"),
            (["revoke", "alpha"], "revoke TENANT EMAIL"),
            (["members"], "members TENANT"),
        )
    }
    (repo / ".env.hosted").write_text(f"HOSTED_URL={URL}\n", encoding="utf-8")
    checked, calls = _run(repo, ["check"], ["client-id.access", "client-SECRET"], FULL_REPORT="1")

    assert usage.returncode == 2
    listed = "|" + usage.stderr.split("hosted.sh ", 1)[1].strip() + "|"
    for command in (
        "deploy",
        "secrets",
        "containers",
        "register [TENANT [STATUS]]",
        "schedule TENANT TIMES TIMEZONE [CAP [MOST]]",
        "seed",
        "run [morning|evening]",
        "poll",
        "address [show|rotate|revoke]",
        "newsletter-source TENANT NAME FROM [HOMEPAGE]",
        "check",
        "invite TENANT EMAIL",
        "revoke TENANT EMAIL",
        "members TENANT",
        "spend [YYYY-MM]",
        "budget TENANT USD",
        "backup",
        "restore DATE TENANT NEW_TENANT",
        "tail",
        "delete",
    ):
        assert f"|{command}|" in listed, command
    for args, ((done, sent), says) in refused.items():
        assert done.returncode == 2, args
        assert says in done.stderr and "deploy|secrets" not in done.stderr, args
        assert sent == [], args
    assert checked.returncode == 0, checked.stderr
    assert [c["args"][-1] for c in calls if c["cmd"] == "curl"] == [
        f"{URL}/hosted/tenants/alpha/report",
        f"{URL}/hosted/tenants/alpha/page",
        f"{URL}/hosted/tenants/bravo/report",
        f"{URL}/hosted/tenants/bravo/page",
    ]
    assert "PASS alpha" in checked.stdout
    assert "PASS bravo" in checked.stdout


# What `.env` may say about the account, in the shapes a dotenv file takes.
ACCOUNT_LINES = [
    f"CLOUDFLARE_ACCOUNT_ID={ACCOUNT}",
    f'CLOUDFLARE_ACCOUNT_ID="{ACCOUNT}"',
    f"export CLOUDFLARE_ACCOUNT_ID='{ACCOUNT}'",
    f"CLOUDFLARE_ACCOUNT_ID={ACCOUNT} # the trial account",
]


@pytest.mark.parametrize("line", ACCOUNT_LINES)
@pytest.mark.parametrize("action", ["deploy", "delete"])
def test_the_account_is_the_one_in_dotenv_and_is_still_confirmed_by_name(
    repo: Path, action: str, line: str
) -> None:
    (repo / ".env").write_text(f"CLOUDFLARE_API_TOKEN=production-token\n{line}\n", encoding="utf-8")

    done, calls = _run(repo, [action], ["yes"])

    assert done.returncode == 0, done.stderr
    assert f"account id:   {ACCOUNT}" in done.stdout
    assert "account name: Trial account" in done.stdout
    assert "ACCOUNT_ID of the trial Cloudflare account" not in done.stdout + done.stderr
    assert [c["args"][2] for c in calls] == ["whoami", action]


def test_nothing_is_done_to_the_account_dotenv_names_until_yes(repo: Path) -> None:
    (repo / ".env").write_text(f"CLOUDFLARE_ACCOUNT_ID={ACCOUNT}\n", encoding="utf-8")

    done, calls = _run(repo, ["delete"], ["no"])

    assert done.returncode == 1
    assert "not confirmed; nothing was done" in done.stderr
    assert [c["args"][2] for c in calls] == ["whoami"]


def test_secrets_takes_the_account_from_dotenv_and_still_asks_for_yes(repo: Path) -> None:
    (repo / ".env").write_text(f"CLOUDFLARE_ACCOUNT_ID={ACCOUNT}\n", encoding="utf-8")
    (repo / ".env.hosted").write_text(f"HOSTED_URL={URL}\n", encoding="utf-8")

    done, calls = _run(repo, ["secrets"], SECRETS_TYPED[1:])

    assert done.returncode == 0, done.stderr
    [put] = [c for c in calls if c["args"][4:5] == ["CLOUDFLARE_ACCOUNT_ID"]]
    assert put["stdin"] == ACCOUNT


def test_no_other_key_of_dotenv_reaches_wrangler_and_the_file_is_never_sourced(
    repo: Path,
) -> None:
    """`.env` holds production's CLOUDFLARE_API_TOKEN, and a dotenv file is not always shell."""
    (repo / ".env").write_text(
        "CLOUDFLARE_API_TOKEN=production-token\n"
        "ANTHROPIC_API_KEY=production-anthropic\n"
        f"CLOUDFLARE_ACCOUNT_ID={ACCOUNT}\n"
        "touch SOURCED\n"
        "OTHER=$(touch SOURCED)\n",
        encoding="utf-8",
    )

    done, calls = _run(repo, ["deploy"], ["yes"])

    assert done.returncode == 0, done.stderr
    assert not (repo / "SOURCED").exists()
    assert calls
    for c in calls:
        # Only the account is in wrangler's environment, and it is the one the file names.
        assert c["env"] == {"CLOUDFLARE_ACCOUNT_ID": ACCOUNT}
        assert "production" not in json.dumps(c)


def test_the_account_is_asked_for_when_dotenv_has_none(repo: Path) -> None:
    (repo / ".env").write_text("CLOUDFLARE_API_TOKEN=production-token\n", encoding="utf-8")

    done, calls = _run(repo, ["deploy"], [ACCOUNT, "yes"])

    assert done.returncode == 0, done.stderr
    assert "ACCOUNT_ID of the trial Cloudflare account" not in done.stderr  # a prompt, not an error
    assert [c["args"][2] for c in calls] == ["whoami", "deploy"]
    assert all(c["env"] == {"CLOUDFLARE_ACCOUNT_ID": ACCOUNT} for c in calls)


@pytest.mark.parametrize("bad", ["abc", "G" * 32, ACCOUNT.upper(), ""])
def test_an_account_in_dotenv_that_is_not_32_hex_stops_before_wrangler(
    repo: Path, bad: str
) -> None:
    (repo / ".env").write_text(f"CLOUDFLARE_ACCOUNT_ID={bad}\n", encoding="utf-8")

    # An empty value is no value: the account is asked for, as without the key.
    done, calls = _run(repo, ["deploy"], [ACCOUNT, "yes"] if not bad else [ACCOUNT])

    if bad:
        assert done.returncode == 1
        assert "CLOUDFLARE_ACCOUNT_ID in .env must be 32 hex characters" in done.stderr
        assert calls == []
    else:
        assert done.returncode == 0, done.stderr


def test_the_environments_account_never_picks_the_account(repo: Path) -> None:
    (repo / ".env").write_text(f"CLOUDFLARE_ACCOUNT_ID={ACCOUNT}\n", encoding="utf-8")

    done, calls = _run(repo, ["deploy"], ["yes"], CLOUDFLARE_ACCOUNT_ID="e" * 32)

    assert done.returncode == 0, done.stderr
    assert all(c["env"] == {"CLOUDFLARE_ACCOUNT_ID": ACCOUNT} for c in calls)


STATE_TOKEN = (
    f"HOSTED_URL={URL}\n"
    "CYRIS_ACCESS_CLIENT_ID=state-id.access\n"
    "CYRIS_ACCESS_CLIENT_SECRET=state-SECRET\n"
)


@pytest.mark.parametrize("args", [["check"], ["spend"], ["run", "morning"]])
def test_ask_access_commands_read_the_service_token_from_the_state_file(
    repo: Path, args: list[str]
) -> None:
    (repo / ".env.hosted").write_text(STATE_TOKEN, encoding="utf-8")

    done, calls = _run(repo, args, [])

    assert done.returncode == 0, done.stderr
    assert "CF-Access-Client-Id of a service token" not in done.stdout + done.stderr
    curls = [c for c in calls if c["cmd"] == "curl"]
    assert curls
    for c in curls:
        assert 'header = "CF-Access-Client-Id: state-id.access"' in c["stdin"]
        assert 'header = "CF-Access-Client-Secret: state-SECRET"' in c["stdin"]
        assert "state-SECRET" not in " ".join(c["args"])
    assert "state-SECRET" not in done.stdout + done.stderr


def test_a_token_in_the_environment_wins_over_the_state_files(repo: Path) -> None:
    (repo / ".env.hosted").write_text(STATE_TOKEN, encoding="utf-8")

    done, calls = _run(
        repo,
        ["spend"],
        [],
        CYRIS_ACCESS_CLIENT_ID="env-id.access",
        CYRIS_ACCESS_CLIENT_SECRET="env-SECRET",
    )

    assert done.returncode == 0, done.stderr
    [curl] = [c for c in calls if c["cmd"] == "curl"]
    assert "CF-Access-Client-Id: env-id.access" in curl["stdin"]
    assert "state-" not in curl["stdin"]


@pytest.mark.parametrize(
    "half",
    ["CYRIS_ACCESS_CLIENT_ID=state-id.access\n", "CYRIS_ACCESS_CLIENT_SECRET=state-SECRET\n"],
)
def test_half_a_token_in_the_state_file_is_not_mixed_with_one_typed(repo: Path, half: str) -> None:
    (repo / ".env.hosted").write_text(f"HOSTED_URL={URL}\n{half}", encoding="utf-8")

    done, calls = _run(repo, ["spend"], ["typed-id.access", "typed-SECRET"])

    assert done.returncode == 0, done.stderr
    [curl] = [c for c in calls if c["cmd"] == "curl"]
    assert "CF-Access-Client-Id: typed-id.access" in curl["stdin"]
    assert "CF-Access-Client-Secret: typed-SECRET" in curl["stdin"]


def test_the_state_file_keeps_its_other_keys_when_deploy_rewrites_the_url(repo: Path) -> None:
    state = repo / ".env.hosted"
    state.write_text(
        "HOSTED_URL=https://old.example\nCYRIS_ACCESS_CLIENT_ID=state-id.access\n", encoding="utf-8"
    )
    state.chmod(0o644)

    done, _ = _run(repo, ["deploy"], [ACCOUNT, "yes"])

    assert done.returncode == 0, done.stderr
    assert state.read_text(encoding="utf-8") == (
        f"CYRIS_ACCESS_CLIENT_ID=state-id.access\nHOSTED_URL={URL}\n"
    )
    assert stat.S_IMODE(state.stat().st_mode) == 0o600


# The seven names `secrets` asks for, in its order, and the value the tests type for each.
ASKED = [
    "CYRIS_ACCESS_TEAM_DOMAIN",
    "CYRIS_ACCESS_AUD",
    "CYRIS_HOSTED_OPERATORS",
    "ANTHROPIC_API_KEY",
    "CLOUDFLARE_EMBEDDING_API_TOKEN",
    "CYRIS_HOSTED_OPERATOR_WEBHOOK",
    "CYRIS_HOSTED_MAIL_FROM",
]
VALUES = dict(zip(ASKED, SECRETS_TYPED[2:], strict=True))
# Always put, never asked: the account and the host deploy kept.
NOT_ASKED = ["CLOUDFLARE_ACCOUNT_ID", "CYRIS_STORE_HOSTED_DOMAIN"]


def _secrets(
    repo: Path, typed: list[str], state: str = "", **env: str
) -> tuple[subprocess.CompletedProcess, dict[str, str], list[dict]]:
    """Run `secrets` with the account in `.env`, and what it put by name."""
    (repo / ".env").write_text(f"CLOUDFLARE_ACCOUNT_ID={ACCOUNT}\n", encoding="utf-8")
    (repo / ".env.hosted").write_text(f"HOSTED_URL={URL}\n{state}", encoding="utf-8")
    done, calls = _run(repo, ["secrets"], ["yes", *typed], **env)
    puts = {c["args"][4]: c["stdin"] for c in calls if c["args"][2:4] == ["secret", "put"]}
    return done, puts, calls


def test_secrets_lists_the_workers_secret_names_first_and_says_which_are_set(repo: Path) -> None:
    done, _, calls = _secrets(repo, [""] * 7, SET_SECRETS=json.dumps(ASKED))

    assert done.returncode == 0, done.stderr
    assert [c["args"][2:4] for c in calls if c["cmd"] == "bun"][:2] == [
        ["whoami", "--json"],
        ["secret", "list"],
    ]
    for name in ASKED:
        assert f"{name}: already set on the Worker" in done.stdout


def test_enter_on_every_name_already_set_puts_only_the_two_that_are_never_asked(
    repo: Path,
) -> None:
    done, puts, calls = _secrets(repo, [""] * 7, SET_SECRETS=json.dumps(ASKED))

    assert done.returncode == 0, done.stderr
    assert list(puts) == NOT_ASKED
    assert puts["CLOUDFLARE_ACCOUNT_ID"] == ACCOUNT
    assert puts["CYRIS_STORE_HOSTED_DOMAIN"] == "cyris-hosted.trial.workers.dev"
    # Nothing was missing, so nothing was looked up.
    assert [c for c in calls if c["cmd"] == "cf"] == []
    assert "CYRIS_ACCESS_AUD: kept as set" in done.stdout


def test_a_name_set_on_the_worker_is_replaced_only_by_a_value_typed(repo: Path) -> None:
    typed = ["", "", "", "sk-ant-NEW", "", "", ""]

    done, puts, _ = _secrets(repo, typed, SET_SECRETS=json.dumps(ASKED))

    assert done.returncode == 0, done.stderr
    assert puts == {
        "ANTHROPIC_API_KEY": "sk-ant-NEW",
        "CLOUDFLARE_ACCOUNT_ID": ACCOUNT,
        "CYRIS_STORE_HOSTED_DOMAIN": "cyris-hosted.trial.workers.dev",
    }
    assert "sk-ant-NEW" not in done.stdout + done.stderr


def test_a_name_not_set_and_with_no_value_is_required(repo: Path) -> None:
    done, puts, _ = _secrets(repo, ["team.cloudflareaccess.com", "AUD-1"])

    assert done.returncode == 1
    assert "not set on the Worker yet" in done.stdout
    assert "CYRIS_HOSTED_OPERATORS is required" in done.stderr
    assert puts == {}


def test_the_state_files_values_are_the_defaults_and_enter_accepts_them(repo: Path) -> None:
    state = "".join(f"{k}={v}\n" for k, v in VALUES.items())

    done, puts, calls = _secrets(repo, [""] * 7, state, CLOUDFLARE_API_TOKEN="production-token")

    assert done.returncode == 0, done.stderr
    for secret in ("sk-ant-SECRET", "cf-embed-SECRET", OPERATOR_HOOK):
        assert not any(secret in " ".join(c["args"]) for c in calls)
    assert puts == {
        **VALUES,
        "CLOUDFLARE_ACCOUNT_ID": ACCOUNT,
        "CYRIS_STORE_HOSTED_DOMAIN": "cyris-hosted.trial.workers.dev",
    }
    # Offered from the file, so nothing was typed and nothing asked to be saved again.
    assert "Typed this time" not in done.stdout
    assert "from .env.hosted" in done.stdout


def test_a_default_for_a_secret_is_shown_masked_and_never_whole(repo: Path) -> None:
    state = "".join(f"{k}={v}\n" for k, v in VALUES.items())

    done, _, _ = _secrets(repo, [""] * 7, state)

    out = done.stdout + done.stderr
    assert "Enter uses sk-a*** from .env.hosted" in out
    assert "Enter uses http*** from .env.hosted" in out
    # Not a secret, so shown as it is.
    assert "Enter uses cyris@mail.example from .env.hosted" in out
    for secret in ("sk-ant-SECRET", "cf-embed-SECRET", OPERATOR_HOOK):
        assert secret not in out


def test_a_short_secret_default_is_masked_whole(repo: Path) -> None:
    state = "ANTHROPIC_API_KEY=sk-a\n"

    done, _, _ = _secrets(repo, ["t", "a", "o", "", "e", "https://w", "m"], state)

    assert "Enter uses **** from .env.hosted" in done.stdout


def test_typed_values_are_kept_in_the_state_file_when_the_owner_says_so(repo: Path) -> None:
    typed = [*SECRETS_TYPED[2:], "y"]

    done, puts, _ = _secrets(repo, typed, "CYRIS_ACCESS_CLIENT_ID=state-id.access\n")

    assert done.returncode == 0, done.stderr
    assert puts["ANTHROPIC_API_KEY"] == "sk-ant-SECRET"
    state = repo / ".env.hosted"
    kept = dict(line.split("=", 1) for line in state.read_text(encoding="utf-8").splitlines())
    assert kept == {
        "HOSTED_URL": URL,
        "CYRIS_ACCESS_CLIENT_ID": "state-id.access",
        **VALUES,
    }
    assert stat.S_IMODE(state.stat().st_mode) == 0o600
    # The offer names the keys, never the values.
    assert "Typed this time: CYRIS_ACCESS_TEAM_DOMAIN CYRIS_ACCESS_AUD" in done.stdout
    for secret in ("sk-ant-SECRET", "cf-embed-SECRET", OPERATOR_HOOK):
        assert secret not in done.stdout + done.stderr
    assert not list(repo.glob(".env.hosted.new"))


@pytest.mark.parametrize("answer", ["n", "", "no"])
def test_typed_values_are_not_kept_unless_the_owner_says_so(repo: Path, answer: str) -> None:
    done, _, _ = _secrets(repo, [*SECRETS_TYPED[2:], answer])

    assert done.returncode == 0, done.stderr
    assert (repo / ".env.hosted").read_text(encoding="utf-8") == f"HOSTED_URL={URL}\n"


def test_only_what_was_typed_is_offered_for_saving(repo: Path) -> None:
    state = f"CYRIS_ACCESS_TEAM_DOMAIN={VALUES['CYRIS_ACCESS_TEAM_DOMAIN']}\n"
    typed = ["", *SECRETS_TYPED[3:], "y"]

    done, _, _ = _secrets(repo, typed, state)

    assert "Typed this time: CYRIS_ACCESS_AUD CYRIS_HOSTED_OPERATORS" in done.stdout
    assert "Typed this time: CYRIS_ACCESS_TEAM_DOMAIN" not in done.stdout


def test_a_failed_listing_is_reported_and_every_name_is_asked_as_unset(repo: Path) -> None:
    done, puts, _ = _secrets(repo, SECRETS_TYPED[2:], LIST_FAILS="1")

    assert done.returncode == 0, done.stderr
    assert "could not list the Worker's secrets" in done.stderr
    assert "not set on the Worker yet" in done.stdout
    assert set(puts) == set(ASKED + NOT_ASKED)


def test_the_operator_webhook_from_the_state_file_must_still_be_https(repo: Path) -> None:
    state = "CYRIS_HOSTED_OPERATOR_WEBHOOK=hook\n"

    done, puts, _ = _secrets(repo, [*SECRETS_TYPED[2:7], "", "mail"], state)

    assert done.returncode == 1
    assert "CYRIS_HOSTED_OPERATOR_WEBHOOK must be an https:// URL" in done.stderr
    assert puts == {}


# What cf answers: the organization, the applications of the account and its service tokens.
CF_ORG = json.dumps({"auth_domain": "cf-team.cloudflareaccess.com", "name": "team"})
CF_APP = {
    "name": "cyris-hosted",
    "aud": "AUD-FROM-CF",
    "domain": "cyris-hosted.trial.workers.dev/hosted/*",
    "policies": [
        {"decision": "bypass", "include": [{"service_token": {"token_id": "tok-bypass"}}]},
        {"decision": "non_identity", "include": [{"service_token": {"token_id": "tok-1"}}]},
    ],
}
CF_OTHER_APP = {
    "name": "cyris-other",
    "aud": "AUD-OTHER",
    "domain": "other.example.com/*",
    "policies": [{"decision": "allow", "include": [{"service_token": {"token_id": "tok-2"}}]}],
}
CF_TOKENS = [
    {"id": "tok-1", "name": "operator", "client_id": "cf-id.access"},
    {"id": "tok-2", "name": "other", "client_id": "other-id.access"},
    {"id": "tok-bypass", "name": "bypass", "client_id": "bypass-id.access"},
]


def _cf(*, org: str = CF_ORG, apps: list | None = None, tokens: list | None = None) -> dict:
    return {
        "CF_ORG": org,
        "CF_APPS": json.dumps([CF_OTHER_APP, CF_APP] if apps is None else apps),
        "CF_TOKENS": json.dumps(CF_TOKENS if tokens is None else tokens),
    }


def test_team_domain_aud_and_client_id_are_read_from_cf_and_offered_as_defaults(
    repo: Path,
) -> None:
    typed = ["", "", "", *SECRETS_TYPED[5:]]

    done, puts, calls = _secrets(repo, typed, CLOUDFLARE_API_TOKEN="production-token", **_cf())

    assert done.returncode == 0, done.stderr
    assert puts["CYRIS_ACCESS_TEAM_DOMAIN"] == "cf-team.cloudflareaccess.com"
    assert puts["CYRIS_ACCESS_AUD"] == "AUD-FROM-CF"
    # The service token the application's allowing policy names, not the bypass one.
    assert puts["CYRIS_HOSTED_OPERATORS"] == "cf-id.access"
    assert "Enter uses cf-team.cloudflareaccess.com from cf" in done.stdout
    # cf ran where no .env is, with no token from the shell, on the account from .env.
    cf_calls = [c for c in calls if c["cmd"] == "cf"]
    assert [c["args"] for c in cf_calls] == [
        ["zero-trust", "organization", "get"],
        [
            "zero-trust",
            "access",
            "applications",
            "list",
            "--domain",
            "cyris-hosted.trial.workers.dev",
        ],
        ["zero-trust", "access", "service-tokens", "list"],
    ]
    for c in cf_calls:
        assert Path(c["cwd"]).resolve() == (repo / "workers").resolve()
        assert (c["dotenv_here"], c["token_set"], c["account"]) == (False, False, ACCOUNT)


def test_cf_answers_in_a_result_envelope_are_read_too(repo: Path) -> None:
    env = _cf(org=json.dumps({"result": json.loads(CF_ORG)}))
    env["CF_APPS"] = json.dumps({"result": json.loads(env["CF_APPS"])})
    env["CF_TOKENS"] = json.dumps({"result": CF_TOKENS})

    done, puts, _ = _secrets(repo, ["", "", "", *SECRETS_TYPED[5:]], **env)

    assert done.returncode == 0, done.stderr
    assert puts["CYRIS_ACCESS_AUD"] == "AUD-FROM-CF"
    assert puts["CYRIS_HOSTED_OPERATORS"] == "cf-id.access"


def test_a_value_typed_over_a_cf_default_is_put_and_offered_for_saving(repo: Path) -> None:
    typed = ["", "", "ops-id.access,me@example.com", *SECRETS_TYPED[5:], "y"]

    done, puts, _ = _secrets(repo, typed, **_cf())

    assert done.returncode == 0, done.stderr
    assert puts["CYRIS_HOSTED_OPERATORS"] == "ops-id.access,me@example.com"
    kept = (repo / ".env.hosted").read_text(encoding="utf-8")
    assert "CYRIS_HOSTED_OPERATORS=ops-id.access,me@example.com\n" in kept
    # What cf supplied was not typed, so it is not kept.
    assert "AUD-FROM-CF" not in kept


def test_a_name_the_state_file_holds_is_not_looked_up_in_cf(repo: Path) -> None:
    state = (
        "CYRIS_ACCESS_TEAM_DOMAIN=state-team.cloudflareaccess.com\n"
        "CYRIS_ACCESS_AUD=AUD-STATE\n"
        "CYRIS_HOSTED_OPERATORS=state-id.access\n"
    )

    done, puts, calls = _secrets(repo, ["", "", "", *SECRETS_TYPED[5:]], state, **_cf())

    assert done.returncode == 0, done.stderr
    assert puts["CYRIS_ACCESS_AUD"] == "AUD-STATE"
    assert puts["CYRIS_HOSTED_OPERATORS"] == "state-id.access"
    assert [c for c in calls if c["cmd"] == "cf"] == []


def test_a_name_already_set_is_not_looked_up_either(repo: Path) -> None:
    done, _, calls = _secrets(
        repo,
        ["", "", "", *SECRETS_TYPED[5:]],
        SET_SECRETS=json.dumps(ASKED[:3]),
        **_cf(),
    )

    assert done.returncode == 0, done.stderr
    assert [c for c in calls if c["cmd"] == "cf"] == []


def test_when_cf_fails_the_reason_is_printed_and_the_owner_is_asked(repo: Path) -> None:
    done, puts, _ = _secrets(repo, SECRETS_TYPED[2:], CF_FAILS="Authentication error (10000)")

    assert done.returncode == 0, done.stderr
    assert "cf could not read the team domain: Authentication error (10000)" in done.stderr
    assert "cf could not read the Access applications: Authentication error (10000)" in done.stderr
    assert puts["CYRIS_ACCESS_TEAM_DOMAIN"] == "team.cloudflareaccess.com"
    assert puts["CYRIS_ACCESS_AUD"] == "AUD-1"
    assert puts["CYRIS_HOSTED_OPERATORS"] == "client-id.access,owner@example.com"


def test_an_unreadable_cf_answer_falls_back_to_asking_with_the_reason(repo: Path) -> None:
    done, puts, _ = _secrets(
        repo, SECRETS_TYPED[2:], CF_ORG="not json", CF_APPS="not json", CF_TOKENS="[]"
    )

    assert done.returncode == 0, done.stderr
    assert "cf: could not read the answer" in done.stderr
    assert puts["CYRIS_ACCESS_AUD"] == "AUD-1"


def test_no_application_covering_the_hosted_path_is_reported_and_asked(repo: Path) -> None:
    done, puts, _ = _secrets(
        repo,
        ["", "AUD-1", "client-id.access", *SECRETS_TYPED[5:]],
        **_cf(apps=[CF_OTHER_APP]),
    )

    assert done.returncode == 0, done.stderr
    assert "no Access application covers cyris-hosted.trial.workers.dev/hosted/*" in done.stderr
    assert puts["CYRIS_ACCESS_TEAM_DOMAIN"] == "cf-team.cloudflareaccess.com"
    assert puts["CYRIS_ACCESS_AUD"] == "AUD-1"
    assert puts["CYRIS_HOSTED_OPERATORS"] == "client-id.access"


def test_several_service_tokens_are_listed_and_the_client_id_is_asked(repo: Path) -> None:
    app = {
        **CF_APP,
        "policies": [
            {
                "decision": "non_identity",
                "include": [
                    {"service_token": {"token_id": "tok-1"}},
                    {"service_token": {"token_id": "tok-2"}},
                ],
            }
        ],
    }

    done, puts, _ = _secrets(
        repo, ["", "", "typed-id.access", *SECRETS_TYPED[5:]], **_cf(apps=[app])
    )

    assert done.returncode == 0, done.stderr
    assert (
        "allows several service tokens: operator (cf-id.access), other (other-id.access)"
        in done.stderr
    )
    assert puts["CYRIS_ACCESS_AUD"] == "AUD-FROM-CF"
    assert puts["CYRIS_HOSTED_OPERATORS"] == "typed-id.access"


def test_an_application_that_allows_any_service_token_lists_the_accounts_and_asks(
    repo: Path,
) -> None:
    app = {
        **CF_APP,
        "policies": [{"decision": "non_identity", "include": [{"any_valid_service_token": {}}]}],
    }

    done, puts, _ = _secrets(
        repo, ["", "", "typed-id.access", *SECRETS_TYPED[5:]], **_cf(apps=[app])
    )

    assert done.returncode == 0, done.stderr
    assert "allows any valid service token; the account has: operator (cf-id.access)" in done.stderr
    assert puts["CYRIS_HOSTED_OPERATORS"] == "typed-id.access"


def test_two_applications_covering_the_path_are_listed_and_the_aud_is_asked(repo: Path) -> None:
    twin = {
        **CF_APP,
        "name": "cyris-hosted-2",
        "aud": "AUD-TWIN",
        "domain": "cyris-hosted.trial.workers.dev/*",
    }

    done, puts, _ = _secrets(
        repo, ["", "AUD-TYPED", "id.access", *SECRETS_TYPED[5:]], **_cf(apps=[CF_APP, twin])
    )

    assert done.returncode == 0, done.stderr
    assert (
        "several Access applications cover cyris-hosted.trial.workers.dev/hosted/*" in done.stderr
    )
    assert "cyris-hosted (AUD-FROM-CF), cyris-hosted-2 (AUD-TWIN)" in done.stderr
    assert puts["CYRIS_ACCESS_AUD"] == "AUD-TYPED"


def test_service_tokens_that_cf_cannot_list_cost_only_the_client_id(repo: Path) -> None:
    env = _cf()
    del env["CF_TOKENS"]

    done, puts, _ = _secrets(repo, ["", "", "typed-id.access", *SECRETS_TYPED[5:]], **env)

    assert done.returncode == 0, done.stderr
    assert "cf could not read the service tokens: no such thing in this account" in done.stderr
    assert puts["CYRIS_ACCESS_AUD"] == "AUD-FROM-CF"
    assert puts["CYRIS_HOSTED_OPERATORS"] == "typed-id.access"
