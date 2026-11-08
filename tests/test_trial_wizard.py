"""scripts/trial-wizard.sh: resuming, waiting, and what each stage does with its answers.

The wizard runs in a copy of the repo with every external command on PATH replaced by a
fake that records its arguments. Its stdin is closed unless a test types answers, so a
stage that would wait for a key reads end-of-file instead: a "no" at every y/N gate.
"""

import json
import re
import shutil
import stat
import subprocess
import sys
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
      TRIAL_RSS_WORKER=cyris-rss-t1 TRIAL_NEWSLETTER_WORKER=cyris-newsletter-t1 \
      TRIAL_NEWSLETTER_KV_TITLE=cyris-newsletter-t1 ;;
  *"trial_api.py check-token"*)
    cat > "$FAKE_DIR/stdin"
    [[ "$(cat "$FAKE_DIR/stdin")" == "$FAKE_GOOD_TOKEN" ]] && exit 0
    echo "  ✗ the token lacks Cloudflare Pages → Edit"; exit 1 ;;
  *"provision_trial.py render"*)
    for f in wrangler.trial-t1.toml workers/promote/wrangler.trial-t1.toml \
      workers/rss/wrangler.trial-t1.toml workers/newsletter/wrangler.trial-t1.toml; do
      [[ -n "${FAKE_RENDER_SKIP:-}" && "$f" == *"workers/$FAKE_RENDER_SKIP/"* ]] && continue
      # As the real render: no newsletter KV id, no newsletter config.
      [[ "$f" == workers/newsletter/* && "$*" != *"--newsletter-kv-id"* ]] && continue
      echo rendered > "$f"
    done ;;
  *"cyris settings push"*)
    # Under `env -i`, which keeps only HOME and PATH: what it was given goes to HOME.
    echo "uv $*" >> "$HOME/push.calls"
    sources=$(sed -E 's/.*--sources ([^ ]+).*/\1/' <<<"$*")
    cp "$sources" "$HOME/pushed-sources.yaml"
    echo "D1 database 0948e71f-2374-4e4e-8f30-44890139011b" ;;
  *"cyris sources push"*) echo "uv $*" >> "$HOME/push.calls" ;;
esac
"""

FAKE_CURL = r"""#!/bin/bash
ids=no; secrets=no; bearer=no
for a in "$@"; do
  if [[ "$a" == @* ]]; then
    grep -q '^CF-Access-Client-Id: ' "${a#@}" && ids=yes
    grep -q '^CF-Access-Client-Secret: ' "${a#@}" && secrets=yes
    grep -q '^Authorization: Bearer ' "${a#@}" && bearer=yes
  fi
done
echo "curl $* access-id=$ids access-secret=$secrets bearer=$bearer" >> "$CALLS"
case "$*" in
  *"/stats"*) echo "${FAKE_STATS_CODE:-401}" ;;
  *"/poll"*)
    if [[ -n "${FAKE_POLL_REPLY:-}" ]]; then echo "$FAKE_POLL_REPLY"
    else printf '{"feeds":%s}\n' "${FAKE_POLL_FEEDS:-0}"; fi ;;
  *"/api/build"*)
    n=$(( $(cat "$FAKE_DIR/build" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$FAKE_DIR/build"
    if [[ -z "${FAKE_BUILD_NEVER:-}" && $n -gt ${FAKE_BUILD_FAIL_FIRST:-0} ]]; then echo 200
    else echo 503; fi ;;
  *"/settings"*)
    out=""; prev=""
    for a in "$@"; do [[ "$prev" == -o ]] && out="$a"; prev="$a"; done
    [[ -n "$out" ]] && printf '%s' "${FAKE_SETTINGS_BODY:-}" > "$out"
    echo "${FAKE_SETTINGS_CODE:-302}" ;;
  *"/newsletters"*)
    if [[ $bearer == yes ]]; then echo "${FAKE_NEWSLETTERS_CODE:-200}"
    elif [[ -n "${FAKE_NEWSLETTERS_OPEN:-}" ]]; then echo 200
    else echo 401; fi ;;
  *"pages.dev"*) echo 200 ;;
