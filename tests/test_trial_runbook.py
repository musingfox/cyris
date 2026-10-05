"""docs/trial-deployment.md: following it cannot touch production.

A trial runs from the same checkout as production, so one wrangler command without the
trial's own config redeploys, re-secrets or re-seeds the production Worker. The runbook
is prose, so these rules read its shell blocks the way the operator pastes them and
refuse the lines that would reach production. Each rule has a red case: the real runbook
with one line injected, moved or changed, which the checker must name.
"""

import re
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.guard]

ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = ROOT / "docs/trial-deployment.md"
GUIDE = ROOT / "docs/install-cloudflare.md"
D1_ID = "0948e71f-2374-4e4e-8f30-44890139011b"
SECRETS_PATH_SLUG = "alice"
TOKEN = "TRIAL_WORKER_TOKEN"
REQUIRED_ANCHORS = (
    "4-write-the-secrets-file",
    "running-the-cli-against-the-deployment",
    "optional-workers",
)


def code_lines(text: str) -> list[str]:
    """Every command in the fenced sh/bash blocks, continuation lines joined."""
    lines: list[str] = []
    for block in re.findall(r"^```(?:sh|bash)\n(.*?)^```", text, re.M | re.S):
        joined = re.sub(r"\\\n\s*", " ", block)
        lines += [ln.strip() for ln in joined.splitlines() if ln.strip()]
    return [ln for ln in lines if not ln.startswith("#")]


def as_doc(lines: list[str]) -> str:
    return "```sh\n" + "\n".join(lines) + "\n```\n"


def _unquote(value: str) -> str:
    return value.strip("\"'")


def _option(line: str, flag: str) -> str | None:
    found = re.search(rf"{re.escape(flag)}[ =](\"[^\"]*\"|'[^']*'|\S+)", line)
    return _unquote(found.group(1)) if found else None


def _is_wrangler(line: str) -> bool:
    return re.search(r"(^|[\s/])wrangler(@\S+)?(\s|$)", line) is not None


def _is_deploy(line: str) -> bool:
    return _is_wrangler(line) and re.search(r"\sdeploy(\s|$)", line) is not None


def _kind(line: str) -> str:
    config = _option(line, "--config") or ""
    if "/" not in config:
        return "app"
    return "promote" if config.startswith("workers/promote/") else "rss"


def _commands(line: str) -> list[str]:
    return [c.strip() for c in re.split(r"&&|\|\||;|\|", line) if c.strip()]


