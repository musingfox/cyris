"""Release-image invariants that cannot be exercised by the application suite."""

import math
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = [pytest.mark.unit, pytest.mark.guard]

ROOT = Path(__file__).resolve().parents[1]


def git_sha_directives_are_placed_after_dependencies(dockerfile: str) -> bool:
    lines = dockerfile.splitlines()
    try:
        uv_sync = next(i for i, line in enumerate(lines) if line == "RUN uv sync --frozen --no-dev")
        arg = next(i for i, line in enumerate(lines) if line == "ARG GIT_SHA")
        env = next(i for i, line in enumerate(lines) if line == "ENV CYRIS_GIT_SHA=${GIT_SHA}")
        last_copy = max(i for i, line in enumerate(lines) if line.startswith("COPY "))
        cmd = next(i for i, line in enumerate(lines) if line.startswith("CMD "))
    except (StopIteration, ValueError):
        return False
    return arg > uv_sync and env > last_copy and env < cmd


def test_dockerfile_bakes_git_sha_after_dependency_layer() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "ARG GIT_SHA" in dockerfile.splitlines()
    assert "ENV CYRIS_GIT_SHA=${GIT_SHA}" in dockerfile.splitlines()
    assert git_sha_directives_are_placed_after_dependencies(dockerfile)


def test_git_sha_directives_reject_bad_placement() -> None:
    env_before_last_copy = "\n".join(
        [
            "RUN uv sync --frozen --no-dev",
            "ARG GIT_SHA",
            "ENV CYRIS_GIT_SHA=${GIT_SHA}",
            "COPY a b",
            'CMD ["x"]',
        ]
    )
    arg_above_dependency_layer = "\n".join(
        [
            "ARG GIT_SHA",
            "ENV CYRIS_GIT_SHA=${GIT_SHA}",
            "RUN uv sync --frozen --no-dev",
            "COPY a b",
            'CMD ["x"]',
        ]
    )
    assert not git_sha_directives_are_placed_after_dependencies(env_before_last_copy)
    assert not git_sha_directives_are_placed_after_dependencies(arg_above_dependency_layer)


def _release_job() -> dict:
    workflow = yaml.safe_load((ROOT / ".github/workflows/release-image.yml").read_text())
    return workflow["jobs"]["release"]


def _steps_by_id() -> dict[str, tuple[int, dict]]:
    return {
        step["id"]: (index, step)
        for index, step in enumerate(_release_job()["steps"])
        if "id" in step
    }


def test_release_workflow_reads_back_matching_registry_digests() -> None:
    steps = _steps_by_id()
    assert steps["push_release"][0] < steps["digest"][0] < steps["git_tag"][0]
    run = steps["digest"][1]["run"]
    assert "digest=" in run and "$GITHUB_OUTPUT" in run
    assert "steps.verify_sha.outputs.digest" in run
    assert ":release" in run
    login = _steps_by_id()["registry_login"][1]["run"]
    assert "jq -r .password | docker login" in login
    assert "-z" in run
    assert '"$sha_digest" != "$release_digest"' in run


def test_release_workflow_builds_pinned_amd64_image() -> None:
    workflow_text = (ROOT / ".github/workflows/release-image.yml").read_text()
    job = _release_job()
    build = _steps_by_id()["build"][1]["run"]
    assert "--platform linux/amd64" in build
    assert "--build-arg GIT_SHA=${{ github.sha }}" in build
    assert "wrangler containers build" not in "\n".join(
        step.get("run", "") for step in job["steps"]
    )
    assert workflow_text.split("env:", 1)[1].split("jobs:", 1)[0].find("IMAGE_NAME: cyris-app") >= 0
    assert "IMAGE_NAME" in build and "cyris-app" not in build


def test_release_workflow_runs_checks_before_building() -> None:
    steps = _steps_by_id()
    assert steps["check"][0] < steps["build"][0]
    assert "scripts/check.sh" in steps["check"][1]["run"]
    for step in _release_job()["steps"][: steps["check"][0]]:
        run = step.get("run", "")
        assert "docker build" not in run
        assert "wrangler containers push" not in run
        assert "git tag" not in run
    actions = {step.get("uses") for step in _release_job()["steps"][: steps["check"][0]]}
    assert {"actions/checkout@v4", "astral-sh/setup-uv@v5", "oven-sh/setup-bun@v2"} <= actions


