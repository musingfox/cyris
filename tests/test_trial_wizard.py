"""scripts/trial-wizard.sh: resuming, waiting, and what each stage does with its answers.

The wizard runs in a copy of the repo with every external command on PATH replaced by a
fake that records its arguments. Its stdin is closed unless a test types answers, so a
stage that would wait for a key reads end-of-file instead: a "no" at every y/N gate.
"""

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
SLUG = "t1"
ACCOUNT = "a" * 32
D1_ID = "0948e71f-2374-4e4e-8f30-44890139011b"
TOKEN = "cf-token-NOT-FOR-OUTPUT"
# Enter at the banner and at the resume notice, before the first stage reads anything.
START = "\n\n"

FAKE_UV = r"""#!/bin/bash
echo "uv $*" >> "$CALLS"
case "$*" in
  *"provision_trial.py names"*)
    printf '%s\n' TRIAL_APP_WORKER=cyris-app-t1 TRIAL_CONTAINER=cyris-app-cyriscontainer-t1 \
      TRIAL_PROMOTE_WORKER=cyris-promote-t1 TRIAL_KV_TITLE=cyris-promote-t1 \
      TRIAL_D1_NAME=cyris-app-t1 TRIAL_PAGES_PROJECT=cyris-app-t1-0123456789abcdef \
      TRIAL_RSS_WORKER=cyris-rss-t1 ;;
  *"trial_api.py check-token"*)
    cat > "$FAKE_DIR/stdin"
    [[ "$(cat "$FAKE_DIR/stdin")" == "$FAKE_GOOD_TOKEN" ]] && exit 0
    echo "  ✗ the token lacks Cloudflare Pages → Edit"; exit 1 ;;
  *"trial_api.py latest-run"*) cat > /dev/null; echo "${FAKE_LATEST:-4}" ;;
  *"trial_api.py wait-run"*)
    cat > /dev/null; echo "  run 5: status ok"; exit "${FAKE_WAIT_RC:-0}" ;;
  *"python -c"*) echo 1 ;;
esac
"""

FAKE_CURL = r"""#!/bin/bash
echo "curl $*" >> "$CALLS"
case " $* " in
  *" -D - "*)
    n=$(( $(cat "$FAKE_DIR/login" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$FAKE_DIR/login"
    if [[ -z "${FAKE_LOGIN_NEVER:-}" && $n -gt ${FAKE_LOGIN_FAIL_FIRST:-0} ]]; then
      printf 'HTTP/2 302\r\nlocation: /login?next=%%2F\r\n\r\n'
    else
      printf 'HTTP/2 522\r\n\r\n'
    fi ;;
  *"/run?period="*) echo '{"started":"run","at":"2026-10-06T10:50:10Z"}' ;;
esac
"""

FAKE_BUNX = r"""#!/bin/bash
echo "bunx $*" >> "$CALLS"
case " $* " in
  *" --dry-run "*) ;;
  *" deploy "*) echo "https://w-t1.x.workers.dev" ;;
esac
"""

FAKE_QUIET = '#!/bin/bash\necho "$(basename "$0") $*" >> "$CALLS"\n'

EXAMPLE_CONFIG = """[llm_provider]
provider = "anthropic"
model = "claude"
[digest]
max_articles_per_digest = 400
output_language = "zh-Hant"
"""


def _state(done: str, email: str = "") -> str:
    return "\n".join(
        [
            "DOMAIN=example.com",
            f"ACCOUNT_ID={ACCOUNT}",
            f"TESTER_EMAIL={email}",
            "OUTPUT_LANGUAGE=en",
            "MAX_ARTICLES=50",
            "IMAGE_TAG=image/00fa8ab",
            "IMAGE_DIGEST=sha256:" + "b" * 64,
            "TRIAL_SUFFIX=0123456789abcdef",
            f"TRIAL_D1_ID={D1_ID}",
            "TRIAL_KV_ID=" + "c" * 32,
            "PAGES_CREATED=yes",
            "TRIAL_PROMOTE_URL=https://p-t1.x.workers.dev",
            "SOURCE_COUNT=1",
            f"STAGES_DONE={done}",
            "",
        ]
    )


