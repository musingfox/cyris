"""scripts/trial-wizard.sh cannot touch production; docs/trial-deployment.md points, not copies.

A trial runs from the same checkout as production, so one wrangler command without the
trial's own config redeploys, re-secrets or re-seeds the production Worker. These rules
read the wizard's source and refuse the lines that would reach production. Each rule has
a red case: the real wizard with one line injected or one flag removed, which the checker
must name.
"""

import re
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.guard]

ROOT = Path(__file__).resolve().parents[1]
WIZARD = ROOT / "scripts/trial-wizard.sh"
RUNBOOK = ROOT / "docs/trial-deployment.md"
GUIDE = ROOT / "docs/install-cloudflare.md"
SECRETS_PATH_SLUG = "alice"
TOKENS = ("TRIAL_WORKER_TOKEN", "TRIAL_PROMOTE_TOKEN", "CLOUDFLARE_API_TOKEN", "GEMINI_API_KEY")
PRINTERS = ("echo", "say", "note", "warn", "step")
REQUIRED_ANCHORS = (
    "4-write-the-secrets-file",
    "running-the-cli-against-the-deployment",
    "optional-workers",
)
# Every top-level command `wrangler --help` lists (4.127.1). Matching a command rather than
# the bare word keeps prose such as "Log wrangler in" from counting as a call.
_SUBCOMMANDS = (
    "agent-memory",
    "ai",
    "ai-search",
    "artifacts",
    "auth",
    "browser",
    "cert",
    "complete",
    "containers",
    "d1",
    "delete",
    "deploy",
    "deployments",
    "dev",
    "dispatch-namespace",
    "docs",
    "email",
    "flagship",
    "hyperdrive",
    "init",
    "kv",
    "login",
    "logout",
    "mtls-certificate",
    "pages",
    "pipelines",
    "preview",
    "queues",
    "r2",
    "rollback",
    "secret",
    "secrets-store",
    "setup",
    "tail",
    "triggers",
    "tunnel",
    "turnstile",
    "types",
    "vectorize",
    "versions",
    "vpc",
    "websearch",
    "whoami",
    "workflows",
)
_SUBCOMMAND = "(" + "|".join(re.escape(s) for s in _SUBCOMMANDS) + ")"
WRANGLER = re.compile(rf"wrangler(@\S+)?\s+{_SUBCOMMAND}(\s|$)")
# The commands that change what a Worker serves, so each must name a trial config.
DEPLOY = re.compile(
    r"wrangler(@\S+)?\s+(deploy|versions\s+(deploy|upload)|triggers\s+deploy|rollback|delete)(\s|$)"
)


def script_lines(text: str) -> list[str]:
    """Every logical line of the script, continuations joined, comment lines dropped."""
    joined = re.sub(r"\\\n\s*", " ", text)
    lines = [ln.strip() for ln in joined.splitlines()]
    return [ln for ln in lines if ln and not ln.startswith("#")]


def _paths(lines: list[str]) -> dict[str, str]:
    """The script's `NAME="...$SLUG..."` assignments, so `--config "$APP_CFG"` can be read."""
    found = (re.fullmatch(r'([A-Z_]+)="([^"]*\$SLUG[^"]*)"', ln) for ln in lines)
    return {m.group(1): m.group(2) for m in found if m}


def _resolve(line: str, paths: dict[str, str]) -> str:
    for name, value in paths.items():
        line = re.sub(rf"\$\{{?{name}\b\}}?", value, line)
    return line


def _unquote(value: str) -> str:
    return value.strip("\"'")


def _option(line: str, flag: str) -> str | None:
    found = re.search(rf"{re.escape(flag)}[ =](\"[^\"]*\"|'[^']*'|\S+)", line)
    return _unquote(found.group(1)) if found else None


def _commands(line: str) -> list[str]:
    return [c.strip() for c in re.split(r"&&|\|\||;|\|", line) if c.strip()]