def _release_workflow() -> dict:
    return yaml.safe_load((ROOT / ".github/workflows/release-image.yml").read_text())


def test_release_workflow_runs_on_every_push_to_main_and_on_dispatch() -> None:
    """Every push to main releases; no path filter decides which pushes count (ADR 0024)."""
    workflow = _release_workflow()
    # PyYAML reads a bare `on:` key as the boolean True.
    triggers = workflow[True]
    assert set(triggers) == {"push", "workflow_dispatch"}
    assert triggers["push"] == {"branches": ["main"]}
    deploy = triggers["workflow_dispatch"]["inputs"]["deploy"]
    assert deploy["type"] == "boolean"
    assert deploy["default"] is True


def _truthy(value: object) -> bool:
    """GitHub's falsy values: false, 0, -0, "", null and NaN."""
    if isinstance(value, float) and math.isnan(value):
        return False
    return value not in (False, 0, "", None)


def _github_expression(expression: str, context: dict) -> object:
    """Evaluate the subset of GitHub's expression syntax a job condition uses here.

    The rules are the documented ones
    (docs.github.com/en/actions/reference/workflows-and-actions/expressions): a
    comparison of two types converts both to numbers, null and false both to 0;
    strings compare case-insensitively; `&&` and `||` return an operand, not a
    boolean. A property that does not exist, `inputs.deploy` on a push, is an empty
    string (docs.github.com/en/actions/reference/workflows-and-actions/contexts).
    """
    body = expression.strip()
    if body.startswith("${{") and body.endswith("}}"):
        body = body[3:-2]
    tokens = re.findall(r"'(?:[^']|'')*'|==|!=|&&|\|\||[!()]|[\w.-]+", body)
    position = 0

    def number(value: object) -> float:
        if value is None:
            return 0.0
        if isinstance(value, bool):
            return float(value)
        if isinstance(value, str):
            if value == "":
                return 0.0
            if re.fullmatch(r"-?(0|[1-9]\d*)(\.\d+)?([eE][+-]?\d+)?", value.strip()):
                return float(value)
            return math.nan
        return float(value)

    def equal(left: object, right: object) -> bool:
        if type(left) is type(right) and isinstance(left, str):
            return left.lower() == right.lower()
        if type(left) is type(right) and not isinstance(left, float):
            return left == right
        return number(left) == number(right)

    def atom() -> object:
        nonlocal position
        token = tokens[position]
        position += 1
        if token == "(":
            value = either()
            position += 1
            return value
        if token == "!":
            return not _truthy(atom())
        if token.startswith("'"):
            return token[1:-1].replace("''", "'")
        if token in ("true", "false", "null"):
            return {"true": True, "false": False, "null": None}[token]
        if re.fullmatch(r"-?\d+(\.\d+)?", token):
            return float(token)
        value: object = context
        for part in token.split("."):
            value = value.get(part, "") if isinstance(value, dict) else ""
        return value

    def comparison() -> object:
        nonlocal position
        left = atom()
        while position < len(tokens) and tokens[position] in ("==", "!="):
            operator = tokens[position]
            position += 1
            left = equal(left, atom()) == (operator == "==")
        return left

    def both() -> object:
        nonlocal position
        left = comparison()
        while position < len(tokens) and tokens[position] == "&&":
            position += 1
            right = comparison()
            left = right if _truthy(left) else left
        return left

    def either() -> object:
        nonlocal position
        left = both()
        while position < len(tokens) and tokens[position] == "||":
            position += 1
            right = both()
            left = left if _truthy(left) else right
        return left

    value = either()
    assert position == len(tokens), f"unparsed tail in {expression!r}"
    return value


RELEASED = "1" * 40
NEWER = "2" * 40


def _guarded(context: dict, current: str = "true", attempt: str = "1") -> dict:
    """`context` once the head guard has answered: run outputs are strings."""
    return {
        **context,
        "github": {**context["github"], "sha": RELEASED, "run_attempt": "1"},
        "needs": {"current": {"outputs": {"current": current, "attempt": attempt}}},
    }


PUSH = {"github": {"event_name": "push"}, "inputs": {}}


