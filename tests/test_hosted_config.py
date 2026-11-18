"""Hosted mode is its own deployment, and the default one deploys what it deployed before.

`wrangler.toml` is what a push to main deploys to production (ADR-0024). The hosted Worker
lives in `wrangler.hosted.toml` instead, deployed by hand to the trial account, and the two
must share no name, class, binding, container application or migration: a hosted name in the
default file would deploy a tenant's database class to production, and a production name in the
hosted file would deploy over production's. The seed the hosted Worker writes into a tenant's
database must also be settings and sources a run can load, which only cyris can say.
"""

import json
import re
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from cyris.adapters.store.source_store import source_from_row
from cyris.adapters.store.tenant_gateway import GATEWAY_ORIGIN
from cyris.config import REQUIRED_KEYS, TENANT_ID, validate_setting

pytestmark = [pytest.mark.unit, pytest.mark.guard]

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "workers/app/src"

# Every name hosted mode introduces, any one of which in the default file would deploy it.
HOSTED_NAMES = (
    "cyris-hosted",
    "hosted.js",
    "TenantContainer",
    "TenantDb",
    "TENANT_RUN",
    "TENANT_DB",
    "CYRIS_HOSTED_TENANTS",
)


def _toml(name: str) -> dict:
    return tomllib.loads((ROOT / name).read_text(encoding="utf-8"))


def _js(name: str) -> str:
    return (SRC / name).read_text(encoding="utf-8")


def _classes(config: dict) -> set[str]:
    bound = {b["class_name"] for b in config.get("durable_objects", {}).get("bindings", [])}
    contained = {c["class_name"] for c in config.get("containers", [])}
    migrated = {c for m in config.get("migrations", []) for c in m.get("new_sqlite_classes", [])}
    return bound | contained | migrated


def _imports(module: str) -> set[str]:
    """Every local module `module` reaches through its imports, itself included."""
    seen: set[str] = set()
    todo = [module]
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        found = re.findall(
            r'^\s*(?:import|export)\b[^;]*?from\s+"\./([\w.]+)"', _js(name), re.M | re.S
        )
        todo.extend(found)
    return seen


def test_the_default_file_names_nothing_hosted_and_defines_no_environment() -> None:
    """A second environment would make every production deploy warn that none was named."""
    text = (ROOT / "wrangler.toml").read_text(encoding="utf-8")
    config = tomllib.loads(text)

    assert [name for name in HOSTED_NAMES if name in text] == []
    assert "env" not in config
    assert config["name"] == "cyris-app"
    assert config["main"] == "workers/app/src/index.js"
    assert _classes(config) == {"CyrisContainer"}


def test_the_default_worker_bundles_no_hosted_module() -> None:
    reached = _imports("index.js")

    assert "container.js" in reached
    assert [m for m in reached if m.startswith(("hosted", "tenant_db"))] == []


def test_the_hosted_file_shares_nothing_with_the_default_one() -> None:
    default, hosted = _toml("wrangler.toml"), _toml("wrangler.hosted.toml")

    assert hosted["name"] == "cyris-hosted" != default["name"]
    assert hosted["main"] == "workers/app/src/hosted.js"
    assert _classes(hosted) == {"TenantContainer", "TenantDb"}
    assert _classes(hosted) & _classes(default) == set()
    [container] = hosted["containers"]
    assert container["name"] not in {c["name"] for c in default["containers"]}
    bindings = {b["name"] for b in hosted["durable_objects"]["bindings"]}
    assert bindings == {"TENANT_RUN", "TENANT_DB"}
    assert bindings & {b["name"] for b in default["durable_objects"]["bindings"]} == set()
    assert "CYRIS_APP_WORKER_NAME" not in hosted["vars"]


def test_the_hosted_file_runs_the_same_image_on_the_same_runtime() -> None:
    default, hosted = _toml("wrangler.toml"), _toml("wrangler.hosted.toml")
    [container] = hosted["containers"]

    assert (ROOT / container["image"]).resolve() == (
        ROOT / default["containers"][0]["image"]
    ).resolve()
    assert hosted["compatibility_date"] == default["compatibility_date"]
    assert (hosted["workers_dev"], hosted["preview_urls"]) == (True, False)