esac
"""

FAKE_BUN = r"""#!/bin/bash
echo "bun $* in $(basename "$PWD")" >> "$CALLS"
[[ -n "${FAKE_BUN_FAIL:-}" && "$(basename "$PWD")" == "$FAKE_BUN_FAIL" ]] && exit 1
exit 0
"""

FAKE_BUNX = r"""#!/bin/bash
echo "bunx $*" >> "$CALLS"
[[ -n "${FAKE_DRY_FAIL:-}" && "$*" == *"--dry-run --config $FAKE_DRY_FAIL "* ]] && exit 1
args=("$@")
for ((i = 0; i < ${#args[@]}; i++)); do
  if [[ "${args[i]}" == --secrets-file ]]; then
    cp "${args[i+1]}" "$FAKE_DIR/snap-$(basename "${args[i+1]}")"
  fi
done
case " $* " in
  *" whoami "*) echo "${FAKE_WHOAMI:-}" ;;
  *" --dry-run "*) ;;
  *" deploy "*)
    case "$*" in
      *"workers/newsletter/"*) echo "https://n-t1.x.workers.dev" ;;
      *"workers/rss/"*) echo "https://r-t1.x.workers.dev" ;;
      *) echo "https://w-t1.x.workers.dev" ;;
    esac ;;
esac
"""

FAKE_QUIET = '#!/bin/bash\necho "$(basename "$0") $*" >> "$CALLS"\n'
# pbcopy reads what is piped to it, as the real one does. Exiting unread would
# race the pipe's writer, which dies of SIGPIPE under the wizard's pipefail.
FAKE_PBCOPY = FAKE_QUIET + "cat > /dev/null\n"

FAKE_GIT = r"""#!/bin/bash
echo "git $*" >> "$CALLS"
if [[ "$1" == check-ignore && -n "${FAKE_UNIGNORED:-}" && "$*" == *"$FAKE_UNIGNORED"* ]]; then
  exit 1
