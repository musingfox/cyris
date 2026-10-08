"""The deploy half of the release path: a reference to a published image.

Separate from `test_release_image.py` on purpose — that module's two `-k`
selections are what bind the release specs, and widening them would report
those specs as violated whenever something here changes.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = [pytest.mark.unit, pytest.mark.guard]

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/derive-wrangler-config.sh"


def _derive(tmp_path: Path, tag: str, src: Path | None = None) -> str:
    out = tmp_path / "wrangler.deploy.toml"
    script = SCRIPT
    if src is not None:
        # The script reads wrangler.toml beside itself, so a copied tree is how
        # a different source file is fed to it.
        script = tmp_path / "scripts/derive-wrangler-config.sh"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
        script.chmod(0o755)
        (tmp_path / "wrangler.toml").write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    done = subprocess.run(
        [str(script), "acct-under-test", "cyris-app", tag, str(out)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert done.returncode == 0
    return out.read_text(encoding="utf-8")


def _image_line(text: str) -> str:
    return next(line for line in text.splitlines() if line.startswith("image = "))


def test_the_derived_config_points_at_the_registry(tmp_path: Path) -> None:
    line = _image_line(_derive(tmp_path, "release"))

    assert line == 'image = "registry.cloudflare.com/acct-under-test/cyris-app:release"'


def test_a_digest_pins_one_exact_image(tmp_path: Path) -> None:
    """Pinning is this path's job: the build workflow refuses to republish a commit."""
    line = _image_line(_derive(tmp_path, "sha256:" + "a" * 64))

    assert line == f'image = "registry.cloudflare.com/acct-under-test/cyris-app@sha256:{"a" * 64}"'


def test_nothing_but_the_image_line_changes(tmp_path: Path) -> None:
    """Every other setting is the deployment's behaviour, and this step is not about it."""
    derived = _derive(tmp_path, "release").splitlines()
    tracked = (ROOT / "wrangler.toml").read_text(encoding="utf-8").splitlines()

    assert [ln for ln in derived if not ln.startswith("image = ")] == [
        ln for ln in tracked if not ln.startswith("image = ")
    ]


def test_a_config_without_the_anchor_line_refuses_rather_than_deploys(tmp_path: Path) -> None:
    """A near-miss must fail loudly.

    Silently leaving `image = "./Dockerfile"` in place deploys a locally built
    image with no build sha — a successful-looking deploy that production
    cannot date, which is the 2026-09-14 failure exactly.
    """
    src = tmp_path / "src.toml"
    src.write_text('name = "cyris-app"\nimage = "./Dockerfile" # comment\n', encoding="utf-8")

    with pytest.raises(subprocess.CalledProcessError):
        _derive(tmp_path / "run", "release", src=src)


def test_the_derived_config_is_not_committable() -> None:
    assert "wrangler.deploy.toml" in (ROOT / ".gitignore").read_text(encoding="utf-8")


def _deploy_steps() -> dict[str, tuple[int, dict]]:
    workflow = yaml.safe_load((ROOT / ".github/workflows/deploy.yml").read_text())
    steps = workflow["jobs"]["deploy"]["steps"]
    return {s["id"]: (i, s) for i, s in enumerate(steps) if "id" in s}


def test_the_deploy_workflow_is_dispatch_only_and_defaults_to_no_tag() -> None:
    """Chaining it to the build would collapse two different failures into one."""
    workflow = yaml.safe_load((ROOT / ".github/workflows/deploy.yml").read_text())
    # PyYAML reads a bare `on:` key as the boolean True.
    triggers = workflow[True]
    image_tag = triggers["workflow_dispatch"]["inputs"]["image_tag"]

    assert set(triggers) == {"workflow_dispatch"}
    assert image_tag["required"] is False
    assert image_tag.get("default", "") == ""


def test_the_deploy_workflow_serialises_dispatches() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/deploy.yml").read_text())

    assert workflow["concurrency"]["cancel-in-progress"] is False


def test_the_account_id_is_required_before_anything_is_rendered() -> None:
    steps = _deploy_steps()

    assert steps["preflight"][0] < steps["derive"][0]
    assert steps["derive"][0] < steps["deploy"][0]


def test_the_deploy_never_uses_the_tracked_config() -> None:
    """`wrangler deploy` with no --config builds ./Dockerfile on the runner."""
    steps = _deploy_steps()
    run = steps["deploy"][1]["run"]

    assert "--config wrangler.deploy.toml" in run
    assert "--env-file /dev/null" in run


