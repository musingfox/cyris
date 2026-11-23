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

from cyris.adapters.hosted import OPERATOR_ORIGIN, OPERATOR_TIMEOUT_SECONDS
from cyris.adapters.output.hosted_pages import SETTINGS_PAGE, SITE_PATH, tenant_settings_path
from cyris.adapters.store.source_store import source_from_row
from cyris.adapters.store.tenant_gateway import GATEWAY_ORIGIN
from cyris.config import HOSTED_B_GRADE_ENV_VARS, REQUIRED_KEYS, TENANT_ID, validate_setting

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
    "TenantRegistry",
    "REGISTRY",
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
    assert [m for m in reached if m.startswith(("hosted", "tenant_"))] == []


def test_the_hosted_file_shares_nothing_with_the_default_one() -> None:
    default, hosted = _toml("wrangler.toml"), _toml("wrangler.hosted.toml")

    assert hosted["name"] == "cyris-hosted" != default["name"]
    assert hosted["main"] == "workers/app/src/hosted.js"
    assert _classes(hosted) == {"TenantContainer", "TenantDb", "TenantRegistry"}
    assert _classes(hosted) & _classes(default) == set()
    [container] = hosted["containers"]
    assert container["name"] not in {c["name"] for c in default["containers"]}
    bindings = {b["name"] for b in hosted["durable_objects"]["bindings"]}
    assert bindings == {"TENANT_RUN", "TENANT_DB", "REGISTRY"}
    assert bindings & {b["name"] for b in default["durable_objects"]["bindings"]} == set()
    assert "CYRIS_APP_WORKER_NAME" not in hosted.get("vars", {})


def test_only_the_hosted_file_binds_the_sign_in_mail() -> None:
    """The readers' sign-in mail (ADR-0033) is the hosted Worker's; production sends none."""
    default, hosted = _toml("wrangler.toml"), _toml("wrangler.hosted.toml")

    assert "send_email" not in default
    assert hosted["send_email"] == [{"name": "MAIL"}]
    assert "env.MAIL" in _js("hosted.js")


def test_the_sign_in_sender_is_one_name_from_the_runbook_to_the_worker() -> None:
    assert "env.CYRIS_HOSTED_MAIL_FROM" in _js("hosted.js")
    assert "put_secret CYRIS_HOSTED_MAIL_FROM " in (ROOT / "scripts/hosted.sh").read_text(
        encoding="utf-8"
    )


def test_the_backups_go_to_one_bucket_only_the_hosted_file_binds() -> None:
    """The daily backup's R2 copy (ADR-0041) is the hosted Worker's; production writes none."""
    default, hosted = _toml("wrangler.toml"), _toml("wrangler.hosted.toml")
    backup = _js("hosted_backup.js")
    [binding] = re.findall(r'^export const BACKUP_BINDING = "(\w+)";', backup, re.M)

    assert "r2_buckets" not in default
    assert hosted["r2_buckets"] == [{"binding": binding, "bucket_name": "cyris-hosted-backup"}]


def test_the_backup_runs_from_the_hourly_tick_and_never_a_second_cron() -> None:
    hosted = _toml("wrangler.hosted.toml")
    backup = _js("hosted_backup.js")
    [hour] = re.findall(r"^export const BACKUP_HOUR_UTC = (\d+);", backup, re.M)
    [check] = re.findall(r"^export const BACKUP_CHECK_HOURS = (\d+);", backup, re.M)

    assert hosted["triggers"]["crons"] == ["0 * * * *"]
    assert 0 <= int(hour) <= 23
    # The check falls on the backup's own UTC date, so it looks for that day's export.
    assert int(check) > 0 and int(hour) + int(check) <= 23
    assert "dailyBackup(env, { now: at })" in _js("hosted.js")


def test_the_b2_settings_are_set_by_the_runbook_and_never_handed_to_a_container() -> None:
    [block] = re.findall(r"export const B2_SETTINGS = \{(.*?)\};", _js("hosted_backup.js"), re.S)
    names = re.findall(r'"(CYRIS_HOSTED_B2_\w+)"', block)
    [handed] = re.findall(r"const HOSTED_SECRETS = \[(.*?)\];", _js("hosted_container.js"), re.S)
    script = (ROOT / "scripts/hosted.sh").read_text(encoding="utf-8")

    assert len(names) == 4
    for name in names:
        assert name not in handed
        assert f"put_secret {name} " in script


