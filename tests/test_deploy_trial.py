"""scripts/deploy_trial.py: which image it resolves, what it sends, when it calls a deploy good.

git, bun and wrangler are one fake runner that records each command; the trial's
/api/build answer is a stub. The configs are rendered for real, in a temporary tree.
"""

import os
import subprocess
import tomllib
from pathlib import Path

import httpx
import pytest
from trial_tree import D1, DIGEST, NEWSLETTER_KV, SLUG, load, make_tree

pytestmark = pytest.mark.unit

COMMIT = "2056593b" + "c" * 32
OTHER = "2056593b" + "d" * 32
NEW = "sha256:" + "b" * 64
# The commit the wizard state's IMAGE_TAG (image/1111111) names.
PROVISIONED = "1" * 40
MESSAGE = f"This commit has an image. commit={COMMIT} digest={NEW}"
APP_CONFIG = f"wrangler.trial-{SLUG}.toml"
ACCESS = {"CF-Access-Client-Id": "cid", "CF-Access-Client-Secret": "sek-SECRET"}
RSS_CONFIG = f"workers/rss/wrangler.trial-{SLUG}.toml"
PROMOTE_CONFIG = f"workers/promote/wrangler.trial-{SLUG}.toml"
NEWSLETTER_CONFIG = f"workers/newsletter/wrangler.trial-{SLUG}.toml"


class FakeRunner:
    def __init__(self, *, dirty: str = "", message: str = MESSAGE, changed=()) -> None:
        self.dirty = dirty
        self.message = message
        self.changed = set(changed)
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str]) -> subprocess.CompletedProcess:
        self.calls.append(cmd)
        out, code = "", 0
        if cmd[:2] == ["git", "rev-parse"]:
            out = COMMIT if cmd[-1] in ("HEAD", "image/2056593^{commit}") else PROVISIONED
        elif cmd[:2] == ["git", "status"]:
            out = self.dirty
        elif cmd[:2] == ["git", "tag"]:
            out = self.message
        elif cmd[:2] == ["git", "diff"]:
            # A directory differs from every commit but the one this run deploys.
            code = 1 if cmd[-1] in self.changed and cmd[3] != COMMIT else 0
        elif cmd[:3] == ["bunx", "wrangler", "deploy"] and self.fails(cmd):
            return subprocess.CompletedProcess(cmd, 1, "", "boom")
        return subprocess.CompletedProcess(cmd, code, out + "\n", "")

    def fails(self, cmd: list[str]) -> bool:
        return False

    def deploys(self) -> list[list[str]]:
        return [c for c in self.calls if c[:3] == ["bunx", "wrangler", "deploy"]]


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    t = make_tree(tmp_path, "deploy_trial")
    (t / f".env.trial-{SLUG}.access").write_text(
        "CYRIS_ACCESS_CLIENT_ID=cid\nCYRIS_ACCESS_CLIENT_SECRET=sek-SECRET\n", encoding="utf-8"
    )
    (t / f".env.trial-{SLUG}").write_text(f"CYRIS_STORE_DATABASE_ID={D1}\n", encoding="utf-8")
    return t


@pytest.fixture
def deploy(tree: Path, monkeypatch):
    return load(tree, "deploy_trial", monkeypatch)


def _main(deploy, runner, *args, online=COMMIT):
    said, slept, asked = [], [], []

    def fetch(base, headers):
        asked.append((base, headers))
        return online

    code = deploy.main(
        ["--slug", SLUG, *args], run=runner, say=said.append, fetch=fetch, sleep=slept.append
    )
    return code, said, slept, asked


def _image(tree: Path) -> str:
    return tomllib.loads((tree / APP_CONFIG).read_text())["containers"][0]["image"]


# --- pure logic


def test_the_digest_is_read_only_from_a_message_naming_the_commit(deploy) -> None:
    assert deploy.recorded_digest(MESSAGE, COMMIT) == NEW
    assert deploy.recorded_digest(MESSAGE, OTHER) is None
    assert deploy.recorded_digest(f"commit={COMMIT} digest=sha256:abc", COMMIT) is None
    assert deploy.recorded_digest("", COMMIT) is None
    assert deploy.image_tag(COMMIT) == "image/2056593"


