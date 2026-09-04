"""The three lists a deploy reads must agree, or a fork deploys half-configured.

A Deploy to Cloudflare button asks for what `.env.example` names and renders the
guidance in `package.json`'s `cloudflare.bindings` beside each field; the Worker
forwards what `workers/app/src/index.js` names into the container. A name in one
and not the others is a secret nobody is asked for, or a field that goes nowhere.
Both fail at the stranger's first run, not here — hence this test.
"""

import json
import re
from pathlib import Path

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


def _worker_env() -> set[str]:
    js = (ROOT / "workers/app/src/index.js").read_text(encoding="utf-8")
    router = (ROOT / "workers/app/src/router.js").read_text(encoding="utf-8")
    forwarded = set(re.findall(r"^\s+([A-Z][A-Z0-9_]+): env\.", js, re.M))
    worker_only = {
        name
        for name in ("CYRIS_UI_TOKEN", "CYRIS_UI_ACCESS_HOST", "DIGEST_ORIGIN")
        if name in js + router
    }
    return forwarded | worker_only


def test_env_example_and_deploy_form_name_the_same_variables() -> None:
    assert _env_example() == set(_bindings())


def test_every_variable_the_worker_reads_is_asked_for() -> None:
    assert _worker_env() - set(_bindings()) == _ENTRYPOINT_DEFAULTED


def test_the_deploy_form_asks_for_nothing_the_worker_ignores() -> None:
    assert set(_bindings()) - _worker_env() == set()


def test_manual_deploy_sets_every_required_secret() -> None:
    """The copy-pasteable loop in the app README must cover every **Required** field."""
    required = {k for k, desc in _bindings().items() if desc.startswith("**Required.**")}
    readme = (ROOT / "workers/app/README.md").read_text(encoding="utf-8")
    loop = re.search(r"for s in (.*?); do", readme, re.S)
    assert loop, "the secret-setting loop is gone from workers/app/README.md"
    assert required - set(re.findall(r"[A-Z][A-Z0-9_]+", loop.group(1))) == set()


def test_every_worker_is_deployable_by_button() -> None:
    """Four Workers, four buttons, each with guidance for the secrets it needs.

    A Deploy to Cloudflare button treats the directory it points at as the whole
    repository, so a subdirectory Worker needs its own wrangler.toml and
    package.json. Without `cloudflare.bindings` the deploy page renders the
    fields bare, and an untokened Worker answers 401 to every pull it exists to
    serve — a fork that looks deployed and returns nothing.
    """
    app_button = "?url=https://github.com/musingfox/cyris)"
    assert app_button in (ROOT / "README.md").read_text(encoding="utf-8")

    for worker in sorted(p.parent for p in ROOT.glob("workers/*/wrangler.toml")):
        name = worker.name
        readme = worker / "README.md"
        assert readme.is_file(), f"workers/{name} has no README"
        assert (
            f"?url=https://github.com/musingfox/cyris/tree/main/workers/{name})"
            in readme.read_text(encoding="utf-8")
        ), f"workers/{name}/README.md has no deploy button"

        pkg_path = worker / "package.json"
        assert pkg_path.is_file(), f"workers/{name} has no package.json"
        pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
        assert pkg.get("cloudflare", {}).get("bindings"), f"workers/{name} declares no bindings"
