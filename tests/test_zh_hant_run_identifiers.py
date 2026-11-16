"""A zh-Hant issue stores, files and links its values as English identifiers (ADR-0027).

The run reads a zh-Hant catalog that translates every key, so a value that came from a
displayed name would be stored, filed or linked as its translation.
"""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fakes import FakeLLM, SqliteD1, make_config
from test_run_digest import FakeSource, make_deps

from cyris import i18n
from cyris.adapters.output import html_digest
from cyris.adapters.output.html_digest import HtmlDigestWriter
from cyris.adapters.store.d1_store import D1ArticleStore
from cyris.adapters.store.digests import D1DigestStore
from cyris.config import AgentVaultConfig
from cyris.domain.models import Article, Lane
from cyris.service_layer.run_digest import RunOptions, run_digest

pytestmark = pytest.mark.integration

LANGUAGE = "zh-Hant"


def _translation(key: str) -> str:
    return f"譯 {key}"


def _forget_catalogs() -> None:
    for cached in (
        i18n.catalogs,
        i18n._message,
        i18n._translator,
        i18n.browser_file,
        html_digest._environment,
    ):
        cached.cache_clear()


@pytest.fixture
def every_key_translated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    english = json.loads((i18n.LOCALES / "en.json").read_text(encoding="utf-8"))
    translated = {key: {"text": _translation(key), "source": text} for key, text in english.items()}
    locales = tmp_path / "locales"
    locales.mkdir()
    (locales / "en.json").write_text(json.dumps(english), encoding="utf-8")
    (locales / f"{LANGUAGE}.json").write_text(json.dumps(translated), encoding="utf-8")
    monkeypatch.setattr(i18n, "LOCALES", locales)
    _forget_catalogs()
    yield
    monkeypatch.undo()
    _forget_catalogs()


def _article(i: int, lane: Lane) -> Article:
    return Article(
        id=i,
        title=f"Story {i}",
        url=f"https://example.com/{i}",
        content="A launch.",
        published_at=datetime.now(UTC) - timedelta(hours=1),
        source_name="TechSource",
        source_lane=lane,
        source_tags=["tech"],
    )


def _llm() -> FakeLLM:
    return FakeLLM(
        [
            json.dumps({"scores": [{"id": 0, "score": 85, "language": "en"}]}),
            json.dumps(
                {
                    "sections": [
                        {
                            "heading": "AI 趨勢",
                            "summary": "企業加速導入 AI",
                            "articles": [{"id": "0", "title": "Story 1", "source": "TechSource"}],
                        }
                    ]
                }
            ),
        ]
    )


async def _run(tmp_path: Path) -> tuple[SqliteD1, dict[str, bytes]]:
    deps, _ = make_deps(
        tmp_path, _llm(), FakeSource([_article(1, Lane.READ), _article(2, Lane.FOLLOW)])
    )
    db = SqliteD1()
    published: list[dict[str, bytes]] = []

    def publish_site(pages, slug):
        published.append(pages)
        return True

    deps = replace(
        deps,
        cfg=make_config(
            digest={"output_language": LANGUAGE},
            agent_vault=AgentVaultConfig(path=tmp_path / "agent-vault"),
        ),
        store=D1ArticleStore(db),
        digest_store=D1DigestStore(db),
        html_writer=HtmlDigestWriter(tmp_path / "html", LANGUAGE),
        publish_site=publish_site,
    )
    report = await run_digest(deps, RunOptions(period="evening"))
    assert report.status == "ok"
    [pages] = published
    return db, pages


async def test_a_zh_hant_issue_stores_english_identifiers(
    tmp_path: Path, every_key_translated
) -> None:
    db, _ = await _run(tmp_path)

    articles = db.query("SELECT source_lane, state FROM stored_articles ORDER BY url").rows
    [issue] = db.query("SELECT period, content FROM digests").rows

    assert articles == [
        {"source_lane": "read", "state": "accepted"},
        {"source_lane": "follow", "state": "accepted"},
    ]
    assert issue["period"] == json.loads(issue["content"])["period"] == "evening"
    assert datetime.fromisoformat(json.loads(issue["content"])["started_at"]).utcoffset() == (
        timedelta(hours=8)
    )


async def test_a_zh_hant_issue_is_filed_and_linked_by_its_english_period(
    tmp_path: Path, every_key_translated
) -> None:
    db, pages = await _run(tmp_path)
    [date] = [row["date"] for row in db.query("SELECT date FROM digests").rows]
    digest, raw = f"{date}-evening.html", f"{date}-evening-raw.html"

    assert set(pages) >= {f"/{digest}", f"/{raw}", "/index.html"}
    for page in (pages[f"/{digest}"], pages[f"/{raw}"]):
        html = page.decode()
        assert f'<html lang="{LANGUAGE}">' in html
        assert f'href="{digest}"' in html
        assert f'href="{raw}"' in html
    assert f'href="{digest}"' in pages["/index.html"].decode()


async def test_a_zh_hant_issue_shows_the_names_of_its_identifiers(
    tmp_path: Path, every_key_translated
) -> None:
    db, pages = await _run(tmp_path)
    [row] = db.query("SELECT date, content FROM digests").rows
    date = row["date"]
    started = datetime.fromisoformat(json.loads(row["content"])["started_at"])
    digest = pages[f"/{date}-evening.html"].decode()
    raw = pages[f"/{date}-evening-raw.html"].decode()
    accepted = _translation("article_state.accepted")

    assert f'<span class="label">{started:%H}00</span>' in digest
    assert "evening" not in digest.split('<div class="issue-id">', 1)[1].split("</div>", 1)[0]
    assert f'<span class="state accepted">{accepted}</span>' in raw