@pytest.mark.parametrize(
    "online,ok",
    [(COMMIT, True), ("", False), (COMMIT[:7], False), (OTHER, False), (COMMIT.upper(), False)],
)
def test_only_the_full_deployed_sha_is_a_match(deploy, online: str, ok: bool) -> None:
    assert deploy.build_matches(online, COMMIT) is ok


def test_an_optional_worker_deploys_when_changed_unknown_or_asked(deploy) -> None:
    pick = deploy.workers_to_deploy

    assert pick({"rss": False, "promote": False}, set(), False) == {}
    assert set(pick({"rss": True, "promote": False}, set(), False)) == {"rss"}
    assert set(pick({"rss": None, "promote": None}, set(), False)) == {"rss", "promote"}
    assert set(pick({"rss": False, "promote": False}, {"promote"}, False)) == {"promote"}
    assert pick({"rss": True, "promote": None}, set(), True) == {}


REPORT = """\
✓ config file — read
✗ settings — missing x
  push x with `cyris settings push`
! llm — slow
  retry later
✗ pages — unreachable
✓ sources — 3

2 problem(s) would break a run."""


def test_doctor_failures_keeps_each_failed_check_with_its_fix_and_nothing_else(deploy) -> None:
    assert deploy.doctor_failures(REPORT) == [
        "✗ settings — missing x",
        "  push x with `cyris settings push`",
        "✗ pages — unreachable",
    ]
    assert deploy.doctor_failures("✓ a — fine\n\nReady to run.") == []


# --- the run


def test_a_deploy_renders_the_new_digest_deploys_and_checks_the_build(deploy, tree: Path) -> None:
    runner = FakeRunner()

    code, said, slept, asked = _main(deploy, runner)

    assert code == 0, said
    assert _image(tree).endswith(f"@{NEW}")
    assert runner.deploys() == [
        ["bunx", "wrangler", "deploy", "--config", APP_CONFIG, "--env-file", "/dev/null",
         "--containers-rollout", "immediate"],
    ]  # fmt: skip
    assert asked == [(f"https://{SLUG}.example.com", ACCESS)]
    assert slept == [deploy.WAIT_SECONDS]
    state = (tree / f".env.trial-{SLUG}-wizard").read_text()
    assert "IMAGE_TAG=image/2056593\n" in state and f"IMAGE_DIGEST={NEW}\n" in state
    assert state.count("IMAGE_DIGEST=") == 1


def test_the_tag_is_fetched_by_name_as_deploy_yml_does(deploy) -> None:
    runner = FakeRunner()

    _main(deploy, runner)

    tag = "image/2056593"
    assert ["git", "fetch", "-q", "--no-tags", "origin", f"+refs/tags/{tag}:refs/tags/{tag}"] in (
        runner.calls
    )


def test_a_build_naming_another_commit_fails_after_two_asks(deploy, capsys) -> None:
    code, said, slept, _ = _main(deploy, FakeRunner(), online=OTHER)

    assert code == 1
    assert slept == [deploy.WAIT_SECONDS] * 2
    assert "does not report the deployed commit" in capsys.readouterr().err


def test_a_build_answer_that_is_not_json_is_a_failed_attempt(deploy) -> None:
    answers = iter([ValueError("Expecting value: line 1 column 1"), COMMIT])
    slept: list[float] = []

    def fetch(base, headers):
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    code = deploy.main(
        ["--slug", SLUG], run=FakeRunner(), say=lambda _: None, fetch=fetch, sleep=slept.append
    )

    assert code == 0
    assert slept == [deploy.WAIT_SECONDS] * 2


def test_changed_workers_deploy_with_their_own_configs(deploy) -> None:
    runner = FakeRunner(changed={"workers/rss"})

    code, *_ = _main(deploy, runner)

    assert code == 0
    configs = [c[c.index("--config") + 1] for c in runner.deploys()]
    assert configs == [APP_CONFIG, RSS_CONFIG]
    assert ["bun", "install", "--frozen-lockfile", "--cwd", "workers/rss"] in runner.calls