def test_the_package_deploy_script_ignores_a_local_dotenv() -> None:
    """A `.env` beside wrangler.toml would replace the `wrangler login` session."""
    scripts = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["scripts"]

    assert "--env-file /dev/null" in scripts["deploy"]


def test_the_worker_bundle_has_its_dependencies_before_the_deploy() -> None:
    """`wrangler deploy` bundles the Worker, which imports @cloudflare/containers.

    The first dispatch failed here: the image resolved, the config rendered, and
    the bundle then died on a missing module — a deploy path that gets all the
    way to the last step before discovering it cannot run.
    """
    steps = _deploy_steps()

    assert "bun install --frozen-lockfile" in steps["deps"][1]["run"]
    assert steps["deps"][0] < steps["deploy"][0]


def test_the_digest_is_read_back_before_the_deploy() -> None:
    """`release` says nothing six months later about what went live today."""
    steps = _deploy_steps()

    assert steps["resolve"][0] < steps["deploy"][0]
    assert "docker manifest inspect" in steps["resolve"][1]["run"]


def test_the_deploy_fails_unless_production_reports_the_deployed_image() -> None:
    """A green deploy means the rollout started, not that the new image serves."""
    steps = _deploy_steps()
    index, verify = steps["verify"]
    run = verify["run"]

    assert steps["deploy"][0] < index
    assert verify["env"]["DIGEST"] == "${{ steps.resolve.outputs.digest }}"
    assert "CYRIS_GIT_SHA" in run
    assert "/login" in run and "/api/build" in run
    assert "exit 1" in run


def test_the_ui_token_reaches_only_the_verify_step() -> None:
    """The login token grants all of /settings; no other step's code should see it."""
    steps = _deploy_steps()

    holders = [sid for sid, (_, s) in steps.items() if "secrets.CYRIS_UI_TOKEN" in yaml.dump(s)]
    assert holders == ["verify"]
    assert steps["verify"][1]["env"]["CYRIS_DEPLOYMENT_URL"] == "${{ vars.CYRIS_DEPLOYMENT_URL }}"


def test_the_session_cookie_is_masked_and_the_token_stays_off_the_command_line() -> None:
    """The cookie is sha256(token), a credential GitHub does not know to mask."""
    run = _deploy_steps()["verify"][1]["run"]

    assert "::add-mask::" in run and "sha256sum" in run
    assert run.index("::add-mask::") < run.index("/login")
    assert "token@-" in run
    assert "token=$CYRIS_UI_TOKEN" not in run
    assert " -v " not in run and "--verbose" not in run


def test_the_first_ask_waits_out_a_warm_instance() -> None:
    """Each request renews a warm instance's five idle minutes, keeping the old image."""
    run = _deploy_steps()["verify"][1]["run"]

    assert run.index("sleep ") < run.index("/api/build")


def test_the_deployed_config_names_the_resolved_digest_not_the_tag() -> None:
    """`:release` reads the same on every deploy, so wrangler sees no change and rolls nothing.

    Observed 2026-09-21 and again 2026-09-26: the deploy went green and production
    kept the previous image until it was redeployed by digest.
    """
    derive = _deploy_steps()["derive"][1]

    assert derive["env"]["DIGEST"] == "${{ steps.resolve.outputs.digest }}"
    assert '"$DIGEST"' in derive["run"]
    assert "inputs.image_tag" not in derive["run"]


SHA_DIGEST = "sha256:" + "a" * 64


