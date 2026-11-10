"""scripts/deploy_trial.py runs `cyris doctor` on the trial's settings once the build matches.

The 2026-10-21 failure: trial1's D1 lacked two required settings keys, every run exited
in `require_complete_settings`, and /api/build still named the deployed commit. Here the
fake runner answers git, bun and wrangler as test_deploy_trial.py does, and answers the
`cyris doctor` command by running the real CLI against a fake D1 behind the one seam,
`build_d1_client`, holding whichever settings the test stored. The Pages probe is the other
fake: doctor would otherwise call Cloudflare.
"""

import os
import stat
from pathlib import Path

import pytest
from fakes import SqliteD1, seed_d1_settings
from test_deploy_trial import COMMIT, OTHER, FakeRunner
from trial_tree import D1, SLUG, load, make_tree
from typer.testing import CliRunner

from cyris.config import REQUIRED_KEYS
from cyris.entrypoints.cli import app

pytestmark = pytest.mark.integration

TOKEN = "tok-trial-SECRET"
ACCOUNT = "0123456789abcdef0123456789abcdef"
DOTENV = (
    f"CLOUDFLARE_ACCOUNT_ID={ACCOUNT}\nCLOUDFLARE_API_TOKEN={TOKEN}\nCYRIS_STORE_DATABASE_ID={D1}\n"
    "CYRIS_PROMOTE_PAGES_PROJECT=cyris-alice-0123456789abcdef\n"
)


class DoctorRunner(FakeRunner):
    """FakeRunner whose `uv run cyris doctor` is the real command over `d1`."""

    def __init__(self, d1: SqliteD1, monkeypatch, **kwargs) -> None:
        super().__init__(**kwargs)
        self.d1 = d1
        self.monkeypatch = monkeypatch
        self.reached: list[tuple[str, str]] = []
        self.dotenv = ""
        self.dotenv_mode = 0
        self.workdir: Path | None = None

    def doctors(self) -> list[list[str]]:
        return [c for c in self.calls if c[-4:-3] == ["doctor"] or "doctor" in c[:12]]

    def __call__(self, cmd: list[str]):
        if "doctor" not in cmd:
            return super().__call__(cmd)
        import subprocess

        self.calls.append(cmd)
        config = Path(cmd[cmd.index("--config") + 1])
        self.dotenv = (config.parent / ".env").read_text(encoding="utf-8")
        self.dotenv_mode = stat.S_IMODE((config.parent / ".env").stat().st_mode)
        self.workdir = config.parent
        scrubbed = dict(arg.split("=", 1) for arg in cmd[2 : cmd.index("uv")] if "=" in arg)

        def client(cfg):
            self.reached.append((cfg.app.store.database_id, cfg.app.store.api_token))
            return self.d1

        with self.monkeypatch.context() as m:
            m.setattr(os, "environ", scrubbed)
            m.setattr("cyris.bootstrap.build_d1_client", client)
            m.setattr(
                "cyris.adapters.cloudflare.check_pages_access",
                lambda account, project, token: (True, f"{project} reachable"),
            )
            result = CliRunner().invoke(
                app, ["doctor", *cmd[cmd.index("--config") :]], catch_exceptions=False
            )
        return subprocess.CompletedProcess(cmd, result.exit_code, result.output, "")


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    t = make_tree(tmp_path, "deploy_trial")
    (t / f".env.trial-{SLUG}.access").write_text(
        "CYRIS_ACCESS_CLIENT_ID=cid\nCYRIS_ACCESS_CLIENT_SECRET=sek-SECRET\n", encoding="utf-8"
    )
    (t / f".env.trial-{SLUG}").write_text(DOTENV, encoding="utf-8")
    return t


@pytest.fixture
def deploy(tree: Path, monkeypatch):
    return load(tree, "deploy_trial", monkeypatch)


def _d1(**seed) -> SqliteD1:
    db = SqliteD1()
    seed_d1_settings(db, **seed)
    return db


def _main(deploy, runner, *args, online=COMMIT):
    said: list[str] = []
    code = deploy.main(
        ["--slug", SLUG, *args],
        run=runner,
        say=said.append,
        fetch=lambda base, headers: online,
        sleep=lambda _: None,
    )
    return code, said


def test_complete_settings_let_the_deploy_succeed(deploy, monkeypatch, capsys) -> None:
    runner = DoctorRunner(_d1(), monkeypatch)

    code, said = _main(deploy, runner)

    assert code == 0, capsys.readouterr().err
    assert len(runner.doctors()) == 1
    assert "settings are complete" in said[-1]


