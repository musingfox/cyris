"""A hosted tenant's settings page holds a value to the rule the tenant's run holds it to.

The hosted Worker checks a tenant's save itself (workers/app/src/hosted_settings_rules.js), with no
container started (ADR-0040), and the run then reads the row with `validate_setting`. So each value
the Worker takes must be one the run takes, and each it refuses one the run refuses or one no tenant
may write. `workers/app/test/hosted_settings_cases.json` is the one list both suites read. The
policy file the Worker reads is the one the run pins its settings to, so its caps are checked here
against the run's own: the poll's against `feed_check.py`'s, the seed's model against the policy's.
"""

import json
import re
from pathlib import Path
from typing import get_args

import pytest

from cyris.adapters.fetch import feed_check
from cyris.adapters.hosted import POLICY_PATH, hosted_policy
from cyris.adapters.store.source_store import source_from_row
from cyris.config import GRADE_D_KEYS, validate_setting
from cyris.entrypoints.triage_server import LockReason
from cyris.i18n import LOCALES, resolve

pytestmark = [pytest.mark.unit, pytest.mark.guard]

ROOT = Path(__file__).resolve().parents[1]
CASES = json.loads((ROOT / "workers/app/test/hosted_settings_cases.json").read_text("utf-8"))
POLICY = hosted_policy()
WORKER = ROOT / "workers/app/src"
TOKENS = ROOT / "src/cyris/adapters/output/templates/_tokens.css.j2"


def _runs_on(key: str, value: object) -> bool:
    """Whether a hosted run takes `value` for `key`: its rule, and the policy's cap on top."""
    try:
        valid = validate_setting(key, value)
    except ValueError:
        return False
    return key != "digest.max_articles_per_digest" or valid <= POLICY["max_articles_per_digest"]


def _named(case: dict) -> str:
    return f"{case['key']}={case['value']!r}"


@pytest.mark.parametrize("case", CASES["settings"]["accepted"], ids=_named)
def test_a_value_the_page_takes_is_one_the_run_takes_as_it_is(case) -> None:
    assert case["key"] in POLICY["writable_settings"]
    assert _runs_on(case["key"], case["value"])
    assert validate_setting(case["key"], case["value"]) == case["value"]


@pytest.mark.parametrize("case", CASES["settings"]["refused"], ids=_named)
def test_a_value_the_page_refuses_is_one_the_run_refuses_or_no_tenant_writes(case) -> None:
    key, reason = case["key"], case["reason"]
    if reason == "operator_key":
        assert key not in POLICY["writable_settings"]
        assert key in GRADE_D_KEYS or key in POLICY["operator_keys"]
    elif reason == "unknown_key":
        assert key not in GRADE_D_KEYS and key not in POLICY["operator_keys"]
    else:
        assert key in POLICY["writable_settings"]
        assert not _runs_on(key, case["value"])


def test_every_writable_key_is_a_settings_key_and_none_is_the_operators() -> None:
    writable = set(POLICY["writable_settings"])

    assert writable <= set(GRADE_D_KEYS)
    assert not writable & {"llm_provider.provider", "llm_provider.model", *POLICY["blank_settings"]}
    assert not any(key.startswith("notify.") for key in writable)


def test_the_shared_page_locks_for_one_of_the_two_reasons() -> None:
    """The shared settings page locks every key a tenant does not write (ADR-0043), for a reason
    the page words."""
    assert POLICY["shared_page"]["operator_reason"] in get_args(LockReason)


@pytest.mark.parametrize("case", CASES["sources"]["written"], ids=lambda c: c["row"]["name"])
def test_a_source_the_page_writes_is_one_the_run_fetches(case) -> None:
    row = case["row"]

    source = source_from_row({**row, "config": json.dumps(row["config"])})

    assert source.fetchable
    assert source.url == row["url"]
    assert source.lane.value == row["config"]["lane"]
    assert source.tags == row["config"].get("tags", [])


@pytest.mark.parametrize("case", CASES["languages"], ids=lambda c: c["tag"])
def test_the_page_is_worded_in_the_catalog_the_run_words_its_pages_in(case) -> None:
    assert resolve(case["tag"]) == case["catalog"]


def test_the_page_reads_every_catalog_there_is() -> None:
    source = (WORKER / "hosted_settings.js").read_text("utf-8")
    imported = set(re.findall(r'from "\.\./\.\./\.\./src/cyris/locales/([\w-]+)\.json"', source))

    assert imported == {path.stem for path in LOCALES.glob("*.json")}


def test_the_poll_caps_are_the_feed_checks() -> None:
    assert POLICY["poll"]["max_redirects"] == feed_check.MAX_REDIRECTS
    assert POLICY["poll"]["max_feed_bytes"] == feed_check.MAX_BYTES


def test_the_seed_names_the_policys_model() -> None:
    seed = json.loads((WORKER / "hosted_seed.json").read_text("utf-8"))["settings"]

    assert seed["llm_provider.provider"] == POLICY["provider"]
    assert seed["llm_provider.model"] == POLICY["model"]
    assert all(seed[key] == "" for key in POLICY["blank_settings"])
    assert (
        POLICY["max_articles_per_digest"]
        >= json.loads((ROOT / "src/cyris/settings_starting_values.json").read_text("utf-8"))[
            "digest.max_articles_per_digest"
        ]
    )


# The Worker's own pages, each with its tokens in one `const STYLE`.
@pytest.mark.parametrize("page", ["hosted_settings.js", "hosted_legal.js"])
def test_the_pages_tokens_are_the_specs(page) -> None:
    declared = dict(re.findall(r"(--[\w-]+):\s*([^;}\n]+?)\s*;", TOKENS.read_text("utf-8")))
    source = (WORKER / page).read_text("utf-8")
    root = source.split("const STYLE = `", 1)[1].split("}", 1)[0]
    used = {name: value.strip() for name, value in re.findall(r"(--[\w-]+):([^;}]+)", root)}

    assert used
    assert used == {name: declared[name] for name in used}


def test_the_image_carries_the_policy_where_the_run_reads_it() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text("utf-8")
    workdir = re.search(r"^WORKDIR (\S+)$", dockerfile, re.M)[1]
    relative = "workers/app/src/hosted_settings_policy.json"

    assert re.search(r"^COPY src \./src$", dockerfile, re.M)
    assert f"COPY {relative} {workdir}/{relative}" in dockerfile.splitlines()
    assert f"!{relative}" in (ROOT / ".dockerignore").read_text("utf-8").splitlines()
    assert ROOT / relative == POLICY_PATH
