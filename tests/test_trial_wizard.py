"""scripts/trial-wizard.sh: resuming, waiting, and what each stage does with its answers.

The wizard runs in a copy of the repo with every external command on PATH replaced by a
fake that records its arguments. Its stdin is closed unless a test types answers, so a
stage that would wait for a key reads end-of-file instead: a "no" at every y/N gate.
"""

import re
import shutil
import stat
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
  *"cyris doctor"*) cp .env "${0%/*}/../fake/doctor.env" ;;  # env -i hides FAKE_DIR
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
ids=no; secrets=no
for a in "$@"; do
  if [[ "$a" == @* ]]; then
    grep -q '^CF-Access-Client-Id: ' "${a#@}" && ids=yes
    grep -q '^CF-Access-Client-Secret: ' "${a#@}" && secrets=yes
  fi
done
echo "curl $* access-id=$ids access-secret=$secrets" >> "$CALLS"
case "$*" in
  *"/api/build"*)
    n=$(( $(cat "$FAKE_DIR/build" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$FAKE_DIR/build"
    if [[ -z "${FAKE_BUILD_NEVER:-}" && $n -gt ${FAKE_BUILD_FAIL_FIRST:-0} ]]; then echo 200
    else echo 503; fi ;;
  *"/settings"*) echo "${FAKE_SETTINGS_CODE:-302}" ;;
  *"/run?period="*)
    body='{"started":"run","at":"2026-10-06T10:50:10Z"}'
    [[ -n "${FAKE_RUN_BODY+x}" ]] && body="$FAKE_RUN_BODY"
    printf '%s\n%s\n' "$body" "${FAKE_RUN_CODE:-200}" ;;
  *"pages.dev"*) echo 200 ;;
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
            f"CLOUDFLARE_API_TOKEN={TOKEN}\nGEMINI_API_KEY=g\nCYRIS_STORE_DATABASE_ID={D1_ID}\n"
        )
        (repo / f".env.trial-{SLUG}-promote").write_text("PROMOTE_TOKEN=p\n")
        (repo / f".env.trial-{SLUG}.access").write_text(
            "CYRIS_ACCESS_CLIENT_ID=cid\nCYRIS_ACCESS_CLIENT_SECRET=sek-9f3\n"
        )
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


@pytest.mark.parametrize("args", [("--from", "19"), ("--from", "0"), ("--from",), ("again",)])
def test_a_bad_mode_prints_the_usage(rig, args: tuple[str, ...]) -> None:
    run = rig(_stages(14)).run(*args)
    assert run.code == 1
    assert "Usage: bash scripts/trial-wizard.sh <slug> [receipts | --from N]" in run.out


def test_from_18_runs_the_receipts_stage_last(rig) -> None:
    run = rig(_stages(17)).run("--from", "18")
    assert "▸ Stage 18/18 · Receipts" in run.out
    assert "Usage:" not in run.out


ACCESS_ANSWERS = "https://t.cloudflareaccess.com/\naud1\ncid\nsek-9f3\n"


def test_the_access_answers_land_in_the_app_file_without_a_ui_token(rig) -> None:
    r = rig(_stages(10))
    r.run(stdin=START + "\n" + ACCESS_ANSWERS)
    lines = (r.repo / f".env.trial-{SLUG}").read_text().splitlines()
    assert "CYRIS_ACCESS_TEAM_DOMAIN=t.cloudflareaccess.com" in lines
    assert "CYRIS_ACCESS_AUD=aud1" in lines
    text = "\n".join(lines)
    assert "CYRIS_ACCESS_CLIENT_SECRET" not in text
    assert "UI_TOKEN" not in text


def test_the_service_token_pair_is_kept_in_a_mode_600_ignored_file(rig) -> None:
    r = rig(_stages(10))
    r.run(stdin=START + "\n" + ACCESS_ANSWERS)
    path = r.repo / f".env.trial-{SLUG}.access"
    lines = path.read_text().splitlines()
    assert "CYRIS_ACCESS_CLIENT_ID=cid" in lines
    assert "CYRIS_ACCESS_CLIENT_SECRET=sek-9f3" in lines
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert subprocess.run(["git", "check-ignore", "-q", path.name], cwd=ROOT).returncode == 0


