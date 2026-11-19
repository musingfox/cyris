#!/usr/bin/env python3
"""One run of `e2e_onboarding.py` on this machine: a real server, a temp store, a loopback feed.

`pass` is the walk with `--skip-feed-checker`, since the real check refuses a loopback feed
by design, so this run covers everything but `FeedChecker`. `break` adds `--break feed-check`,
and `non-empty` stores a source first, which the walk must refuse. The server is a real
`cyris triage-ui` on `cyris.toml.example`, copied unchanged into a temp directory, with no
key in its environment, so the Model step opens on None and no model is called. The feed
is written at each request, because a run reads only the articles inside its window.

Everything started here is stopped here, and the exit code is the walk's. Like the
walk, this must never be collected by pytest.
"""

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent.parent
WALK = ROOT / "scripts" / "e2e_onboarding.py"
SQLITE_PATH = Path("state") / "cyris.sqlite3"
HOST = "127.0.0.1"
SERVER_START_S = 30
SERVER_STOP_S = 30
LOG_TAIL_CHARS = 3000
SCENARIO_FLAGS = {
    "pass": ["--skip-feed-checker"],
    "break": ["--break", "feed-check"],
    "non-empty": [],
}
# Hours before now. The newest half is inside the run's 24-hour window, and the older
# half is what the votes step deals cards from, so a vote leaves the issue something to read.
ARTICLE_AGES_H = {"n1": 1, "n2": 2, "n3": 3, "o1": 50, "o2": 60, "o3": 70}
# All a server needs from this environment. Passing the rest could hand it a provider key,
# and the Model step opens on the first provider whose key is present.
SERVER_ENV = ("PATH", "HOME", "TMPDIR", "LANG")


def feed_xml(now: datetime, base: str) -> bytes:
    """An RSS feed whose articles are dated from `now`."""
    items = "".join(
        f"<item><title>Walk article {key}</title>"
        f"<link>{escape(base)}/posts/{key}</link><guid>{escape(base)}/posts/{key}</guid>"
        f"<pubDate>{format_datetime(now - timedelta(hours=hours))}</pubDate>"
        f"<description>The text of article {key}.</description></item>"
        for key, hours in ARTICLE_AGES_H.items()
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
        f"<title>Walk feed</title><link>{escape(base)}/</link>{items}</channel></rss>"
    ).encode()


class _Feed(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = feed_xml(datetime.now(UTC), f"http://{HOST}:{self.server.server_port}")
        self.send_response(200)
        self.send_header("Content-Type", "application/rss+xml")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


@contextmanager
def feed_server() -> Iterator[str]:
    server = ThreadingHTTPServer((HOST, 0), _Feed)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://{HOST}:{server.server_port}/feed.xml"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind((HOST, 0))
        return probe.getsockname()[1]


# The server's own address is never proxied, whatever this environment asks for.
_LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _wait_until_up(url: str, process: subprocess.Popen, log: Path) -> None:
    deadline = time.monotonic() + SERVER_START_S
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SystemExit(f"cyris triage-ui exited early:\n{log.read_text(encoding='utf-8')}")
        try:
            _LOCAL.open(f"{url}/api/build", timeout=5).close()
            return
        except (urllib.error.URLError, OSError):
            time.sleep(0.2)
    raise SystemExit(f"cyris triage-ui did not start in {SERVER_START_S}s")


@contextmanager
def triage_ui(install: Path) -> Iterator[str]:
    port = free_port()
    url = f"http://{HOST}:{port}"
    log_path = install / "triage-ui.log"
    with log_path.open("w") as log:
        process = subprocess.Popen(
            [
                str(Path(sys.executable).parent / "cyris"), "triage-ui",
                "--port", str(port),
                "--config", str(install / "cyris.toml"),
                "--sources", str(install / "sources.yaml"),
            ],
            cwd=install,
            env={name: os.environ[name] for name in SERVER_ENV if name in os.environ},
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )  # fmt: skip
    try:
        _wait_until_up(url, process, log_path)
        yield url
    finally:
        _end(process)


def _end(process: subprocess.Popen) -> None:
    """Stop the server's group, killing it past SERVER_STOP_S.

    A server that already exited may leave no group to signal, and that must not replace
    the failure it exited with.
    """
    _signal_group(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=SERVER_STOP_S)
    except subprocess.TimeoutExpired:
        _signal_group(process.pid, signal.SIGKILL)
        process.wait(timeout=SERVER_STOP_S)


def _signal_group(group: int, sent: signal.Signals) -> None:
    # macOS answers EPERM for a group left with only zombies.
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(group, sent)


def store_a_source(url: str, feed: str) -> None:
    request = urllib.request.Request(
        f"{url}/api/sources",
        data=json.dumps({"name": "Already here", "url": feed, "tags": []}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    _LOCAL.open(request, timeout=30).close()


def walk_command(scenario: str, url: str, sqlite: Path, feed: str) -> list[str]:
    command = [
        sys.executable, str(WALK),
        "--url", url,
        "--sqlite", str(sqlite),
        "--feed", feed,
    ]  # fmt: skip
    return command + SCENARIO_FLAGS[scenario]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("scenario", choices=sorted(SCENARIO_FLAGS))
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory(prefix="cyris-e2e-onboarding-") as temp:
        install = Path(temp)
        shutil.copyfile(ROOT / "cyris.toml.example", install / "cyris.toml")
        with feed_server() as feed, triage_ui(install) as url:
            if args.scenario == "non-empty":
                store_a_source(url, feed)
            walk = walk_command(args.scenario, url, install / SQLITE_PATH, feed)
            print(f"$ {' '.join(walk)}", flush=True)
            code = subprocess.run(walk, check=False).returncode
        if code != 0:
            log = (install / "triage-ui.log").read_text(encoding="utf-8")
            print(f"--- triage-ui.log (tail) ---\n{log[-LOG_TAIL_CHARS:]}")
    return code


if __name__ == "__main__":
    sys.exit(main())
