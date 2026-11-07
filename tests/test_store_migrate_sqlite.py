"""`cyris store migrate --to sqlite` moves a whole json install into its SQLite file.

The json install is built with every kind of state that backend keeps: article
partitions (one URL stored twice, as the 8-day dedup allows), human verdicts,
`topics.json`, `usage.jsonl`, every grade-D key and `[[tracked_topics]]` in
`cyris.toml`, and `sources.yaml`. After the move the install is loaded again with
`backend = "sqlite"`, through `load_effective_config` and the stores `bootstrap`
builds, and must hold what the json side held.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fakes import settings_toml
from typer.testing import CliRunner

from cyris import bootstrap
from cyris.adapters.store import ArticleStore
from cyris.adapters.store.d1_store import D1ArticleStore
from cyris.adapters.store.json_topics import JsonTopicStore
from cyris.adapters.store.local_d1 import LocalD1Client
from cyris.adapters.store.settings import D1Settings
from cyris.adapters.store.source_store import D1SourceStore
from cyris.adapters.store.tracked_topics import D1TrackedTopicStore
from cyris.domain.models import (
    ArticleState,
    Lane,
    SourceConfig,
    StoredArticle,
    TopicRecord,
    TopicTag,
    TopicVote,
    TrackedTopic,
)
from cyris.entrypoints.cli import _ALL_ARTICLES, _newest_by_url, app

pytestmark = pytest.mark.integration

runner = CliRunner()

FIELDS = ("state", "score", "language", "triaged_at", "digest_date", "rejection_reason")

TRACKED = """
[[tracked_topics]]
name = "Rust"
description = "The Rust programming language and its ecosystem"
threshold = 0.42
model = "text-embedding-3-small"

[[tracked_topics]]
name = "Fusion"
description = "Nuclear fusion power research"
threshold = 0.5
model = "text-embedding-3-small"
"""

SOURCES = """\
sources:
  - name: Alpha
    url: https://alpha.example/feed.xml
    lane: read
    tags: [tech]
  - name: Beta
    url: https://beta.example/rss
    language: en
