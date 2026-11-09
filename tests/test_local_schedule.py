"""`cyris triage-ui` runs a local install's hourly tick, and one store runs one digest at a time.

The tick is `docker/crontab`'s hour, spawned apart from the server. The digest's rule lives in
`cyris run`, not in the server, because under compose the tick and `POST /run` are started by
two containers. Plain `def` tests where the command runs: it calls `asyncio.run()`.
"""

import asyncio
import itertools
import logging
import os
import signal
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fakes import seed_d1_settings
from typer.testing import CliRunner

from cyris.adapters.store.d1 import apply_schema
from cyris.adapters.store.digests import D1DigestStore
from cyris.adapters.store.local_d1 import LocalD1Client, take_digest_run_lock
from cyris.adapters.store.source_store import D1SourceStore
from cyris.domain.models import DigestContent, SourceConfig
from cyris.entrypoints import cli
from cyris.entrypoints.cli import app

pytestmark = pytest.mark.integration

runner = CliRunner()

# 07:59:50 UTC: ten seconds before a tick, whichever hour the test is about.
BEFORE_THE_HOUR = datetime(2026, 11, 9, 7, 59, 50, tzinfo=UTC)
TODAY = "2026-09-19"


class _ClockRanOutError(Exception):
    """Ends the tick loop, which otherwise never returns."""


class _Clock:
    """A wall clock a test moves: each wait advances it, and passing `until` ends the loop."""

    def __init__(self, at: datetime, until: datetime) -> None:
        self.now = at
        self.until = until

    def __call__(self) -> datetime:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)
        if self.now >= self.until:
            raise _ClockRanOutError


async def _ticks(start_tick, clock: _Clock) -> None:
    with pytest.raises(_ClockRanOutError):
        await cli._tick_on_the_hour(start_tick, clock=clock, sleep=clock.sleep)


class _Child:
    """A spawned tick whose `poll` answers from a list, the last answer repeating."""

    def __init__(self, polls: list[int | None]) -> None:
        self._polls = polls

    def poll(self) -> int | None:
        return self._polls.pop(0) if len(self._polls) > 1 else self._polls[0]


# The hour


async def test_a_tick_fires_on_each_hour_and_not_when_the_server_starts() -> None:
    clock = _Clock(BEFORE_THE_HOUR, BEFORE_THE_HOUR + timedelta(hours=2, seconds=20))
    fired: list[datetime] = []

    def start():
        fired.append(clock())
        return _Child([0])

    await _ticks(start, clock)

    assert fired == [
        datetime(2026, 11, 9, 8, tzinfo=UTC),
        datetime(2026, 11, 9, 9, tzinfo=UTC),
        datetime(2026, 11, 9, 10, tzinfo=UTC),
    ]


async def test_a_clock_that_jumps_past_the_hour_fires_at_once_and_only_once() -> None:
    """asyncio's clock stops while a laptop sleeps, so the wall clock is what is read."""
    start_at = BEFORE_THE_HOUR
    clock = _Clock(start_at, start_at + timedelta(hours=3, minutes=30))
    fired: list[datetime] = []

    async def sleep(seconds: float) -> None:
        if clock.now == start_at:
            clock.now = datetime(2026, 11, 9, 10, 40, tzinfo=UTC)
            return
        await clock.sleep(seconds)

    def start():
        fired.append(clock())
        return _Child([0])

    with pytest.raises(_ClockRanOutError):
        await cli._tick_on_the_hour(start, clock=clock, sleep=sleep)

    assert fired == [
        datetime(2026, 11, 9, 10, 40, tzinfo=UTC),
        datetime(2026, 11, 9, 11, tzinfo=UTC),
    ]


async def test_the_loop_never_waits_longer_than_the_wake_cap() -> None:
    """A laptop waking mid-wait fires within the cap, so no single wait may exceed it."""
    clock = _Clock(BEFORE_THE_HOUR, BEFORE_THE_HOUR + timedelta(hours=1, seconds=20))
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)
        await clock.sleep(seconds)

    with pytest.raises(_ClockRanOutError):
        await cli._tick_on_the_hour(lambda: _Child([0]), clock=clock, sleep=sleep)

    assert len(waits) > 100
    assert max(waits) <= 30