def _stages(last: int) -> str:
    return ",".join(str(n) for n in range(1, last + 1))


@dataclass
class Run:
    code: int
    out: str
    calls: str
    state: str


@dataclass
class Rig:
    repo: Path
    env: dict[str, str]

    def run(self, *args: str, stdin: str = "") -> Run:
        done = subprocess.run(
            ["bash", str(self.repo / "scripts/trial-wizard.sh"), SLUG, *args],
            input=stdin,
            capture_output=True,
            text=True,
            env=self.env,
            cwd=self.repo,
            timeout=60,
        )
        calls = self.repo / "calls.log"
        return Run(
            done.returncode,
            done.stdout + done.stderr,
            calls.read_text() if calls.exists() else "",
            (self.repo / f".env.trial-{SLUG}-wizard").read_text(),
        )

    def done(self) -> str:
        state = (self.repo / f".env.trial-{SLUG}-wizard").read_text()
        return next(ln for ln in state.splitlines() if ln.startswith("STAGES_DONE="))


@pytest.fixture
def rig(tmp_path: Path):
    def make(done: str, email: str = "", **env: str) -> Rig:
        repo = tmp_path / "repo"
        (repo / "scripts").mkdir(parents=True)
        shutil.copy(ROOT / "scripts/trial-wizard.sh", repo / "scripts/trial-wizard.sh")
        (repo / "scripts/provision_trial.py").write_text("")
        (repo / "cyris.toml.example").write_text(EXAMPLE_CONFIG)
        (repo / f".env.trial-{SLUG}-wizard").write_text(_state(done, email))
        (repo / f".env.trial-{SLUG}").write_text(
            f"CLOUDFLARE_API_TOKEN={TOKEN}\nGEMINI_API_KEY=g\nCYRIS_UI_TOKEN=u\n"
            f"CYRIS_STORE_DATABASE_ID={D1_ID}\n"
        )
        (repo / f".env.trial-{SLUG}-promote").write_text("PROMOTE_TOKEN=p\n")
        fakes = tmp_path / "bin"
        fakes.mkdir()
        scripts = {"uv": FAKE_UV, "curl": FAKE_CURL, "bunx": FAKE_BUNX}
        for name in ("git", "gh", "open", "xdg-open", "pbcopy"):
            scripts[name] = FAKE_QUIET
        for name, body in scripts.items():
            (fakes / name).write_text(body)
            (fakes / name).chmod(0o755)
        state_dir = tmp_path / "fake"
        state_dir.mkdir()
        base = {
            "PATH": f"{fakes}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "CALLS": str(repo / "calls.log"),
            "FAKE_DIR": str(state_dir),
            "TRIAL_WIZARD_LOGIN_INTERVAL": "0",
        }
        return Rig(repo, base | env)

    return make


def test_a_resumed_run_starts_at_the_first_stage_not_done(rig) -> None:
    run = rig(_stages(11)).run()
    assert "Stages 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11 were done earlier" in run.out
    assert "▸ Stage 12/18 · Deploy the app" in run.out
    for earlier in range(1, 12):
        assert f"▸ Stage {earlier}/18" not in run.out
    assert "Domain on the trial account" not in run.out
    assert "d1 create" not in run.calls
    assert "provision_trial.py names --slug t1 --pages-suffix 0123456789abcdef" in run.calls


def test_a_completed_stage_is_recorded_and_a_failed_one_is_not(rig) -> None:
    run = rig(_stages(11)).run()
    assert run.code == 1
    assert "Stopped before settings push." in run.out
    assert f"STAGES_DONE={_stages(12)}" in run.state.splitlines()


def test_from_clears_that_stage_and_every_later_one(rig) -> None:
    r = rig(_stages(14))
    run = r.run("--from", "12")
    assert "▸ Stage 12/18 · Deploy the app" in run.out
    assert r.done() == f"STAGES_DONE={_stages(12)}"


