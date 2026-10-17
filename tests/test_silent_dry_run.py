"""A dry run changes no table but its own run record, and sends nothing."""

import json
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path

import pytest
from fakes import FakeLLM, RecordingD1, make_config

from cyris.adapters.output.html_digest import HtmlDigestWriter
from cyris.adapters.output.usage_log import append_usage_d1
from cyris.adapters.store.areas import D1AreaStore
from cyris.adapters.store.d1_store import D1ArticleStore
from cyris.adapters.store.digests import D1DigestStore
from cyris.adapters.store.runs import D1RunLog
from cyris.adapters.store.similarity_shadow import D1SimilarityShadow
from cyris.adapters.store.stories import D1StoryStore
from cyris.adapters.store.tags import D1TagStore
from cyris.bootstrap import Deps
from cyris.config import AgentVaultConfig
from cyris.domain.models import Article, Lane
from cyris.service_layer.run_digest import RunOptions, run_digest

pytestmark = pytest.mark.integration


class _Source:
    def __init__(self, articles: list[Article]) -> None:
        self._articles = articles

    async def fetch_articles(self, **kwargs) -> list[Article]:
        return self._articles

    async def health_check(self) -> bool:
        return True


def _article() -> Article:
    return Article(
        id=7,
        title="Notify Path",
        url="https://example.com/notify",
        content="Enterprises accelerate AI adoption.",
        published_at=datetime.now(UTC) - timedelta(hours=1),
        source_name="NotifySource",
        source_lane=Lane.READ,
        source_tags=["tech"],
    )


def _llm() -> FakeLLM:
    return FakeLLM(
        [
            json.dumps({"scores": [{"id": 7, "score": 85, "language": "en"}]}),
            json.dumps(
                {
                    "sections": [
                        {
                            "heading": "AI 趨勢",
                            "summary": "企業加速導入 AI",
                            "articles": [
                                {"id": 7, "title": "Notify Path", "source": "NotifySource"}
                            ],
                        }
                    ]
                }
            ),
        ]
    )


def _deps(tmp_path: Path, db: RecordingD1, sent: list, log_usage=None) -> Deps:
    async def discord(webhook_url, content, digest_url="", publish_failed=False, degraded=False):
        sent.append("discord")

    cfg = make_config(agent_vault=AgentVaultConfig(path=tmp_path / "agent-vault"))
    cfg.app.notify.email_to = "me@example.org"
    cfg.app.notify.email_from = "digest@example.org"
    return Deps(
        cfg=cfg,
        store=D1ArticleStore(db),
        llm=_llm(),
        fetch_sources=[_Source([_article()])],
        html_writer=HtmlDigestWriter(tmp_path / "html"),
        publish=None,
        sync_promotions=None,
        log_usage=log_usage or partial(append_usage_d1, client=db),
        tag_store=D1TagStore(db),
        story_store=D1StoryStore(db),
        area_store=D1AreaStore(db),
        record_run=D1RunLog(db, "").record,
        record_similarity=D1SimilarityShadow(db, "test-model").record,
        digest_store=D1DigestStore(db),
        send_discord=discord,
    )


def _seeded(tmp_path: Path, sent: list, log_usage=None) -> tuple[Deps, RecordingD1]:
    db = RecordingD1()
    D1ArticleStore(db).save([_article()])
    db.statements.clear()
    return _deps(tmp_path, db, sent, log_usage), db


async def test_a_dry_run_writes_only_its_run_record(tmp_path: Path) -> None:
    sent: list = []
    deps, db = _seeded(tmp_path, sent)

    report = await run_digest(deps, RunOptions(dry_run=True))

    assert report.status == "ok"
    assert db.written_tables() == {"digest_runs"}
    assert sent == []


async def test_a_publishing_run_writes_the_tables_the_dry_run_skips(tmp_path: Path) -> None:
    deps, db = _seeded(tmp_path, [])

    await run_digest(deps, RunOptions(dry_run=False))

    assert {"usage_log", "digests", "stored_articles"} <= db.written_tables()


async def test_a_dry_run_does_not_fail_when_usage_logging_would_raise(tmp_path: Path) -> None:
    def boom(content) -> None:
        raise AssertionError("usage was logged")

    deps, _ = _seeded(tmp_path, [], log_usage=boom)

    report = await run_digest(deps, RunOptions(dry_run=True))

    assert report.status == "ok"