async def test_an_hour_whose_last_tick_is_still_running_is_skipped_and_said(caplog) -> None:
    clock = _Clock(BEFORE_THE_HOUR, BEFORE_THE_HOUR + timedelta(hours=2, seconds=20))
    fired: list[datetime] = []
    slow = _Child([None, 0])

    def start():
        fired.append(clock())
        return slow

    with caplog.at_level(logging.WARNING, logger="cyris"):
        await _ticks(start, clock)

    assert fired == [datetime(2026, 11, 9, 8, tzinfo=UTC), datetime(2026, 11, 9, 10, tzinfo=UTC)]
    assert "still running" in caplog.text


async def test_a_tick_that_cannot_start_leaves_the_loop_for_the_next_hour(caplog) -> None:
    clock = _Clock(BEFORE_THE_HOUR, BEFORE_THE_HOUR + timedelta(hours=1, seconds=20))
    fired: list[datetime] = []

    def start():
        fired.append(clock())
        if len(fired) == 1:
            raise OSError("no such file")
        return _Child([0])

    await _ticks(start, clock)

    assert len(fired) == 2
    assert "could not start" in caplog.text


# What a tick runs


def _stub_interpreter(tmp_path: Path, monkeypatch, fails: str = "") -> Path:
    """`sys.executable` as a script recording each step, failing the steps `fails` names."""
    calls = tmp_path / "calls.log"
    stub = tmp_path / "python-stub"
    stub.write_text(
        f"#!/bin/sh\n"
        f"echo \"$*\" >> '{calls}'\n"
        f'case "$3" in {fails or "__none__"}) exit 1 ;; esac\n'
        f"exit 0\n"
    )
    stub.chmod(0o755)
    monkeypatch.setattr(sys, "executable", str(stub))
    return calls


async def _one_real_tick(tmp_path: Path) -> tuple[subprocess.Popen, Path, Path]:
    """The real shell line, started by the loop on the clock's hour; waited for."""
    config, sources = tmp_path / "my conf" / "cyris.toml", tmp_path / "sources.yaml"
    clock = _Clock(BEFORE_THE_HOUR, BEFORE_THE_HOUR + timedelta(seconds=20))
    children: list[subprocess.Popen] = []

    def start():
        children.append(cli._start_detached_tick(config, sources))
        return children[-1]

    await _ticks(start, clock)

    [child] = children
    await asyncio.to_thread(child.wait, 10)
    return child, config, sources


@pytest.mark.parametrize("fails", ["", "ingest", "run", "ingest|run"])
async def test_a_tick_runs_ingest_then_the_digest_then_promote_sync_whatever_fails(
    tmp_path: Path, monkeypatch, fails: str
) -> None:
    """`docker/crontab`'s order, with its `;`: a failed poll must not skip the digest."""
    calls = _stub_interpreter(tmp_path, monkeypatch, fails)

    _, config, sources = await _one_real_tick(tmp_path)

    paths = f"--config {config} --sources {sources}"
    assert calls.read_text().splitlines() == [
        f"-m cyris ingest {paths}",
        f"-m cyris run --if-due {paths}",
        f"-m cyris promote-sync {paths}",
    ]


def test_a_tick_stands_apart_from_the_server_and_logs_where_it_does(monkeypatch) -> None:
    spawned = []

    class Popen:
        pid = 1

        def __init__(self, argv, **kwargs) -> None:
            spawned.append((argv, kwargs))

        def wait(self) -> int:
            return 0

    monkeypatch.setattr(subprocess, "Popen", Popen)

    cli._start_detached_tick(Path("cyris.toml"), Path("sources.yaml"))

    [(argv, kwargs)] = spawned
    assert argv[:2] == ["sh", "-c"]
    # Its own session: stopping the server does not stop a run half way, as for `POST /run`.
    assert kwargs == {"stdin": subprocess.DEVNULL, "start_new_session": True}


# Which servers tick


