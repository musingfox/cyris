"""/settings suggests an area per source and proposes areas for all, never failing a save."""

import asyncio
import json
import logging
import signal
from datetime import UTC, datetime
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from fakes import FakeLLM, SqliteD1
from typer.testing import CliRunner

from cyris.adapters.store.areas import D1AreaStore
from cyris.adapters.store.d1 import MAX_BOUND_PARAMS
from cyris.adapters.store.source_store import D1SourceStore
from cyris.adapters.store.source_titles import D1SourceTitles
from cyris.diagnostics import doctor
from cyris.domain.models import SourceConfig
from cyris.entrypoints.cli import app as cli_app
from cyris.entrypoints.triage_server import TriageServer

pytestmark = pytest.mark.integration

SECRET = "sk-secret-123"


def _row(db: SqliteD1, area_id: int, name: str, status: str, **counts) -> None:
    db.query(
        "INSERT INTO areas (id, name, position, status, proposal_articles, proposal_issues) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [area_id, name, area_id, status, counts.get("articles", 0), counts.get("issues", 0)],
    )


async def _client(db: SqliteD1, llm_factory, **extra) -> TestClient:
    server = TriageServer(
        area_store=D1AreaStore(db),
        source_store=D1SourceStore(db),
        llm_factory=llm_factory,
        **extra,
    )
    client = TestClient(TestServer(server._app))
    await client.start_server()
    return client


def _suggest_body(**fields) -> dict:
    return {
        "name": "Feed",
        "url": "https://feed.example/rss",
        "type": "rss",
        "tags": ["x"],
        "homepage": None,
        **fields,
    }


@pytest.fixture
def db() -> SqliteD1:
    db = SqliteD1()
    _row(db, 1, "Tech", "active")
    _row(db, 2, "Food", "active")
    return db


async def test_a_suggestion_is_one_active_area_or_none(db: SqliteD1) -> None:
    llm = FakeLLM(['{"area": "tech"}', '{"area": "Art"}'])
    client = await _client(db, lambda values: llm)
    try:
        first = await (await client.post("/api/sources/suggest-area", json=_suggest_body())).json()
        second = await (await client.post("/api/sources/suggest-area", json=_suggest_body())).json()
    finally:
        await client.close()

    assert first["suggestion"] == {"id": 1, "name": "Tech"}
    assert second["suggestion"] is None
    assert len(llm.calls) == 2