fi
"""

EXAMPLE_CONFIG = """[llm_provider]
provider = "anthropic"
model = "claude"
[digest]
max_articles_per_digest = 400
output_language = "zh-Hant"
"""


NEWSLETTER_KV = "e" * 32


def _state(done: str, email: str = "", state: dict[str, str | None] | None = None) -> str:
    fields: dict[str, str | None] = {
        "DOMAIN": "example.com",
        "ACCOUNT_ID": ACCOUNT,
        "TESTER_EMAIL": email,
        "OUTPUT_LANGUAGE": "en",
        "MAX_ARTICLES": "50",
        "IMAGE_TAG": "image/00fa8ab",
        "IMAGE_DIGEST": "sha256:" + "b" * 64,
        "TRIAL_SUFFIX": "0123456789abcdef",
        "TRIAL_D1_ID": D1_ID,
        "TRIAL_KV_ID": "c" * 32,
        "TRIAL_NEWSLETTER_KV_ID": NEWSLETTER_KV,
        "PAGES_CREATED": "yes",
        "TRIAL_PROMOTE_URL": "https://p-t1.x.workers.dev",
        "TRIAL_NEWSLETTER_URL": "https://n-t1.x.workers.dev",
        "STAGES_DONE": done,
    }
    fields |= state or {}
    return "\n".join(f"{k}={v}" for k, v in fields.items() if v is not None) + "\n"


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
    def make(
        done: str, email: str = "", state: dict[str, str | None] | None = None, **env: str
    ) -> Rig:
        repo = tmp_path / "repo"
        (repo / "scripts").mkdir(parents=True)
        shutil.copy(ROOT / "scripts/trial-wizard.sh", repo / "scripts/trial-wizard.sh")
        (repo / "scripts/provision_trial.py").write_text("")
        for worker in ("promote", "rss", "newsletter"):
            (repo / "workers" / worker).mkdir(parents=True)
        (repo / "settings.example.toml").write_text(EXAMPLE_CONFIG)
        (repo / f".env.trial-{SLUG}-wizard").write_text(_state(done, email, state))
        (repo / f".env.trial-{SLUG}").write_text(
            f"CLOUDFLARE_API_TOKEN={TOKEN}\nGEMINI_API_KEY=g\nCYRIS_STORE_DATABASE_ID={D1_ID}\n"
        )
        (repo / f".env.trial-{SLUG}-promote").write_text("PROMOTE_TOKEN=p\n")
        (repo / f".env.trial-{SLUG}.access").write_text(
            "CYRIS_ACCESS_CLIENT_ID=cid\nCYRIS_ACCESS_CLIENT_SECRET=sek-9f3\n"
        )
        fakes = tmp_path / "bin"
        fakes.mkdir()
        scripts = {"uv": FAKE_UV, "curl": FAKE_CURL, "bunx": FAKE_BUNX, "bun": FAKE_BUN}
        scripts["git"] = FAKE_GIT
        for name in ("gh", "open", "xdg-open"):
            scripts[name] = FAKE_QUIET
        scripts["pbcopy"] = FAKE_PBCOPY
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


def test_the_newsletter_kv_is_created_behind_its_own_gate(rig) -> None:
    r = rig(_stages(4), state={"TRIAL_NEWSLETTER_KV_ID": None})
    run = r.run(stdin=START + "y\n" + "d" * 32 + "\n")
    assert "kv namespace create cyris-newsletter-t1 --env-file /dev/null" in run.calls
    assert "kv namespace create cyris-promote-t1" not in run.calls
    assert f"TRIAL_NEWSLETTER_KV_ID={'d' * 32}" in run.state.splitlines()


def test_a_saved_newsletter_kv_id_creates_nothing(rig) -> None:
    run = rig(_stages(4)).run()
    assert "kv namespace create" not in run.calls
    assert f"Already created: {NEWSLETTER_KV}" in run.out


def test_declining_the_newsletter_kv_stops_stage_5(rig) -> None:
    r = rig(_stages(4), state={"TRIAL_NEWSLETTER_KV_ID": None})
    run = r.run(stdin=START)
    assert run.code == 1
    assert "Stopped before creating the newsletter KV." in run.out
    assert "TRIAL_NEWSLETTER_KV_ID" not in run.state
    assert f"STAGES_DONE={_stages(4)}" in run.state.splitlines()


def test_a_newsletter_kv_id_that_is_not_hex_is_asked_again(rig) -> None:
    r = rig(_stages(4), state={"TRIAL_NEWSLETTER_KV_ID": None})
    run = r.run(stdin=START + "y\nxyz\n" + "e" * 32 + "\n")
    assert "That does not look right" in run.out
    assert f"TRIAL_NEWSLETTER_KV_ID={'e' * 32}" in run.state.splitlines()


def test_the_stage_8_render_passes_the_newsletter_kv_and_writes_four(rig) -> None:
    r = rig(_stages(7), state={"TRIAL_NEWSLETTER_KV_ID": "d" * 32})
    run = r.run()
    assert "provision_trial.py render --slug t1" in run.calls
    assert f"--kv-id {'c' * 32} --newsletter-kv-id {'d' * 32}" in run.calls
    assert "four configs written, all gitignored" in run.out


# A trial wired before the newsletter Worker has no newsletter KV id in its state.
NO_NEWSLETTER = {"TRIAL_NEWSLETTER_KV_ID": None, "TRIAL_NEWSLETTER_URL": None}
NO_NEWSLETTER_NOTE = "This trial has no newsletter KV, so it gets no newsletter Worker."


def test_a_trial_without_a_newsletter_kv_renders_three_configs(rig) -> None:
    r = rig(_stages(7), state=NO_NEWSLETTER)
    run = r.run()
    assert "provision_trial.py render --slug t1" in run.calls
    assert "--newsletter-kv-id" not in run.calls
    assert "three configs written, all gitignored" in run.out
    assert NO_NEWSLETTER_NOTE in run.out
    assert "8" in r.done().removeprefix("STAGES_DONE=").split(",")


def test_a_trial_without_a_newsletter_kv_dry_runs_three_configs(rig) -> None:
    r = rig(_stages(8), state=NO_NEWSLETTER)
    run = r.run()
    assert "in newsletter" not in run.calls
    assert "workers/newsletter/" not in run.calls
    assert "all three dry runs passed" in run.out
    assert "9" in r.done().removeprefix("STAGES_DONE=").split(",")


def test_a_render_that_skips_the_newsletter_config_dies(rig) -> None:
    run = rig(_stages(7), FAKE_RENDER_SKIP="newsletter").run()
    assert run.code == 1
    assert "workers/newsletter/wrangler.trial-t1.toml was not written." in run.out


def test_a_newsletter_config_git_does_not_ignore_dies(rig) -> None:
    run = rig(_stages(7), FAKE_UNIGNORED="workers/newsletter/wrangler.trial-t1.toml").run()
    assert run.code == 1
    assert "workers/newsletter/wrangler.trial-t1.toml is not gitignored." in run.out


def test_stage_9_installs_both_workers_and_dry_runs_all_four_configs(rig) -> None:
    run = rig(_stages(8)).run()
    calls = run.calls.splitlines()
    assert calls.count("bun install --frozen-lockfile in rss") == 1
    assert calls.count("bun install --frozen-lockfile in newsletter") == 1
    dry = [
        next(i for i, c in enumerate(calls) if f"--dry-run --config {cfg} " in c)
        for cfg in (
            "wrangler.trial-t1.toml",
            "workers/promote/wrangler.trial-t1.toml",
            "workers/rss/wrangler.trial-t1.toml",
            "workers/newsletter/wrangler.trial-t1.toml",
        )
    ]
    assert dry == sorted(dry)
    assert "all four dry runs passed" in run.out


def test_a_newsletter_install_failure_dies_before_any_dry_run(rig) -> None:
    run = rig(_stages(8), FAKE_BUN_FAIL="newsletter").run()
    assert run.code == 1
    assert "bun install in workers/newsletter failed." in run.out
    assert "--dry-run --config workers/newsletter/" not in run.calls


def test_a_newsletter_dry_run_failure_dies_naming_the_worker(rig) -> None:
    run = rig(_stages(8), FAKE_DRY_FAIL="workers/newsletter/wrangler.trial-t1.toml").run()
    assert run.code == 1
    assert "Newsletter Worker dry run failed." in run.out


RSS_TOKEN = "rsstok-NOT-FOR-OUTPUT"
# What FROM_STAGE_14 answers after the newsletter Worker's URL.
TAIL = "n\n\n\n\nn\n\n"


def _seed_rss_token(r: Rig) -> None:
    (r.repo / f".env.trial-{SLUG}-rss").write_text(f"RSS_TOKEN={RSS_TOKEN}\n")


def test_the_newsletter_worker_gets_the_shared_token_and_its_own_config(rig) -> None:
    r = rig(_stages(13), FAKE_POLL_FEEDS="1")
    _seed_rss_token(r)
    run = r.run(stdin=START + "y\n\ny\n\n" + TAIL)
    snap = (Path(r.env["FAKE_DIR"]) / f"snap-.env.trial-{SLUG}-newsletter").read_text()
    assert snap == f"NEWSLETTER_TOKEN={RSS_TOKEN}\n"
    assert (
        f"bunx wrangler deploy --config workers/newsletter/wrangler.trial-{SLUG}.toml "
        f"--env-file /dev/null --secrets-file .env.trial-{SLUG}-newsletter"
    ) in run.calls
    assert "TRIAL_NEWSLETTER_URL=https://n-t1.x.workers.dev" in run.state.splitlines()


@pytest.mark.parametrize("existing", [False, True])
def test_the_newsletter_secrets_file_is_mode_600(rig, existing: bool) -> None:
    """A file an earlier run left world-readable is made private too."""
    r = rig(_stages(13), FAKE_POLL_FEEDS="1")
    _seed_rss_token(r)
    path = r.repo / f".env.trial-{SLUG}-newsletter"
    if existing:
        path.write_text("NEWSLETTER_TOKEN=old\n")
        path.chmod(0o644)
    r.run(stdin=START + "y\n\ny\n\n" + TAIL)
    assert path.read_text() == f"NEWSLETTER_TOKEN={RSS_TOKEN}\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_the_newsletter_token_stays_out_of_output_and_calls(rig) -> None:
    r = rig(_stages(13), FAKE_POLL_FEEDS="1")
    _seed_rss_token(r)
    run = r.run(stdin=START + "y\n\ny\n\n" + TAIL)
    for line in run.calls.splitlines():
        if line.startswith(("bunx", "uv")) or (line.startswith("curl") and "/newsletters" in line):
            assert RSS_TOKEN not in line
        assert "secret put" not in line
    assert RSS_TOKEN not in run.out


def test_the_newsletter_probes_send_no_bearer_then_the_bearer(rig) -> None:
    r = rig(_stages(13), FAKE_POLL_FEEDS="1")
    _seed_rss_token(r)
    run = r.run(stdin=START + "y\n\ny\n\n" + TAIL)
    probes = [ln for ln in run.calls.splitlines() if "/newsletters" in ln]
    assert len(probes) == 2
    assert probes[0].endswith("bearer=no")
    assert probes[1].endswith("bearer=yes")
    assert "✓ /newsletters without the bearer → 401" in run.out
    assert "✓ /newsletters with the bearer → 200" in run.out


def test_an_open_newsletter_worker_stops_stage_14(rig) -> None:
    r = rig(_stages(13), FAKE_POLL_FEEDS="1", FAKE_NEWSLETTERS_OPEN="1")
    _seed_rss_token(r)
    run = r.run(stdin=START + "y\n\ny\n\n" + TAIL)
    assert run.code == 1
    assert "without the bearer answered 200" in run.out
    app_env = (r.repo / f".env.trial-{SLUG}").read_text()
    for key in ("CYRIS_WORKER_TOKEN", "CYRIS_RSS_WORKER_URL", "CYRIS_NEWSLETTER_WORKER_URL"):
        assert key not in app_env
    assert "wrangler deploy --config wrangler.trial-t1.toml" not in run.calls


# TAIL with the app deploy accepted.
APP_YES = "y" + TAIL.removeprefix("n")


def test_the_app_secrets_file_gets_the_three_urls_and_token_once_each(rig) -> None:
    r = rig(_stages(13), FAKE_POLL_FEEDS="1")
    _seed_rss_token(r)
    run = r.run(stdin=START + "y\n\ny\n\n" + APP_YES)
    snap = (Path(r.env["FAKE_DIR"]) / f"snap-.env.trial-{SLUG}").read_text().splitlines()
    for line in (
        f"CYRIS_WORKER_TOKEN={RSS_TOKEN}",
        "CYRIS_RSS_WORKER_URL=https://r-t1.x.workers.dev",
        "CYRIS_NEWSLETTER_WORKER_URL=https://n-t1.x.workers.dev",
    ):
        assert snap.count(line) == 1, line
    assert "✓ service token → 200 from /api/build" in run.out


def test_the_app_is_redeployed_once_after_the_three_lines(rig) -> None:
    r = rig(_stages(13), FAKE_POLL_FEEDS="1")
    _seed_rss_token(r)
    run = r.run(stdin=START + "y\n\ny\n\n" + APP_YES)
    calls = run.calls.splitlines()
    app = [i for i, c in enumerate(calls) if "wrangler deploy --config wrangler.trial-t1.toml" in c]
    rss = next(i for i, c in enumerate(calls) if "workers/rss/wrangler.trial-t1.toml" in c)
    newsletter = next(i for i, c in enumerate(calls) if "workers/newsletter/" in c)
    assert len(app) == 1
    assert app[0] > rss and app[0] > newsletter


def test_a_rerun_from_14_keeps_each_key_once(rig) -> None:
    r = rig(_stages(13), FAKE_POLL_FEEDS="1")
    _seed_rss_token(r)
    r.run(stdin=START + "y\n\ny\n\n" + APP_YES)
    r.run("--from", "14", stdin=START + "y\n\ny\n\n" + APP_YES)
    app_env = (r.repo / f".env.trial-{SLUG}").read_text().splitlines()
    for key in ("CYRIS_WORKER_TOKEN", "CYRIS_RSS_WORKER_URL", "CYRIS_NEWSLETTER_WORKER_URL"):
        assert sum(line.startswith(f"{key}=") for line in app_env) == 1, key


def test_a_completed_stage_14_is_recorded(rig) -> None:
    r = rig(_stages(13), FAKE_POLL_FEEDS="1", state={"TRIAL_RSS_URL": RSS_URL})
    _seed_rss_token(r)
    run = r.run(stdin=START + "n\n\nn\n\n" + TAIL)
    assert "14" in next(ln for ln in run.state.splitlines() if ln.startswith("STAGES_DONE=")).split(
        "="
    )[1].split(",")


def test_a_bearer_the_worker_refuses_says_the_secret_did_not_land(rig) -> None:
    r = rig(_stages(13), FAKE_POLL_FEEDS="1", FAKE_NEWSLETTERS_CODE="401")
    _seed_rss_token(r)
    run = r.run(stdin=START + "y\n\ny\n\n" + TAIL)
    assert run.code == 1
    assert "with the bearer answered 401" in run.out
    assert "did not land" in run.out


def test_a_declined_newsletter_deploy_keeps_the_url_and_still_probes(rig) -> None:
    r = rig(
        _stages(13),
        FAKE_POLL_FEEDS="1",
        state={"TRIAL_NEWSLETTER_URL": "https://n-t1.x.workers.dev"},
    )
    _seed_rss_token(r)
    run = r.run(stdin=START + "y\n\nn\n\n" + TAIL)
    assert "workers/newsletter/wrangler.trial-t1.toml --env-file" not in run.calls
    assert len([ln for ln in run.calls.splitlines() if "/newsletters" in ln]) == 2
    assert "TRIAL_NEWSLETTER_URL=https://n-t1.x.workers.dev" in run.state.splitlines()


def test_the_names_fake_keys_match_what_provision_trial_prints() -> None:
    real = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/provision_trial.py"),
            "names",
            "--slug",
            SLUG,
            "--pages-suffix",
            "0123456789abcdef",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    fake = set(re.findall(r"(TRIAL_[A-Z0-9_]+)=", FAKE_UV))
    assert fake == {line.split("=", 1)[0] for line in real.splitlines()}


def test_a_resumed_run_starts_at_the_first_stage_not_done(rig) -> None:
    run = rig(_stages(11)).run()
    assert "Stages 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11 were done earlier" in run.out
    assert "▸ Stage 12/17 · Deploy the app" in run.out
    for earlier in range(1, 12):
        assert f"▸ Stage {earlier}/17" not in run.out
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
    assert "▸ Stage 12/17 · Deploy the app" in run.out
    assert r.done() == f"STAGES_DONE={_stages(12)}"


def test_from_keeps_the_values_already_saved(rig) -> None:
    run = rig(_stages(14)).run("--from", "12")
    assert "TRIAL_SUFFIX=0123456789abcdef" in run.state.splitlines()
    assert f"TRIAL_D1_ID={D1_ID}" in run.state.splitlines()


@pytest.mark.parametrize(
    "args", [("--from", "17"), ("--from", "18"), ("--from", "0"), ("--from",), ("again",)]
)
def test_a_bad_mode_prints_the_usage(rig, args: tuple[str, ...]) -> None:
    run = rig(_stages(14)).run(*args)
    assert run.code == 1
    assert "Usage: bash scripts/trial-wizard.sh <slug> [receipts | --from N]" in run.out


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


def test_a_403_from_the_worker_itself_fails_receipt_1_naming_access(rig) -> None:
    body = '{"error":"forbidden","detail":"This page needs a Cloudflare Access sign-in."}'
    run = rig(_stages(17), FAKE_SETTINGS_CODE="403", FAKE_SETTINGS_BODY=body).run("receipts")
    assert "403 from the Worker, not Access; Access does not cover it" in run.out
    assert "✓ unauthenticated https://t1.example.com/settings" not in run.out


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
    assert "UI_TOKEN" not in "\n".join(doctor)


def test_a_declined_deploy_looks_once_instead_of_waiting(rig) -> None:
    run = rig(_stages(11), FAKE_BUILD_NEVER="1").run()
    assert "waiting for" not in run.out
    assert "does not answer 200 with the service token, and nothing was deployed now." in run.out
    assert run.calls.count("/api/build") == 1
    assert "▸ Stage 13/17" in run.out


def test_the_doctor_gets_the_newsletter_worker_url(rig) -> None:
    r = rig(_stages(17))
    (r.repo / f".env.trial-{SLUG}").write_text(
        f"CLOUDFLARE_API_TOKEN={TOKEN}\nCYRIS_NEWSLETTER_WORKER_URL=https://n-t1.x.workers.dev\n"
    )
    r.run("receipts")
    doctor = (Path(r.env["FAKE_DIR"]) / "doctor.env").read_text().splitlines()
    assert "CYRIS_NEWSLETTER_WORKER_URL=https://n-t1.x.workers.dev" in doctor


def test_the_doctor_gets_no_newsletter_url_when_none_is_saved(rig) -> None:
    r = rig(_stages(17))
    (r.repo / f".env.trial-{SLUG}").write_text(f"CLOUDFLARE_API_TOKEN={TOKEN}\n")
    r.run("receipts")
    doctor = (Path(r.env["FAKE_DIR"]) / "doctor.env").read_text().splitlines()
    assert not any(line.startswith("CYRIS_NEWSLETTER_WORKER_URL=") for line in doctor)


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
    assert "▸ Stage 8/17" in run.out


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


def test_the_wizard_no_longer_uses_a_session_cookie() -> None:
    script = (ROOT / "scripts/trial-wizard.sh").read_text()
    assert "shasum" not in script


# Every answer from stage 14 on, in order: the rss deploy declined, the Worker
# URL, the app deploy declined, then the pauses and the clipboard of stages 14
# to 16. A prompt left unanswered would loop on end-of-file rather than stop.
RSS_URL = "https://rss-t1.x.workers.dev"
FROM_STAGE_14 = ["n", RSS_URL, "n", "", "n", "", "", "", "n", ""]
# Stage 13's push, confirmed, and the pause after it.
STAGE_13 = ["y", ""]


def _answers(*lines: str) -> str:
    return START + "\n".join(lines) + "\n"


def test_the_first_boot_pushes_the_settings_and_no_source(rig) -> None:
    r = rig(_stages(12))

    run = r.run(stdin=_answers(*STAGE_13, *FROM_STAGE_14))

    home = Path(r.env["HOME"])
    pushes = (home / "push.calls").read_text().splitlines()
    assert run.code == 0, run.out
    assert [p.split(" push ")[0] for p in pushes] == ["uv run cyris settings"]
    assert (home / "pushed-sources.yaml").read_text() == "sources: []\n"
    assert "✓ settings push went to" in run.out
    assert not list(r.repo.glob(".env.trial-*-sources.yaml"))


def test_a_pass_from_the_first_boot_to_the_receipts_starts_no_run(rig) -> None:
    run = rig(_stages(12)).run(stdin=_answers(*STAGE_13, *FROM_STAGE_14))

    assert run.code == 0, run.out
    assert "/run" not in run.calls
    assert "wait-run" not in run.calls and "latest-run" not in run.calls
    assert "▸ Stage 17/17 · Receipts" in run.out


def test_a_state_from_before_the_first_digest_stage_was_dropped_still_runs_the_receipts(
    rig,
) -> None:
    """An older state lists 17, the first-digest stage then; 17 is the receipts now."""
    run = rig(_stages(17)).run()

    assert "Stages 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16 were done" in run.out
    assert "▸ Stage 17/17 · Receipts" in run.out
    assert "/run" not in run.calls


def test_stage_14_takes_feeds_0_on_a_trial_with_no_source(rig) -> None:
    r = rig(_stages(13), FAKE_POLL_FEEDS="0")

    run = r.run(stdin=_answers(*FROM_STAGE_14))

    assert "/poll feeds = 0: no source yet, so the trial waits for onboarding." in run.out
    assert run.code == 0
    assert "14" in r.done().removeprefix("STAGES_DONE=").split(",")


def test_stage_14_counts_the_feeds_a_tester_already_picked(rig) -> None:
    r = rig(_stages(13), FAKE_POLL_FEEDS="3")

    run = r.run(stdin=_answers(*FROM_STAGE_14))

    assert "✓ /poll feeds = 3" in run.out
    assert run.code == 0


def test_stage_14_stops_when_the_poll_reports_no_feed_count(rig) -> None:
    r = rig(_stages(13), FAKE_POLL_REPLY='{"error":"unauthorized"}')

    run = r.run(stdin=_answers(*FROM_STAGE_14))

    assert "/poll answered without a feed count." in run.out
    assert run.code == 1
    assert "14" not in r.done().removeprefix("STAGES_DONE=").split(",")


def test_the_routing_notice_names_the_rule_and_the_gmail_read(rig) -> None:
    run = rig(_stages(14)).run(stdin=START + "\n\n\n")
    for text in (
        "t1@example.com",
        "cyris-newsletter-t1",
        "Email Routing",
        "https://n-t1.x.workers.dev/newsletters",
        "Bearer",
        "ACK",
    ):
        assert text in run.out, text
    assert "Destination Addresses" not in run.out
    assert run.out.index("Gmail") < run.out.index("Stage 17/17")


def test_the_tester_is_told_the_gmail_steps_in_order(rig) -> None:
    out = rig(_stages(14)).run(stdin=START + "\n\n\n").out
    add = out.index("Add a forwarding address → t1@example.com")
    read = out.index("GET https://n-t1.x.workers.dev/newsletters")
    confirm = out.index("mail-settings.google.com/mail/vf-")
    filt = out.index("Forward to t1@example.com")
    assert add < read < confirm < filt < out.index("Stage 16/17")
    assert "workers/newsletter/README.md" in out


def test_a_tester_email_adds_the_destination_address_steps(rig) -> None:
    run = rig(_stages(14), email="tester@example.com").run(stdin=START + "\n\n\n\n")
    assert "Email Routing" in run.out
    assert "add tester@example.com" in run.out


def test_the_routing_notice_never_prints_the_app_token(rig) -> None:
    r = rig(_stages(14))
    (r.repo / f".env.trial-{SLUG}").write_text(
        f"CLOUDFLARE_API_TOKEN={TOKEN}\nCYRIS_WORKER_TOKEN=app-tok-NOT-FOR-OUTPUT\n"
    )
    run = r.run(stdin=START + "\n\n\n")
    assert "app-tok-NOT-FOR-OUTPUT" not in run.out
    assert "app-tok-NOT-FOR-OUTPUT" not in run.calls


def test_a_trial_without_a_newsletter_kv_skips_the_newsletter_worker(rig) -> None:
    r = rig(_stages(13), FAKE_POLL_FEEDS="1", state=NO_NEWSLETTER)
    _seed_rss_token(r)
    # The rss deploy declined, its URL, the app deploy declined, then the pause.
    run = r.run(stdin=_answers("n", RSS_URL, "n", ""))
    assert NO_NEWSLETTER_NOTE in run.out
    assert f"bash scripts/trial-wizard.sh {SLUG} --from 5" in run.out
    assert "workers/newsletter/" not in run.calls
    assert "/newsletters" not in run.calls
    assert not (r.repo / f".env.trial-{SLUG}-newsletter").exists()
    app_env = (r.repo / f".env.trial-{SLUG}").read_text()
    assert "CYRIS_NEWSLETTER_WORKER_URL" not in app_env
    assert f"CYRIS_RSS_WORKER_URL={RSS_URL}" in app_env.splitlines()
    assert "14" in r.done().removeprefix("STAGES_DONE=").split(",")


def test_a_trial_without_a_newsletter_kv_skips_the_routing_notice(rig) -> None:
    r = rig(_stages(14), state=NO_NEWSLETTER)
    run = r.run(stdin=START + "\n")
    assert NO_NEWSLETTER_NOTE in run.out
    assert "Routing rules" not in run.out
    assert "15" in r.done().removeprefix("STAGES_DONE=").split(",")


def test_a_trial_without_a_newsletter_kv_still_adds_the_tester_address(rig) -> None:
    r = rig(_stages(14), state=NO_NEWSLETTER, email="tester@example.com")
    run = r.run(stdin=START + "\n\n")
    assert "Routing rules" not in run.out
    assert "Destination Addresses" in run.out
    assert "tester@example.com" in run.out
    assert "15" in r.done().removeprefix("STAGES_DONE=").split(",")


def test_the_routing_notice_needs_a_saved_newsletter_url(rig) -> None:
    run = rig(_stages(14), state={"TRIAL_NEWSLETTER_URL": None}).run(stdin=START)
    assert run.code == 1
    assert "--from 14" in run.out


def test_the_hand_over_keeps_the_newsletter_secrets_file(rig) -> None:
    run = rig(_stages(15)).run()
    keep = next(ln for ln in run.out.splitlines() if "Keep " in ln)
    for name in (
        f".env.trial-{SLUG}",
        f".env.trial-{SLUG}.access",
        f".env.trial-{SLUG}-promote",
        f".env.trial-{SLUG}-rss",
        f".env.trial-{SLUG}-newsletter",
    ):
        assert name in keep, name


def test_the_hand_over_copies_no_token_to_the_clipboard(rig) -> None:
    run = rig(_stages(15)).run()
    assert "pbcopy" not in run.calls
    assert "password manager" not in run.out
    assert "Access one-time PIN" in run.out


# gh answers from JSON fixtures through the wizard's own -q filter, so the filter is what is
# tested. Its list key is the workflow file, its view key the run id.
FAKE_GH = r"""#!/bin/bash
echo "gh $*" >> "$CALLS"
q=""; prev=""
for a in "$@"; do [[ "$prev" == -q ]] && q="$a"; prev="$a"; done
case "$1 $2" in
  "run list") key="list-$4" ;;
  "run view") key="view-$3" ;;
  *) exit 0 ;;
