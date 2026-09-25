"""`build_promotion_sync`: the one wiring of the vote pull, shared by a run and `promote-sync`."""

from pathlib import Path

import pytest
from fakes import make_config

from cyris import bootstrap
from cyris.config import AgentVaultConfig, PromoteConfig

pytestmark = pytest.mark.integration


def test_no_vote_worker_builds_nothing(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    cfg = make_config(
        promote=PromoteConfig(worker_url="", token=""), agent_vault=AgentVaultConfig(path=vault)
    )

    assert bootstrap.build_promotion_sync(cfg) is None
    assert not vault.exists()  # no store was built for a pull that cannot happen


def test_a_vote_worker_binds_the_pull_to_the_store(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        "cyris.bootstrap.sync_promotions",
        lambda url, token, store: calls.append((url, token, store)) or 3,
    )
    cfg = make_config(promote=PromoteConfig(worker_url="https://votes.example", token="t"))
    # promote-sync starts on an empty D1, where the settings tables are unbuilt.
    cfg.app.llm_provider = None
    cfg.app.vote_similarity = None
    store = object()

    sync = bootstrap.build_promotion_sync(cfg, store)

    assert sync() == 3
    assert calls == [("https://votes.example", "t", store)]