def _dispatch(deploy: bool) -> dict:
    return {"github": {"event_name": "workflow_dispatch"}, "inputs": {"deploy": deploy}}


def test_the_expression_model_reproduces_the_null_equals_false_pitfall() -> None:
    """Red fixture: the condition that reads naturally skips the deploy on every push."""
    assert not _truthy(_github_expression("inputs.deploy != false", PUSH))
    assert _truthy(_github_expression("inputs.deploy == false", PUSH))
    assert _truthy(_github_expression("${{ github.event_name == 'PUSH' }}", PUSH))


def _deploy_job() -> dict:
    return _release_workflow()["jobs"]["deploy"]


def test_release_workflow_deploys_only_after_a_successful_release() -> None:
    """A separate job, so a red build and a red deploy stay two different red Xs."""
    job = _deploy_job()
    assert job["uses"] == "./.github/workflows/deploy.yml"
    assert set(job["needs"]) == {"release", "current"}
    assert _current_job()["needs"] in ("release", ["release"])
    # A status function would replace the implicit success() that holds the deploy back.
    for condition in (job["if"], _current_job()["if"]):
        assert not re.search(r"\b(always|failure|cancelled)\(", condition)


def test_release_workflow_deploys_on_push_and_by_default_skipping_only_on_false() -> None:
    condition = _deploy_job()["if"]
    assert _truthy(_github_expression(condition, _guarded(PUSH)))
    assert _truthy(_github_expression(condition, _guarded(_dispatch(True))))
    assert not _truthy(_github_expression(condition, _guarded(_dispatch(False))))


def _current_job() -> dict:
    return _release_workflow()["jobs"]["current"]


def test_release_workflow_asks_for_the_head_only_when_a_deploy_is_wanted() -> None:
    """A dispatch that unticks `deploy` has nothing for the guard to hold back."""
    condition = _current_job()["if"]
    assert _truthy(_github_expression(condition, PUSH))
    assert _truthy(_github_expression(condition, _dispatch(True)))
    assert not _truthy(_github_expression(condition, _dispatch(False)))


def test_release_workflow_deploys_only_while_its_commit_is_the_head_of_its_ref() -> None:
    """A re-run keeps its GITHUB_SHA, and the release queue's order is not guaranteed.

    Either can bring an older commit to the deploy job after a newer one; the guard's
    answer holds it back on a push and on a dispatch alike.
    """
    condition = _deploy_job()["if"]
    for wanted in (PUSH, _dispatch(True)):
        assert _truthy(_github_expression(condition, _guarded(wanted)))
        assert not _truthy(_github_expression(condition, _guarded(wanted, current="false")))
        assert not _truthy(_github_expression(condition, _guarded(wanted, current="")))


def test_release_workflow_ignores_a_head_answer_from_an_earlier_attempt() -> None:
    """Re-running only `deploy` would leave `current` as an earlier attempt answered it.

    That answer was given when the commit was still the head; it says nothing about now.
    """
    condition = _deploy_job()["if"]
    assert not _truthy(_github_expression(condition, _guarded(PUSH, attempt="0")))
    assert not _truthy(_github_expression(condition, _guarded(PUSH, attempt="")))


def test_release_workflow_reads_the_head_of_its_ref_from_the_remote() -> None:
    job = _current_job()
    assert job["permissions"] == {"contents": "read"}
    steps = job["steps"]
    # checkout leaves the job's token on `origin`, which ls-remote then reads.
    assert steps[0]["uses"] == "actions/checkout@v4"
    [head] = [step for step in steps if step.get("id") == "head"]
    assert 'git ls-remote origin "$GITHUB_REF"' in head["run"]
    assert job["outputs"] == {
        "current": "${{ steps.head.outputs.current }}",
        "attempt": "${{ steps.head.outputs.attempt }}",
    }


