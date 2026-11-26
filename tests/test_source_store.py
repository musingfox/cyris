"""Sources in D1, and a D1 deployment that fetches exactly what the table holds."""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx
from fakes import SqliteD1

from cyris.adapters.fetch.newsletter_worker_source import CloudflareNewsletterSource
from cyris.adapters.store.source_store import D1SourceStore
from cyris.domain.models import SOURCE_DECISIONS, Lane, SourceConfig

pytestmark = pytest.mark.integration

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
    lane: skim
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
        lane=Lane.READ,
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


def test_a_push_keeps_each_reviewed_row_and_the_review_of_a_source_it_names(
    store: D1SourceStore,
) -> None:
    """A stopped row is what keeps its sender's mail out; the file cannot say it."""

    def letter(name: str, review: str | None = None, lane: Lane = Lane.READ) -> SourceConfig:
        return SourceConfig(
            name=name, type="newsletter", lane=lane, email_match=f"from:{name}", review=review
        )

    store.replace_all(_sources(SourceConfig(name="Old", url="https://old.test/feed")))
    for made in (letter("new@x.test", "new"), letter("gone@x.test", "declined")):
        store.insert(made)
    store.insert(letter("named@x.test", "declined"))

    store.replace_all(
        _sources(letter("named@x.test", lane=Lane.FOLLOW), letter("plain@x.test", "declined"))
    )

    assert store.list_sources() == _sources(
        letter("gone@x.test", "declined"),
        letter("named@x.test", "declined", Lane.FOLLOW),
        letter("new@x.test", "new"),
        letter("plain@x.test"),
    )


async def test_a_kept_source_outlives_a_push_and_takes_its_senders_next_mail(
    store: D1SourceStore,
) -> None:
    """Keep is a decision that lasts: a push that never names the source leaves its row."""
    worker = "https://newsletters.example"
    store.replace_all(_sources(SourceConfig(name="Blog", url="https://blog.test/feed")))
    store.insert(
        SourceConfig(
            name="news@letter.test",
            type="newsletter",
            lane=Lane.READ,
            email_match="from:news@letter.test",
            review="new",
        )
    )

    store.set_review("news@letter.test", SOURCE_DECISIONS["keep"])
    store.replace_all(_sources(SourceConfig(name="Blog", url="https://blog.test/feed")))
    mail = {"id": "nl:1", "from": "news@letter.test", "subject": "Issue 8", "text": "body"}
    with respx.mock:
        pull = respx.get(f"{worker}/newsletters").mock(
            return_value=httpx.Response(200, json=[mail])
        )
        ack = respx.post(f"{worker}/ack").mock(return_value=httpx.Response(200, json={}))
        source = CloudflareNewsletterSource(worker, "tok", create_source=store.insert)
        now = datetime(2026, 11, 26, tzinfo=UTC)
        articles = await source.fetch_articles(now, now, store.list_sources())

    assert {name: s.review for name, s in store.list_sources().items()} == {
        "Blog": None,
        "news@letter.test": "kept",
    }
    assert [a.source_name for a in articles] == ["news@letter.test"]
    assert (pull.call_count, ack.call_count) == (1, 1)
    assert json.loads(ack.calls.last.request.content) == {"ids": ["nl:1"]}


def test_insert_writes_a_new_name_and_never_overwrites_one(store: D1SourceStore) -> None:
    held = SourceConfig(
        name="Letter", type="newsletter", lane=Lane.FOLLOW, email_match="from:a@x.test"
    )
    store.replace_all(_sources(held))
    other = SourceConfig(
        name="Letter", type="newsletter", lane=Lane.READ, email_match="from:b@x.test"
    )
    new = SourceConfig(
        name="b@x.test", type="newsletter", lane=Lane.READ, email_match="from:b@x.test"
    )

    assert store.insert(other) is False
    assert store.insert(new) is True
    assert store.list_sources() == {"Letter": held, "b@x.test": new}


def test_a_review_is_set_and_dropped_and_an_ordinary_row_carries_none(store: D1SourceStore) -> None:
    plain = SourceConfig(name="Plain", url="https://plain.test/feed")
    new = SourceConfig(
        name="b@x.test", type="newsletter", email_match="from:b@x.test", review="new"
    )
    store.replace_all(_sources(plain))
    store.insert(new)

    declined = store.set_review("b@x.test", "declined")
    after_decline = store.list_sources()["b@x.test"].review
    kept = store.set_review("b@x.test", None)

    assert (declined, after_decline, kept) == (1, "declined", 1)
    assert store.list_sources() == {
        "Plain": plain,
        "b@x.test": new.model_copy(update={"review": None}),
    }
    assert store.set_review("Nobody", "declined") == 0
    rows = store._db.query("SELECT name, config FROM sources ORDER BY name").rows
    assert all("review" not in json.loads(row["config"]) for row in rows)


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