def test_the_b2_key_the_runbook_asks_for_only_writes_as_the_worker_only_puts() -> None:
    """The registry's export holds the vote key and live session hashes (ADR-0041), so the B2
    key in the Worker's secrets is writeFiles only: enough while the Worker's one B2 request is
    a single-part PUT, with no listing, read or multipart upload."""
    sigv4 = _js("s3_sigv4.js")
    runbook = (ROOT / "docs/hosted-tenants.md").read_text(encoding="utf-8")
    [step] = re.findall(r"^3\. Add an application key.*?(?=^### )", runbook, re.M | re.S)

    assert set(re.findall(r'method: "(\w+)"', sigv4)) == {"PUT"}
    assert "uploads" not in sigv4 + _js("hosted_backup.js")
    assert "`writeFiles`" in step
    assert "Read and Write" not in step


def test_the_hosted_file_runs_the_same_image_on_the_same_runtime() -> None:
    default, hosted = _toml("wrangler.toml"), _toml("wrangler.hosted.toml")
    [container] = hosted["containers"]

    assert (ROOT / container["image"]).resolve() == (
        ROOT / default["containers"][0]["image"]
    ).resolve()
    assert hosted["compatibility_date"] == default["compatibility_date"]
    assert (hosted["workers_dev"], hosted["preview_urls"]) == (True, False)


def test_a_pending_container_monitor_keeps_the_tenant_object_in_memory() -> None:
    # The run queue learns a run ended from monitor(); on a date before 2026-10-01 only this
    # flag stops the object being evicted while monitor() waits.
    hosted = _toml("wrangler.hosted.toml")

    assert hosted["compatibility_date"] < "2026-10-01"
    assert "durable_object_io_tasks_prevent_eviction" in hosted.get("compatibility_flags", [])
    assert "durable_object_io_tasks_do_not_prevent_eviction" not in hosted.get(
        "compatibility_flags", []
    )


def test_the_hosted_file_is_deployed_by_no_workflow() -> None:
    """Only wrangler.toml deploys on a push; the hosted Worker goes to the trial account by hand."""
    workflows = sorted((ROOT / ".github/workflows").glob("*.yml"))

    assert workflows
    assert [p.name for p in workflows if "wrangler.hosted" in p.read_text(encoding="utf-8")] == []


def test_the_hosted_worker_exports_exactly_the_classes_it_binds() -> None:
    exported = set(re.findall(r"^export \{ ([\w, ]+) \} from", _js("hosted.js"), re.M))
    names = {name.strip() for group in exported for name in group.split(",")}

    assert names == _classes(_toml("wrangler.hosted.toml")) | {
        "TenantDbGateway",
        "OperatorAlertGateway",
        "SpendGateway",
    }


def test_every_tenant_database_is_hinted_to_the_containers_only_region() -> None:
    [container] = _toml("wrangler.hosted.toml")["containers"]
    [hint] = re.findall(r'^export const LOCATION_HINT = "(\w+)";', _js("tenant_db.js"), re.M)

    assert container["constraints"]["regions"] == [hint.upper()]


def test_the_run_queue_starts_as_many_runs_at_once_as_the_container_has_instances() -> None:
    """Fewer would leave an instance idle; more would only wait for one (ADR-0032)."""
    [container] = _toml("wrangler.hosted.toml")["containers"]
    [slots] = re.findall(r"^export const RUN_SLOTS = (\d+);", _js("run_queue.js"), re.M)

    assert int(slots) == container["max_instances"]


def test_the_registry_stores_only_time_zones_the_container_reads() -> None:
    """The Worker's schedule check and the container's agree on every shared case (ADR-0032).

    `workers/app/test/run_queue.test.js` holds `checkSchedule` to the same file: a zone it stores
    is copied into the tenant's `general.timezone`, so one the container refuses would fail every
    run.
    """
    cases = json.loads((ROOT / "workers/app/test/time_zone_cases.json").read_text("utf-8"))["cases"]

    for case in cases:
        if case["stored"] is not None:
            assert validate_setting("general.timezone", case["stored"]) == case["stored"]
        elif case["zone"].lower() == case["zone"] and "/" in case["zone"]:
            # A lower-case name the container may read on a case-blind file system; the Worker
            # refuses it because a case-sensitive one would not.
            continue
        else:
            with pytest.raises(ValueError):
                validate_setting("general.timezone", case["zone"])


