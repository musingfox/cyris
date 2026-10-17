"""A run offers the active areas to scoring and records unplaced suggestions, never failing."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fakes import FakeLLM, SqliteD1, settings_toml
from test_run_digest import FakeSource, make_deps

from cyris.adapters.store.areas import D1AreaStore
from cyris.adapters.store.digests import D1DigestStore
from cyris.domain.areas import Area
from cyris.domain.models import Article, ArticleState, Lane
from cyris.domain.selection import digest_urls
from cyris.service_layer.run_digest import RunOptions, run_digest

pytestmark = pytest.mark.integration


def _active(area_id: int, name: str) -> Area:
    return Area(
        id=area_id,
        name=name,
        position=area_id - 1,
        status="active",
        merged_into=None,
        proposal_articles=0,
        proposal_issues=0,
        last_proposed_run=None,
    )


def _articles() -> list[Article]:
    return [
        Article(
            id=i,
            title=f"Orbit story {i}",
            url=f"https://example.com/{i}",
            content="A launch and a satellite.",
            published_at=datetime.now(UTC) - timedelta(hours=1),
            source_name="TechSource",
            source_lane=Lane.READ,
            source_tags=["tech"],
        )
        for i in (1, 2, 3)
    ]


def _scoring_reply(suggest: str | None) -> str:
    entries = [
        {
            "id": i,
            "score": 80,
            "language": "en",
            "tags": ["space"],
            **({"suggest_area": suggest} if suggest else {}),
        }
        for i in (1, 2, 3)
    ]
    return json.dumps({"scores": entries})


SUMMARY_REPLY = json.dumps(
    {
        "sections": [
            {
                "heading": "Space",
                "summary": "Launches.",
                "articles": [{"id": "0", "title": "Orbit story 1", "source": "TechSource"}],
            }
        ]
    }
)


class _SpyStore(D1AreaStore):
    def __init__(self, db: SqliteD1, *, fail: bool = False) -> None:
        super().__init__(db)
        self.calls: list[tuple[dict, str | None]] = []
        self._fail = fail

    def record_proposals(self, counts, run_id):
        self.calls.append((dict(counts), run_id))
        if self._fail:
            raise RuntimeError("D1 unavailable")
        super().record_proposals(counts, run_id)


async def _run(tmp_path: Path, suggest: str | None, store: _SpyStore, *, dry_run: bool = False):
    llm = FakeLLM([_scoring_reply(suggest), SUMMARY_REPLY])
    deps, _ = make_deps(tmp_path, llm, FakeSource(_articles()))
    deps.cfg.areas = [_active(1, "Tech")]
    summaries: list[dict] = []
    deps = replace(deps, area_store=store, record_run=summaries.append)
    report = await run_digest(deps, RunOptions(dry_run=dry_run))
    return report, llm, summaries


def _proposed(db: SqliteD1, name: str) -> dict | None:
    rows = db.query(
        "SELECT status, proposal_articles, proposal_issues FROM areas WHERE name = ?", [name]
    ).rows
    return dict(rows[0]) if rows else None


async def test_a_suggested_name_is_proposed_with_its_article_count(tmp_path: Path) -> None:
    db = SqliteD1()
    report, llm, _ = await _run(tmp_path, "Space", _SpyStore(db))

    assert report.status == "ok"
    assert _proposed(db, "Space") == {
        "status": "proposed",
        "proposal_articles": 3,
        "proposal_issues": 1,
    }
    assert "Tech" in llm.calls[0]["system"]


async def test_a_dry_run_records_nothing(tmp_path: Path) -> None:
    db = SqliteD1()
    store = _SpyStore(db)

    await _run(tmp_path, "Space", store, dry_run=True)

    assert store.calls == []
    assert _proposed(db, "Space") is None


async def test_a_failed_record_leaves_the_run_and_names_the_error(tmp_path: Path) -> None:
    db = SqliteD1()
    report, _, summaries = await _run(tmp_path, "Space", _SpyStore(db, fail=True))

    assert report.status == "ok"
    assert report.html_path is not None and report.html_path.exists()
    assert summaries[-1]["area_proposal_error"] == "RuntimeError"


async def test_a_run_with_no_suggestion_makes_no_store_call(tmp_path: Path) -> None:
    db = SqliteD1()
    store = _SpyStore(db)

    await _run(tmp_path, None, store)

    assert store.calls == []
    assert _proposed(db, "Space") is None


def test_an_articles_score_suggestion_is_proposed_with_no_issue(
    tmp_path: Path, monkeypatch
) -> None:
    from typer.testing import CliRunner

    from cyris.adapters.store import ArticleStore
    from cyris.domain.models import Article
    from cyris.entrypoints.cli import app

    vault = tmp_path / "agent-vault"
    vault.mkdir()
    config_path = tmp_path / "cyris.toml"
    config_path.write_text(
        settings_toml() + f'\n[agent_vault]\npath = "{vault}"\n', encoding="utf-8"
    )
    sources_path = tmp_path / "sources.yaml"
    sources_path.write_text("sources: []", encoding="utf-8")
    store = ArticleStore(vault)
    now = datetime.now(UTC)
    store.save(
        [
            Article(
                id=1,
                title="Launch",
                url="https://example.com/launch",
                content="A rocket.",
                published_at=now,
                source_name="Src",
                source_lane=Lane.READ,
            )
        ],
        now=now,
    )
    db = SqliteD1()
    reply = json.dumps(
        {"scores": [{"id": 1, "score": 60, "language": "en", "suggest_area": "Space"}]}
    )
    monkeypatch.setattr("cyris.bootstrap.build_d1_client", lambda cfg: db)
    monkeypatch.setattr("cyris.bootstrap.build_store", lambda cfg, d1=None: store)
    monkeypatch.setattr("cyris.bootstrap.build_llm", lambda llm_cfg: FakeLLM(reply))

    result = CliRunner().invoke(
        app, ["articles", "score", "--config", str(config_path), "--sources", str(sources_path)]
    )

    assert result.exit_code == 0, result.output
    assert _proposed(db, "Space") == {
        "status": "proposed",
        "proposal_articles": 1,
        "proposal_issues": 0,
    }


def _walk_items(content) -> list:
    items = []
    for name in (
        "featured_articles",
        "news_clusters",
        "thematic_summaries",
        "attention_sections",
        "fan_sections",
        "tracked_topics",
    ):
        for section in getattr(content, name):
            items.extend(section.items)
    return items + content.filtered_headlines


async def _run_saved(
    tmp_path: Path, scoring_reply: str, *, area_store=None, sources=None, areas=()
):
    from cyris.adapters.store.digests import D1DigestStore
    from cyris.domain.models import SourceConfig

    db = SqliteD1()
    llm = FakeLLM([scoring_reply, SUMMARY_REPLY])
    deps, _ = make_deps(tmp_path, llm, FakeSource(_articles()))
    deps.cfg.areas = list(areas)
    deps.cfg.sources = (
        sources
        if sources is not None
        else {"TechSource": SourceConfig(name="TechSource", url="https://x.example/f", area_id=1)}
    )
    summaries: list[dict] = []
    digest_store = D1DigestStore(db)
    deps = replace(
        deps, area_store=area_store, digest_store=digest_store, record_run=summaries.append
    )
    report = await run_digest(deps, RunOptions())
    (key,) = digest_store.issues()
    return report, summaries, digest_store.load(*key).content


async def test_every_saved_item_carries_its_area(tmp_path: Path) -> None:
    reply = json.dumps(
        {
            "scores": [
                {"id": 1, "score": 80, "language": "en", "tags": ["s"], "area": "Tech"},
                {"id": 2, "score": 70, "language": "en", "tags": ["s"]},
                {"id": 3, "score": 60, "language": "en", "tags": ["s"], "area": 7},
            ]
        }
    )

    _, summaries, content = await _run_saved(
        tmp_path, reply, area_store=D1AreaStore(SqliteD1()), areas=[_active(1, "Tech")]
    )

    items = _walk_items(content)
    assert items and all(item.area == "Tech" for item in items)
    stamp = summaries[-1]["area_stamp"]
    assert stamp["picked"] + stamp["source"] + stamp["none"] == len(items)
    assert stamp["source"] >= 1


async def test_a_json_run_stamps_no_area(tmp_path: Path) -> None:
    reply = json.dumps(
        {"scores": [{"id": i, "score": 50, "language": "en", "tags": ["s"]} for i in (1, 2, 3)]}
    )

    _, summaries, content = await _run_saved(tmp_path, reply, sources={}, areas=())

    items = _walk_items(content)
    assert items and all(item.area is None for item in items)
    assert summaries[-1]["area_stamp"]["none"] == len(items)


async def test_a_source_naming_a_missing_area_stamps_none_and_the_run_completes(
    tmp_path: Path,
) -> None:
    from cyris.domain.models import SourceConfig

    reply = json.dumps(
        {"scores": [{"id": i, "score": 50, "language": "en", "tags": ["s"]} for i in (1, 2, 3)]}
    )
    report, _, content = await _run_saved(
        tmp_path,
        reply,
        area_store=D1AreaStore(SqliteD1()),
        areas=[_active(1, "Tech")],
        sources={
            "TechSource": SourceConfig(name="TechSource", url="https://x.example/f", area_id=99)
        },
    )

    assert report.status == "ok"
    assert all(item.area is None for item in _walk_items(content))


async def test_the_saved_issue_records_the_active_areas_in_position_order(tmp_path: Path) -> None:
    reply = json.dumps(
        {"scores": [{"id": i, "score": 50, "language": "en", "tags": ["s"]} for i in (1, 2, 3)]}
    )
    tech = _active(1, "Tech").model_copy(update={"position": 1})
    health = _active(2, "Health").model_copy(update={"position": 0})

    _, _, content = await _run_saved(
        tmp_path,
        reply,
        area_store=D1AreaStore(SqliteD1()),
        areas=[tech, health],
        sources={},
    )

    assert content.area_order == ["Health", "Tech"]


async def test_a_dismissed_area_stays_out_of_the_saved_order(tmp_path: Path) -> None:
    reply = json.dumps(
        {"scores": [{"id": i, "score": 50, "language": "en", "tags": ["s"]} for i in (1, 2, 3)]}
    )
    old = _active(2, "Old").model_copy(update={"status": "dismissed"})

    _, _, content = await _run_saved(
        tmp_path,
        reply,
        area_store=D1AreaStore(SqliteD1()),
        areas=[_active(1, "Tech"), old],
        sources={},
    )

    assert content.area_order == ["Tech"]


async def test_a_json_run_saves_an_empty_area_order(tmp_path: Path) -> None:
    reply = json.dumps(
        {"scores": [{"id": i, "score": 50, "language": "en", "tags": ["s"]} for i in (1, 2, 3)]}
    )

    _, _, content = await _run_saved(tmp_path, reply, sources={}, areas=())

    assert content.area_order == []


async def test_a_failed_stamp_saves_no_area_order_and_no_area(tmp_path: Path, monkeypatch) -> None:
    reply = json.dumps(
        {"scores": [{"id": i, "score": 50, "language": "en", "tags": ["s"]} for i in (1, 2, 3)]}
    )

    def fail(*args, **kwargs):
        raise RuntimeError("stamp broke")

    monkeypatch.setattr("cyris.service_layer.run_digest.stamp_areas", fail)
    report, _, content = await _run_saved(
        tmp_path, reply, area_store=D1AreaStore(SqliteD1()), areas=[_active(1, "Tech")]
    )

    assert report.status == "ok"
    assert content.area_order == []
    assert all(item.area is None for item in _walk_items(content))


def _radar_articles(n: int, *, synthetic_last: bool = False) -> list[Article]:
    """Low-scored read-lane articles: each one is a radar row, with no LLM call."""
    return [
        Article(
            id=i,
            title=f"Radar story {i}",
            url="newsletter:abc" if synthetic_last and i == n else f"https://example.com/{i}",
            content="A launch.",
            published_at=datetime.now(UTC) - timedelta(hours=1),
            source_name="TechSource",
            source_lane=Lane.READ,
            source_tags=["tech"],
        )
        for i in range(1, n + 1)
    ]


async def _run_radar(tmp_path: Path, articles: list[Article], *, areas=(), filtered=()):
    """Run a digest over `articles` plus `filtered` (skim-lane), all scored into Tech."""
    everything = [*articles, *filtered]
    scores = [
        {"id": a.id, "score": 10, "language": "en", "tags": ["s"], "area": "Tech"}
        for a in everything
    ]
    replies = [json.dumps({"scores": scores})] + (
        [json.dumps({"selected": []})] if filtered else []
    )
    deps, _ = make_deps(tmp_path, FakeLLM(replies), FakeSource(everything))
    deps.cfg.areas = list(areas)
    deps.cfg.sources = {}
    db = SqliteD1()
    digest_store = D1DigestStore(db)
    summaries: list[dict] = []
    deps = replace(
        deps,
        area_store=D1AreaStore(SqliteD1()) if areas else None,
        digest_store=digest_store,
        record_run=summaries.append,
    )
    report = await run_digest(deps, RunOptions())
    (key,) = digest_store.issues()
    content = digest_store.load(*key).content
    states = {a.url: a.state for a in deps.store.get_by_urls([a.url for a in everything])}
    return report, content, states, summaries


async def test_the_accepted_set_is_the_shown_set_after_the_quota(tmp_path: Path) -> None:
    articles = _radar_articles(7)

    _, content, states, summaries = await _run_radar(tmp_path, articles, areas=[_active(1, "Tech")])

    urls = [a.url for a in articles]
    accepted = {u for u, s in states.items() if s == ArticleState.ACCEPTED}
    assert accepted == digest_urls(content) == set(urls[:5])
    assert {u for u, s in states.items() if s == ArticleState.PENDING} == set(urls[5:])
    assert summaries[-1]["area_quota_cut"] == 2
    assert content.articles_included == len(digest_urls(content)) == 5


async def test_a_cut_article_with_a_synthetic_url_is_not_a_dead_link(tmp_path: Path) -> None:
    articles = _radar_articles(7, synthetic_last=True)

    _, content, _, _ = await _run_radar(tmp_path, articles, areas=[_active(1, "Tech")])

    assert content.dead_link_count == 0
    assert content.synthetic_url_count == 1


async def test_a_failed_stamp_cuts_nothing_and_accepts_every_shown_article(
    tmp_path: Path, monkeypatch
) -> None:
    def fail(*args, **kwargs):
        raise RuntimeError("stamp broke")

    monkeypatch.setattr("cyris.service_layer.run_digest.stamp_areas", fail)
    articles = _radar_articles(7)

    _, content, states, summaries = await _run_radar(tmp_path, articles, areas=[_active(1, "Tech")])

    assert summaries[-1]["area_quota_cut"] == 0
    assert content.articles_included == 7
    assert set(states.values()) == {ArticleState.ACCEPTED}
    assert digest_urls(content) == {a.url for a in articles}


async def test_a_failed_cap_keeps_every_shown_article_and_logs_its_own_failure(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    def fail(*args, **kwargs):
        raise RuntimeError("cap broke")

    monkeypatch.setattr("cyris.service_layer.run_digest.cap_lower_layers", fail)
    articles = _radar_articles(7)

    _, content, states, summaries = await _run_radar(tmp_path, articles, areas=[_active(1, "Tech")])

    assert summaries[-1]["area_quota_cut"] == 0
    assert set(states.values()) == {ArticleState.ACCEPTED}
    assert digest_urls(content) == {a.url for a in articles}
    assert "Capping area rows failed" in caplog.text
    assert "Stamping areas failed" not in caplog.text


async def test_a_json_run_accepts_and_shows_every_article(tmp_path: Path) -> None:
    articles = _radar_articles(7)

    _, content, states, _ = await _run_radar(tmp_path, articles)

    assert set(states.values()) == {ArticleState.ACCEPTED}
    assert digest_urls(content) == {a.url for a in articles}


async def test_a_filter_rejected_article_stays_rejected_past_the_quota(tmp_path: Path) -> None:
    articles = _radar_articles(7)
    rejected = Article(
        id=8,
        title="Filtered story",
        url="https://example.com/8",
        content="Not news.",
        published_at=datetime.now(UTC) - timedelta(hours=1),
        source_name="Filtered",
        source_lane=Lane.SKIM,
        source_tags=["misc"],
    )

    _, _, states, _ = await _run_radar(
        tmp_path, articles, areas=[_active(1, "Tech")], filtered=[rejected]
    )

    assert states[rejected.url] == ArticleState.REJECTED