def test_a_d1_missing_a_required_key_fails_the_deploy_and_names_it(
    deploy, monkeypatch, capsys
) -> None:
    lacking = list(REQUIRED_KEYS[:2])
    runner = DoctorRunner(_d1(omit=lacking), monkeypatch)

    code, said = _main(deploy, runner)

    err = capsys.readouterr().err
    assert code == 1
    for key in lacking:
        assert key in err
    assert "cyris doctor reports an error" in err
    assert "settings serves" not in "".join(said)


def test_doctor_reads_the_trials_own_env_beside_its_config_on_d1(deploy, monkeypatch, tree) -> None:
    (tree / f".env.trial-{SLUG}").write_text(DOTENV + "CYRIS_STORE_BACKEND=sqlite\n")
    runner = DoctorRunner(_d1(), monkeypatch)

    code, _ = _main(deploy, runner)

    assert code == 0
    assert set(runner.reached) == {(D1, TOKEN)}
    assert runner.dotenv.splitlines()[-1] == "CYRIS_STORE_BACKEND=d1"
    assert "CYRIS_STORE_BACKEND=sqlite" not in runner.dotenv


def test_doctor_starts_from_an_empty_environment(deploy, monkeypatch) -> None:
    runner = DoctorRunner(_d1(), monkeypatch)
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "from-the-shell")

    _main(deploy, runner)

    cmd = runner.doctors()[0]
    assert cmd[:2] == ["env", "-i"]
    assert not any("from-the-shell" in arg for arg in cmd)
    assert set(runner.reached) == {(D1, TOKEN)}


def test_a_build_that_does_not_match_never_reaches_doctor(deploy, monkeypatch) -> None:
    runner = DoctorRunner(_d1(omit=list(REQUIRED_KEYS[:1])), monkeypatch)

    code, _ = _main(deploy, runner, online=OTHER)

    assert code == 1
    assert runner.doctors() == []


def test_a_dry_run_does_not_run_doctor(deploy, monkeypatch) -> None:
    runner = DoctorRunner(_d1(omit=list(REQUIRED_KEYS[:1])), monkeypatch)

    code, _ = _main(deploy, runner, "--dry-run")

    assert code == 0
    assert runner.doctors() == []


def test_a_missing_env_file_stops_before_any_deploy(deploy, monkeypatch, tree, capsys) -> None:
    (tree / f".env.trial-{SLUG}").unlink()
    runner = DoctorRunner(_d1(), monkeypatch)

    code, _ = _main(deploy, runner)

    assert code == 1
    assert f".env.trial-{SLUG} not found" in capsys.readouterr().err
    assert runner.deploys() == []


OTHER_D1 = "99999999-2222-4333-8444-555555555555"


def test_an_env_naming_another_database_stops_before_doctor_and_any_deploy(
    deploy, monkeypatch, tree, capsys
) -> None:
    (tree / f".env.trial-{SLUG}").write_text(DOTENV.replace(D1, OTHER_D1), encoding="utf-8")
    runner = DoctorRunner(_d1(), monkeypatch)

    code, said = _main(deploy, runner)

    err = capsys.readouterr().err
    assert code == 1
    assert OTHER_D1 in err and D1 in err
    assert runner.doctors() == []
    assert runner.deploys() == []
    assert runner.reached == []


def test_an_env_naming_no_database_stops_before_doctor(deploy, monkeypatch, tree, capsys) -> None:
    dotenv = "".join(
        ln + "\n" for ln in DOTENV.splitlines() if not ln.startswith("CYRIS_STORE_DATABASE_ID")
    )
    (tree / f".env.trial-{SLUG}").write_text(dotenv, encoding="utf-8")
    runner = DoctorRunner(_d1(), monkeypatch)

    code, _ = _main(deploy, runner)

    assert code == 1
    assert "CYRIS_STORE_DATABASE_ID=(none)" in capsys.readouterr().err
    assert runner.doctors() == []


def test_a_value_the_trials_file_sets_beats_the_run_role_default(deploy, monkeypatch, tree) -> None:
    (tree / f".env.trial-{SLUG}").write_text(DOTENV + "CYRIS_PROMOTE_PUBLISH_ENABLED=false\n")
    runner = DoctorRunner(_d1(), monkeypatch)

    code, _ = _main(deploy, runner)

    assert code == 0
    lines = runner.dotenv.splitlines()
    assert [ln for ln in lines if ln.startswith("CYRIS_PROMOTE_PUBLISH_ENABLED=")] == [
        "CYRIS_PROMOTE_PUBLISH_ENABLED=false"
    ]
    assert [ln for ln in lines if ln.startswith("CYRIS_HTML_OUTPUT_ENABLED=")] == [
        "CYRIS_HTML_OUTPUT_ENABLED=true"
    ]