class _OpenServer:
    """Stays up long enough for the tick loop to go round, then stops the command."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def start(self) -> None:
        asyncio.get_running_loop().call_later(0.1, signal.raise_signal, signal.SIGINT)

    async def stop(self) -> None:
        pass


def _spawned_by_the_server(tmp_path: Path, monkeypatch, *flags: str) -> list[list[str]]:
    """What `cyris triage-ui` spawns while each reading of its clock is a new hour."""
    config = tmp_path / "cyris.toml"
    config.write_text(f'[agent_vault]\npath = "{tmp_path / "vault"}"\n')
    spawned: list[list[str]] = []

    class Popen:
        pid = 1

        def __init__(self, argv, **_kwargs) -> None:
            spawned.append(argv)

        def poll(self) -> int:
            return 0

        def wait(self) -> int:
            return 0

    hours = itertools.count()
    monkeypatch.setattr(cli, "_local_now", lambda: BEFORE_THE_HOUR + timedelta(hours=next(hours)))
    monkeypatch.setattr(cli, "TICK_WAKE_SECONDS", 0.001)
    monkeypatch.setattr(subprocess, "Popen", Popen)
    monkeypatch.setattr("cyris.entrypoints.triage_server.TriageServer", _OpenServer)

    result = runner.invoke(
        app,
        ["triage-ui", *flags, "--config", str(config), "--sources", str(tmp_path / "sources.yaml")],
    )

    assert result.exit_code == 0, result.output
    return spawned


def test_a_local_server_ticks_by_default(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("CYRIS_STORE_BACKEND", raising=False)

    spawned = _spawned_by_the_server(tmp_path, monkeypatch)

    assert spawned
    assert all(argv[:2] == ["sh", "-c"] and " ingest " in argv[2] for argv in spawned)


def test_no_schedule_spawns_nothing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("CYRIS_STORE_BACKEND", raising=False)

    assert _spawned_by_the_server(tmp_path, monkeypatch, "--no-schedule") == []


def test_a_store_on_d1_does_not_tick(tmp_path: Path, monkeypatch, d1) -> None:
    """Its schedule is the Worker's, and the `ui` container has no flag to say so."""
    seed_d1_settings(d1)

    assert _spawned_by_the_server(tmp_path, monkeypatch) == []


# One digest at a time