async def test_a_failed_call_is_a_200_without_the_key(db: SqliteD1, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    llm = FakeLLM(error=RuntimeError(f"401 key={SECRET}"))
    client = await _client(db, lambda values: llm)
    try:
        response = await client.post("/api/sources/suggest-area", json=_suggest_body())
        body = await response.text()
    finally:
        await client.close()

    data = json.loads(body)
    assert response.status == 200
    assert data["suggestion"] is None
    assert data["note"] == "The model call failed (RuntimeError)."
    assert SECRET not in body
    assert SECRET not in caplog.text


async def test_the_save_still_succeeds_after_a_failed_suggestion(db: SqliteD1) -> None:
    llm = FakeLLM(error=RuntimeError("boom"))
    client = await _client(db, lambda values: llm)
    try:
        await client.post("/api/sources/suggest-area", json=_suggest_body())
        save = await client.post(
            "/api/sources", json={"name": "Feed", "url": "https://feed.example/rss"}
        )
    finally:
        await client.close()

    assert save.status == 200
    assert "Feed" in D1SourceStore(db).list_sources()


async def test_the_factory_gets_the_provider_saved_on_the_settings_page(
    db: SqliteD1, monkeypatch
) -> None:
    async def healthy(llm_cfg):
        return doctor.Check("llm probe", "ok", "answered")

    monkeypatch.setattr(doctor, "probe_llm", healthy)

    received: list[dict] = []

    class Settings:
        def set(self, values):
            self.values = values

    def factory(values):
        received.append(dict(values))
        return FakeLLM('{"area": null}')

    client = await _client(db, factory, settings=Settings())
    try:
        saved = await client.post(
            "/api/settings", json={"provider": "gemini", "model": "gemini-test-model"}
        )
        assert saved.status == 200
        await client.post("/api/sources/suggest-area", json=_suggest_body())
    finally:
        await client.close()

    assert received[-1]["llm_provider.provider"] == "gemini"
    assert received[-1]["llm_provider.model"] == "gemini-test-model"


async def test_no_active_area_or_no_llm_asks_the_model_nothing(db: SqliteD1) -> None:
    llm = FakeLLM('{"area": "Tech"}')
    client = await _client(db, lambda values: None)
    try:
        no_llm = await (await client.post("/api/sources/suggest-area", json=_suggest_body())).json()
    finally:
        await client.close()

    empty = SqliteD1()
    client = await _client(empty, lambda values: llm)
    try:
        no_area = await (
            await client.post("/api/sources/suggest-area", json=_suggest_body())
        ).json()
    finally:
        await client.close()

    assert no_llm["suggestion"] is None and "LLM" in no_llm["note"]
    assert no_area["suggestion"] is None
    assert llm.calls == []


async def test_proposing_areas_creates_proposals_once(db: SqliteD1) -> None:
    sources = D1SourceStore(db)
    for name in ("a", "b", "c"):
        sources.upsert(SourceConfig(name=name, url=f"https://{name}.example/f"))
    llm = FakeLLM(json.dumps({"Tech": ["a", "b"], "Space": ["c"]}))
    client = await _client(db, lambda values: llm)
    try:
        first = await (await client.post("/api/areas/propose")).json()
        second = await (await client.post("/api/areas/propose")).json()
    finally:
        await client.close()

    assert first["created"] == 1
    assert first["proposals"][0] == {"id": 1, "name": "Tech", "sources": ["a", "b"]}
    space = [p for p in first["proposals"] if p["name"] == "Space"][0]
    assert space["sources"] == ["c"]
    assert second["created"] == 0
    assert len(D1AreaStore(db).list_areas()) == 3


async def test_a_dismissed_name_is_dropped_and_a_merged_name_takes_its_target(db: SqliteD1) -> None:
    db.query("UPDATE areas SET status = 'dismissed' WHERE id = 2")
    _row(db, 6, "Chips", "merged")
    db.query("UPDATE areas SET merged_into = 1 WHERE id = 6")
    sources = D1SourceStore(db)
    sources.upsert(SourceConfig(name="a", url="https://a.example/f"))
    llm = FakeLLM(json.dumps({"Food": ["a"], "Chips": ["a"]}))
    client = await _client(db, lambda values: llm)
    try:
        data = await (await client.post("/api/areas/propose")).json()
    finally:
        await client.close()

    assert [(p["id"], p["name"]) for p in data["proposals"]] == [(1, "Tech")]
    assert len(D1AreaStore(db).list_areas()) == 3


async def test_a_model_failure_writes_nothing_and_names_only_the_class(db: SqliteD1) -> None:
    D1SourceStore(db).upsert(SourceConfig(name="a", url="https://a.example/f"))
    llm = FakeLLM(error=RuntimeError(f"401 key={SECRET}"))
    client = await _client(db, lambda values: llm)
    try:
        response = await client.post("/api/areas/propose")
        body = await response.text()
    finally:
        await client.close()

    assert response.status == 502
    assert json.loads(body)["error"] == "The model call failed (RuntimeError)."
    assert SECRET not in body
    assert len(D1AreaStore(db).list_areas()) == 2


async def test_no_llm_or_no_sources_is_refused(db: SqliteD1) -> None:
    client = await _client(db, lambda values: None)
    try:
        D1SourceStore(db).upsert(SourceConfig(name="a", url="https://a.example/f"))
        no_llm = await client.post("/api/areas/propose")
    finally:
        await client.close()

    empty = SqliteD1()
    _row(empty, 1, "Tech", "active")
    client = await _client(empty, lambda values: FakeLLM("{}"))
    try:
        no_sources = await client.post("/api/areas/propose")
    finally:
        await client.close()

    assert no_llm.status == 409
    assert no_sources.status == 400


async def test_names_differing_only_in_case_are_one_proposal_with_both_sources(
    db: SqliteD1,
) -> None:
    sources = D1SourceStore(db)
    for name in ("a", "b"):
        sources.upsert(SourceConfig(name=name, url=f"https://{name}.example/f"))
    llm = FakeLLM(json.dumps({"Économie": ["a"], "économie": ["b"]}))
    client = await _client(db, lambda values: llm)
    try:
        response = await client.post("/api/areas/propose")
        body = await response.json()
    finally:
        await client.close()

    assert response.status == 200
    assert body["created"] == 1
    assert [(p["name"], p["sources"]) for p in body["proposals"]] == [("Économie", ["a", "b"])]
    assert [r.name for r in D1AreaStore(db).list_areas() if r.status == "proposed"] == ["Économie"]


async def test_only_the_host_of_a_feed_url_reaches_the_propose_model(db: SqliteD1) -> None:
    D1SourceStore(db).upsert(SourceConfig(name="a", url="https://a.example/f?token=sk-feed-9"))
    llm = FakeLLM(json.dumps({"Tech": ["a"]}))
    client = await _client(db, lambda values: llm)
    try:
        response = await client.post("/api/areas/propose")
    finally:
        await client.close()

    assert response.status == 200
    assert "a.example" in llm.calls[0]["prompt"]
    assert "sk-feed-9" not in llm.calls[0]["prompt"]


async def test_only_the_host_of_a_feed_url_reaches_the_suggest_model(db: SqliteD1) -> None:
    llm = FakeLLM('{"area": "tech"}')
    client = await _client(db, lambda values: llm)
    try:
        response = await client.post(
            "/api/sources/suggest-area",
            json=_suggest_body(url="https://feed.example/rss?token=sk-feed-9"),
        )
    finally:
        await client.close()

    assert response.status == 200
    assert "feed.example" in llm.calls[0]["prompt"]
    assert "sk-feed-9" not in llm.calls[0]["prompt"]


async def test_a_factory_that_raises_is_the_fixed_model_failure(db: SqliteD1) -> None:
    def broken(values):
        raise RuntimeError(f"401 key={SECRET}")

    D1SourceStore(db).upsert(SourceConfig(name="a", url="https://a.example/f"))
    client = await _client(db, broken)
    try:
        response = await client.post("/api/areas/propose")
        body = await response.text()
    finally:
        await client.close()

    assert response.status == 502
    assert json.loads(body)["error"] == "The model call failed (RuntimeError)."
    assert SECRET not in body
    assert len(D1AreaStore(db).list_areas()) == 2


class _FailsWhileWritingSecondName(SqliteD1):
    """Refuses the insert that carries the second new name, as a store failing mid-write would."""

    def query(self, sql, params=None):
        if sql.startswith("INSERT INTO areas") and "Pizza" in (params or []):
            raise RuntimeError("write refused")
        return super().query(sql, params)


async def test_a_store_failure_while_proposing_leaves_no_proposed_row() -> None:
    db = _FailsWhileWritingSecondName()
    _row(db, 1, "Tech", "active")
    sources = D1SourceStore(db)
    for name in ("a", "b", "c"):
        sources.upsert(SourceConfig(name=name, url=f"https://{name}.example/f"))
    llm = FakeLLM(json.dumps({"Chips": ["a"], "Pizza": ["b"], "Tech": ["c"]}))
    client = await _client(db, lambda values: llm)
    try:
        response = await client.post("/api/areas/propose")
        body = await response.json()
    finally:
        await client.close()

    assert response.status == 500
    assert body == {"ok": False, "error": "The area table could not be written."}
    assert [r.name for r in D1AreaStore(db).list_areas() if r.status == "proposed"] == []


async def test_more_new_names_than_one_d1_statement_binds_are_refused_unwritten(
    db: SqliteD1,
) -> None:
    D1SourceStore(db).upsert(SourceConfig(name="a", url="https://a.example/f"))
    llm = FakeLLM(json.dumps({f"Area {i}": ["a"] for i in range(MAX_BOUND_PARAMS + 1)}))
    client = await _client(db, lambda values: llm)
    try:
        response = await client.post("/api/areas/propose")
        body = await response.json()
    finally:
        await client.close()

    assert response.status == 502
    assert body == {
        "ok": False,
        "error": f"The model proposed more than {MAX_BOUND_PARAMS} new areas; nothing was saved.",
    }
    assert [r.name for r in D1AreaStore(db).list_areas() if r.status == "proposed"] == []


async def test_a_line_break_in_a_proposed_name_becomes_a_space(db: SqliteD1) -> None:
    D1SourceStore(db).upsert(SourceConfig(name="a", url="https://a.example/f"))
    llm = FakeLLM(json.dumps({"Line\nBreak": ["a"]}))
    client = await _client(db, lambda values: llm)
    try:
        response = await client.post("/api/areas/propose")
        body = await response.json()
    finally:
        await client.close()

    assert response.status == 200
    assert [(p["name"], p["sources"]) for p in body["proposals"]] == [("Line Break", ["a"])]
    assert [r.name for r in D1AreaStore(db).list_areas() if r.status == "proposed"] == [
        "Line Break"
    ]


def _article(db: SqliteD1, url: str, source: str, title: str) -> None:
    seen = datetime.now(UTC).isoformat(timespec="microseconds")
    db.query(
        "INSERT INTO stored_articles (url, title, published_at, source_name, source_lane, "
        "first_seen_at) VALUES (?, ?, ?, ?, 'skim', ?)",
        [url, title, seen, source, seen],
    )


async def test_the_propose_prompt_puts_each_sources_titles_under_its_line(db: SqliteD1) -> None:
    D1SourceStore(db).upsert(SourceConfig(name="a", url="https://a.example/f"))
    _article(db, "https://a.example/1", "a", "Title of A")
    _article(db, "https://x.example/1", "stranger", "Stranger headline")
    llm = FakeLLM(json.dumps({"Tech": ["a"]}))
    client = await _client(db, lambda values: llm, source_titles=D1SourceTitles(db))
    try:
        response = await client.post("/api/areas/propose")
    finally:
        await client.close()

    assert response.status == 200
    lines = llm.calls[0]["prompt"].splitlines()
    line = lines.index('  recent titles: ["Title of A"]')
    assert lines[line - 1].startswith("- a (a.example) tags:")
    assert "Stranger headline" not in llm.calls[0]["prompt"]


async def test_without_a_title_reader_the_prompt_has_no_titles(db: SqliteD1) -> None:
    D1SourceStore(db).upsert(SourceConfig(name="a", url="https://a.example/f"))
    llm = FakeLLM(json.dumps({"Sports": ["a"]}))
    client = await _client(db, lambda values: llm, source_titles=None)
    try:
        response = await client.post("/api/areas/propose")
        data = await response.json()
    finally:
        await client.close()

    assert response.status == 200
    assert data["created"] == 1
    assert "recent titles" not in llm.calls[0]["prompt"]


class _BrokenTitles:
    def recent(self, now):
        raise RuntimeError("secret title X")


async def test_a_failing_title_reader_is_logged_by_class_and_never_fails_the_propose(
    db: SqliteD1, caplog
) -> None:
    caplog.set_level(logging.DEBUG)
    D1SourceStore(db).upsert(SourceConfig(name="a", url="https://a.example/f"))
    llm = FakeLLM(json.dumps({"Tech": ["a"]}))
    client = await _client(db, lambda values: llm, source_titles=_BrokenTitles())
    try:
        response = await client.post("/api/areas/propose")
        data = await response.json()
    finally:
        await client.close()

    assert response.status == 200
    assert data["ok"] is True
    assert "RuntimeError" in caplog.text
    assert "secret title X" not in caplog.text
    assert "recent titles" not in llm.calls[0]["prompt"]


def _triage_ui_kwargs(tmp_path: Path, monkeypatch, d1) -> dict:
    config_path = tmp_path / "cyris.toml"
    config_path.write_text(
        f'[general]\ntimezone = "Asia/Taipei"\n\n[llm_provider]\napi_key = "test"\n'
        f'model = "test"\n\n[agent_vault]\npath = "{tmp_path / "agent-vault"}"\n'
    )
    sources_path = tmp_path / "sources.yaml"
    sources_path.write_text("sources: []")
    (tmp_path / "agent-vault").mkdir()
    captured: dict = {}

    class Capture:
        def __init__(self, *args, **kwargs) -> None:
            captured.update(kwargs)

        async def start(self) -> None:
            asyncio.get_running_loop().call_soon(signal.raise_signal, signal.SIGINT)

        async def stop(self) -> None:
            pass

    monkeypatch.setattr("cyris.bootstrap.build_d1_client", lambda cfg: d1)
    monkeypatch.setattr("cyris.entrypoints.triage_server.TriageServer", Capture)
    result = CliRunner().invoke(
        cli_app, ["triage-ui", "--config", str(config_path), "--sources", str(sources_path)]
    )
    assert result.exit_code == 0, result.output
    return captured


def test_triage_ui_on_d1_reads_titles_from_d1(tmp_path: Path, monkeypatch) -> None:
    kwargs = _triage_ui_kwargs(tmp_path, monkeypatch, SqliteD1())

    assert isinstance(kwargs["source_titles"], D1SourceTitles)


def test_triage_ui_without_d1_has_no_title_reader(tmp_path: Path, monkeypatch) -> None:
    kwargs = _triage_ui_kwargs(tmp_path, monkeypatch, None)

    assert kwargs["source_titles"] is None


async def test_the_propose_prompt_lists_active_then_proposed_areas_and_no_others(
    db: SqliteD1,
) -> None:
    _row(db, 5, "Energy", "proposed")
    _row(db, 6, "Old", "dismissed")
    _row(db, 7, "Sports", "merged")
    db.query("UPDATE areas SET merged_into = 1 WHERE id = 7")
    D1SourceStore(db).upsert(SourceConfig(name="a", url="https://a.example/f"))
    llm = FakeLLM(json.dumps({"Tech": ["a"]}))
    client = await _client(db, lambda values: llm)
    try:
        await client.post("/api/areas/propose")
    finally:
        await client.close()

    prompt = llm.calls[0]["prompt"]
    section = prompt.split("Existing areas", 1)[1]
    assert "- Tech" in section and "- Food" in section and "- Energy" in section
    assert "Old" not in section and "Sports" not in section
    assert section.index("- Food") < section.index("- Energy")


async def test_with_no_areas_the_propose_prompt_has_no_existing_section() -> None:
    db = SqliteD1()
    D1SourceStore(db).upsert(SourceConfig(name="a", url="https://a.example/f"))
    llm = FakeLLM(json.dumps({"Sports": ["a"]}))
    client = await _client(db, lambda values: llm)
    try:
        await client.post("/api/areas/propose")
    finally:
        await client.close()

    assert "Existing areas" not in llm.calls[0]["prompt"]


async def test_a_reply_reusing_an_existing_name_in_other_case_is_that_area(db: SqliteD1) -> None:
    D1SourceStore(db).upsert(SourceConfig(name="A", url="https://a.example/f"))
    llm = FakeLLM(json.dumps({"tech": ["A"]}))
    client = await _client(db, lambda values: llm)
    try:
        data = await (await client.post("/api/areas/propose")).json()
    finally:
        await client.close()

    assert data["created"] == 0
    assert [(p["id"], p["name"]) for p in data["proposals"]] == [(1, "Tech")]


async def test_an_unreadable_area_table_is_refused_before_the_model_is_asked(
    db: SqliteD1, monkeypatch
) -> None:
    D1SourceStore(db).upsert(SourceConfig(name="a", url="https://a.example/f"))

    def unreadable(self):
        raise RuntimeError("boom")

    monkeypatch.setattr(D1AreaStore, "list_areas", unreadable)
    llm = FakeLLM(json.dumps({"Tech": ["a"]}))
    client = await _client(db, lambda values: llm)
    try:
        response = await client.post("/api/areas/propose")
        data = await response.json()
    finally:
        await client.close()

    assert response.status == 500
    assert data["error"] == "The area table could not be read."
    assert len(llm.calls) == 0
