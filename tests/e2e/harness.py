"""Run `cyris run` against the fakes in `tests/e2e/fakes.py`, and read what they received.

The run is a subprocess whose environment is built from nothing: the venv's
`cyris` on PATH, HOME and TMPDIR inside the run's directory, the proxy, and
`SSL_CERT_FILE` naming the proxy's own CA alone. Its `cyris.toml` and `.env`
sit together in that directory with fake credentials, because cyris reads the
`.env` beside its config (docs/spec/e2e-egress-fails-closed.md).
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from contextlib import closing, contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fakes import TEST_SETTINGS, SqliteD1

from cyris.adapters.store.d1 import SCHEMA_PATH
from cyris.adapters.store.d1_store import D1ArticleStore
from cyris.adapters.store.settings import D1Settings
from cyris.adapters.store.source_store import D1SourceStore
from cyris.domain.models import SourceConfig, StoredArticle

MITMPROXY = "mitmproxy==12.2.3"
# Its dependencies, each pinned: uvx resolves them fresh on a cold cache otherwise, so a
# new release of any one could change the proxy under the suite. Regenerate with
#   echo mitmproxy==12.2.3 | uv pip compile - --universal --python-version 3.12 \
#     --no-header --no-annotate -o tests/e2e/mitmproxy-constraints.txt
MITMPROXY_CONSTRAINTS = Path(__file__).with_name("mitmproxy-constraints.txt")
FAKES = Path(__file__).with_name("fakes.py")
# The first `uvx` run installs mitmproxy; a warm cache starts it in about a second.
PROXY_START_SECONDS = 180
RUN_SECONDS = 180

ACCOUNT_ID = "e2e-account"
DATABASE_ID = "e2e-database"
CF_TOKEN = "e2e-cf-token"
GEMINI_KEY = "e2e-gemini-key"
WORKER_TOKEN = "e2e-worker-token"
PROMOTE_TOKEN = "e2e-promote-token"
PAGES_PROJECT = "cyris-e2e"
PAGES_UPLOAD_JWT = "e2e-upload-jwt"
APP_WORKER = "cyris-e2e-app"
GIT_SHA = "e2e-build-sha"
HOSTS = {
    "pages_host": f"{PAGES_PROJECT}.pages.dev",
    "rss_host": "rss.e2e.test",
    "newsletter_host": "newsletter.e2e.test",
    "promote_host": "promote.e2e.test",
}
WORKER_DOMAINS = ["digest.e2e.test"]
DISCORD_WEBHOOK = "https://discord.com/api/webhooks/e2e-hook/e2e-hook-token"
_CF_AUTH = ("authorization", f"Bearer {CF_TOKEN}")
_JWT_AUTH = ("authorization", f"Bearer {PAGES_UPLOAD_JWT}")
_WORKER_AUTH = ("authorization", f"Bearer {WORKER_TOKEN}")
_PROMOTE_AUTH = ("authorization", f"Bearer {PROMOTE_TOKEN}")
# The one credential header each route's service requires, and its value. Discord's
# credential is the webhook path and the deployed site is public, so neither has one.
CREDENTIALS = {
    "d1_query": _CF_AUTH,
    "pages_deployments_list": _CF_AUTH,
    "pages_upload_token": _CF_AUTH,
    "pages_check_missing": _JWT_AUTH,
    "pages_upload": _JWT_AUTH,
    "pages_upsert_hashes": _JWT_AUTH,
    "pages_deployment_create": _CF_AUTH,
    "pages_deployment_get": _CF_AUTH,
    "pages_project_create": _CF_AUTH,
    "workers_domains": _CF_AUTH,
    "email_send": _CF_AUTH,
    "gemini_generate": ("x-goog-api-key", GEMINI_KEY),
    "rss_articles": _WORKER_AUTH,
    "newsletter_list": _WORKER_AUTH,
    "newsletter_ack": _WORKER_AUTH,
    "promote_list": _PROMOTE_AUTH,
    "promote_ack": _PROMOTE_AUTH,
}


@dataclass
class Scenario:
    settings: dict[str, Any]
    sources: list[SourceConfig]
    rss_rows: list[dict]
    newsletters: list[Path]
    llm: dict[str, Any]
    promotions: list[dict] = field(default_factory=list)
    drop: list[str] = field(default_factory=list)
    # How many reads of a new Pages deployment answer "active" before "success".
    pages_pending_reads: int = 0
    # Rows an earlier run left in the store, written as they are.
    stored: list[StoredArticle] = field(default_factory=list)


@dataclass
class Run:
    dir: Path
    returncode: int
    stdout: str
    stderr: str
    records: list[dict]
    routes: list[str]
    started_at: datetime
    finished_at: datetime
    # The tables cyris only reads, as the seed left them before the run.
    seeded: dict[str, list[dict]]
    scenario: Scenario

    @property
    def d1_path(self) -> Path:
        return self.dir / "d1.sqlite"

    def rows(self, sql: str, params: tuple = ()) -> list[dict]:
        """Read the fake D1 after the run, on a connection of its own."""
        return _rows(self.d1_path, sql, params)

    def diagnostics(self) -> str:
        """What a failed assertion should print: cyris's stderr and the proxy's log."""
        proxy_log = (self.dir / "proxy.log").read_text(encoding="utf-8", errors="replace")
        return (
            f"\n--- cyris exit {self.returncode}, stderr (tail) ---\n{self.stderr[-6000:]}"
            f"\n--- proxy log (tail) ---\n{proxy_log[-3000:]}"
        )


def _rows(path: Path, sql: str, params: tuple = ()) -> list[dict]:
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


READ_ONLY_TABLES = ("settings", "sources")


class _FileD1(SqliteD1):
    """SqliteD1's query semantics over the file the fake D1 serves."""

    def __init__(self, path: Path) -> None:
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row

    def close(self) -> None:
        self._conn.close()