def _run_resolve_step(tmp_path: Path, image_tag: str, tag_message: str | None):
    """Run the deploy's resolve step in a shallow, tagless clone, as checkout leaves it.

    The origin carries an annotated `image/<sha7>` tag with `tag_message` when it is
    given; `{sha}` in it becomes the commit's full sha. `docker` is stubbed to serve
    whatever digest it is asked for and to log each call.
    """
    if shutil.which("jq") is None:
        pytest.skip("jq is not on PATH")

    # `git rebase -x` exports GIT_DIR to the command it runs; inherited here, it
    # would point these throwaway repos at the real one.
    clean_env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}

    def git(*args: str, cwd: Path) -> str:
        done = subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@test", *args],
            cwd=cwd,
            env=clean_env,
            capture_output=True,
            text=True,
            check=True,
        )
        return done.stdout.strip()

    origin = tmp_path / "origin"
    origin.mkdir()
    git("init", "-q", "-b", "main", cwd=origin)
    git("commit", "-q", "--allow-empty", "--no-verify", "-m", "chore: c", cwd=origin)
    sha = git("rev-parse", "HEAD", cwd=origin)
    if tag_message is not None:
        git(
            "tag",
            "-a",
            f"image/{sha[:7]}",
            "-m",
            tag_message.format(sha=sha),
            cwd=origin,
        )
    clone = tmp_path / "clone"
    git("clone", "-q", "--depth=1", "--no-tags", f"file://{origin}", str(clone), cwd=tmp_path)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "docker-calls"
    calls.touch()
    (bin_dir / "docker").write_text(
        "#!/bin/sh\n"
        f'echo "$*" >> "{calls}"\n'
        "for arg; do ref=$arg; done\n"
        'printf \'{"Descriptor": {"digest": "%s"}}\' "${ref#*@}"\n'
    )
    (bin_dir / "docker").chmod(0o755)
    output = tmp_path / "output"
    output.touch()
    env = {
        **clean_env,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "CLOUDFLARE_ACCOUNT_ID": "acct",
        "IMAGE_NAME": "img",
        "IMAGE_TAG": image_tag,
        "GITHUB_SHA": sha,
        "GITHUB_OUTPUT": str(output),
    }
    script = _deploy_steps()["resolve"][1]["run"]
    done = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
        cwd=clone,
        env=env,
        capture_output=True,
        text=True,
    )
    return done.returncode, output.read_text(), done.stderr, calls.read_text()


def test_the_resolve_step_reads_its_input_from_the_environment() -> None:
    resolve = _deploy_steps()["resolve"][1]

    assert resolve["env"]["IMAGE_TAG"] == "${{ inputs.image_tag }}"
    assert "inputs.image_tag" not in resolve["run"]


def test_both_workflows_name_the_image_tag_the_same_way() -> None:
    """The release writes `image/<sha7>` and the deploy reads it; a drift deploys nothing."""
    release = (ROOT / ".github/workflows/release-image.yml").read_text(encoding="utf-8")

    assert 'tag="image/${GITHUB_SHA::7}"' in release
    assert 'tag="image/${GITHUB_SHA::7}"' in _deploy_steps()["resolve"][1]["run"]


def test_no_input_deploys_the_digest_the_release_recorded(tmp_path: Path) -> None:
    """`:release` is never asked: on 2026-10-08 it read back stale for over 50 seconds."""
    message = f"This commit has an image. commit={{sha}} digest={SHA_DIGEST}"
    code, output, _, calls = _run_resolve_step(tmp_path, "", message)

    assert code == 0
    assert output == f"digest={SHA_DIGEST}\n"
    assert calls.splitlines() == [
        f"manifest inspect -v registry.cloudflare.com/acct/img@{SHA_DIGEST}"
    ]


@pytest.mark.parametrize(
    "message",
    [
        None,
        "This commit has an image. commit={sha}",
        f"This commit has an image. commit={'f' * 40} digest={SHA_DIGEST}",
    ],
    ids=["no tag", "no digest", "another commit"],
)
def test_no_input_refuses_a_commit_the_release_never_recorded(
    tmp_path: Path, message: str | None
) -> None:
    code, output, stderr, calls = _run_resolve_step(tmp_path, "", message)

    assert code != 0
    assert output == ""
    assert "run the release" in stderr
    assert calls == ""


def test_an_explicit_digest_still_pins_one_image(tmp_path: Path) -> None:
    pinned = "sha256:" + "b" * 64
    code, output, _, calls = _run_resolve_step(tmp_path, pinned, None)

    assert code == 0
    assert output == f"digest={pinned}\n"
    assert calls.splitlines() == [f"manifest inspect -v registry.cloudflare.com/acct/img@{pinned}"]


@pytest.mark.parametrize("name", ["release", "latest"])
def test_a_mutable_tag_name_is_refused(tmp_path: Path, name: str) -> None:
    message = f"This commit has an image. commit={{sha}} digest={SHA_DIGEST}"
    code, output, stderr, calls = _run_resolve_step(tmp_path, name, message)

    assert code != 0
    assert output == ""
    assert "sha256:" in stderr
    assert calls == ""