def _run_head_step(
    tmp_path: Path, ls_remote: str, ref: str = "refs/heads/main", fails: bool = False
) -> tuple[int, str, str, str]:
    """Run the guard's head step with `git` stubbed to answer `ls_remote`."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    answer = tmp_path / "ls-remote"
    answer.write_text(ls_remote)
    (bin_dir / "git").write_text(
        "#!/bin/sh\n"
        + ('echo "fatal: unable to access" >&2; exit 128\n' if fails else "")
        + '[ "$1 $2" = "ls-remote origin" ] || exit 2\n'
        + f'cat "{answer}"\n'
    )
    (bin_dir / "git").chmod(0o755)
    output, summary = tmp_path / "output", tmp_path / "summary"
    output.touch()
    summary.touch()
    [head] = [step for step in _current_job()["steps"] if step.get("id") == "head"]
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "GITHUB_SHA": RELEASED,
        "GITHUB_REF": ref,
        "GITHUB_RUN_ATTEMPT": "3",
        "GITHUB_OUTPUT": str(output),
        "GITHUB_STEP_SUMMARY": str(summary),
    }
    result = subprocess.run(
        ["bash", "-e", "-c", head["run"]], env=env, capture_output=True, text=True
    )
    return result.returncode, output.read_text(), summary.read_text(), result.stdout


def test_release_workflow_guard_passes_the_head_of_its_ref(tmp_path: Path) -> None:
    code, output, summary, stdout = _run_head_step(tmp_path, f"{RELEASED}\trefs/heads/main\n")
    assert code == 0
    assert "current=true\n" in output and "attempt=3\n" in output
    assert "::notice::" not in stdout


def test_release_workflow_guard_skips_a_commit_the_ref_has_moved_past(tmp_path: Path) -> None:
    """Skipped, not failed: the newer head deploys from its own run. Visible all the same."""
    code, output, summary, stdout = _run_head_step(tmp_path, f"{NEWER}\trefs/heads/main\n")
    assert code == 0
    assert "current=false\n" in output and "current=true" not in output
    [notice] = [line for line in stdout.splitlines() if line.startswith("::notice::")]
    assert RELEASED in notice and NEWER in notice
    assert RELEASED in summary and NEWER in summary


def test_release_workflow_guard_skips_when_the_ref_is_gone(tmp_path: Path) -> None:
    code, output, _, stdout = _run_head_step(tmp_path, "", ref="refs/heads/gone")
    assert code == 0
    assert "current=false\n" in output
    assert "::notice::" in stdout


def test_release_workflow_guard_matches_the_ref_exactly(tmp_path: Path) -> None:
    """ls-remote matches a pattern against a ref's tail, so other refs can answer too."""
    answer = f"{RELEASED}\trefs/heads/feature/refs/heads/main\n{NEWER}\trefs/heads/main\n"
    code, output, _, _ = _run_head_step(tmp_path, answer)
    assert code == 0
    assert "current=false\n" in output


def test_release_workflow_guard_peels_an_annotated_tag(tmp_path: Path) -> None:
    """A tag's ls-remote line names the tag object; `^{}` names the commit."""
    answer = f"{NEWER}\trefs/tags/v1\n{RELEASED}\trefs/tags/v1^{{}}\n"
    code, output, _, _ = _run_head_step(tmp_path, answer, ref="refs/tags/v1")
    assert code == 0
    assert "current=true\n" in output


def test_release_workflow_guard_fails_when_the_head_cannot_be_read(tmp_path: Path) -> None:
    """An unreadable head is red, not a quiet skip that looks like a newer push."""
    code, output, _, _ = _run_head_step(tmp_path, "", fails=True)
    assert code != 0
    assert "current=true" not in output


def test_release_workflow_deploys_the_commit_it_released_never_a_pinned_image() -> None:
    """No `with`: deploy.yml's resolve then reads `image/<sha7>` of the caller's commit.

    A called workflow's `github` context is the caller's, so GITHUB_SHA there is the
    commit this release just tagged, not whatever main's head has become since.
    """
    job = _deploy_job()
    assert "with" not in job
    assert job["permissions"] == {"contents": "read"}
    assert job["secrets"] == {
        name: f"${{{{ secrets.{name} }}}}"
        for name in (
            "CLOUDFLARE_CONTAINERS_TOKEN",
            "CYRIS_ACCESS_CLIENT_ID",
            "CYRIS_ACCESS_CLIENT_SECRET",
        )
    }