def _state(tree: Path) -> dict[str, str]:
    lines = (tree / f".env.trial-{SLUG}-wizard").read_text().splitlines()
    return dict(line.split("=", 1) for line in lines)


def _deployed(runner: FakeRunner) -> list[str]:
    return [c[c.index("--config") + 1] for c in runner.deploys()]


def test_a_failed_rss_deploy_is_offered_again_next_run(deploy, tree: Path) -> None:
    class RssFails(FakeRunner):
        def fails(self, cmd):
            return RSS_CONFIG in cmd

    code, *_ = _main(deploy, RssFails(changed={"workers/rss"}))

    assert code == 1
    assert _state(tree)["IMAGE_TAG"] == "image/2056593"
    assert _state(tree)["RSS_WORKER_COMMIT"] == PROVISIONED

    again = FakeRunner(changed={"workers/rss"})
    assert _main(deploy, again)[0] == 0
    assert _deployed(again) == [APP_CONFIG, RSS_CONFIG]
    assert _state(tree)["RSS_WORKER_COMMIT"] == COMMIT

    third = FakeRunner(changed={"workers/rss"})
    assert _main(deploy, third)[0] == 0
    assert _deployed(third) == [APP_CONFIG]


def test_an_app_only_run_leaves_the_workers_to_the_next_run(deploy, tree: Path) -> None:
    first = FakeRunner(changed={"workers/rss", "workers/promote"})

    assert _main(deploy, first, "--app-only")[0] == 0
    assert _deployed(first) == [APP_CONFIG]
    assert _state(tree)["RSS_WORKER_COMMIT"] == PROVISIONED
    assert _state(tree)["PROMOTE_WORKER_COMMIT"] == PROVISIONED

    second = FakeRunner(changed={"workers/rss", "workers/promote"})
    assert _main(deploy, second)[0] == 0
    assert _deployed(second) == [APP_CONFIG, RSS_CONFIG, PROMOTE_CONFIG]


def test_an_unknown_baseline_is_kept_unknown_after_the_app_moves(deploy, tree: Path) -> None:
    state = tree / f".env.trial-{SLUG}-wizard"
    state.write_text("\n".join(ln for ln in state.read_text().splitlines()
                               if not ln.startswith("IMAGE_TAG=")) + "\n")  # fmt: skip

    assert _main(deploy, FakeRunner(), "--app-only")[0] == 0
    assert _state(tree)["RSS_WORKER_COMMIT"] == "unknown"

    again = FakeRunner()
    assert _main(deploy, again)[0] == 0
    assert _deployed(again) == [APP_CONFIG, RSS_CONFIG, PROMOTE_CONFIG]


def test_every_wrangler_call_names_a_trial_config_and_no_env_file(deploy) -> None:
    runner = FakeRunner()

    _main(deploy, runner, "--rss", "--promote")

    wrangler = [c for c in runner.calls if c[:2] == ["bunx", "wrangler"]]
    assert len(wrangler) == 3
    for cmd in wrangler:
        assert cmd[cmd.index("--config") + 1] in (APP_CONFIG, RSS_CONFIG, PROMOTE_CONFIG)
        assert cmd[cmd.index("--env-file") + 1] == "/dev/null"


def test_a_dry_run_deploys_nothing_and_leaves_configs_and_state(deploy, tree: Path) -> None:
    state = (tree / f".env.trial-{SLUG}-wizard").read_text()
    runner = FakeRunner()

    code, said, slept, asked = _main(deploy, runner, "--dry-run", "--promote")

    assert code == 0
    assert [c[-1] for c in runner.deploys()] == ["--dry-run", "--dry-run"]
    assert _image(tree).endswith(f"@{DIGEST}")
    assert (tree / f".env.trial-{SLUG}-wizard").read_text() == state
    assert slept == [] and asked == []


# --- refusals


def test_a_dirty_checkout_is_refused_before_any_deploy(deploy, capsys) -> None:
    runner = FakeRunner(dirty=" M workers/app/src/index.js")

    code, *_ = _main(deploy, runner)

    assert code == 1
    assert "uncommitted changes" in capsys.readouterr().err
    assert runner.deploys() == []