def _first_failure(lines: list[str]) -> str | None:
    commands = [(ln, c) for ln in lines for c in _commands(ln)]
    wrangler = [(ln, c) for ln, c in commands if _is_wrangler(c)]
    for ln, c in wrangler:
        if "--env-file /dev/null" not in c:
            return f"(a) wrangler without --env-file /dev/null: {ln}"
    deploys = [(ln, c) for ln, c in wrangler if _is_deploy(c)]
    for ln, c in deploys:
        if not (_option(c, "--config") or "").rsplit("/", 1)[-1].startswith("wrangler.trial-"):
            return f"(b) wrangler deploy without a wrangler.trial- --config: {ln}"
    for ln, c in wrangler:
        if re.search(r"\ssecret(\s|$)", c):
            return f"(c) wrangler secret: {ln}"
    deploys = [c for _, c in deploys]
    pushes = [ln for ln in lines if re.search(r"cyris (settings|sources) push", ln)]
    for ln in pushes:
        ok = (
            ln.find("env -i") != -1
            and ln.find("env -i") < ln.find("uv run cyris")
            and '--config "$TRIAL_DIR/' in ln
            and '--sources "$TRIAL_DIR/' in ln
        )
        if not ok:
            return f"(d) push outside the trial directory or without env -i: {ln}"
    for i, ln in enumerate(lines):
        if "sources push" in ln and not any("settings push" in p for p in lines[:i]):
            return f"(e) sources push with no settings push before it: {ln}"
    live = [ln for ln in deploys if "--dry-run" not in ln]
    dry = [ln for ln in deploys if "--dry-run" in ln]
    for kind in ("app", "promote", "rss"):
        if not any(_kind(ln) == kind for ln in live):
            return f"(f) no non-dry-run {kind} deploy"
        if not any(_kind(ln) == kind for ln in dry):
            return f"(f) no dry-run {kind} deploy"
    if sum(_kind(ln) == "app" for ln in live) < 2:
        return "(f) fewer than two non-dry-run app deploys"
    for word in ("settings push", "sources push"):
        if not any(word in ln for ln in pushes):
            return f"(f) no {word} line"
    for ln in lines:
        path = _option(ln, "--secrets-file")
        if path is None:
            continue
        path = re.sub(r"\$\{?SLUG\}?", SECRETS_PATH_SLUG, path)
        if subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT).returncode != 0:
            return f"(g) --secrets-file path is not gitignored: {ln}"
    ids = [_option(ln, "--d1-id") for ln in lines if "--d1-id" in ln]
    if not ids or any(i != "$TRIAL_D1_ID" for i in ids):
        return f'(h) --d1-id is not "$TRIAL_D1_ID": {ids}'
    for receipt in (
        "CYRIS_STORE_DATABASE_ID=$TRIAL_D1_ID",
        "RSS_TOKEN=$TRIAL_WORKER_TOKEN",
        "CYRIS_WORKER_TOKEN=$TRIAL_WORKER_TOKEN",
    ):
        if not any("grep" in ln and receipt in ln for ln in lines):
            return f"(h) no grep line checking {receipt}"
    for i, ln in enumerate(lines):
        if _is_deploy(ln) and "--dry-run" not in ln and _kind(ln) == "rss":
            if not any("sources push" in p for p in lines[:i]):
                return f"(i) rss deploy before any sources push: {ln}"
            if not any(
                _is_deploy(a) and "--dry-run" not in a and _kind(a) == "app" for a in lines[i + 1 :]
            ):
                return f"(i) no app deploy after the rss deploy: {ln}"
    for ln in lines:
        for cmd in _commands(ln):
            named = re.search(rf"\$\{{?{TOKEN}\b", cmd) is not None
            if not named:
                continue
            words = cmd.split()
            if words[0] == "echo":
                return f"(j) echo names the token: {ln}"
            if words[0] == "printf" and ">" not in cmd:
                return f"(j) printf names the token without redirecting: {ln}"
            if words[0] == "grep" and not any(
                w.startswith("-") and not w.startswith("--") and "q" in w for w in words
            ):
                return f"(j) grep names the token without -q: {ln}"
    return None


def assert_runbook_safe(text: str) -> None:
    failure = _first_failure(code_lines(text))
    assert failure is None, failure


def slug(heading: str) -> str:
    kept = re.sub(r"[^\w\- ]", "", heading.lower())
    return kept.replace(" ", "-")


def _guide_anchors(guide: str) -> set[str]:
    outside_fences = re.sub(r"^```.*?^```", "", guide, flags=re.M | re.S)
    return {slug(h) for h in re.findall(r"^#+ (.+)$", outside_fences, re.M)}


def assert_points_not_copies(runbook: str, guide: str) -> None:
    anchors = _guide_anchors(guide)
    linked = re.findall(r"install-cloudflare\.md#([\w-]+)", runbook)
    for anchor in linked:
        assert anchor in anchors, f"dead anchor {anchor}"
    for anchor in REQUIRED_ANCHORS:
        assert anchor in linked, f"the runbook does not link {anchor}"
    for ln in code_lines(runbook):
        assert not re.match(r"(export\s+)?CYRIS_UI_TOKEN=", ln), f"copied secret line: {ln}"


def _runbook_lines() -> list[str]:
    return code_lines(RUNBOOK.read_text(encoding="utf-8"))


def _index(lines: list[str], *needles: str) -> int:
    return next(i for i, ln in enumerate(lines) if all(n in ln for n in needles))


def _moved_above(lines: list[str], mover: int, target: int) -> list[str]:
    rest = lines[:mover] + lines[mover + 1 :]
    return rest[:target] + [lines[mover]] + rest[target:]


def _with(extra: str) -> str:
    return as_doc(_runbook_lines() + [extra])


def _raises(text: str, rule: str) -> None:
    with pytest.raises(AssertionError, match=re.escape(f"({rule})")):
        assert_runbook_safe(text)


def test_the_runbook_is_safe() -> None:
    assert_runbook_safe(RUNBOOK.read_text(encoding="utf-8"))


TRIAL_DEPLOY = 'bunx wrangler deploy --config "wrangler.trial-$SLUG.toml" --env-file /dev/null'