def test_release_workflow_pushes_annotated_image_tag() -> None:
    job = _release_job()
    checkout = next(s for s in job["steps"] if s.get("uses") == "actions/checkout@v4")
    assert checkout["with"]["fetch-tags"] is True
    steps = _steps_by_id()
    assert job["permissions"]["contents"] == "write"
    run = steps["git_tag"][1]["run"]
    assert "git tag -a" in run
    assert "image/" in run
    assert "${{ steps.digest.outputs.digest }}" in run
    assert "commit=${{ github.sha }}" in run
    assert "git push" in run
    assert " -f" not in run and "--force" not in run
    assert "git config user.name" in run
    assert "git config user.email" in run
    assert "git rev-parse" in run
    assert steps["git_tag"][0] == len(job["steps"]) - 1


def test_release_workflow_refuses_to_republish_a_published_commit() -> None:
    steps = _steps_by_id()
    assert steps["existing"][0] < steps["build"][0]
    for step_id in ("build", "smoke", "push_sha", "push_release", "digest"):
        assert steps[step_id][1]["if"] == "steps.existing.outputs.published != 'true'"
    assert steps["git_tag"][1].get("if") is None
    git_tag = steps["git_tag"][1]["run"]
    assert "steps.existing.outputs.digest" in git_tag
    assert "no longer serves" in git_tag


def test_release_workflow_requires_the_account_id_before_publishing() -> None:
    steps = _steps_by_id()
    assert steps["preflight"][0] < steps["push_sha"][0]
    run = steps["preflight"][1]["run"]
    assert "CLOUDFLARE_ACCOUNT_ID" in run
    assert "-z" in run and "exit 1" in run


def test_release_workflow_confirms_the_sha_tag_before_moving_release() -> None:
    steps = _steps_by_id()
    assert steps["push_sha"][0] < steps["verify_sha"][0] < steps["push_release"][0]
    run = steps["verify_sha"][1]["run"]
    assert "docker manifest inspect" in run
    assert "-z" in run and "exit 1" in run


def test_release_workflow_serialises_dispatches() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/release-image.yml").read_text())
    assert workflow["concurrency"]["group"] == "release-image"
    assert workflow["concurrency"]["cancel-in-progress"] is False


def test_release_workflow_pushes_sha_then_release_tag() -> None:
    steps = _steps_by_id()
    assert steps["push_sha"][0] < steps["push_release"][0]
    sha = steps["push_sha"][1]
    release = steps["push_release"][1]
    assert "wrangler containers push" in sha["run"]
    assert ":${{ github.sha }}" in sha["run"]
    assert "wrangler containers push" in release["run"]
    assert ":release" in release["run"]
    assert "docker build" not in sha["run"]
    assert "docker build" not in release["run"]
    expected_env = {
        "CLOUDFLARE_API_TOKEN": "${{ secrets.CLOUDFLARE_CONTAINERS_TOKEN }}",
        "CLOUDFLARE_ACCOUNT_ID": "${{ vars.CLOUDFLARE_ACCOUNT_ID }}",
    }
    assert sha["env"] == expected_env
    assert release["env"] == expected_env


def test_release_workflow_smokes_baked_sha_in_container() -> None:
    steps = _steps_by_id()
    assert steps["build"][0] < steps["smoke"][0] < steps["push_sha"][0]
    run = steps["smoke"][1]["run"]
    assert "CYRIS_GIT_SHA" in run
    assert "${{ github.sha }}" in run
    assert "docker run" in run
    assert "sh -c" in run or "--entrypoint" in run
    assert "cyris --help" in run
    assert "pkgutil.walk_packages(cyris.__path__" in run
    assert "importlib.import_module(name)" in run
    assert '".__main__"' in run


def test_spec_binds_git_sha_invariant_to_release_image_tests() -> None:
    text = (ROOT / "docs/spec/image-carries-its-git-sha.md").read_text()
    frontmatter = text.split("---", 2)[1]
    verify_lines = [line for line in frontmatter.splitlines() if line.startswith("verify: ")]
    assert len(verify_lines) == 1
    verify = verify_lines[0].removeprefix("verify: ")
    assert verify.startswith("check:")
    command = verify.removeprefix("check:").strip()
    assert "\n" not in command
    assert "tests/test_release_image.py" in command


def test_wrangler_toml_stays_fork_neutral() -> None:
    wrangler = (ROOT / "wrangler.toml").read_text()
    assert 'image = "./Dockerfile"' in wrangler
    assert "registry.cloudflare.com" not in wrangler
    assert "account_id" not in wrangler


