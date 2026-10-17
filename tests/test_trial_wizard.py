"""scripts/trial-wizard.sh: resuming, waiting, and what each stage does with its answers.

The wizard runs in a copy of the repo with every external command on PATH replaced by a
fake that records its arguments. Its stdin is closed unless a test types answers, so a
stage that would wait for a key reads end-of-file instead: a "no" at every y/N gate.
"""

import re
import shlex
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
# `env -i` in the wizard strips the rig's variables, so read them back from the file.
[[ -n "${CALLS:-}" ]] || . "${0%/*}/../fake/env"
echo "uv $*" >> "$CALLS"
case "$*" in
  *"cyris settings push"*) echo "D1 database $FAKE_D1_ID" ;;
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
  *"trial_api.py latest-run"*) cat > /dev/null; echo "${FAKE_LATEST:-4}" ;;
  *"trial_api.py wait-run"*)
    cat > /dev/null; echo "  run 5: status ok"; exit "${FAKE_WAIT_RC:-0}" ;;
  *"provision_trial.py render"*)
    for f in wrangler.trial-t1.toml workers/promote/wrangler.trial-t1.toml \
      workers/rss/wrangler.trial-t1.toml workers/newsletter/wrangler.trial-t1.toml; do
      [[ -n "${FAKE_RENDER_SKIP:-}" && "$f" == *"workers/$FAKE_RENDER_SKIP/"* ]] && continue
      echo rendered > "$f"
    done ;;
  *"cyris sources list"*)
    if [[ -n "${FAKE_D1_SOURCES:-}" ]]; then printf '%s\n' "$FAKE_D1_SOURCES"
    else echo "D1 has no sources; runs stop until you add one on /settings."
    fi ;;
  *"python -c"*) shift 2; exec "$REAL_PYTHON" "$@" ;;
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
  *"/api/build"*)
    n=$(( $(cat "$FAKE_DIR/build" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$FAKE_DIR/build"
    if [[ -z "${FAKE_BUILD_NEVER:-}" && $n -gt ${FAKE_BUILD_FAIL_FIRST:-0} ]]; then echo 200
    else echo 503; fi ;;
  *"/settings"*) echo "${FAKE_SETTINGS_CODE:-302}" ;;
  *"/stats"*) echo 401 ;;
  *"/poll"*) echo "{\"feeds\":${FAKE_POLL_FEEDS:-0}}" ;;
  *"/newsletters"*)
    if [[ $bearer == yes ]]; then echo "${FAKE_NEWSLETTERS_CODE:-200}"
    elif [[ -n "${FAKE_NEWSLETTERS_OPEN:-}" ]]; then echo 200
    else echo 401; fi ;;
  *"/run?period="*)
    body='{"started":"run","at":"2026-10-06T10:50:10Z"}'
    [[ -n "${FAKE_RUN_BODY+x}" ]] && body="$FAKE_RUN_BODY"
    printf '%s\n%s\n' "$body" "${FAKE_RUN_CODE:-200}" ;;
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
        "SOURCE_COUNT": "1",
        "RSS_SOURCE_COUNT": "1",
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
        (repo / "cyris.toml.example").write_text(EXAMPLE_CONFIG)
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
        for name in ("gh", "open", "xdg-open", "pbcopy"):
            scripts[name] = FAKE_QUIET
        for name, body in scripts.items():
            (fakes / name).write_text(body)
            (fakes / name).chmod(0o755)
        state_dir = tmp_path / "fake"
        state_dir.mkdir()
        base = {
            "PATH": f"{fakes}:/usr/bin:/bin",
            "REAL_PYTHON": sys.executable,
            "HOME": str(tmp_path),
            "CALLS": str(repo / "calls.log"),
            "FAKE_DIR": str(state_dir),
            "TRIAL_WIZARD_LOGIN_INTERVAL": "0",
            "FAKE_D1_ID": D1_ID,
        }
        (state_dir / "env").write_text(
            "".join(f"export {k}={shlex.quote(v)}\n" for k, v in (base | env).items())
        )
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


SOURCES_BOTH = """sources:
  - {name: a, url: "https://a.example/feed", type: rss}
  - {name: b, url: "https://b.example/feed"}
  - {name: c, email_match: "news@sender.example", type: newsletter}
"""


def test_a_newsletter_source_is_accepted_and_both_counts_are_saved(rig) -> None:
    r = rig(_stages(12))
    (r.repo / f".env.trial-{SLUG}-sources.yaml").write_text(SOURCES_BOTH)
    run = r.run(stdin=START)
    assert "has a newsletter source" not in run.out
    assert "3 source(s), 2 for the rss Worker." in run.out
    assert "SOURCE_COUNT=3" in run.state.splitlines()
    assert "RSS_SOURCE_COUNT=2" in run.state.splitlines()