def test_the_gateway_host_and_the_tenant_pattern_are_one_on_both_sides() -> None:
    js = _js("tenant_db.js")
    [host] = re.findall(r'^export const DB_HOST = "([^"]+)";', js, re.M)
    [pattern] = re.findall(r"^export const TENANT = /\^(.+)\$/;", js, re.M)

    assert urlsplit(GATEWAY_ORIGIN).hostname == host
    assert pattern == TENANT_ID.pattern


def test_the_operator_alert_host_is_one_on_both_sides_and_not_the_databases() -> None:
    [host] = re.findall(r'^export const OPS_HOST = "([^"]+)";', _js("hosted_alerts.js"), re.M)

    assert urlsplit(OPERATOR_ORIGIN).hostname == host
    assert host != urlsplit(GATEWAY_ORIGIN).hostname


def test_the_operator_alert_gives_up_before_the_run_and_well_inside_the_lease() -> None:
    """The gateway answers a run only once its webhook post ends, and a stop report waits on the
    run object's alert, so the post's bound is under the run's own and far under the lease."""
    [alert_ms] = re.findall(
        r"^export const ALERT_TIMEOUT_MS = ([\d_]+);", _js("hosted_alerts.js"), re.M
    )
    [lease_minutes] = re.findall(
        r"^export const RUN_LEASE_MS = (\d+) \* 60_000;", _js("run_queue.js"), re.M
    )

    assert int(alert_ms) < OPERATOR_TIMEOUT_SECONDS * 1000
    assert int(alert_ms) * 100 <= int(lease_minutes) * 60_000


def test_the_operators_webhook_is_no_name_a_container_is_handed() -> None:
    [name] = re.findall(
        r'^export const OPERATOR_WEBHOOK = "([^"]+)";', _js("hosted_alerts.js"), re.M
    )
    [block] = re.findall(r"const HOSTED_SECRETS = \[(.*?)\];", _js("hosted_container.js"), re.S)

    assert name not in block


def test_the_tenants_site_path_is_one_shape_on_both_sides() -> None:
    """The run links `<domain>/t/<tenant>/<page>`, and the Worker serves the tenant's site there."""
    [route] = re.findall(r"^export const SITE_ROUTE = /(.+)/;", _js("hosted_pages.js"), re.M)
    site = SITE_PATH.format(tenant="alpha")

    for page in ("/", "/2026-11-18-morning", "/2026-11-18-morning-raw.html"):
        match = re.fullmatch(route, site + page)
        assert match is not None
        assert match.groups() == ("alpha", page)


def test_a_tenants_settings_link_is_the_path_the_worker_serves_its_settings_page_at() -> None:
    settings = _js("hosted_settings.js")
    [page] = re.findall(r'^export const SETTINGS_PAGE = "([^"]+)";', settings, re.M)
    [route] = re.findall(r"^export const SITE_ROUTE = /(.+)/;", _js("hosted_pages.js"), re.M)

    assert page == SETTINGS_PAGE
    assert re.fullmatch(route, tenant_settings_path("alpha")).groups() == ("alpha", page)


def test_the_vote_buttons_and_the_vote_route_agree_on_path_token_and_header() -> None:
    """The page posts to the Worker's route, with the token the Worker put in its <meta>."""
    script = (ROOT / "src/cyris/adapters/output/templates/_promote_script.html.j2").read_text()
    [path] = re.findall(r'^export const VOTE_PATH = "([^"]+)";', _js("hosted_pages.js"), re.M)
    [meta] = re.findall(r'^export const VOTE_TOKEN_META = "([^"]+)";', _js("hosted_pages.js"), re.M)
    [header] = re.findall(r'^export const VOTE_HEADER = "([^"]+)";', _js("hosted_vote.js"), re.M)
    [ended] = re.findall(r'^export const SESSION_ENDED = "([^"]+)";', _js("hosted_vote.js"), re.M)

    probe = (ROOT / "src/cyris/adapters/output/templates/_probe_script.html.j2").read_text()

    assert f"fetch('{path}'" in script
    assert f"fetch('{path}'" in probe
    assert f'meta[name="{meta}"]' in probe
    assert f"'{header}'" in probe
    assert "data.settings !== false" in probe
    assert f'meta[name="{meta}"]' in script
    assert f"'{header}'" in script
    assert f"body.code === '{ended}'" in script


