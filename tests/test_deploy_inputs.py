"""The three lists a deploy reads must agree, or a fork deploys half-configured.

A Deploy to Cloudflare button asks for what `.env.example` names and renders the
guidance in `package.json`'s `cloudflare.bindings` beside each field; the Worker
forwards what `workers/app/src/index.js` names into the container. A name in one
and not the others is a secret nobody is asked for, or a field that goes nowhere.
Both fail at the stranger's first run, not here — hence this test.
"""

import json
import re
import tomllib
from pathlib import Path

import pytest

from cyris.diagnostics.config_show import IDENTITY, SECRETS

pytestmark = [pytest.mark.unit, pytest.mark.guard]

ROOT = Path(__file__).resolve().parents[1]

# Set by docker/entrypoint.sh for the `run` and `ui` roles, so the deploy form
# deliberately does not ask for them.
_ENTRYPOINT_DEFAULTED = {
    "CYRIS_STORE_BACKEND",
    "CYRIS_HTML_OUTPUT_ENABLED",
    "CYRIS_PROMOTE_PUBLISH_ENABLED",
}


def _env_example() -> set[str]:
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    return set(re.findall(r"^([A-Z][A-Z0-9_]+)=", text, re.M))


def _bindings() -> dict[str, str]:
    pkg = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    return {k: v["description"] for k, v in pkg["cloudflare"]["bindings"].items()}


_WORKER_ONLY = (
    "CYRIS_UI_TOKEN",
    "CYRIS_UI_ACCESS_HOST",
    "DIGEST_ORIGIN",
    "CYRIS_PRIVATE_ARCHIVE",
)


def _worker_env() -> set[str]:
    js = (ROOT / "workers/app/src/index.js").read_text(encoding="utf-8")
    router = (ROOT / "workers/app/src/router.js").read_text(encoding="utf-8")
    forwarded = set(re.findall(r"^\s+([A-Z][A-Z0-9_]+): env\.", js, re.M))
    worker_only = {name for name in _WORKER_ONLY if name in js + router}
    return forwarded | worker_only


def test_env_example_and_deploy_form_name_the_same_variables() -> None:
    assert _env_example() == set(_bindings())


def test_config_show_lists_every_name_the_container_can_read() -> None:
    """`cyris config show` cannot read `.env.example`, which the image does not ship.

    A Worker-only name never reaches the container, so it is left out; `CYRIS_UI_TOKEN`
    is shown anyway, because `cyris doctor --deployment` reads it from a local `.env`.
    """
    assert _env_example() - set(_WORKER_ONLY) - set(SECRETS) - set(IDENTITY.values()) == set()
    assert set(SECRETS) <= _env_example()


def _wrangler_vars() -> dict[str, str]:
    return tomllib.loads((ROOT / "wrangler.toml").read_text(encoding="utf-8")).get("vars", {})


def test_every_variable_the_worker_reads_is_asked_for() -> None:
    assert _worker_env() - set(_bindings()) == _ENTRYPOINT_DEFAULTED | set(_wrangler_vars())


def test_worker_only_names_stay_in_the_worker() -> None:
    """A name the container is handed is not Worker-only; the two lists must not overlap."""
    js = (ROOT / "workers/app/src/index.js").read_text(encoding="utf-8")
    forwarded = set(re.findall(r"^\s+([A-Z][A-Z0-9_]+): env\.", js, re.M))
    assert forwarded & set(_WORKER_ONLY) == set()


def test_the_worker_name_the_container_asks_about_is_the_deployed_name() -> None:
    """A different name lists another Worker's domains, or none, and the link goes wrong."""
    config = tomllib.loads((ROOT / "wrangler.toml").read_text(encoding="utf-8"))
    assert _wrangler_vars()["CYRIS_APP_WORKER_NAME"] == config["name"]


def test_the_deploy_form_asks_for_nothing_the_worker_ignores() -> None:
    assert set(_bindings()) - _worker_env() == set()


def test_manual_deploy_sets_every_required_secret() -> None:
    """The install guide's `secret bulk` file must cover every **Required** field."""
    required = {k for k, desc in _bindings().items() if desc.startswith("**Required.**")}
    guide = (ROOT / "docs/install-cloudflare.md").read_text(encoding="utf-8")
    block = re.search(r"# \.env\.cloud(.*?)```", guide, re.S)
    assert block, "the .env.cloud secrets file is gone from docs/install-cloudflare.md"
    assert required - set(re.findall(r"^([A-Z][A-Z0-9_]+)=", block.group(1), re.M)) == set()


