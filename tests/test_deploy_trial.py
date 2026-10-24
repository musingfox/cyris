"""scripts/deploy_trial.py: which image it resolves, what it sends, when it calls a deploy good.

git, bun and wrangler are one fake runner that records each command; the trial's
/api/build answer is a stub. The configs are rendered for real, in a temporary tree.
"""

import subprocess
import tomllib
from pathlib import Path

import httpx
import pytest
from trial_tree import DIGEST, SLUG, load, make_tree

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
