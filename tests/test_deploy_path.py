"""The deploy half of the release path: a reference to a published image.

Separate from `test_release_image.py` on purpose — that module's two `-k`
selections are what bind the release specs, and widening them would report
those specs as violated whenever something here changes.
"""

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


def test_the_deploy_workflow_is_dispatch_only_and_takes_a_tag() -> None:
    """Chaining it to the build would collapse two different failures into one."""
    workflow = yaml.safe_load((ROOT / ".github/workflows/deploy.yml").read_text())
    # PyYAML reads a bare `on:` key as the boolean True.
    triggers = workflow[True]

    assert set(triggers) == {"workflow_dispatch"}
    assert triggers["workflow_dispatch"]["inputs"]["image_tag"]["default"] == "release"


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