def test_every_worker_directory_deploys_on_its_own() -> None:
    """Each Worker ships the files a Deploy to Cloudflare button needs.

    The button treats the directory it points at as the whole
    repository, so a subdirectory Worker needs its own wrangler.toml and
    package.json. Without `cloudflare.bindings` the deploy page renders the
    fields bare, and an untokened Worker answers 401 to every pull it exists to
    serve — a fork that looks deployed and returns nothing.
    """
    for worker in sorted(p.parent for p in ROOT.glob("workers/*/wrangler.toml")):
        name = worker.name
        readme = worker / "README.md"
        assert readme.is_file(), f"workers/{name} has no README"

        pkg_path = worker / "package.json"
        assert pkg_path.is_file(), f"workers/{name} has no package.json"
        pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
        assert pkg.get("cloudflare", {}).get("bindings"), f"workers/{name} declares no bindings"


def test_container_placement_excludes_asia_pacific() -> None:
    """Gemini and OpenAI refuse requests from Hong Kong, which `APAC` placement includes.

    On 2026-09-08 every Gemini call from the Container started failing with
    `FAILED_PRECONDITION: User location is not supported`, while the same key
    answered from the Worker; unconstrained placement is nearest-to-request.
    """
    config = tomllib.loads((ROOT / "wrangler.toml").read_text(encoding="utf-8"))
    regions = config["containers"][0]["constraints"]["regions"]
    assert regions
    assert set(regions) <= {"ENAM", "WNAM", "EEUR", "WEUR"}


def _container_image_name() -> str:
    """The registry repository `wrangler deploy` builds into, from the tracked config."""
    containers = tomllib.loads((ROOT / "wrangler.toml").read_text(encoding="utf-8"))["containers"]
    [container] = containers
    return container["name"]


def _workflow_image_name(rel: str) -> str:
    text = (ROOT / rel).read_text(encoding="utf-8")
    [name] = re.findall(r"^\s+IMAGE_NAME:\s*(\S+)", text, re.M)
    return name


def test_the_container_image_name_is_stated_rather_than_derived() -> None:
    """Left unset, wrangler derives it from the Worker name plus the class name.

    A derived name is one nothing in this repo can be held to, and the release
    workflow carries an IMAGE_NAME of its own — so the two drift with no diff to
    notice.
    """
    assert _container_image_name()


def test_every_deploy_path_pushes_to_one_registry_repository() -> None:
    """A local `wrangler deploy` and a CI deploy must land in the same repository.

    On 2026-09-21 they did not: the tracked config left the name to wrangler
    (`cyris-app-cyriscontainer`) while both workflows said `cyris-app`, so each
    path pointed the live container application at an image the other had never
    written. A CI deploy after a local one silently served the older code — the
    shape that ships a reverted security fix with a green deploy log.
    """
    names = {
        "wrangler.toml": _container_image_name(),
        "release-image.yml": _workflow_image_name(".github/workflows/release-image.yml"),
        "deploy.yml": _workflow_image_name(".github/workflows/deploy.yml"),
    }

    assert len(set(names.values())) == 1, names


def ungraded_worker_only_names(doc: str) -> list[str]:
    """Worker-only names with no row graded B or C in the doc's "Every setting, graded" table."""
    section = doc.split("### Every setting, graded", 1)[1].split("\n### ", 1)[0]
    graded = []
    for row in section.splitlines():
        cells = [c.strip() for c in row.split("|")]
        if len(cells) > 3 and cells[2].strip("*").startswith(("B", "C")):
            graded.append(row)
    return [name for name in _WORKER_ONLY if not any(name in row for row in graded)]


def test_every_worker_only_name_is_graded() -> None:
    doc = (ROOT / "docs/architecture.md").read_text(encoding="utf-8")
    assert ungraded_worker_only_names(doc) == []


def test_the_grading_guard_names_a_variable_whose_row_is_gone() -> None:
    doc = (ROOT / "docs/architecture.md").read_text(encoding="utf-8")
    rows = [r for r in doc.splitlines() if not r.startswith("| Private archive |")]
    assert ungraded_worker_only_names("\n".join(rows)) == ["CYRIS_PRIVATE_ARCHIVE"]
