"""Sources in D1, and the fallback that keeps a half-migrated deployment fetching."""

from pathlib import Path

import pytest
from fakes import SqliteD1

from cyris.adapters.store.source_store import D1SourceStore
from cyris.config import load_config
from cyris.domain.models import SourceConfig, Tier

CONFIG = """
[agent_vault]
path = "{vault}"

[obsidian]
user_vault_path = "{vault}"

[store]
backend = "{backend}"
database_id = "db"
"""

SOURCES_YAML = """
sources:
  - name: "From File"
    url: "https://file.test/feed"
    tier: filter
"""


@pytest.fixture
def store() -> D1SourceStore:
    return D1SourceStore(SqliteD1())


def _sources(*configs: SourceConfig) -> dict[str, SourceConfig]:
    return {s.name: s for s in configs}


def test_round_trips_every_field(store: D1SourceStore) -> None:
    """Only name/url/type are columns; the rest has to survive the JSON blob."""
    source = SourceConfig(
        name="曼報",
        url=None,
        type="newsletter",
        tier=Tier.SUMMARIZE,
        tags=["newsletter", "tech"],
        language="zh",
        email_match="from:hi@manpao.test",
        homepage="https://manpao.test",
    )

    store.replace_all(_sources(source))

    assert store.list_sources() == {"曼報": source}


def test_push_is_a_replacement_not_a_merge(store: D1SourceStore) -> None:
    """A source deleted from sources.yaml has to stop being polled."""
    store.replace_all(_sources(SourceConfig(name="Old", url="https://old.test/feed")))

    store.replace_all(_sources(SourceConfig(name="New", url="https://new.test/feed")))

    assert list(store.list_sources()) == ["New"]


def test_push_batches_past_the_bound_parameter_limit(store: D1SourceStore) -> None:
    sources = _sources(
        *[SourceConfig(name=f"Feed {i}", url=f"https://a.test/{i}") for i in range(60)]
    )

    assert store.replace_all(sources) == 60
    assert len(store.list_sources()) == 60


def _write_config(tmp_path: Path, backend: str) -> tuple[Path, Path]:
    config_path = tmp_path / "cyris.toml"
    config_path.write_text(CONFIG.format(vault=tmp_path, backend=backend), encoding="utf-8")
    sources_path = tmp_path / "sources.yaml"
    sources_path.write_text(SOURCES_YAML, encoding="utf-8")
    return config_path, sources_path


def test_the_file_is_used_when_the_backend_is_json(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "tok")
    config_path, sources_path = _write_config(tmp_path, "json")

    cfg = load_config(config_path=config_path, sources_path=sources_path)

    assert list(cfg.sources) == ["From File"]


def test_d1_sources_win_when_the_table_has_rows(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "tok")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct")
    db = SqliteD1()
    D1SourceStore(db).replace_all(_sources(SourceConfig(name="From D1", url="https://d1.test/f")))
    monkeypatch.setattr("cyris.adapters.store.d1.D1Client.__new__", lambda _cls, **_kw: db)
    config_path, sources_path = _write_config(tmp_path, "d1")

    cfg = load_config(config_path=config_path, sources_path=sources_path)

    assert list(cfg.sources) == ["From D1"]


def test_an_empty_table_falls_back_to_the_file(tmp_path: Path, monkeypatch) -> None:
    """A deployment that switched to D1 but has not pushed yet must still fetch."""
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "tok")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct")
    db = SqliteD1()
    monkeypatch.setattr("cyris.adapters.store.d1.D1Client.__new__", lambda _cls, **_kw: db)
    config_path, sources_path = _write_config(tmp_path, "d1")

    cfg = load_config(config_path=config_path, sources_path=sources_path)

    assert list(cfg.sources) == ["From File"]


def test_an_unreachable_d1_falls_back_to_the_file(tmp_path: Path, monkeypatch) -> None:
    """Dropping every source would look like a quiet news day, not an outage."""
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "tok")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct")

    class Dead:
        def query(self, *_a, **_k):
            raise RuntimeError("connection refused")

    monkeypatch.setattr("cyris.adapters.store.d1.D1Client.__new__", lambda _cls, **_kw: Dead())
    config_path, sources_path = _write_config(tmp_path, "d1")

    cfg = load_config(config_path=config_path, sources_path=sources_path)

    assert list(cfg.sources) == ["From File"]