def test_a_single_newsletter_source_counts_zero_rss(rig) -> None:
    r = rig(_stages(12))
    (r.repo / f".env.trial-{SLUG}-sources.yaml").write_text(
        'sources:\n  - {name: c, email_match: "news@sender.example", type: newsletter}\n'
    )
    state = r.run(stdin=START).state.splitlines()
    assert "SOURCE_COUNT=1" in state
    assert "RSS_SOURCE_COUNT=0" in state


def test_a_source_with_no_type_counts_as_rss(rig) -> None:
    r = rig(_stages(12))
    (r.repo / f".env.trial-{SLUG}-sources.yaml").write_text(
        'sources:\n  - {name: a, url: "https://a.example/feed", type: rss}\n'
        '  - {name: b, url: "https://b.example/feed"}\n'
    )
    run = r.run(stdin=START)
    assert "RSS_SOURCE_COUNT=2" in run.state.splitlines()


def test_a_sources_file_that_is_not_yaml_stops_stage_13(rig) -> None:
    r = rig(_stages(12))
    (r.repo / f".env.trial-{SLUG}-sources.yaml").write_text("sources: [\n")
    run = r.run(stdin=START)
    assert run.code == 1
    assert f"{SLUG}-sources.yaml is not valid YAML with a sources list." in run.out


def test_stage_13_tells_the_operator_to_match_the_sender(rig) -> None:
    r = rig(_stages(12))
    (r.repo / f".env.trial-{SLUG}-sources.yaml").write_text(SOURCES_BOTH)
    run = r.run(stdin=START)
    assert "email_match" in run.out
    assert "RSS only" not in run.out


# What `cyris sources list` prints for a D1 the tester changed on /settings: a dropped, b
# kept, d added, c a newsletter.
D1_EDITED = """summarize  rss         b — https://b.example/feed
filter     rss         d — https://d.example/feed
filter     newsletter  c — —

3 sources in D1."""
# Enter keeps the editor closed, Enter passes the pause, y pushes the settings.
TO_SOURCES = START + "n\n\ny\n"
# Stage 14 then skips the rss deploy, keeps the saved URL and stops at the /poll count, so a
# closed stdin never reaches a prompt that asks again.
TO_POLL = "n\n\n"


def test_an_empty_d1_gets_the_file_without_a_question(rig) -> None:
    r = rig(_stages(12), state={"TRIAL_RSS_URL": RSS_URL})
    (r.repo / f".env.trial-{SLUG}-sources.yaml").write_text(SOURCES_BOTH)
    run = r.run(stdin=TO_SOURCES + TO_POLL)
    assert "cyris sources push" in run.calls
    assert "Replace D1's sources" not in run.out


def test_sources_the_tester_changed_are_kept_unless_the_operator_replaces_them(rig) -> None:
    r = rig(_stages(12), state={"TRIAL_RSS_URL": RSS_URL}, FAKE_D1_SOURCES=D1_EDITED)
    (r.repo / f".env.trial-{SLUG}-sources.yaml").write_text(SOURCES_BOTH)
    run = r.run(stdin=TO_SOURCES + "n\n" + TO_POLL)
    assert "cyris sources push" not in run.calls
    lines = run.out.splitlines()
    assert "    -d" in lines
    assert "    +a" in lines
    assert "Kept the 3 source(s) in D1." in run.out
    state = run.state.splitlines()
    assert "SOURCE_COUNT=3" in state
    assert "RSS_SOURCE_COUNT=2" in state
    assert f"STAGES_DONE={_stages(13)}" in state


def test_the_operator_can_replace_what_d1_holds(rig) -> None:
    r = rig(_stages(12), state={"TRIAL_RSS_URL": RSS_URL}, FAKE_D1_SOURCES=D1_EDITED)
    (r.repo / f".env.trial-{SLUG}-sources.yaml").write_text(SOURCES_BOTH)
    run = r.run(stdin=TO_SOURCES + "y\n" + TO_POLL)
    assert "cyris sources push" in run.calls
    assert "RSS_SOURCE_COUNT=2" in run.state.splitlines()


RSS_URL = "https://r-t1.x.workers.dev"
RSS_TOKEN = "rsstok-NOT-FOR-OUTPUT"
# After the newsletter URL line: the app deploy gate, then the Enters of stages 14-16 (stage 15
# has two), then a "morning" for stage 17's period and an "n" that declines its post.
TAIL = "\n" * 5 + "morning\nn\n"
# Declines both Worker deploys, keeps the URLs, and reaches stage 17 without a hang.
POLL_PASS_STDIN = START + "n\n\nn\n\n" + TAIL


def test_the_poll_counts_the_rss_sources_only(rig) -> None:
    run = rig(
        _stages(13),
        state={"TRIAL_RSS_URL": RSS_URL, "SOURCE_COUNT": "3", "RSS_SOURCE_COUNT": "2"},
        FAKE_POLL_FEEDS="2",
    ).run(stdin=POLL_PASS_STDIN)
    assert "/poll feeds = 2" in run.out