def settings(**overrides: Any) -> dict[str, Any]:
    """Every grade-D setting, from TEST_SETTINGS with `overrides` by `table.field`."""
    unknown = sorted(set(overrides) - set(TEST_SETTINGS))
    assert not unknown, f"not a grade-D key: {unknown}"
    return {**TEST_SETTINGS, **overrides}


def _seed_d1(path: Path, scenario: Scenario) -> None:
    """The schema cyris itself sends on boot, then the settings and sources it will read.

    Closed before the proxy opens the same file, so nothing is left uncommitted.
    """
    d1 = _FileD1(path)
    try:
        d1.query(SCHEMA_PATH.read_text(encoding="utf-8"))
        D1Settings(d1).set(scenario.settings)
        D1SourceStore(d1).replace_all({s.name: s for s in scenario.sources})
        D1ArticleStore(d1).import_articles(scenario.stored)
    finally:
        d1.close()


def _write_deployment(run_dir: Path) -> Path:
    """cyris.toml and its .env, with fake credentials only. Returns the config path."""
    config = run_dir / "cyris.toml"
    config.write_text(
        f"""
[store]
backend = "d1"
database_id = "{DATABASE_ID}"

[html_output]
enabled = true
output_dir = "html"

[promote]
publish_enabled = true
pages_project = "{PAGES_PROJECT}"
worker_url = "https://{HOSTS["promote_host"]}"

[newsletter]
worker_url = "https://{HOSTS["newsletter_host"]}"

[rss]
worker_url = "https://{HOSTS["rss_host"]}"
""",
        encoding="utf-8",
    )
    (run_dir / ".env").write_text(
        f"CLOUDFLARE_ACCOUNT_ID={ACCOUNT_ID}\n"
        f"CLOUDFLARE_API_TOKEN={CF_TOKEN}\n"
        f"GEMINI_API_KEY={GEMINI_KEY}\n"
        f"CYRIS_WORKER_TOKEN={WORKER_TOKEN}\n"
        f"CYRIS_PROMOTE_TOKEN={PROMOTE_TOKEN}\n"
        # From wrangler.toml's [vars] in production; here the .env carries it.
        f"CYRIS_APP_WORKER_NAME={APP_WORKER}\n",
        encoding="utf-8",
    )
    return config


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def _proxy(run_dir: Path, script: Path, *, connection_strategy: str = "lazy"):
    """mitmdump with the fakes loaded, isolated through uvx; yields (proxy URL, CA file)."""
    uvx = shutil.which("uvx")
    assert uvx, "the end-to-end suite runs mitmproxy through uvx, which is not on PATH"
    port = _free_port()
    confdir = run_dir / "mitmproxy"
    ready = Path(json.loads(script.read_text())["ready"])
    with (run_dir / "proxy.log").open("w") as log:
        proc = subprocess.Popen(
            [
                uvx, "--from", MITMPROXY, "--constraints", str(MITMPROXY_CONSTRAINTS), "mitmdump",
                "-s", str(FAKES),
                "--listen-host", "127.0.0.1",
                "--listen-port", str(port),
                "--set", f"confdir={confdir}",
                # Lazy: never open an upstream connection before the addon has answered.
                "--set", f"connection_strategy={connection_strategy}",
                "--set", f"e2e_script={script}",
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
            # Its own group, so stopping it also stops what uvx execs into.
            start_new_session=True,
        )  # fmt: skip
        try:
            deadline = time.monotonic() + PROXY_START_SECONDS
            while not ready.exists():
                assert proc.poll() is None, (run_dir / "proxy.log").read_text()
                assert time.monotonic() < deadline, "mitmdump did not start in time"
                time.sleep(0.1)
            yield f"http://127.0.0.1:{port}", confdir / "mitmproxy-ca-cert.pem"
        finally:
            # A group already gone, as when mitmdump died at startup, raises here and
            # would replace the assertion carrying its log.
            with suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                with suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()


def _write_script(run_dir: Path, scenario: Scenario) -> Path:
    """The file the fakes read: where to record, what to serve, what to drop."""
    script = run_dir / "script.json"
    script.write_text(
        json.dumps(
            {
                "record": str(run_dir / "requests.jsonl"),
                "routes_out": str(run_dir / "routes.json"),
                "ready": str(run_dir / "proxy.ready"),
                "d1_sqlite": str(run_dir / "d1.sqlite"),
                "hosts": HOSTS,
                "pages_project": PAGES_PROJECT,
                "pages_upload_jwt": PAGES_UPLOAD_JWT,
                "worker_domains": WORKER_DOMAINS,
                "rss_rows": scenario.rss_rows,
                "newsletters": [str(p) for p in scenario.newsletters],
                "promotions": scenario.promotions,
                "llm": scenario.llm,
                "drop": scenario.drop,
                "credentials": CREDENTIALS,
                "discord_path": DISCORD_WEBHOOK.removeprefix("https://discord.com"),
                "pages_pending_reads": scenario.pages_pending_reads,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (run_dir / "requests.jsonl").touch()
    return script


@contextmanager
def fakes(run_dir: Path, scenario: Scenario, *, connection_strategy: str = "lazy"):
    """The fakes serving `scenario` behind a running proxy; yields (proxy URL, CA file)."""
    with _proxy(
        run_dir, _write_script(run_dir, scenario), connection_strategy=connection_strategy
    ) as proxy:
        yield proxy


def read_records(run_dir: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in (run_dir / "requests.jsonl").read_text(encoding="utf-8").splitlines()
    ]


def run_deployment(run_dir: Path, scenario: Scenario, *, period: str = "morning") -> Run:
    """Seed the fake D1, start the fakes, run `cyris run` once, and collect the record."""
    _seed_d1(run_dir / "d1.sqlite", scenario)
    seeded = {t: _rows(run_dir / "d1.sqlite", f"SELECT * FROM {t}") for t in READ_ONLY_TABLES}
    config = _write_deployment(run_dir)
    home = run_dir / "home"
    tmp = run_dir / "tmp"
    home.mkdir()
    tmp.mkdir()
    venv_bin = Path(sys.executable).parent

    started_at = datetime.now(UTC)
    with fakes(run_dir, scenario) as (proxy_url, ca_file):
        env = {
            "PATH": os.pathsep.join([str(venv_bin), "/usr/bin", "/bin"]),
            "HOME": str(home),
            "TMPDIR": str(tmp),
            "HTTPS_PROXY": proxy_url,
            "HTTP_PROXY": proxy_url,
            "SSL_CERT_FILE": str(ca_file),
            "CYRIS_GIT_SHA": GIT_SHA,
            # The container runs in UTC; without it the run takes the host's local zone.
            "TZ": "UTC",
        }
        result = subprocess.run(
            [
                str(venv_bin / "cyris"), "run",
                "--period", period,
                "--config", str(config),
                "--sources", str(run_dir / "sources.yaml"),
            ],
            cwd=run_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=RUN_SECONDS,
        )  # fmt: skip

    records = read_records(run_dir)
    return Run(
        dir=run_dir,
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
        records=records,
        routes=json.loads((run_dir / "routes.json").read_text(encoding="utf-8")),
        started_at=started_at,
        seeded=seeded,
        scenario=scenario,
        finished_at=datetime.now(UTC),
    )