def test_the_hosted_file_is_deployed_by_no_workflow() -> None:
    """Only wrangler.toml deploys on a push; the hosted Worker goes to the trial account by hand."""
    workflows = sorted((ROOT / ".github/workflows").glob("*.yml"))

    assert workflows
    assert [p.name for p in workflows if "wrangler.hosted" in p.read_text(encoding="utf-8")] == []


def test_the_hosted_worker_exports_exactly_the_classes_it_binds() -> None:
    exported = set(re.findall(r"^export \{ ([\w, ]+) \} from", _js("hosted.js"), re.M))
    names = {name.strip() for group in exported for name in group.split(",")}

    assert names == _classes(_toml("wrangler.hosted.toml")) | {"TenantDbGateway"}


def test_every_tenant_database_is_hinted_to_the_containers_only_region() -> None:
    [container] = _toml("wrangler.hosted.toml")["containers"]
    [hint] = re.findall(r'^export const LOCATION_HINT = "(\w+)";', _js("tenant_db.js"), re.M)

    assert container["constraints"]["regions"] == [hint.upper()]


def test_the_gateway_host_and_the_tenant_pattern_are_one_on_both_sides() -> None:
    js = _js("tenant_db.js")
    [host] = re.findall(r'^export const DB_HOST = "([^"]+)";', js, re.M)
    [pattern] = re.findall(r"^export const TENANT = /\^(.+)\$/;", js, re.M)

    assert urlsplit(GATEWAY_ORIGIN).hostname == host
    assert pattern == TENANT_ID.pattern


def test_a_hosted_container_is_handed_only_names_the_default_worker_knows() -> None:
    """The allow-list holds no Worker URL or bearer, and no name the default Worker lacks."""
    [block] = re.findall(r"const HOSTED_SECRETS = \[(.*?)\];", _js("hosted_container.js"), re.S)
    hosted = set(re.findall(r'"([A-Z][A-Z0-9_]+)"', block))
    [secrets] = re.findall(r"const SECRETS = \{(.*?)\};", _js("index.js"), re.S)
    known = set(re.findall(r"^\s+([A-Z][A-Z0-9_]+): env\.", secrets, re.M))

    assert hosted and hosted <= known
    closed = {
        "CYRIS_PROMOTE_WORKER_URL",
        "CYRIS_NEWSLETTER_WORKER_URL",
        "CYRIS_RSS_WORKER_URL",
        "CYRIS_WORKER_TOKEN",
        "CYRIS_PROMOTE_TOKEN",
        "CLOUDFLARE_API_TOKEN",
    }
    assert hosted & closed == set()


def _seed() -> dict:
    return json.loads((SRC / "hosted_seed.json").read_text(encoding="utf-8"))


def _seeded_settings() -> dict:
    """What the seed writes: the starting values with the seed's own laid over them."""
    starting = json.loads((ROOT / "src/cyris/settings_starting_values.json").read_text("utf-8"))
    return {**starting, **_seed()["settings"]}


def test_the_seed_seeds_every_listed_tenant_and_only_those() -> None:
    tenants = _toml("wrangler.hosted.toml")["vars"]["CYRIS_HOSTED_TENANTS"]

    assert len(tenants) == 2
    assert all(TENANT_ID.fullmatch(t) for t in tenants)
    assert set(_seed()["sources"]) == set(tenants)


def test_the_seeded_settings_are_every_required_key_each_valid() -> None:
    settings = _seeded_settings()

    assert set(REQUIRED_KEYS) - set(settings) == set()
    for key, value in settings.items():
        validate_setting(key, value)
    assert settings["llm_provider.provider"] == "anthropic"
    assert settings["llm_provider.model"] == "claude-haiku-5-5"


def test_the_seeded_sources_are_catalog_feeds_no_two_tenants_share() -> None:
    catalog = {
        f["name"]: f
        for f in json.loads((ROOT / "src/cyris/onboarding_feeds.json").read_text("utf-8"))["feeds"]
    }
    sources = _seed()["sources"]

    for tenant, names in sources.items():
        assert 0 < len(names) == len(set(names)), tenant
        for name in names:
            feed = {k: v for k, v in catalog[name].items() if k not in {"name", "url", "topics"}}
            row = {
                "name": name,
                "url": catalog[name]["url"],
                "type": "rss",
                "config": json.dumps(feed),
            }
            assert source_from_row(row).fetchable, name
    first, second = sources.values()
    assert set(first) & set(second) == set()