def test_the_access_secret_is_not_printed_nor_passed_on_argv(rig) -> None:
    r = rig(_stages(10))
    run = r.run(stdin=START + "\n" + ACCESS_ANSWERS)
    assert "sek-9f3" not in run.out
    assert "sek-9f3" not in run.calls
    assert "cid" not in (r.repo / f".env.trial-{SLUG}").read_text()


def test_an_empty_access_answer_asks_again_naming_it(rig) -> None:
    r = rig(_stages(10))
    run = r.run(stdin=START + "\n\n" + ACCESS_ANSWERS)
    assert "CYRIS_ACCESS_TEAM_DOMAIN is empty" in run.out
    app = (r.repo / f".env.trial-{SLUG}").read_text()
    assert "CYRIS_ACCESS_TEAM_DOMAIN=t.cloudflareaccess.com" in app


def test_a_team_domain_that_is_not_a_host_asks_again_with_the_example(rig) -> None:
    r = rig(_stages(10))
    run = r.run(stdin=START + "\nno such host\n" + ACCESS_ANSWERS)
    assert "CYRIS_ACCESS_TEAM_DOMAIN does not look right" in run.out
    assert "yourteam.cloudflareaccess.com" in run.out
    app = (r.repo / f".env.trial-{SLUG}").read_text()
    assert "CYRIS_ACCESS_TEAM_DOMAIN=t.cloudflareaccess.com" in app


def test_enter_keeps_the_access_values_already_saved(rig) -> None:
    r = rig(_stages(10))
    (r.repo / f".env.trial-{SLUG}.access").write_text(
        "CYRIS_ACCESS_CLIENT_ID=saved-id\nCYRIS_ACCESS_CLIENT_SECRET=saved-sek\n"
    )
    r.run(stdin=START + "\n" + "https://t.cloudflareaccess.com/\naud1\n\n\n")
    lines = (r.repo / f".env.trial-{SLUG}.access").read_text().splitlines()
    assert "CYRIS_ACCESS_CLIENT_ID=saved-id" in lines
    assert "CYRIS_ACCESS_CLIENT_SECRET=saved-sek" in lines


def test_an_access_answer_that_ends_stops_stage_11_naming_it(rig) -> None:
    run = rig(_stages(10)).run(stdin=START + "\nhttps://t.cloudflareaccess.com/\n")
    assert run.code == 1
    assert "No answer for CYRIS_ACCESS_AUD." in run.out


def test_a_saved_access_answer_that_does_not_match_stops_when_input_ends(rig) -> None:
    r = rig(_stages(10))
    (r.repo / f".env.trial-{SLUG}").write_text(
        f"CLOUDFLARE_API_TOKEN={TOKEN}\nCYRIS_ACCESS_AUD=not a tag\n"
    )
    run = r.run(stdin=START + "\nhttps://t.cloudflareaccess.com/\n")
    assert run.code == 1
    assert "CYRIS_ACCESS_AUD" in run.out


def test_the_service_token_wait_needs_no_keypress_and_shows_the_time(rig) -> None:
    run = rig(_stages(11), FAKE_BUILD_FAIL_FIRST="2").run(stdin=START + "y\n")
    waiting = "waiting for https://t1.example.com/ to answer 200 with the service token: 0m0"
    assert run.out.count(waiting) == 3
    assert "✓ service token → 200 from /api/build" in run.out
    assert "Retry" not in run.out


def test_the_service_token_wait_stops_at_its_limit(rig) -> None:
    r = rig(_stages(11), FAKE_BUILD_NEVER="1", TRIAL_WIZARD_LOGIN_WAIT="0")
    run = r.run(stdin=START + "y\n")
    assert run.code == 1
    assert re.search(r"No 200 from /api/build with the service token after \d+s\.", run.out)
    assert "docs/install-cloudflare.md step 6" in run.out
    assert r.done() == f"STAGES_DONE={_stages(11)}"


def test_the_gate_sends_the_service_token_and_no_cookie(rig) -> None:
    run = rig(_stages(11), FAKE_BUILD_FAIL_FIRST="1").run(stdin=START + "y\n")
    gate = [ln for ln in run.calls.splitlines() if "/api/build" in ln]
    assert len(gate) == 2
    assert all("access-id=yes access-secret=yes" in ln for ln in gate)
    assert all(" -b " not in ln for ln in run.calls.splitlines())
    assert "sek-9f3" not in run.calls
    assert "sek-9f3" not in run.out