def test_a_commit_with_no_recorded_image_is_refused(deploy, tree: Path, capsys) -> None:
    runner = FakeRunner(message=f"This commit has an image. commit={OTHER} digest={NEW}")

    code, *_ = _main(deploy, runner)

    assert code == 1
    assert "run the release (Release container image)" in capsys.readouterr().err
    assert runner.deploys() == []
    assert _image(tree).endswith(f"@{DIGEST}")


def test_a_config_naming_production_is_refused_before_any_call(deploy, tree: Path) -> None:
    path = tree / APP_CONFIG
    path.write_text(path.read_text().replace('name = "cyris-app-alice"', 'name = "cyris-app"'))
    runner = FakeRunner()

    code, *_ = _main(deploy, runner)

    assert code == 1
    assert runner.calls == []


def test_a_failed_app_deploy_puts_the_old_config_back(deploy, tree: Path) -> None:
    class Failing(FakeRunner):
        def __call__(self, cmd):
            done = super().__call__(cmd)
            if cmd[:3] == ["bunx", "wrangler", "deploy"]:
                return subprocess.CompletedProcess(cmd, 1, "", "boom")
            return done

    state = (tree / f".env.trial-{SLUG}-wizard").read_text()

    code, *_ = _main(deploy, Failing())

    assert code == 1
    assert _image(tree).endswith(f"@{DIGEST}")
    assert (tree / f".env.trial-{SLUG}-wizard").read_text() == state


def test_no_service_token_stops_before_any_deploy(deploy, tree: Path, capsys) -> None:
    (tree / f".env.trial-{SLUG}.access").write_text("CYRIS_ACCESS_CLIENT_ID=cid\n")
    runner = FakeRunner()

    code, *_ = _main(deploy, runner)

    assert code == 1
    assert "CYRIS_ACCESS_CLIENT_SECRET" in capsys.readouterr().err
    assert runner.deploys() == []


@pytest.mark.parametrize("access", ["", "CYRIS_ACCESS_CLIENT_ID=cid\n"], ids=["empty", "no-secret"])
def test_a_dry_run_refuses_a_service_token_file_the_real_run_refuses(
    deploy, tree: Path, capsys, access: str
) -> None:
    (tree / f".env.trial-{SLUG}.access").write_text(access)
    real, dry = FakeRunner(), FakeRunner()

    assert _main(deploy, real)[0] == 1
    real_err = capsys.readouterr().err
    code, *_ = _main(deploy, dry, "--dry-run")

    assert code == 1
    assert capsys.readouterr().err == real_err
    assert ".access holds no CYRIS_ACCESS_CLIENT_" in real_err
    assert dry.deploys() == []


def test_a_dry_run_with_no_service_token_file_is_refused(deploy, tree: Path, capsys) -> None:
    (tree / f".env.trial-{SLUG}.access").unlink()
    runner = FakeRunner()

    code, *_ = _main(deploy, runner, "--dry-run")

    assert code == 1
    assert f".env.trial-{SLUG}.access holds no" in capsys.readouterr().err
    assert runner.deploys() == []


def test_the_build_is_asked_through_access_with_no_login(deploy) -> None:
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"git_sha": COMMIT})

    client = httpx.Client(transport=httpx.MockTransport(answer))
    sha = deploy.fetch_build_sha("https://t.example.com", ACCESS, client)

    assert sha == COMMIT
    assert [str(r.url) for r in seen] == ["https://t.example.com/api/build"]
    assert seen[0].headers["CF-Access-Client-Id"] == "cid"
    assert seen[0].headers["CF-Access-Client-Secret"] == "sek-SECRET"


def test_an_access_redirect_is_a_failed_ask(deploy) -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(302, headers={"Location": "/x"}))
    )

    with pytest.raises(deploy.DeployError, match="answered 302"):
        deploy.fetch_build_sha("https://t.example.com", ACCESS, client)


# --- the newsletter Worker


def _with_access(tree: Path) -> Path:
    (tree / f".env.trial-{SLUG}.access").write_text(
        "CYRIS_ACCESS_CLIENT_ID=cid\nCYRIS_ACCESS_CLIENT_SECRET=sek-SECRET\n", encoding="utf-8"
    )
    (tree / f".env.trial-{SLUG}").write_text(f"CYRIS_STORE_DATABASE_ID={D1}\n", encoding="utf-8")
    return tree