def _first_failure(lines: list[str]) -> str | None:
    paths = _paths(lines)
    commands = [(ln, _resolve(c, paths)) for ln in lines for c in _commands(ln)]
    wrangler = [(ln, c) for ln, c in commands if WRANGLER.search(c)]
    for ln, c in wrangler:
        if "--env-file /dev/null" not in c:
            return f"(a) wrangler without --env-file /dev/null: {ln}"
    for ln, c in wrangler:
        if DEPLOY.search(c):
            config = _option(c, "--config") or ""
            if not config.rsplit("/", 1)[-1].startswith("wrangler.trial-"):
                return f"(b) wrangler deploy without a wrangler.trial- --config: {ln}"
        if re.search(r"wrangler(@\S+)?\s+secret(\s|$)", c):
            return f"(c) wrangler secret: {ln}"
    for ln in lines:
        if not re.search(r"uv run cyris (settings|sources) push", ln):
            continue
        ok = (
            ln.find("env -i") != -1
            and ln.find("env -i") < ln.find("uv run cyris")
            and '--config "$TRIAL_DIR/' in ln
            and '--sources "$TRIAL_DIR/' in ln
        )
        if not ok:
            return f"(d) push outside the trial directory or without env -i: {ln}"
    for ln in lines:
        path = _option(_resolve(ln, paths), "--secrets-file")
        if path is None:
            continue
        path = re.sub(r"\$\{?SLUG\}?", SECRETS_PATH_SLUG, path)
        if subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT).returncode != 0:
            return f"(g) --secrets-file path is not gitignored: {ln}"
    for ln in lines:
        for cmd in _commands(ln):
            if not any(re.search(rf"\$\{{?{t}\b", cmd) for t in TOKENS):
                continue
            words = cmd.split()
            if words[0] in PRINTERS:
                return f"(j) {words[0]} names a token: {ln}"
            piped = re.search(rf"{re.escape(cmd)}\s*\|\s*uv run python scripts/", ln)
            if words[0] == "printf" and ">" not in cmd and not piped:
                return f"(j) printf names a token without redirecting: {ln}"
            if words[0] == "grep" and not any(
                w.startswith("-") and not w.startswith("--") and "q" in w for w in words
            ):
                return f"(j) grep names a token without -q: {ln}"
    return None


def assert_wizard_safe(text: str) -> None:
    failure = _first_failure(script_lines(text))
    assert failure is None, failure


def _wizard() -> str:
    return WIZARD.read_text(encoding="utf-8")


def _raises(text: str, rule: str) -> None:
    with pytest.raises(AssertionError, match=re.escape(f"({rule})")):
        assert_wizard_safe(text)


def test_the_wizard_is_safe() -> None:
    assert_wizard_safe(_wizard())


def _wrangler_lines() -> list[int]:
    lines = _wizard().split("\n")
    return [i for i, ln in enumerate(lines) if WRANGLER.search(ln)]


def test_the_wizard_calls_wrangler() -> None:
    assert len(_wrangler_lines()) >= 10


@pytest.mark.parametrize("index", _wrangler_lines())
def test_dropping_the_env_file_guard_anywhere_is_refused(index: int) -> None:
    lines = _wizard().split("\n")
    assert "--env-file /dev/null" in lines[index], lines[index]
    lines[index] = lines[index].replace(" --env-file /dev/null", "")
    _raises("\n".join(lines), "a")


def _deploy_lines() -> list[int]:
    lines = _wizard().split("\n")
    return [i for i in _wrangler_lines() if re.search(r"wrangler deploy\b", lines[i])]


def test_the_wizard_deploys_all_three_workers() -> None:
    lines = _wizard().split("\n")
    configs = {_option(lines[i], "--config") for i in _deploy_lines()}
    assert configs == {"$APP_CFG", "$PROMOTE_CFG", "$RSS_CFG"}


@pytest.mark.parametrize("index", _deploy_lines())
def test_dropping_the_trial_config_from_any_deploy_is_refused(index: int) -> None:
    lines = _wizard().split("\n")
    lines[index] = re.sub(r' --config "\$[A-Z_]+"', "", lines[index])
    assert "--config" not in lines[index], lines[index]
    _raises("\n".join(lines), "b")


TRIAL_DEPLOY = 'bunx wrangler deploy --config "$APP_CFG" --env-file /dev/null'


@pytest.mark.parametrize(
    ("line", "rule"),
    [
        ("npx wrangler@latest d1 list", "a"),
        ("bunx wrangler@4.127.1 deploy --env-file /dev/null", "b"),
        ("node_modules/.bin/wrangler deploy --env-file /dev/null", "b"),
        ("bunx wrangler deploy --config wrangler.toml --env-file /dev/null", "b"),
        ("bunx wrangler deploy --config workers/rss/wrangler.toml --env-file /dev/null", "b"),
        ("./node_modules/.bin/wrangler secret put CYRIS_UI_TOKEN --env-file /dev/null", "c"),
        (f"{TRIAL_DEPLOY}; bunx wrangler deploy --env-file /dev/null", "b"),
        (f"{TRIAL_DEPLOY} && bunx wrangler d1 list", "a"),
        ("WHO=$(bunx wrangler whoami 2>&1 || true)", "a"),
        ("bunx wrangler triggers deploy --config wrangler.toml", "a"),
        ('bunx wrangler kv key list --namespace-id "$TRIAL_KV_ID"', "a"),
        ("bunx wrangler versions deploy --env-file /dev/null", "b"),
        ("bunx wrangler rollback --env-file /dev/null", "b"),
        ('gated x "Deploy?" bunx wrangler deploy --config "$APP_CFG" --env-file /dev/null', None),
    ],
)
def test_wrangler_in_any_shape_is_still_wrangler(line: str, rule: str | None) -> None:
    text = _wizard() + "\n" + line + "\n"
    if rule is None:
        assert_wizard_safe(text)
    else:
        _raises(text, rule)