@pytest.mark.parametrize("code", ["302", "401", "403"])
def test_an_unauthenticated_settings_answer_passes_receipt_1(rig, code: str) -> None:
    run = rig(_stages(17), FAKE_SETTINGS_CODE=code).run("receipts")
    assert f"✓ unauthenticated https://t1.example.com/settings → {code}" in run.out


def test_an_unauthenticated_200_fails_receipt_1_naming_access(rig) -> None:
    run = rig(_stages(17), FAKE_SETTINGS_CODE="200").run("receipts")
    assert "Access does not cover it" in run.out
    assert "Check the Access application's destinations" in run.out


def test_receipts_send_the_service_token_and_the_doctor_gets_it(rig) -> None:
    r = rig(_stages(17))
    run = r.run("receipts")
    assert "✓ service token → 200 from /api/build" in run.out
    doctor = (Path(r.env["FAKE_DIR"]) / "doctor.env").read_text().splitlines()
    assert "CYRIS_ACCESS_CLIENT_ID=cid" in doctor
    assert "CYRIS_ACCESS_CLIENT_SECRET=sek-9f3" in doctor
    assert not any(ln.startswith("CYRIS_UI_TOKEN") for ln in doctor)


def test_a_declined_deploy_looks_once_instead_of_waiting(rig) -> None:
    run = rig(_stages(11), FAKE_BUILD_NEVER="1").run()
    assert "waiting for" not in run.out
    assert "does not answer 200 with the service token, and nothing was deployed now." in run.out
    assert run.calls.count("/api/build") == 1
    assert "▸ Stage 13/18" in run.out


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


def test_the_first_digest_waits_for_its_digest_runs_row(rig) -> None:
    run = rig(_stages(16)).run(stdin=START + "evening\ny\n")
    calls = run.calls.splitlines()
    order = [
        next(i for i, c in enumerate(calls) if marker in c)
        for marker in ("trial_api.py latest-run", "/run?period=evening", "trial_api.py wait-run")
    ]
    assert order == sorted(order)
    wait = calls[order[2]]
    assert "--after 4" in wait and "--app-worker cyris-app-t1" in wait
    assert "run 5: status ok" in run.out
    assert " tail " not in run.calls


def test_the_first_run_posts_with_the_service_token_and_no_cookie(rig) -> None:
    run = rig(_stages(16)).run(stdin=START + "morning\ny\n")
    post = next(ln for ln in run.calls.splitlines() if "/run?period=morning" in ln)
    assert "access-id=yes access-secret=yes" in post
    assert " -b " not in post
    assert "✓ run started" in run.out


def test_a_first_run_without_a_started_reply_warns_and_waits(rig) -> None:
    run = rig(_stages(16), FAKE_RUN_CODE="503", FAKE_RUN_BODY="busy").run(
        stdin=START + "morning\ny\n"
    )
    assert "No 'started' reply" in run.out
    assert "POST /run answered" not in run.out
    assert "trial_api.py wait-run" in run.calls


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        ("302", "Access redirected"),
        ("401", "Access refused the token"),
        ("403", "Worker refused the JWT"),
    ],
)
def test_a_first_run_refused_at_the_edge_dies_naming_the_status(rig, code, reason) -> None:
    run = rig(_stages(16), FAKE_RUN_CODE=code, FAKE_RUN_BODY="refused").run(
        stdin=START + "morning\ny\n"
    )
    assert run.code == 1
    assert f"POST /run answered {code}: {reason}." in run.out


def test_the_first_run_keeps_the_secret_out_of_the_transcript_and_argv(rig) -> None:
    run = rig(_stages(16), FAKE_RUN_CODE="403", FAKE_RUN_BODY="refused").run(
        stdin=START + "morning\ny\n"
    )
    assert "sek-9f3" not in run.out
    assert "sek-9f3" not in run.calls


def test_the_wizard_no_longer_uses_a_session_cookie() -> None:
    script = (ROOT / "scripts/trial-wizard.sh").read_text()
    assert "cyris_session" not in script
    assert "shasum" not in script


def test_a_wait_that_times_out_warns_and_goes_on(rig) -> None:
    run = rig(_stages(16), FAKE_WAIT_RC="2").run(stdin=START + "evening\ny\n")
    assert "a second post starts a second run" in run.out
    assert "▸ Stage 18/18 · Receipts" in run.out


def test_declining_the_post_starts_nothing(rig) -> None:
    run = rig(_stages(16)).run(stdin=START + "evening\nn\n")
    assert "/run?period=" not in run.calls
    assert "wait-run" not in run.calls