@pytest.fixture
def newsletter_tree(tmp_path: Path) -> Path:
    return _with_access(make_tree(tmp_path, "deploy_trial", newsletter=True))


@pytest.fixture
def newsletter_deploy(newsletter_tree: Path, monkeypatch):
    return load(newsletter_tree, "deploy_trial", monkeypatch)


def test_a_changed_newsletter_worker_is_installed_and_deployed_with_its_own_config(
    newsletter_deploy, newsletter_tree: Path
) -> None:
    runner = FakeRunner(changed={"workers/newsletter"})

    code, *_ = _main(newsletter_deploy, runner)

    assert code == 0
    assert _deployed(runner) == [APP_CONFIG, NEWSLETTER_CONFIG]
    assert ["bun", "install", "--frozen-lockfile", "--cwd", "workers/newsletter"] in runner.calls
    assert _state(newsletter_tree)["NEWSLETTER_WORKER_COMMIT"] == COMMIT


def test_an_unchanged_newsletter_worker_is_skipped(newsletter_deploy, newsletter_tree) -> None:
    runner = FakeRunner()

    code, said, *_ = _main(newsletter_deploy, runner)

    assert code == 0
    assert _deployed(runner) == [APP_CONFIG]
    assert "newsletter Worker: unchanged, skipped" in said
    assert _state(newsletter_tree)["NEWSLETTER_WORKER_COMMIT"] == PROVISIONED


def test_newsletter_asks_for_the_worker_and_app_only_leaves_it(newsletter_deploy) -> None:
    asked = FakeRunner()
    assert _main(newsletter_deploy, asked, "--newsletter")[0] == 0
    assert _deployed(asked) == [APP_CONFIG, NEWSLETTER_CONFIG]

    app_only = FakeRunner(changed={"workers/newsletter"})
    assert _main(newsletter_deploy, app_only, "--app-only")[0] == 0
    assert _deployed(app_only) == [APP_CONFIG]


def test_the_newsletter_config_is_rendered_again_from_the_saved_kv_id(
    newsletter_deploy, newsletter_tree: Path
) -> None:
    (newsletter_tree / NEWSLETTER_CONFIG).unlink()

    code, *_ = _main(newsletter_deploy, FakeRunner(changed={"workers/newsletter"}))

    assert code == 0
    got = tomllib.loads((newsletter_tree / NEWSLETTER_CONFIG).read_text())
    assert got["kv_namespaces"] == [{"binding": "NEWSLETTERS", "id": NEWSLETTER_KV}]


def test_a_trial_without_a_newsletter_kv_gets_no_newsletter_worker(deploy, tree: Path) -> None:
    runner = FakeRunner(changed={"workers/newsletter"})

    code, said, *_ = _main(deploy, runner)

    assert code == 0
    assert _deployed(runner) == [APP_CONFIG]
    assert not (tree / NEWSLETTER_CONFIG).exists()
    assert not any("newsletter" in line for line in said)
    assert "NEWSLETTER_WORKER_COMMIT" not in _state(tree)


def test_asking_for_a_newsletter_worker_the_trial_lacks_is_refused(deploy, capsys) -> None:
    runner = FakeRunner()

    code, *_ = _main(deploy, runner, "--newsletter")

    assert code == 1
    assert "TRIAL_NEWSLETTER_KV_ID" in capsys.readouterr().err
    assert runner.deploys() == []


def test_a_newsletter_config_naming_production_is_refused_before_any_call(
    newsletter_deploy, newsletter_tree: Path
) -> None:
    path = newsletter_tree / NEWSLETTER_CONFIG
    path.write_text(path.read_text().replace("cyris-newsletter-alice", "cyris-newsletter"))
    runner = FakeRunner()

    code, *_ = _main(newsletter_deploy, runner)

    assert code == 1
    assert runner.calls == []