@pytest.fixture
def install(tmp_path: Path, monkeypatch) -> list[str]:
    """A finished wizard on a real SQLite file; the clock reads a morning digest hour."""
    monkeypatch.delenv("CYRIS_STORE_BACKEND", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setenv("CYRIS_STORE_SQLITE_PATH", str(_store_path(tmp_path)))
    client = LocalD1Client(_store_path(tmp_path))
    apply_schema(client)
    seed_d1_settings(client)
    D1SourceStore(client).upsert(SourceConfig(name="Feed", url="https://a.test/feed"))
    client.close()
    config = tmp_path / "cyris.toml"
    config.write_text(f'[agent_vault]\npath = "{tmp_path / "vault"}"\n')
    monkeypatch.setattr(
        "cyris.utils.timezone.now_in_timezone",
        lambda tz: datetime(2026, 9, 19, 8, 0, 1, tzinfo=ZoneInfo(tz)),
    )
    return ["--config", str(config), "--sources", str(tmp_path / "sources.yaml")]


def _store_path(root: Path) -> Path:
    return root / "state" / "cyris.sqlite3"


def _stored(root: Path, date: str, period: str) -> None:
    client = LocalD1Client(_store_path(root))
    D1DigestStore(client).save(
        DigestContent(
            date=date, period=period, sources_processed=1, articles_received=1, articles_included=1
        ),
        raw_page=False,
    )
    client.close()


@pytest.fixture
def digests(monkeypatch) -> list[str]:
    """One entry for each run that got as far as building its digest."""
    built: list[str] = []

    def build_deps(*_args, **_kwargs):
        built.append("built")
        return SimpleNamespace()

    async def run_digest(_deps, _options):
        return SimpleNamespace(status="ok", rendered=None)

    monkeypatch.setattr("cyris.bootstrap.build_deps", build_deps)
    monkeypatch.setattr("cyris.service_layer.run_digest.run_digest", run_digest)
    return built


TICK = ["run", "--if-due"]
MANUAL = ["run", "--period", "morning"]
OTHER_PERIOD = ["run", "--period", "evening", "--date", "2026-09-18"]


@pytest.mark.parametrize(
    ("first", "second"),
    [(MANUAL, TICK), (OTHER_PERIOD, TICK), (TICK, MANUAL), (MANUAL, MANUAL)],
    ids=["tick-during-post-run", "tick-during-other-period", "post-run-during-tick", "two-manual"],
)
def test_a_second_run_during_a_digest_does_nothing_and_says_so(
    install, digests, monkeypatch, first, second
) -> None:
    """A real lock file, with the second run arriving while the first is in its digest."""
    during: list = []

    async def run_digest(_deps, _options):
        # In a thread: the second run must not borrow this one's running event loop.
        during.append(await asyncio.to_thread(runner.invoke, app, [*second, *install]))
        return SimpleNamespace(status="ok", rendered=None)

    monkeypatch.setattr("cyris.service_layer.run_digest.run_digest", run_digest)

    result = runner.invoke(app, [*first, *install])

    assert result.exit_code == 0, result.output
    [inner] = during
    assert inner.exit_code == 0, inner.output
    assert "already running" in inner.output
    assert digests == ["built"]


def test_a_run_ending_lets_the_next_one_start(install, digests) -> None:
    assert runner.invoke(app, [*MANUAL, *install]).exit_code == 0
    assert runner.invoke(app, [*OTHER_PERIOD, *install]).exit_code == 0

    assert digests == ["built", "built"]


def test_a_run_that_raises_leaves_no_lock_behind(install, digests, monkeypatch) -> None:
    def refuse(*_args, **_kwargs):
        raise RuntimeError("no deps")

    with monkeypatch.context() as patch:
        patch.setattr("cyris.bootstrap.build_deps", refuse)
        assert runner.invoke(app, [*MANUAL, *install]).exit_code == 1

    assert runner.invoke(app, [*MANUAL, *install]).exit_code == 0
    assert digests == ["built"]


def test_a_killed_holder_leaves_no_lock_behind(tmp_path: Path) -> None:
    """A crash or a SIGKILL must not stop every later digest until someone clears a file."""
    store = _store_path(tmp_path)
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys, time; from pathlib import Path\n"
            "from cyris.adapters.store.local_d1 import take_digest_run_lock\n"
            "assert take_digest_run_lock(Path(sys.argv[1])) is not None\n"
            "print('held', flush=True)\n"
            "time.sleep(60)\n",
            str(store),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "held"
        assert take_digest_run_lock(store) is None
    finally:
        holder.kill()
        holder.wait()
        holder.stdout.close()

    held = take_digest_run_lock(store)
    assert held is not None
    os.close(held)


def test_a_preview_does_not_wait_on_a_digest(install, digests, tmp_path: Path) -> None:
    held = take_digest_run_lock(_store_path(tmp_path))
    try:
        result = runner.invoke(app, ["run", "--dry-run", "--period", "morning", *install])
    finally:
        os.close(held)

    assert result.exit_code == 0, result.output
    assert digests == ["built"]


# One issue per period


def test_a_tick_whose_issue_exists_does_nothing_and_says_so(
    install, digests, tmp_path: Path
) -> None:
    _stored(tmp_path, TODAY, "morning")

    result = runner.invoke(app, [*TICK, *install])

    assert result.exit_code == 0, result.output
    assert f"{TODAY} morning issue already exists" in result.output
    assert digests == []


@pytest.mark.parametrize(
    ("date", "period"),
    [(TODAY, "evening"), ("2026-09-18", "morning")],
    ids=["other-period", "other-date"],
)
def test_another_issue_does_not_stop_a_tick(
    install, digests, tmp_path: Path, date: str, period: str
) -> None:
    _stored(tmp_path, date, period)

    result = runner.invoke(app, [*TICK, *install])

    assert result.exit_code == 0, result.output
    assert digests == ["built"]


def test_a_run_naming_its_period_still_replaces_the_issue(install, digests, tmp_path: Path) -> None:
    """Naming the period is the reader asking for that issue again, as the guide says to."""
    _stored(tmp_path, TODAY, "morning")

    result = runner.invoke(app, [*MANUAL, *install])

    assert result.exit_code == 0, result.output
    assert digests == ["built"]


def test_the_cloud_paths_tick_keeps_its_meaning(tmp_path: Path, d1, digests, monkeypatch) -> None:
    """On d1 `--if-due` asks only the schedule: no lock file, no read of the stored issues."""
    apply_schema(d1)
    seed_d1_settings(d1)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    D1SourceStore(d1).upsert(SourceConfig(name="Feed", url="https://a.test/feed"))
    D1DigestStore(d1).save(
        DigestContent(
            date=TODAY,
            period="morning",
            sources_processed=1,
            articles_received=1,
            articles_included=1,
        ),  # fmt: skip
        raw_page=False,
    )
    monkeypatch.setattr(
        "cyris.utils.timezone.now_in_timezone",
        lambda tz: datetime(2026, 9, 19, 8, 0, 1, tzinfo=ZoneInfo(tz)),
    )
    config = tmp_path / "cyris.toml"
    config.write_text(f'[agent_vault]\npath = "{tmp_path / "vault"}"\n')
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        app, [*TICK, "--config", str(config), "--sources", str(tmp_path / "sources.yaml")]
    )

    assert result.exit_code == 0, result.output
    assert digests == ["built"]
    assert list(tmp_path.rglob("*.run-lock")) == []