def test_a_push_outside_the_trial_directory_is_refused() -> None:
    line = 'uv run cyris sources push --config "$TRIAL_DIR/settings.toml" '
    _raises(_wizard() + "\n" + line + '--sources "$TRIAL_DIR/sources.yaml"\n', "d")


def test_a_secrets_file_git_would_track_is_refused() -> None:
    line = f'{TRIAL_DEPLOY} --secrets-file "secrets-$SLUG.txt"'
    _raises(_wizard() + "\n" + line + "\n", "g")


@pytest.mark.parametrize(
    "line",
    [
        'echo "$TRIAL_WORKER_TOKEN"',
        'say "token: $CLOUDFLARE_API_TOKEN"',
        "printf '%s\\n' \"$TRIAL_PROMOTE_TOKEN\"",
        "printf '%s\\n' \"$CLOUDFLARE_API_TOKEN\" | tee x.log",
        'grep -x "RSS_TOKEN=$TRIAL_WORKER_TOKEN" "$RSS_ENV"',
    ],
)
def test_printing_a_token_is_refused(line: str) -> None:
    _raises(_wizard() + "\n" + line + "\n", "j")


def slug(heading: str) -> str:
    kept = re.sub(r"[^\w\- ]", "", heading.lower())
    return kept.replace(" ", "-")


def _guide_anchors(guide: str) -> set[str]:
    outside_fences = re.sub(r"^```.*?^```", "", guide, flags=re.M | re.S)
    return {slug(h) for h in re.findall(r"^#+ (.+)$", outside_fences, re.M)}


def _code_lines(text: str) -> list[str]:
    lines: list[str] = []
    for block in re.findall(r"^```(?:sh|bash)\n(.*?)^```", text, re.M | re.S):
        lines += [ln.strip() for ln in block.splitlines() if ln.strip()]
    return lines


def assert_points_not_copies(runbook: str, guide: str) -> None:
    anchors = _guide_anchors(guide)
    linked = re.findall(r"install-cloudflare\.md#([\w-]+)", runbook)
    for anchor in linked:
        assert anchor in anchors, f"dead anchor {anchor}"
    for anchor in REQUIRED_ANCHORS:
        assert anchor in linked, f"the runbook does not link {anchor}"
    for ln in _code_lines(runbook):
        assert not re.match(r"(export\s+)?CYRIS_UI_TOKEN=", ln), f"copied secret line: {ln}"
        assert not WRANGLER.search(ln), f"a wrangler step the wizard owns: {ln}"


def _runbook() -> str:
    return RUNBOOK.read_text(encoding="utf-8")


def _guide() -> str:
    return GUIDE.read_text(encoding="utf-8")


def test_the_runbook_points_at_the_install_guide() -> None:
    assert_points_not_copies(_runbook(), _guide())


def test_a_dead_install_guide_anchor_is_named() -> None:
    dead = _runbook() + "\n[x](install-cloudflare.md#4-write-the-secret-file)\n"
    with pytest.raises(AssertionError, match="4-write-the-secret-file"):
        assert_points_not_copies(dead, _guide())


def test_a_copied_ui_token_line_is_refused() -> None:
    text = _runbook() + "\n```sh\nCYRIS_UI_TOKEN=abc\n```\n"
    with pytest.raises(AssertionError, match="CYRIS_UI_TOKEN"):
        assert_points_not_copies(text, _guide())


def test_a_wrangler_step_copied_into_the_runbook_is_refused() -> None:
    text = _runbook() + "\n```sh\nbunx wrangler d1 create x --env-file /dev/null\n```\n"
    with pytest.raises(AssertionError, match="the wizard owns"):
        assert_points_not_copies(text, _guide())


def test_slug_follows_the_github_rule() -> None:
    assert slug("4. Write the secrets file") == "4-write-the-secrets-file"
    assert slug("6. Log in and fill `/settings`") == "6-log-in-and-fill-settings"
    assert slug("Optional Workers") == "optional-workers"


def test_dropping_every_optional_workers_link_is_named() -> None:
    text = re.sub(r"install-cloudflare\.md#optional-workers", "install-cloudflare.md", _runbook())
    with pytest.raises(AssertionError, match="optional-workers"):
        assert_points_not_copies(text, _guide())


def test_architecture_points_at_the_trial_tooling() -> None:
    doc = (ROOT / "docs/architecture.md").read_text(encoding="utf-8")
    for path in (
        "scripts/provision_trial.py",
        "scripts/trial-wizard.sh",
        "docs/trial-deployment.md",
    ):
        assert path in doc, f"docs/architecture.md does not mention {path}"
        assert (ROOT / path).is_file(), f"{path} does not exist"
