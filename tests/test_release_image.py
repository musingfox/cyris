"""Release-image invariants that cannot be exercised by the application suite."""

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


def test_release_workflow_is_dispatch_only() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/release-image.yml").read_text())
    triggers = workflow.get("on", workflow.get(True))
    assert set(triggers) == {"workflow_dispatch"}
    assert "push" not in triggers
    assert "pull_request" not in triggers


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
