"""One local store runs one digest at a time, and a tick makes one issue per period.

The rule lives in `cyris run`, not in the server, because under compose the tick and
`POST /run` are started by two containers. Plain `def` tests: the command calls `asyncio.run()`.
"""

import asyncio
import os
import subprocess
import sys
from datetime import datetime
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
from cyris.entrypoints.cli import app

pytestmark = pytest.mark.integration

runner = CliRunner()

TODAY = "2026-09-19"


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