"""

USAGE = [
    {
        "timestamp": "2026-10-01T00:05:01.123456+00:00",
        "date": "2026-10-01",
        "period": "morning",
        "articles_received": 40,
        "articles_included": 12,
        "model": "claude-haiku-5-5",
        "api_calls": 3,
        "input_tokens": 1200,
        "output_tokens": 300,
        "total_tokens": 1500,
        "estimated_cost_usd": 0.004321,
    },
    {
        "timestamp": "2026-10-11T12:05:02.654321+00:00",
        "date": "2026-10-11",
        "period": "evening",
        "articles_received": 25,
        "articles_included": 8,
        "model": "local-unpriced",
        "api_calls": 2,
        "input_tokens": 800,
        "output_tokens": 100,
        "total_tokens": 900,
        "estimated_cost_usd": None,
    },
]


def _article(url: str, seen: datetime, **state) -> StoredArticle:
    return StoredArticle(
        url=url,
        original_id=url,
        title=f"Title of {url}",
        content=f"Body of {url}",
        published_at=seen - timedelta(hours=1),
        source_name="Alpha",
        source_lane=Lane.READ,
        first_seen_at=seen,
        **state,
    )


def _partition(vault: Path, day: str, articles: list[StoredArticle]) -> None:
    path = vault / "articles" / f"{day}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([a.model_dump(mode="json") for a in articles]))


def _json_install(root: Path) -> Path:
    """A json install in `root`, holding every kind of state; returns its cyris.toml."""
    vault = root / "agent-vault"
    day1 = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
    day2 = day1 + timedelta(days=10)
    voted = day1 + timedelta(hours=20)

    _partition(
        vault,
        "2026-10-01",
        [
            # Stored again ten days later: the older copy says pending, the newer accepted.
            _article("https://alpha.example/twice", day1, score=0.3, language="en"),
            _article(
                "https://alpha.example/rejected",
                day1 + timedelta(minutes=5),
                state=ArticleState.REJECTED,
                rejection_reason="off topic",
                digest_date="2026-10-01",
                triaged_at=voted,
                score=0.1,
                language="zh-Hant",
            ),
        ],
    )
    _partition(
        vault,
        "2026-10-11",
        [
            _article(
                "https://alpha.example/twice",
                day2,
                state=ArticleState.ACCEPTED,
                digest_date="2026-10-11",
                triaged_at=day2 + timedelta(hours=2),
                score=0.9,
                language="en",
            ),
            _article(
                "https://alpha.example/pipeline",
                day2 + timedelta(minutes=1),
                state=ArticleState.ACCEPTED,
                digest_date="2026-10-11",
                score=0.7,
                language="ja",
            ),
            _article("https://alpha.example/pending", day2 + timedelta(minutes=2)),
        ],
    )

    topics = JsonTopicStore(vault)
    rust = TopicTag(tag="rust", kind="entity")
    topics.save_topics(
        "2026-10-01",
        "morning",
        [
            TopicRecord(
                id="t-rust", name="Rust 2.0", urls=["https://alpha.example/twice"], tags=[rust]
            ),
            TopicRecord(id="t-gone", name="Gone", urls=["https://alpha.example/rejected"]),
        ],
        day1 + timedelta(hours=1),
    )
    topics.record_topic_votes(
        [
            TopicVote(topic_id="t-gone", direction=-1, clicked_at=voted),
            TopicVote(topic_id="t-rust", direction=1, clicked_at=voted),
        ]
    )
    # A rerun of the issue drops t-gone's member; its vote keeps the topic.
    topics.save_topics(
        "2026-10-01",
        "morning",
        [
            TopicRecord(
                id="t-rust", name="Rust 2.0", urls=["https://alpha.example/twice"], tags=[rust]
            )
        ],
        day1 + timedelta(hours=2),
    )
    topics.save_topics(
        "2026-10-11",
        "evening",
        [
            TopicRecord(
                id="t-rust",
                name="Rust 2.1",
                urls=["https://alpha.example/twice", "https://alpha.example/pipeline"],
                tags=[rust, TopicTag(tag="compilers", kind="domain")],
            )
        ],
        day2 + timedelta(hours=1),
    )

    (vault / "usage.jsonl").write_text("".join(json.dumps(r) + "\n" for r in USAGE))
    (root / "sources.yaml").write_text(SOURCES)
    config = root / "cyris.toml"
    config.write_text(
        '[store]\nbackend = "json"\nsqlite_path = "state/cyris.sqlite3"\n\n'
        '[agent_vault]\npath = "agent-vault"\n\n' + settings_toml() + TRACKED
    )
    return config


def _cli(config: Path, *command: str):
    sources = config.parent / "sources.yaml"
    return runner.invoke(app, [*command, "--config", str(config), "--sources", str(sources)])


def _flip(config: Path, backend: str = "sqlite") -> None:
    text = config.read_text()
    config.write_text(text.replace('backend = "json"', f'backend = "{backend}"', 1))


def _rows(client, table: str, order: str) -> list[dict]:
    return client.query(f"SELECT * FROM {table} ORDER BY {order}").rows


@pytest.fixture
def install(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.chdir(tmp_path)
    for name in ("CYRIS_STORE_BACKEND", "CYRIS_STORE_SQLITE_PATH"):
        monkeypatch.delenv(name, raising=False)
    return _json_install(tmp_path)


def test_a_json_install_lands_whole_in_its_sqlite_file(install: Path) -> None:
    sources = install.parent / "sources.yaml"
    before = bootstrap.load_effective_config(install, sources)
    vault = before.app.agent_vault.path
    expected = _newest_by_url(ArticleStore(vault).list_articles(state=None, limit=_ALL_ARTICLES))
    json_topics = JsonTopicStore(vault)
    raw_topics = json.loads((vault / "topics.json").read_text())

    result = _cli(install, "store", "migrate", "--to", "sqlite")
    assert result.exit_code == 0, result.output
    assert 'backend = "sqlite"' in result.output

    _flip(install)
    cfg = bootstrap.load_effective_config(install, sources)
    assert cfg.app.store.backend == "sqlite"
    assert cfg.settings_values == before.settings_values
    assert cfg.missing_settings == before.missing_settings == []
    assert cfg.sources == before.sources and len(cfg.sources) == 2
    # The table lists by name; the file in the order it was written.
    by_name = sorted(before.tracked_topics, key=lambda t: t.name)
    assert cfg.tracked_topics == by_name and len(by_name) == 2

    store = bootstrap.build_store(cfg)
    moved = {a.url: a for a in store.get_by_urls(list(expected))}
    assert moved.keys() == expected.keys()
    for url, article in expected.items():
        for field in FIELDS:
            assert getattr(moved[url], field) == getattr(article, field), (url, field)
    assert moved["https://alpha.example/twice"].state == ArticleState.ACCEPTED

    topics = bootstrap.build_topic_store(cfg)
    ids = list(raw_topics["topics"])
    assert sorted(ids) == ["t-gone", "t-rust"]
    by_id = lambda t: t.id  # noqa: E731
    assert sorted(topics.topics_by_ids(ids), key=by_id) == sorted(
        json_topics.topics_by_ids(ids), key=by_id
    )
    member_urls = [m["article_url"] for m in raw_topics["members"]]
    assert topics.topics_of_urls(member_urls) == json_topics.topics_of_urls(member_urls)
    by_vote = lambda v: (v.topic_id, v.clicked_at)  # noqa: E731
    assert sorted(topics.topic_votes(), key=by_vote) == sorted(
        json_topics.topic_votes(), key=by_vote
    )

    client = bootstrap.build_d1_client(cfg)
    assert [
        {k: r[k] for k in ("id", "name", "first_seen", "last_seen")}
        for r in _rows(client, "topics", "id")
    ] == [{"id": i, **raw_topics["topics"][i]} for i in sorted(raw_topics["topics"])]
    member_key = ("topic_id", "article_url", "digest_date", "period", "seen_at")
    assert sorted(
        tuple(r[k] for k in member_key) for r in _rows(client, "topic_members", "seen_at")
    ) == sorted(tuple(m[k] for k in member_key) for m in raw_topics["members"])
    assert sorted(
        (r["topic_id"], r["tag"], r["kind"]) for r in _rows(client, "topic_tags", "tag")
    ) == sorted((g["topic_id"], g["tag"], g["kind"]) for g in raw_topics["tags"])

    usage = _rows(client, "usage_log", "logged_at")
    assert len(usage) == len(USAGE)
    for row, record in zip(usage, USAGE, strict=True):
        assert row == {
            "logged_at": record["timestamp"],
            "digest_date": record["date"],
            "period": record["period"],
            "articles_received": record["articles_received"],
            "articles_included": record["articles_included"],
            "model": record["model"],
            "api_calls": record["api_calls"],
            "input_tokens": record["input_tokens"],
            "output_tokens": record["output_tokens"],
            "cost_usd": record["estimated_cost_usd"] or 0,
        }


def _table_counts(path: Path) -> dict[str, int]:
    client = LocalD1Client(path)
    tables = (
        "stored_articles",
        "settings",
        "sources",
        "tracked_topics",
        "topics",
        "topic_members",
        "topic_tags",
        "topic_votes",
        "usage_log",
    )
    return {t: client.query(f"SELECT count(*) AS n FROM {t}").rows[0]["n"] for t in tables}


def test_a_second_migration_writes_nothing_and_says_so(install: Path) -> None:
    first = _cli(install, "store", "migrate", "--to", "sqlite")
    assert first.exit_code == 0, first.output
    counts = _table_counts(install.parent / "state" / "cyris.sqlite3")

    second = _cli(install, "store", "migrate", "--to", "sqlite")

    assert second.exit_code == 0, second.output
    assert "Nothing written" in second.output
    assert _table_counts(install.parent / "state" / "cyris.sqlite3") == counts
    assert counts["usage_log"] == len(USAGE) and counts["stored_articles"] == 4


def test_what_the_file_already_holds_is_kept_and_named(install: Path) -> None:
    path = install.parent / "state" / "cyris.sqlite3"
    client = LocalD1Client(path)
    from cyris.adapters.store.d1 import apply_schema

    apply_schema(client)
    D1Settings(client).set({"llm_provider.model": "kept-model"})
    D1TrackedTopicStore(client).upsert(
        TrackedTopic(name="Rust", description="Kept", threshold=0.3, model="m")
    )
    D1SourceStore(client).upsert(SourceConfig(name="Gamma", url="https://gamma.example/feed"))
    seen = datetime(2026, 10, 11, tzinfo=UTC)
    D1ArticleStore(client).import_articles(
        [_article("https://alpha.example/pending", seen, state=ArticleState.REJECTED)]
    )

    result = _cli(install, "store", "migrate", "--to", "sqlite")

    assert result.exit_code == 0, result.output
    assert D1Settings(client).all()["llm_provider.model"] == "kept-model"
    assert "  kept     llm_provider.model (file has " in result.output
    [rust] = [t for t in D1TrackedTopicStore(client).list_topics() if t.name == "Rust"]
    assert rust.description == "Kept"
    assert "kept     tracked topic Rust" in result.output
    assert list(D1SourceStore(client).list_sources()) == ["Gamma"]
    assert "already holds 1 source" in result.output
    [pending] = D1ArticleStore(client).get_by_urls(["https://alpha.example/pending"])
    assert pending.state == ArticleState.REJECTED
    assert "kept     https://alpha.example/pending" in result.output
    # Everything the file lacked still arrived beside what it kept.
    assert (
        len(
            D1ArticleStore(client).get_by_urls(
                ["https://alpha.example/twice", "https://alpha.example/pipeline"]
            )
        )
        == 2
    )


def test_the_json_side_is_left_as_it_was(install: Path) -> None:
    vault = install.parent / "agent-vault"
    before = {
        p: p.read_bytes()
        for p in [install, install.parent / "sources.yaml", *vault.rglob("*")]
        if p.is_file()
    }

    result = _cli(install, "store", "migrate", "--to", "sqlite")

    assert result.exit_code == 0, result.output
    after = {
        p: p.read_bytes()
        for p in [install, install.parent / "sources.yaml", *vault.rglob("*")]
        if p.is_file()
    }
    assert after == before


@pytest.mark.parametrize("backend", ["json", "sqlite"])
def test_store_diff_refuses_a_file_that_is_not_there_and_creates_none(
    install: Path, backend: str
) -> None:
    _flip(install, backend)

    result = _cli(install, "store", "diff", "--to", "sqlite")

    assert result.exit_code == 1, result.output
    [line] = result.output.splitlines()
    assert line.startswith("✗ No SQLite file at state/cyris.sqlite3"), line
    assert not (install.parent / "state").exists()


def test_store_diff_against_the_file_agrees_after_the_move(install: Path) -> None:
    _cli(install, "store", "migrate", "--to", "sqlite")
    after = _cli(install, "store", "diff", "--to", "sqlite")

    assert after.exit_code == 0, after.output
    assert "only local (0):" in after.output
    assert "only sqlite (0):" in after.output
    assert "differing: 0" in after.output


@pytest.mark.parametrize("backend", ["sqlite", "d1"])
def test_a_store_that_is_not_json_has_nothing_to_move(install: Path, backend: str) -> None:
    _flip(install, backend)

    result = _cli(install, "store", "migrate", "--to", "sqlite")

    assert result.exit_code == 1
    assert f"backend is {backend}" in result.output
    assert not (install.parent / "state" / "cyris.sqlite3").exists()


def test_a_file_inside_the_vault_is_refused(install: Path) -> None:
    text = install.read_text().replace("state/cyris.sqlite3", "agent-vault/cyris.sqlite3")
    install.write_text(text)

    result = _cli(install, "store", "migrate", "--to", "sqlite")

    assert result.exit_code == 1
    assert "inside the agent vault" in result.output
    assert not (install.parent / "agent-vault" / "cyris.sqlite3").exists()


def _spoil_one_article(install: Path) -> Path:
    """Make one article of a partition invalid; the json store drops that partition whole."""
    partition = Path("agent-vault/articles/2026-10-11.json")  # as the vault is named
    articles = json.loads(partition.read_text())
    del articles[1]["title"]
    partition.write_text(json.dumps(articles))
    return partition


def test_an_unreadable_partition_stops_the_move_before_any_write(install: Path) -> None:
    partition = _spoil_one_article(install)

    result = _cli(install, "store", "migrate", "--to", "sqlite")

    assert result.exit_code == 1, result.output
    [line] = result.output.splitlines()
    assert line.startswith(f"✗ {partition}: ") and "https://alpha.example/pipeline" in line
    assert "title" in line
    assert not (install.parent / "state" / "cyris.sqlite3").exists()


def test_store_diff_does_not_agree_over_an_unreadable_partition(install: Path) -> None:
    assert _cli(install, "store", "migrate", "--to", "sqlite").exit_code == 0
    partition = _spoil_one_article(install)

    result = _cli(install, "store", "diff", "--to", "sqlite")

    assert result.exit_code == 1, result.output
    assert str(partition) in result.output
    assert "differing: 0" not in result.output


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("timestamp", None),
        ("timestamp", ""),
        ("model", 5),
        ("api_calls", None),
        ("input_tokens", "12"),
        ("output_tokens", True),
        ("estimated_cost_usd", "free"),
    ],
)
def test_a_usage_record_the_table_cannot_take_stops_the_move_before_any_write(
    install: Path, field: str, value: object
) -> None:
    log = install.parent / "agent-vault" / "usage.jsonl"
    lines = log.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[-1])
    record[field] = value
    log.write_text("\n".join([*lines[:-1], json.dumps(record)]) + "\n", encoding="utf-8")

    result = _cli(install, "store", "migrate", "--to", "sqlite")

    assert result.exit_code == 1, result.output
    assert f"usage.jsonl:{len(lines)}:" in result.output and field in result.output
    assert not (install.parent / "state" / "cyris.sqlite3").exists()


def _file_with_schema(install: Path) -> LocalD1Client:
    from cyris.adapters.store.d1 import apply_schema

    client = LocalD1Client(install.parent / "state" / "cyris.sqlite3")
    apply_schema(client)
    return client


@pytest.mark.parametrize(
    ("table", "key", "row"),
    [
        (
            "sources",
            "Broken",
            (
                "INSERT INTO sources (name, url, type, config) VALUES (?, ?, ?, ?)",
                ["Broken", "https://broken.example/feed", "rss", '{"lane": "nowhere"}'],
            ),
        ),
        (
            "stored_articles",
            "https://alpha.example/pending",
            (
                "INSERT INTO stored_articles (url, original_id, title, content, published_at, "
                "source_name, source_lane, state, first_seen_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    "https://alpha.example/pending",
                    "x",
                    "t",
                    "c",
                    "2026-10-11T00:00:00+00:00",
                    "Alpha",
                    "read",
                    "limbo",
                    "2026-10-11T00:00:00+00:00",
                ],
            ),
        ),
    ],
    ids=["sources", "stored_articles"],
)
def test_an_unreadable_row_in_the_file_ends_the_move_on_one_line_before_any_write(
    install: Path, table: str, key: str, row: tuple
) -> None:
    client = _file_with_schema(install)
    client.query(*row)
    path = install.parent / "state" / "cyris.sqlite3"
    before = _table_counts(path)

    result = _cli(install, "store", "migrate", "--to", "sqlite")

    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit), repr(result.exception)
    [line] = result.output.splitlines()
    assert line.startswith(f"✗ {table} row {key!r} in state/cyris.sqlite3: "), line
    assert _table_counts(path) == before


def test_an_invalid_tracked_topic_row_in_the_file_is_kept_not_overwritten(install: Path) -> None:
    client = _file_with_schema(install)
    # A threshold of 0 fails TrackedTopic's rule, so list_topics leaves the row out.
    client.query(
        "INSERT INTO tracked_topics (name, description, threshold, model) VALUES (?, ?, ?, ?)",
        ["Rust", "Held by the file", 0, "m"],
    )

    result = _cli(install, "store", "migrate", "--to", "sqlite")

    assert result.exit_code == 0, result.output
    [row] = client.query("SELECT * FROM tracked_topics WHERE name = 'Rust'").rows
    assert row == {"name": "Rust", "description": "Held by the file", "threshold": 0, "model": "m"}
    [line] = [x for x in result.output.splitlines() if "tracked topic Rust" in x]
    assert line.startswith("  kept     tracked topic Rust (the file's row is invalid: threshold")
    assert "Tracked topics:\n" + line + "\nInserted 1; 1 already in the file, 1 of them" in (
        result.output
    )


def test_a_rerun_names_no_row_since_every_kept_row_is_identical(install: Path) -> None:
    assert _cli(install, "store", "migrate", "--to", "sqlite").exit_code == 0

    second = _cli(install, "store", "migrate", "--to", "sqlite")

    assert second.exit_code == 0, second.output
    assert [line for line in second.output.splitlines() if line.startswith("  kept")] == []
    assert "26 kept" in second.output


def test_every_kind_of_kept_row_that_differs_is_named_with_what_differs(install: Path) -> None:
    client = _file_with_schema(install)
    voted = "2026-10-01T20:00:00.000000+00:00"
    seen = datetime(2026, 10, 11, 0, 1, tzinfo=UTC)
    D1Settings(client).set({"digest.max_articles_per_digest": 7})
    D1TrackedTopicStore(client).upsert(
        TrackedTopic(
            name="Fusion",
            description="Nuclear fusion power research",
            threshold=0.6,
            model="text-embedding-3-small",
        )
    )
    D1SourceStore(client).upsert(
        SourceConfig(name="Alpha", url="https://alpha.example/feed.xml", lane=Lane.SKIM)
    )
    article = _article("https://alpha.example/pipeline", seen, state=ArticleState.ACCEPTED)
    D1ArticleStore(client).import_articles(
        [
            article.model_copy(
                update={
                    "digest_date": "2026-10-11",
                    "score": 0.7,
                    "language": "ja",
                    "content": "Edited in the file",
                }
            )
        ]
    )
    for sql, values in [
        ("INSERT INTO topics VALUES (?, ?, ?, ?)", ["t-rust", "Other", voted, voted]),
        (
            "INSERT INTO topic_members VALUES (?, ?, ?, ?, ?)",
            ["t-rust", "https://alpha.example/twice", "2026-10-01", "morning", voted],
        ),
        ("INSERT INTO topic_tags VALUES (?, ?, ?, ?)", ["t-rust", "rust", "region", voted]),
        ("INSERT INTO topic_votes VALUES (?, ?, ?)", ["t-rust", -1, voted]),
        (
            "INSERT INTO usage_log (logged_at, digest_date, period, model, input_tokens) "
            "VALUES (?, ?, ?, ?, ?)",
            [USAGE[0]["timestamp"], "2026-10-01", "morning", USAGE[0]["model"], 9999],
        ),
    ]:
        client.query(sql, values)

    result = _cli(install, "store", "migrate", "--to", "sqlite")

    assert result.exit_code == 0, result.output
    kept = [line for line in result.output.splitlines() if line.startswith("  kept")]
    expected = [
        "digest.max_articles_per_digest (file has 200)",
        "tracked topic Fusion (threshold: json 0.5, file 0.6)",
        "source Alpha (lane: json 'read', file 'skim'",
        "https://alpha.example/pipeline (content: json 'Body of https://alpha.example/pipeline', "
        "file 'Edited in the file')",
        "topic t-rust (name: json 'Rust 2.1', file 'Other'",
        "topic member t-rust https://alpha.example/twice 2026-10-01 morning (seen_at: json ",
        "topic tag t-rust rust (kind: json 'entity', file 'region'",
        f"topic vote t-rust {voted} (direction: json 1, file -1)",
        f"usage run {USAGE[0]['timestamp']} 2026-10-01 morning claude-haiku-5-5 "
        "(articles_received: json 40, file 0",
    ]
    for text in expected:
        assert any(text in line for line in kept), (text, kept)
    assert len(kept) == len(expected), kept


def test_the_closing_print_names_what_cyris_toml_will_no_longer_be_read_for(
    install: Path,
) -> None:
    from fakes import TEST_SETTINGS

    result = _cli(install, "store", "migrate", "--to", "sqlite")

    assert result.exit_code == 0, result.output
    closing = result.output.split("To switch,", 1)[1]
    tables: dict[str, list[str]] = {}
    for key in sorted(TEST_SETTINGS):
        table, field = key.split(".", 1)
        tables.setdefault(table, []).append(field)
    for table, fields in tables.items():
        assert f"\n  [{table}] {', '.join(fields)}\n" in closing, (table, closing)
    assert "\n  [[tracked_topics]]: Fusion, Rust\n" in closing
    assert "sources.yaml" in closing.split("[[tracked_topics]]", 1)[1]