def test_every_wrangler_call_of_a_newsletter_trial_names_a_trial_config(
    newsletter_deploy,
) -> None:
    runner = FakeRunner()

    _main(newsletter_deploy, runner, "--rss", "--promote", "--newsletter")

    wrangler = [c for c in runner.calls if c[:2] == ["bunx", "wrangler"]]
    assert [c[c.index("--config") + 1] for c in wrangler] == [
        APP_CONFIG, RSS_CONFIG, PROMOTE_CONFIG, NEWSLETTER_CONFIG,
    ]  # fmt: skip
    assert all(c[c.index("--env-file") + 1] == "/dev/null" for c in wrangler)


def test_a_dry_run_dry_runs_the_newsletter_worker_and_saves_nothing(
    newsletter_deploy, newsletter_tree: Path
) -> None:
    state = (newsletter_tree / f".env.trial-{SLUG}-wizard").read_text()
    runner = FakeRunner()

    code, *_ = _main(newsletter_deploy, runner, "--dry-run", "--newsletter")

    assert code == 0
    assert _deployed(runner) == [APP_CONFIG, NEWSLETTER_CONFIG]
    assert [c[-1] for c in runner.deploys()] == ["--dry-run", "--dry-run"]
    assert (newsletter_tree / f".env.trial-{SLUG}-wizard").read_text() == state


def test_the_snapshot_and_restore_cover_the_newsletter_config(
    newsletter_deploy, newsletter_tree: Path
) -> None:
    path = newsletter_tree / NEWSLETTER_CONFIG
    before = path.read_bytes()

    snapshot = newsletter_deploy._snapshot(SLUG)
    path.write_text("name = 'changed'\n")
    newsletter_deploy._restore(snapshot)

    assert NEWSLETTER_CONFIG in snapshot
    assert path.read_bytes() == before


def test_a_failed_deploy_removes_a_newsletter_config_this_run_rendered(
    newsletter_deploy, newsletter_tree: Path
) -> None:
    path = newsletter_tree / NEWSLETTER_CONFIG
    path.unlink()

    class Failing(FakeRunner):
        def fails(self, cmd):
            return True

    code, *_ = _main(newsletter_deploy, Failing())

    assert code == 1
    assert not path.exists()


# --- the state's id alone says whether the trial has a newsletter Worker


class ConfigChecking(FakeRunner):
    """A wrangler deploy of a config that is not on disk fails, as wrangler's does."""

    def __init__(self, tree: Path, **kwargs) -> None:
        super().__init__(**kwargs)
        self.tree = tree

    def fails(self, cmd):
        return not (self.tree / cmd[cmd.index("--config") + 1]).exists()


def test_a_dry_run_of_a_trial_whose_config_is_missing_renders_it_and_leaves_none(
    newsletter_deploy, newsletter_tree: Path
) -> None:
    (newsletter_tree / NEWSLETTER_CONFIG).unlink()
    state = (newsletter_tree / f".env.trial-{SLUG}-wizard").read_text()
    runner = ConfigChecking(newsletter_tree)

    code, said, *_ = _main(newsletter_deploy, runner, "--dry-run", "--newsletter")

    assert code == 0, said
    assert _deployed(runner) == [APP_CONFIG, NEWSLETTER_CONFIG]
    assert not (newsletter_tree / NEWSLETTER_CONFIG).exists()
    assert (newsletter_tree / f".env.trial-{SLUG}-wizard").read_text() == state


def test_a_dry_run_reads_each_worker_config_as_rendered_this_run(
    newsletter_deploy, newsletter_tree: Path
) -> None:
    path = newsletter_tree / NEWSLETTER_CONFIG
    path.write_text(path.read_text() + "# stale\n")
    seen: list[str] = []

    class Reading(FakeRunner):
        def fails(self, cmd):
            if NEWSLETTER_CONFIG in cmd:
                seen.append((newsletter_tree / NEWSLETTER_CONFIG).read_text())
            return False

    _main(newsletter_deploy, Reading(), "--dry-run", "--newsletter")

    assert seen and "# stale" not in seen[0]
    assert "# stale" in path.read_text()