@pytest.mark.parametrize(
    ("line", "rule"),
    [
        ("npx wrangler@latest d1 list", "a"),
        ("bunx wrangler@4.127.1 deploy --env-file /dev/null", "b"),
        ("node_modules/.bin/wrangler deploy --env-file /dev/null", "b"),
        ("./node_modules/.bin/wrangler secret put CYRIS_UI_TOKEN --env-file /dev/null", "c"),
        (f"{TRIAL_DEPLOY}; bunx wrangler deploy --env-file /dev/null", "b"),
        (f"{TRIAL_DEPLOY} && bunx wrangler d1 list", "a"),
        (f"{TRIAL_DEPLOY} && bunx wrangler secret put X --env-file /dev/null", "c"),
    ],
)
def test_versioned_and_path_qualified_wrangler_is_still_wrangler(line: str, rule: str) -> None:
    _raises(_with(line), rule)


def test_a_deploy_without_a_config_is_refused() -> None:
    _raises(_with("bunx wrangler deploy --env-file /dev/null"), "b")


def test_a_wrangler_command_without_the_env_file_guard_is_refused() -> None:
    _raises(_with("bunx wrangler d1 list"), "a")


def test_wrangler_secret_is_refused() -> None:
    _raises(_with("bunx wrangler secret put CYRIS_UI_TOKEN --env-file /dev/null"), "c")


def test_a_push_outside_the_trial_directory_is_refused() -> None:
    line = 'uv run cyris sources push --config "$TRIAL_DIR/settings.toml" '
    _raises(_with(line + '--sources "$TRIAL_DIR/sources.yaml"'), "d")


def test_sources_push_before_settings_push_is_refused() -> None:
    lines = _runbook_lines()
    moved = _moved_above(lines, _index(lines, "sources push"), _index(lines, "settings push"))
    _raises(as_doc(moved), "e")


def test_a_runbook_with_no_commands_is_refused() -> None:
    _raises("```sh\n```\n", "f")


def test_a_secrets_file_git_would_track_is_refused() -> None:
    line = 'bunx wrangler deploy --config "wrangler.trial-$SLUG.toml" --env-file /dev/null '
    _raises(_with(line + '--secrets-file "secrets-$SLUG.txt"'), "g")


def test_production_rss_config_is_refused() -> None:
    line = "bunx wrangler deploy --config workers/rss/wrangler.toml --env-file /dev/null "
    _raises(_with(line + '--secrets-file ".env.trial-$SLUG-rss"'), "b")


def test_rss_deploy_before_sources_push_is_refused() -> None:
    lines = _runbook_lines()
    rss = next(
        i
        for i, ln in enumerate(lines)
        if "deploy" in ln and "workers/rss/" in ln and "--dry" not in ln
    )
    moved = _moved_above(lines, rss, _index(lines, "sources push"))
    _raises(as_doc(moved), "i")


def test_app_deploy_before_rss_deploy_is_refused() -> None:
    lines = _runbook_lines()
    apps = [
        i
        for i, ln in enumerate(lines)
        if _is_deploy(ln) and "--dry-run" not in ln and _kind(ln) == "app"
    ]
    rss = next(
        i
        for i, ln in enumerate(lines)
        if "deploy" in ln and "workers/rss/" in ln and "--dry" not in ln
    )
    _raises(as_doc(_moved_above(lines, apps[-1], rss)), "i")


def test_a_hardcoded_d1_id_is_refused() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    assert '--d1-id "$TRIAL_D1_ID"' in text
    _raises(text.replace('--d1-id "$TRIAL_D1_ID"', f"--d1-id {D1_ID}"), "h")


def test_a_missing_worker_token_receipt_is_refused() -> None:
    lines = [ln for ln in _runbook_lines() if "CYRIS_WORKER_TOKEN=$TRIAL_WORKER_TOKEN" not in ln]
    _raises(as_doc(lines), "h")


def test_each_config_needs_its_own_dry_run() -> None:
    lines = [
        ln
        for ln in _runbook_lines()
        if not ("--dry-run" in ln and "workers/rss/" in ln and "deploy" in ln)
    ]
    _raises(as_doc(lines), "f")


def test_echoing_the_token_is_refused() -> None:
    _raises(_with('echo "$TRIAL_WORKER_TOKEN"'), "j")


def test_a_loud_token_receipt_is_refused() -> None:
    lines = _runbook_lines()
    i = _index(lines, "grep", "RSS_TOKEN=$TRIAL_WORKER_TOKEN")
    lines[i] = lines[i].replace("grep -qx", "grep -x")
    assert lines[i].count("grep -x") == 1
    _raises(as_doc(lines), "j")


def test_printing_the_token_with_printf_is_refused() -> None:
    _raises(_with("printf '%s\\n' \"$TRIAL_WORKER_TOKEN\""), "j")
