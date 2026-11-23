"""A hosted tenant's run loads a schedule of one digest hour; a self-hosted run never does.

The hosted Worker's registry decides how many digest times a tenant has, one unless the
operator allows two (ADR-0042), and copies them into the tenant's own `settings`. The run
container reads that copy, so its config must take one hour where a self-hosted deployment,
which publishes a morning and an evening issue, still takes exactly two.
"""

from pathlib import Path

import pytest
from fakes import load_files, stored_settings

from cyris.config import OPERATOR_TENANT_ENV_VAR, validate_setting

pytestmark = pytest.mark.unit

SCHEDULE_KEY = "general.digest_schedule"


@pytest.fixture
def no_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("CYRIS_STORE_TENANT_ID", "CYRIS_STORE_HOSTED_DOMAIN", OPERATOR_TENANT_ENV_VAR):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def run_container(no_tenant: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """The names the hosted Worker sets on a tenant's run container (hosted_container.js)."""
    monkeypatch.setenv("CYRIS_STORE_BACKEND", "d1")
    monkeypatch.setenv("CYRIS_STORE_TENANT_ID", "alpha")
    monkeypatch.setenv("CYRIS_STORE_HOSTED_DOMAIN", "hosted.example")


def _load(tmp_path: Path, times: list[str]):
    settings = stored_settings(**{SCHEDULE_KEY: times})
    return load_files(tmp_path / "cyris.toml", tmp_path / "sources.yaml", settings)


def test_a_hosted_tenants_run_loads_its_one_digest_hour(tmp_path: Path, run_container: None):
    cfg = _load(tmp_path, ["13:00"])

    assert cfg.app.store.is_hosted
    assert SCHEDULE_KEY not in cfg.missing_settings
    assert cfg.app.general is not None
    assert cfg.app.general.digest_schedule == ["13:00"]


def test_a_hosted_tenants_run_loads_two_hours_and_refuses_three(
    tmp_path: Path, run_container: None
):
    assert _load(tmp_path, ["20:00", "8:00"]).app.general.digest_schedule == ["08:00", "20:00"]
    assert SCHEDULE_KEY in _load(tmp_path, ["08:00", "12:00", "20:00"]).missing_settings


def test_the_operators_way_into_a_tenant_loads_its_one_hour_too(
    tmp_path: Path, no_tenant: None, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv(OPERATOR_TENANT_ENV_VAR, "alpha")
    monkeypatch.setenv("CYRIS_STORE_HOSTED_DOMAIN", "hosted.example")

    cfg = _load(tmp_path, ["13:00"])

    assert cfg.app.store.operator
    assert cfg.app.general.digest_schedule == ["13:00"]


def test_a_self_hosted_run_still_takes_exactly_two_hours(tmp_path: Path, no_tenant: None):
    cfg = _load(tmp_path, ["13:00"])

    assert not cfg.app.store.is_hosted
    assert SCHEDULE_KEY in cfg.missing_settings
    assert cfg.app.general is None
    with pytest.raises(ValueError, match="exactly two times"):
        validate_setting(SCHEDULE_KEY, ["13:00"])
    assert validate_setting(SCHEDULE_KEY, ["13:00"], hosted=True) == ["13:00"]