def test_a_config_with_no_saved_id_is_not_a_newsletter_worker(
    newsletter_deploy, newsletter_tree: Path, capsys
) -> None:
    state = newsletter_tree / f".env.trial-{SLUG}-wizard"
    state.write_text(
        "".join(ln + "\n" for ln in state.read_text().splitlines() if "NEWSLETTER_KV" not in ln)
    )
    runner = FakeRunner(changed={"workers/newsletter"})

    code, said, *_ = _main(newsletter_deploy, runner)

    assert code == 0
    assert _deployed(runner) == [APP_CONFIG]
    assert "NEWSLETTER_WORKER_COMMIT" not in _state(newsletter_tree)
    assert _main(newsletter_deploy, FakeRunner(), "--newsletter")[0] == 1
    assert "TRIAL_NEWSLETTER_KV_ID" in capsys.readouterr().err


@pytest.mark.parametrize(
    "stray",
    [
        pytest.param(lambda t: t.replace("cyris-newsletter-alice", "cyris-newsletter"), id="prod"),
        pytest.param(lambda t: "this is [not toml", id="unparseable"),
    ],
)
@pytest.mark.parametrize("flags", [(), ("--dry-run",)], ids=["deploy", "dry-run"])
def test_a_config_with_no_saved_id_is_reported_and_never_read_or_validated(
    newsletter_deploy, newsletter_tree: Path, stray, flags
) -> None:
    state = newsletter_tree / f".env.trial-{SLUG}-wizard"
    state.write_text(
        "".join(ln + "\n" for ln in state.read_text().splitlines() if "NEWSLETTER_KV" not in ln)
    )
    path = newsletter_tree / NEWSLETTER_CONFIG
    path.write_text(stray(path.read_text()))
    before = path.read_bytes()
    runner = FakeRunner(changed={"workers/newsletter"})

    code, said, *_ = _main(newsletter_deploy, runner, *flags)

    assert code == 0, said
    notes = [line for line in said if "TRIAL_NEWSLETTER_KV_ID" in line]
    assert notes == [newsletter_deploy.provision_trial.newsletter_note(SLUG)]
    assert notes[0].count(NEWSLETTER_CONFIG) == 1 and "left alone" in notes[0]
    assert not any(NEWSLETTER_CONFIG in c for c in runner.calls)
    assert path.read_bytes() == before


def _without_newsletter_id(tree: Path) -> Path:
    state = tree / f".env.trial-{SLUG}-wizard"
    state.write_text(
        "".join(ln + "\n" for ln in state.read_text().splitlines() if "NEWSLETTER_KV" not in ln)
    )
    return tree / NEWSLETTER_CONFIG


@pytest.mark.parametrize("flags", [(), ("--dry-run",)], ids=["deploy", "dry-run"])
def test_a_config_with_no_saved_id_is_never_opened_or_written(
    newsletter_deploy, newsletter_tree: Path, monkeypatch, flags
) -> None:
    path = _without_newsletter_id(newsletter_tree)
    os.utime(path, ns=(10**18, 10**18))
    before = path.stat().st_mtime_ns
    target = path.resolve()
    touched: list[str] = []

    def probe(name: str) -> None:
        real = getattr(Path, name)

        def wrapper(self, *args, **kwargs):
            if self.resolve() == target:
                touched.append(name)
            return real(self, *args, **kwargs)

        monkeypatch.setattr(Path, name, wrapper)

    for name in ("open", "read_text", "read_bytes", "write_text", "write_bytes", "unlink", "touch"):
        probe(name)

    code, said, *_ = _main(newsletter_deploy, FakeRunner(changed={"workers/newsletter"}), *flags)

    assert code == 0, said
    assert touched == []
    assert path.stat().st_mtime_ns == before


@pytest.mark.parametrize("flags", [(), ("--dry-run",)], ids=["deploy", "dry-run"])
def test_a_trial_with_a_saved_id_gets_no_stray_config_note(newsletter_deploy, flags) -> None:
    code, said, *_ = _main(newsletter_deploy, FakeRunner(), *flags)

    assert code == 0, said
    assert newsletter_deploy.provision_trial.newsletter_note(SLUG) == ""
    assert not any("TRIAL_NEWSLETTER_KV_ID" in line or "left alone" in line for line in said)
