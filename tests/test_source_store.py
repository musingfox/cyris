"""Sources in D1, and a D1 deployment that fetches exactly what the table holds."""

from pathlib import Path

import pytest
from fakes import SqliteD1

from cyris.adapters.store.source_store import D1SourceStore
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


def _load(tmp_path: Path, backend: str, monkeypatch, db=None):
    from cyris.bootstrap import load_effective_config

    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "tok")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct")
    constructed: list[dict] = []

    def client(**kwargs):
        constructed.append(kwargs)
        return db

    monkeypatch.setattr("cyris.adapters.store.d1.D1Client", client)
    config_path, sources_path = _write_config(tmp_path, backend)
    return load_effective_config(config_path, sources_path), constructed


def test_a_json_deployment_reads_the_file_and_never_opens_d1(tmp_path: Path, monkeypatch) -> None:
    cfg, constructed = _load(tmp_path, "json", monkeypatch)

    assert list(cfg.sources) == ["From File"]
    assert constructed == []


def test_a_d1_deployment_reads_only_the_table(tmp_path: Path, monkeypatch) -> None:
    db = SqliteD1()
    D1SourceStore(db).replace_all(_sources(SourceConfig(name="From D1", url="https://d1.test/f")))

    cfg, _ = _load(tmp_path, "d1", monkeypatch, db)

    assert set(cfg.sources) == {"From D1"}


def test_an_empty_table_is_no_sources_not_the_file(tmp_path: Path, monkeypatch) -> None:
    cfg, _ = _load(tmp_path, "d1", monkeypatch, SqliteD1())

    assert cfg.sources == {}


def test_an_unreadable_table_fails_the_load(tmp_path: Path, monkeypatch) -> None:
    """Dropping to the file on error would decide a run from a list D1 no longer holds."""
    from cyris.adapters.store.d1 import D1Error

    def boom(self):
        raise D1Error("boom")

    monkeypatch.setattr(D1SourceStore, "list_sources", boom)

    with pytest.raises(D1Error, match="boom"):
        _load(tmp_path, "d1", monkeypatch, SqliteD1())


def test_a_fresh_database_is_read_after_its_tables_exist(tmp_path: Path, monkeypatch) -> None:
    cfg, _ = _load(tmp_path, "d1", monkeypatch, SqliteD1(with_schema=False))

    assert cfg.sources == {}


def test_sources_list_on_an_empty_table_says_runs_stop(tmp_path: Path, monkeypatch) -> None:
    from typer.testing import CliRunner

    from cyris.entrypoints.cli import app

    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "tok")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct")
    db = SqliteD1()
    monkeypatch.setattr("cyris.adapters.store.d1.D1Client", lambda **_kw: db)
    config_path, sources_path = _write_config(tmp_path, "d1")

    result = CliRunner().invoke(
        app, ["sources", "list", "--config", str(config_path), "--sources", str(sources_path)]
    )

    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == (
        "D1 has no sources; runs stop until you add one on /settings or run `cyris sources push`."
    )
    assert "fall back" not in result.stdout