def test_from_keeps_the_values_already_saved(rig) -> None:
    run = rig(_stages(14)).run("--from", "12")
    assert "TRIAL_SUFFIX=0123456789abcdef" in run.state.splitlines()
    assert f"TRIAL_D1_ID={D1_ID}" in run.state.splitlines()


@pytest.mark.parametrize("args", [("--from", "18"), ("--from", "0"), ("--from",), ("again",)])
def test_a_bad_mode_prints_the_usage(rig, args: tuple[str, ...]) -> None:
    run = rig(_stages(14)).run(*args)
    assert run.code == 1
    assert "Usage: bash scripts/trial-wizard.sh <slug> [receipts | --from N]" in run.out


def test_the_login_wait_needs_no_keypress_and_shows_the_time(rig) -> None:
    run = rig(_stages(11), FAKE_LOGIN_FAIL_FIRST="2").run()
    waiting = "waiting for https://t1.example.com/ to answer 302 → /login: 0m0"
    assert run.out.count(waiting) == 2
    assert "✓ unauthenticated request → 302 /login?next=%2F" in run.out
    assert "Retry" not in run.out


def test_the_login_wait_stops_at_its_limit(rig) -> None:
    r = rig(_stages(11), FAKE_LOGIN_NEVER="1", TRIAL_WIZARD_LOGIN_WAIT="0")
    run = r.run()
    assert run.code == 1
    assert "No 302 → /login after 0s." in run.out
    assert "docs/install-cloudflare.md step 6" in run.out
    assert r.done() == f"STAGES_DONE={_stages(11)}"


def test_receipts_mode_runs_only_the_receipts(rig) -> None:
    run = rig(_stages(17)).run("receipts")
    assert "▸ Stage 1/1 · Receipts" in run.out
    assert "--config workers/rss/wrangler.trial-t1.toml --env-file /dev/null" in run.calls


def test_a_token_missing_a_permission_stops_stage_7_unsaved(rig) -> None:
    r = rig(_stages(6), FAKE_GOOD_TOKEN="good-token")
    run = r.run(stdin=START + "pasted-token-123\n")
    assert run.code == 1
    assert "✗ the token lacks Cloudflare Pages → Edit" in run.out
    assert "Add the permissions marked ✗" in run.out
    assert f"CLOUDFLARE_API_TOKEN={TOKEN}" in (r.repo / f".env.trial-{SLUG}").read_text()
    assert r.done() == f"STAGES_DONE={_stages(6)}"


def test_a_token_with_every_permission_is_saved(rig) -> None:
    r = rig(_stages(6), FAKE_GOOD_TOKEN="good-token")
    run = r.run(stdin=START + "good-token\ngemini-key\n")
    assert "Add the permissions marked ✗" not in run.out
    assert "CLOUDFLARE_API_TOKEN=good-token" in (r.repo / f".env.trial-{SLUG}").read_text()
    assert "▸ Stage 8/18" in run.out


@pytest.mark.parametrize("pasted", ["good-token", "pasted-token-123"])
def test_the_token_reaches_the_check_on_stdin_and_nowhere_else(rig, pasted: str) -> None:
    r = rig(_stages(6), FAKE_GOOD_TOKEN="good-token")
    run = r.run(stdin=START + f"{pasted}\ngemini-key\n")
    assert (Path(r.env["FAKE_DIR"]) / "stdin").read_text().strip() == pasted
    assert pasted not in run.out
    assert pasted not in run.calls
    assert TOKEN not in run.out


@pytest.mark.parametrize(("email", "flag"), [("", False), ("tester@example.com", True)])
def test_mail_permission_is_checked_only_for_a_tester_with_email(rig, email, flag) -> None:
    run = rig(_stages(6), email=email, FAKE_GOOD_TOKEN="good-token").run(
        stdin=START + "good-token\n"
    )
    check = next(ln for ln in run.calls.splitlines() if "check-token" in ln)
    assert ("--email" in check) is flag
    assert "--pages-project cyris-app-t1-0123456789abcdef --app-worker cyris-app-t1" in check