def test_the_temporary_env_holding_the_token_is_private_whatever_the_umask(
    deploy, monkeypatch
) -> None:
    runner = DoctorRunner(_d1(), monkeypatch)
    before = os.umask(0o022)
    try:
        code, _ = _main(deploy, runner)
    finally:
        os.umask(before)

    assert code == 0
    assert runner.dotenv_mode == 0o600


def test_the_temporary_directory_is_gone_after_a_passing_and_a_failing_doctor(
    deploy, monkeypatch
) -> None:
    passing = DoctorRunner(_d1(), monkeypatch)
    failing = DoctorRunner(_d1(omit=list(REQUIRED_KEYS[:1])), monkeypatch)

    assert _main(deploy, passing)[0] == 0
    assert _main(deploy, failing)[0] == 1

    for runner in (passing, failing):
        assert runner.workdir is not None and not runner.workdir.exists()


def test_the_failure_prints_the_failed_checks_and_not_the_passing_ones(
    deploy, monkeypatch, capsys
) -> None:
    lacking = list(REQUIRED_KEYS[:2])
    runner = DoctorRunner(_d1(omit=lacking), monkeypatch)

    _main(deploy, runner)

    err = capsys.readouterr().err
    assert err.count("✗") >= 1
    assert "✓" not in err
    assert "problem(s) would break a run" not in err


def _env_with(tree: Path, text: str) -> None:
    (tree / f".env.trial-{SLUG}").write_text(text, encoding="utf-8")


@pytest.mark.parametrize(
    "ids",
    [(OTHER_D1, D1), (D1, OTHER_D1), (D1, D1)],
    ids=["other-first", "trial-first", "same-twice"],
)
def test_an_env_naming_the_database_twice_is_refused_with_its_lines_and_never_reaches_one(
    deploy, monkeypatch, tree, capsys, ids
) -> None:
    first, last = ids
    _env_with(tree, f"CYRIS_STORE_DATABASE_ID={first}\n" + DOTENV.replace(D1, last))
    runner = DoctorRunner(_d1(), monkeypatch)

    code, _ = _main(deploy, runner)

    err = capsys.readouterr().err
    assert code == 1, runner.reached
    assert "lines 1, 4" in err
    assert runner.reached == []
    assert runner.doctors() == []
    assert runner.deploys() == []


@pytest.mark.parametrize(
    "line",
    [f"CYRIS_STORE_DATABASE_ID={D1} ", f"  CYRIS_STORE_DATABASE_ID = {D1}\t"],
    ids=["trailing-space", "spaced-key-and-value"],
)
def test_the_database_id_is_compared_as_cyris_reads_it_stripped(
    deploy, monkeypatch, tree, line
) -> None:
    _env_with(tree, DOTENV.replace(f"CYRIS_STORE_DATABASE_ID={D1}", line))
    runner = DoctorRunner(_d1(), monkeypatch)

    code, _ = _main(deploy, runner)

    assert code == 0
    assert {db for db, _ in runner.reached} == {D1}


def test_a_commented_out_database_line_is_not_a_second_line(deploy, monkeypatch, tree) -> None:
    _env_with(tree, f"# CYRIS_STORE_DATABASE_ID={OTHER_D1}\n" + DOTENV)
    runner = DoctorRunner(_d1(), monkeypatch)

    code, _ = _main(deploy, runner)

    assert code == 0
    assert {db for db, _ in runner.reached} == {D1}


@pytest.mark.parametrize(
    "dotenv",
    [
        pytest.param(DOTENV.replace(D1, OTHER_D1), id="other-database"),
        pytest.param(f"CYRIS_STORE_DATABASE_ID={OTHER_D1}\n" + DOTENV, id="named-twice"),
        pytest.param(DOTENV.replace(f"CYRIS_STORE_DATABASE_ID={D1}\n", ""), id="none"),
    ],
)
def test_a_dry_run_refuses_what_the_real_run_refuses(
    deploy, monkeypatch, tree, capsys, dotenv
) -> None:
    _env_with(tree, dotenv)
    real = DoctorRunner(_d1(), monkeypatch)
    dry = DoctorRunner(_d1(), monkeypatch)

    assert _main(deploy, real)[0] == 1
    real_err = capsys.readouterr().err
    code, _ = _main(deploy, dry, "--dry-run")

    assert code == 1
    assert capsys.readouterr().err == real_err
    assert dry.deploys() == []


def test_a_dry_run_with_a_missing_env_file_is_refused_as_the_real_run_is(
    deploy, monkeypatch, tree, capsys
) -> None:
    (tree / f".env.trial-{SLUG}").unlink()
    runner = DoctorRunner(_d1(), monkeypatch)

    code, _ = _main(deploy, runner, "--dry-run")

    assert code == 1
    assert f".env.trial-{SLUG} not found" in capsys.readouterr().err
    assert runner.deploys() == []