def test_a_poll_count_other_than_the_rss_sources_dies(rig) -> None:
    run = rig(
        _stages(13),
        state={"TRIAL_RSS_URL": RSS_URL, "SOURCE_COUNT": "3", "RSS_SOURCE_COUNT": "2"},
        FAKE_POLL_FEEDS="3",
    ).run(stdin=START + "n\n")
    assert run.code == 1
    assert "feeds '3'" in run.out


def test_without_an_rss_count_the_total_is_the_expectation(rig) -> None:
    run = rig(
        _stages(13),
        state={"TRIAL_RSS_URL": RSS_URL, "RSS_SOURCE_COUNT": None, "SOURCE_COUNT": "1"},
        FAKE_POLL_FEEDS="1",
    ).run(stdin=POLL_PASS_STDIN)
    assert "/poll feeds = 1" in run.out


def test_zero_rss_sources_and_zero_feeds_pass(rig) -> None:
    run = rig(
        _stages(13),
        state={"TRIAL_RSS_URL": RSS_URL, "RSS_SOURCE_COUNT": "0"},
        FAKE_POLL_FEEDS="0",
    ).run(stdin=POLL_PASS_STDIN)
    assert "/poll feeds = 0" in run.out


def test_zero_feeds_with_rss_sources_sends_the_operator_back(rig) -> None:
    run = rig(
        _stages(13),
        state={"TRIAL_RSS_URL": RSS_URL, "RSS_SOURCE_COUNT": "2"},
        FAKE_POLL_FEEDS="0",
    ).run(stdin=START + "n\n")
    assert run.code == 1
    assert "Go back to stage 13" in run.out


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


APP_YES = "y\n" + "\n" * 4 + "morning\nn\n"


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
    run = r.run(stdin=POLL_PASS_STDIN)
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
    assert "UI_TOKEN" not in "\n".join(doctor)


def test_a_declined_deploy_looks_once_instead_of_waiting(rig) -> None:
    run = rig(_stages(11), FAKE_BUILD_NEVER="1").run()
    assert "waiting for" not in run.out
    assert "does not answer 200 with the service token, and nothing was deployed now." in run.out
    assert run.calls.count("/api/build") == 1
    assert "▸ Stage 13/18" in run.out


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
    assert "shasum" not in script


def test_a_wait_that_times_out_warns_and_goes_on(rig) -> None:
    run = rig(_stages(16), FAKE_WAIT_RC="2").run(stdin=START + "evening\ny\n")
    assert "a second post starts a second run" in run.out
    assert "▸ Stage 18/18 · Receipts" in run.out


def test_declining_the_post_starts_nothing(rig) -> None:
    run = rig(_stages(16)).run(stdin=START + "evening\nn\n")
    assert "/run?period=" not in run.calls
    assert "wait-run" not in run.calls


def test_the_routing_notice_names_the_rule_and_the_gmail_read(rig) -> None:
    run = rig(_stages(14)).run(stdin=START + "\n\n\nmorning\nn\n")
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
    assert run.out.index("Gmail") < run.out.index("Stage 17/18")


def test_the_tester_is_told_the_gmail_steps_in_order(rig) -> None:
    out = rig(_stages(14)).run(stdin=START + "\n\n\nmorning\nn\n").out
    add = out.index("Add a forwarding address → t1@example.com")
    read = out.index("GET https://n-t1.x.workers.dev/newsletters")
    confirm = out.index("mail-settings.google.com/mail/vf-")
    filt = out.index("Forward to t1@example.com")
    assert add < read < confirm < filt < out.index("Stage 16/18")
    assert "workers/newsletter/README.md" in out


def test_a_tester_email_adds_the_destination_address_steps(rig) -> None:
    run = rig(_stages(14), email="tester@example.com").run(stdin=START + "\n\n\n\nmorning\nn\n")
    assert "Email Routing" in run.out
    assert "add tester@example.com" in run.out


def test_the_routing_notice_never_prints_the_app_token(rig) -> None:
    r = rig(_stages(14))
    (r.repo / f".env.trial-{SLUG}").write_text(
        f"CLOUDFLARE_API_TOKEN={TOKEN}\nCYRIS_WORKER_TOKEN=app-tok-NOT-FOR-OUTPUT\n"
    )
    run = r.run(stdin=START + "\n\n\nmorning\nn\n")
    assert "app-tok-NOT-FOR-OUTPUT" not in run.out
    assert "app-tok-NOT-FOR-OUTPUT" not in run.calls


def test_the_routing_notice_needs_a_saved_newsletter_url(rig) -> None:
    run = rig(_stages(14), state={"TRIAL_NEWSLETTER_URL": None}).run(stdin=START)
    assert run.code == 1
    assert "--from 14" in run.out


def test_the_hand_over_keeps_the_newsletter_secrets_file(rig) -> None:
    run = rig(_stages(15)).run(stdin=START + "\n\n\nmorning\nn\n")
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
    run = rig(_stages(15)).run(stdin=START + "\n\n\nmorning\nn\n")
    assert "pbcopy" not in run.calls
    assert "password manager" not in run.out
    assert "Access one-time PIN" in run.out
