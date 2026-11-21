"""The hosted policy file is one file, read by the hosted Worker and by the tenant's run.

The run pins its provider, model and notify targets, and clamps its article cap, to the policy
file the hosted Worker holds a tenant's settings to (ADR-0040). So the image carries the file
where the run reads it, its keys are settings keys no tenant may use to reach the operator's, and
its caps agree with the run's own: the poll's with `feed_check.py`'s, the seed's model with its.
"""

import json
import re
from pathlib import Path

import pytest

from cyris.adapters.fetch import feed_check
from cyris.adapters.hosted import POLICY_PATH, hosted_policy
from cyris.config import GRADE_D_KEYS

pytestmark = [pytest.mark.unit, pytest.mark.guard]

ROOT = Path(__file__).resolve().parents[1]
POLICY = hosted_policy()
WORKER = ROOT / "workers/app/src"


def test_every_writable_key_is_a_settings_key_and_none_is_the_operators() -> None:
    writable = set(POLICY["writable_settings"])

    assert writable <= set(GRADE_D_KEYS)
    assert not writable & {"llm_provider.provider", "llm_provider.model", *POLICY["blank_settings"]}
    assert not any(key.startswith("notify.") for key in writable)


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


def test_the_image_carries_the_policy_where_the_run_reads_it() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text("utf-8")
    workdir = re.search(r"^WORKDIR (\S+)$", dockerfile, re.M)[1]
    relative = "workers/app/src/hosted_settings_policy.json"

    assert re.search(r"^COPY src \./src$", dockerfile, re.M)
    assert f"COPY {relative} {workdir}/{relative}" in dockerfile.splitlines()
    assert f"!{relative}" in (ROOT / ".dockerignore").read_text("utf-8").splitlines()
    assert ROOT / relative == POLICY_PATH