def test_the_hosted_domain_is_one_name_from_the_worker_to_the_container() -> None:
    [name] = re.findall(r'^export const HOSTED_DOMAIN = "(\w+)";', _js("hosted_container.js"), re.M)

    assert name == HOSTED_B_GRADE_ENV_VARS["store.hosted_domain"]
    # The runbook sets it on the Worker under that same name.
    assert f"put_secret {name} " in (ROOT / "scripts/hosted.sh").read_text(encoding="utf-8")


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


def _seeded_settings(hour: int) -> dict:
    """What the seed writes: the starting values with the seed's own laid over them, then the one
    digest hour it gives the tenant (hosted_seed.js `seedSchedule`), here `hour`."""
    starting = json.loads((ROOT / "src/cyris/settings_starting_values.json").read_text("utf-8"))
    return {**starting, **_seed()["settings"], "general.digest_schedule": [f"{hour:02d}:00"]}


def _tenant_vars(variables: dict, tenants: list[str]) -> list[str]:
    """The vars that name or list tenants: a name for a tenant list, or a value naming a tenant.

    A value names one when it, an item of it, or a comma-separated part of it is a tenant id.
    Any other var, which a later hosted card may add, is not this test's business.
    """
    flagged = []
    for name, value in variables.items():
        items = value if isinstance(value, list) else [value]
        parts = {
            part.strip() for item in items if isinstance(item, str) for part in item.split(",")
        }
        if re.search(r"TENANTS|TENANT_IDS|TENANT_LIST", name) or parts & set(tenants):
            flagged.append(name)
    return flagged


def test_the_tenant_var_check_flags_a_tenant_list_and_nothing_else() -> None:
    tenants = ["alpha", "bravo"]

    assert _tenant_vars({"CYRIS_HOSTED_TENANTS": ["alpha", "bravo"]}, tenants) == [
        "CYRIS_HOSTED_TENANTS"
    ]
    assert _tenant_vars({"CYRIS_HOSTED_TENANT_IDS": []}, tenants) == ["CYRIS_HOSTED_TENANT_IDS"]
    assert _tenant_vars({"ROSTER": "alpha, bravo", "ONLY": ["bravo"]}, tenants) == [
        "ROSTER",
        "ONLY",
    ]
    assert _tenant_vars({"CYRIS_HOSTED_DIGEST_ORIGIN": "https://alpha.example"}, tenants) == []
    assert _tenant_vars({"CYRIS_HOSTED_TENANT_LIMIT": "20", "FLAG": True}, tenants) == []


def test_the_hosted_file_lists_no_tenant() -> None:
    """The registry is the tenant list (ADR-0030): a tenant is a row, never a line of config.

    A hosted var of any other kind may come with a later card; only one naming or listing
    tenants is refused.
    """
    hosted = _toml("wrangler.hosted.toml")

    assert _tenant_vars(hosted.get("vars", {}), list(_seed()["sources"])) == []
    assert [m for m in _imports("hosted.js") if "hostedTenants" in _js(m)] == []


def test_the_seed_names_the_two_tenants_the_runbook_registers() -> None:
    """scripts/hosted.sh registers, seeds, runs and checks the tenants the seed names."""
    tenants = list(_seed()["sources"])

    assert tenants == ["alpha", "bravo"]
    assert all(TENANT_ID.fullmatch(t) for t in tenants)


@pytest.mark.parametrize("hour", range(24))
def test_the_seeded_settings_are_every_required_key_each_valid(hour: int) -> None:
    """Whatever hour the seed spreads a tenant to, a hosted run loads what it wrote (ADR-0042)."""
    settings = _seeded_settings(hour)

    assert set(REQUIRED_KEYS) - set(settings) == set()
    for key, value in settings.items():
        validate_setting(key, value, hosted=True)
    assert settings["llm_provider.provider"] == "anthropic"
    assert settings["llm_provider.model"] == "claude-haiku-5-5"


def test_the_seed_file_names_no_digest_hour_every_tenant_would_share() -> None:
    """The seed spreads each tenant to an hour of its own, so its file names none."""
    assert "general.digest_schedule" not in _seed()["settings"]
    assert _seed()["settings"]["general.timezone"] == "UTC"


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