def test_git_sha_is_not_a_deploy_form_input() -> None:
    import json
    import re

    env_names = set(re.findall(r"^([A-Z][A-Z0-9_]+)=", (ROOT / ".env.example").read_text(), re.M))
    bindings = set(json.loads((ROOT / "package.json").read_text())["cloudflare"]["bindings"])
    worker_names = set(
        re.findall(
            r"^\s+([A-Z][A-Z0-9_]+): env\.",
            (ROOT / "workers/app/src/index.js").read_text(),
            re.M,
        )
    )
    deploy_inputs = env_names | bindings | worker_names
    assert {"CYRIS_GIT_SHA", "GIT_SHA", "CLOUDFLARE_CONTAINERS_TOKEN"}.isdisjoint(deploy_inputs)


def _run_digest_step(
    tmp_path: Path, release_reads: list[str], sha: str = "sha256:" + "a" * 64
) -> tuple[int, str, str]:
    """Run the workflow's digest step with `docker` and `sleep` stubbed.

    Each `docker manifest inspect :release` answers the next digest in
    `release_reads`, the last one repeating; `error` makes that read fail. The
    shell flags are GitHub's own for a `run` step, pipefail included.
    """
    if shutil.which("jq") is None:
        pytest.skip("jq is not on PATH")
    reads = tmp_path / "reads"
    reads.write_text("\n".join(release_reads) + "\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(
        "#!/bin/sh\n"
        f'next=$(head -n 1 "{reads}")\n'
        f'[ "$(wc -l < "{reads}")" -gt 1 ] && sed -i.bak 1d "{reads}"\n'
        '[ "$next" = error ] && { echo "manifest unknown" >&2; exit 1; }\n'
        'printf \'{"Descriptor": {"digest": "%s"}}\' "$next"\n'
    )
    (bin_dir / "sleep").write_text("#!/bin/sh\nexit 0\n")
    for stub in bin_dir.iterdir():
        stub.chmod(0o755)
    script = _steps_by_id()["digest"][1]["run"].replace(
        "${{ steps.verify_sha.outputs.digest }}", sha
    )
    output = tmp_path / "output"
    output.touch()
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "CLOUDFLARE_ACCOUNT_ID": "acct",
        "IMAGE_NAME": "img",
        "GITHUB_OUTPUT": str(output),
    }
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
        env=env,
        capture_output=True,
        text=True,
    )
    return result.returncode, output.read_text(), result.stdout


def test_the_digest_step_waits_out_a_stale_release_read(tmp_path: Path) -> None:
    # 2026-10-08, twice: the push printed the new digest, and the read right after
    # it still answered an older one.
    old, new = "sha256:" + "0" * 64, "sha256:" + "a" * 64
    code, output, stdout = _run_digest_step(tmp_path, [old, old, new])
    assert code == 0
    assert output == f"digest={new}\n"
    assert "::warning::" not in stdout


def test_a_release_tag_that_never_moves_warns_and_records_the_sha_digest(tmp_path: Path) -> None:
    """Deploys read the `image/<sha7>` git tag, so a stale `:release` must not withhold it.

    2026-10-08: three of four releases saw `:release` answer the previous digest for
    over 50 seconds after the push that moved it, while the sha tag read back right.
    """
    old, new = "sha256:" + "0" * 64, "sha256:" + "a" * 64
    code, output, stdout = _run_digest_step(tmp_path, [old])
    assert code == 0
    assert output == f"digest={new}\n"
    [warning] = [line for line in stdout.splitlines() if line.startswith("::warning::")]
    assert old in warning and new in warning


def test_an_unreadable_release_tag_warns_and_records_the_sha_digest(tmp_path: Path) -> None:
    new = "sha256:" + "a" * 64
    code, output, stdout = _run_digest_step(tmp_path, ["error"])
    assert code == 0
    assert output == f"digest={new}\n"
    assert "::warning::" in stdout


def test_the_digest_step_fails_without_a_sha_digest(tmp_path: Path) -> None:
    code, output, _ = _run_digest_step(tmp_path, ["sha256:" + "a" * 64], sha="")
    assert code != 0
    assert output == ""