esac
jq -r "$q" "$FAKE_DIR/gh-$key.json"
"""

RELEASED_SHA, MANUAL_SHA, SKIPPED_SHA = "1" * 40, "2" * 40, "3" * 40


def _deploy_history(rig, tmp_path: Path, manual_at: str) -> Run:
    """Stage 2 with a release that deployed, a newer one the head guard skipped, and a
    deploy.yml dispatch finished at `manual_at`."""
    if shutil.which("jq", path="/usr/bin:/bin") is None:
        pytest.skip("jq is not in /usr/bin or /bin")
    r = rig("1", state={"IMAGE_TAG": None, "IMAGE_DIGEST": None}, FAKE_WHOAMI=ACCOUNT)
    fake = tmp_path / "fake"

    def job(name: str, conclusion: str, at: str) -> dict:
        return {"name": name, "conclusion": conclusion, "completedAt": at}

    fixtures = {
        "list-release-image.yml": [
            {"databaseId": 102, "headSha": SKIPPED_SHA},
            {"databaseId": 101, "headSha": RELEASED_SHA},
        ],
        "list-deploy.yml": [{"databaseId": 201, "headSha": MANUAL_SHA}],
        "view-101": {
            "jobs": [
                job("release", "success", "2026-11-03T10:00:00Z"),
                job("current", "success", "2026-11-03T10:01:00Z"),
                job("deploy / deploy", "success", "2026-11-03T10:20:00Z"),
            ]
        },
        "view-102": {
            "jobs": [
                job("release", "success", "2026-11-03T11:00:00Z"),
                job("current", "success", "2026-11-03T11:01:00Z"),
                job("deploy / deploy", "skipped", "2026-11-03T11:01:00Z"),
            ]
        },
        "view-201": {"jobs": [job("deploy", "success", manual_at)]},
    }
    for key, body in fixtures.items():
        (fake / f"gh-{key}.json").write_text(json.dumps(body))
    bin_dir = tmp_path / "bin"
    (bin_dir / "gh").write_text(FAKE_GH)
    (bin_dir / "git").write_text(
        FAKE_GIT
        + 'case "$*" in\n'
        + '  "tag -l image/* --sort=-creatordate") echo image/1111111 ;;\n'
        + f'  "tag -l --format=%(contents) image/1111111") echo "digest=sha256:{"b" * 64}" ;;\n'
        + "esac\n"
    )
    return r.run(stdin=START)


def test_the_last_deploy_note_counts_a_release_that_deployed(rig, tmp_path: Path) -> None:
    """A release deploys inside its own run, so deploy.yml's runs alone miss it."""
    run = _deploy_history(rig, tmp_path, manual_at="2026-11-02T09:00:00Z")
    assert f"Production's last deploy: {RELEASED_SHA} (finished 2026-11-03T10:20:00Z)" in run.out
    assert "Pick the image production runs" in run.out


def test_the_last_deploy_note_counts_a_later_deploy_started_by_hand(rig, tmp_path: Path) -> None:
    run = _deploy_history(rig, tmp_path, manual_at="2026-11-03T12:00:00Z")
    assert f"Production's last deploy: {MANUAL_SHA} (finished 2026-11-03T12:00:00Z)" in run.out
    assert SKIPPED_SHA not in run.out
