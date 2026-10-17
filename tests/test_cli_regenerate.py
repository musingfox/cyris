"""`cyris regenerate` re-renders a stored issue, or reruns it, into a file or onto the site."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fakes import FakeLLM, RecordingD1, make_config
from test_run_digest import FakeSource, make_deps
from typer.testing import CliRunner

from cyris import bootstrap
from cyris.adapters.output.html_digest import HtmlDigestWriter
from cyris.adapters.store.digest_inputs import D1DigestInputStore
from cyris.adapters.store.digests import D1DigestStore
from cyris.adapters.store.runs import D1RunLog
from cyris.bootstrap import Deps
from cyris.config import HtmlOutputConfig, PromoteConfig, SourceConfig, StoreConfig
from cyris.domain.models import Article, DigestContent, Lane, UsageStats
from cyris.entrypoints.cli import app

pytestmark = pytest.mark.integration

runner = CliRunner()

ISSUE = ("2026-10-14", "evening")


def _content(date: str, period: str) -> DigestContent:
    return DigestContent(
        date=date,
        period=period,
        sources_processed=1,
        articles_received=1,
        articles_included=1,
        usage=UsageStats(api_calls=1),
        area_order=["Tech"],
    )


def _article() -> Article:
    return Article(
        id=1,
        title="Enterprise AI Adoption",
        url="https://example.com/ai",
        content="Enterprises accelerate AI adoption.",
        published_at=datetime.now(UTC) - timedelta(hours=1),
        source_name="TechSource",
        source_lane=Lane.READ,
        source_tags=["tech"],
    )


def _config(tmp_path: Path, *, d1: bool = True, promote: bool = False):
    store = (
        StoreConfig(backend="d1", database_id="db-ID-1", account_id="acct", api_token="tok")
        if d1
        else StoreConfig()
    )
    overrides = (
        {"promote": PromoteConfig(worker_url="https://promote.test", token="promote-tok")}
        if promote
        else {}
    )
    return make_config(
        store=store,
        html_output=HtmlOutputConfig(enabled=True, output_dir=str(tmp_path / "html")),
        sources={"feedA": SourceConfig(name="feedA", url="x")},
        **overrides,
    )


def _rerender_deps(tmp_path: Path, db: RecordingD1, cfg, *, publish_site=None) -> Deps:
    async def discord(webhook_url, content, digest_url="", publish_failed=False, degraded=False):
        raise AssertionError("Discord was called")

    return Deps(
        cfg=cfg,
        store=None,
        llm=FakeLLM("{}"),
        fetch_sources=[],
        html_writer=HtmlDigestWriter(tmp_path / "html"),
        publish=None,
        publish_site=publish_site,
        sync_promotions=None,
        log_usage=lambda content: None,
        digest_store=D1DigestStore(db),
        send_discord=discord,
    )


def _rerun_deps(tmp_path: Path, db: RecordingD1, cfg, replies, *, publish_site=None):
    """A rerun's deps over the json article store, with recorded inputs in `db`."""
    deps, _ = make_deps(tmp_path, FakeLLM(replies), FakeSource([]))
    deps = replace(
        deps,
        cfg=cfg,
        digest_input_store=D1DigestInputStore(db),
        digest_store=D1DigestStore(db),
        record_run=D1RunLog(db, "").record,
        publish_site=publish_site,
        page_serves=None,
    )
    return deps


def _scores() -> str:
    return json.dumps({"scores": [{"id": 1, "score": 85, "language": "en"}]})


def _sections() -> str:
    return json.dumps(
        {
            "sections": [
                {
                    "heading": "Tech",
                    "summary": "Launches.",
                    "articles": [
                        {"id": "0", "title": "Enterprise AI Adoption", "source": "TechSource"}
                    ],
                }
            ]
        }
    )


def _run(tmp_path: Path, *args: str):
    return runner.invoke(
        app,
        [
            "regenerate",
            *args,
            "--config",
            str(tmp_path / "c.toml"),
            "--sources",
            str(tmp_path / "s.yaml"),
        ],
    )


@pytest.fixture
def wire(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Route the command at a config and at deps; `build_deps` is a tripwire unless given."""

    def install(cfg, deps=None) -> None:
        monkeypatch.setattr(bootstrap, "load_effective_config", lambda *_: cfg)

        def build(*_args, **_kwargs):
            if deps is None:
                raise AssertionError("build_deps was called")
            return deps

        monkeypatch.setattr(bootstrap, "build_deps", build)

    return install


def test_a_rerender_writes_the_stored_page_and_writes_no_table(tmp_path: Path, wire) -> None:
    db = RecordingD1()
    cfg = _config(tmp_path)
    deps = _rerender_deps(tmp_path, db, cfg)
    deps.digest_store.save(_content(*ISSUE), raw_page=True)
    stored = deps.digest_store.load(*ISSUE)
    expected = deps.html_writer.render(stored.content, raw_page=True, degraded=False)
    db.statements.clear()
    wire(cfg, deps)
    out = tmp_path / "out.html"

    result = _run(tmp_path, "2026-10-14", "evening", "--mode", "rerender", "--output", str(out))

    assert result.exit_code == 0, result.output
    assert out.read_text(encoding="utf-8") == expected
    assert db.written_tables() == set()


def test_publish_and_output_are_each_required_and_exclusive(tmp_path: Path, wire) -> None:
    wire(_config(tmp_path))
    out = str(tmp_path / "out.html")

    neither = _run(tmp_path, "2026-10-14", "evening", "--mode", "rerender")
    both = _run(tmp_path, "2026-10-14", "evening", "--mode", "rerun", "--output", out, "--publish")

    assert neither.exit_code == 2
    assert both.exit_code == 2


def test_malformed_date_period_or_mode_is_a_usage_error(tmp_path: Path, wire) -> None:
    wire(_config(tmp_path))
    out = str(tmp_path / "out.html")

    results = [
        _run(tmp_path, "2026-13-01", "evening", "--mode", "rerender", "--output", out),
        _run(tmp_path, "10-14", "evening", "--mode", "rerender", "--output", out),
        _run(tmp_path, "2026-10-14", "night", "--mode", "rerender", "--output", out),
        _run(tmp_path, "2026-10-14", "evening", "--mode", "x", "--output", out),
    ]

    assert [r.exit_code for r in results] == [2, 2, 2, 2]


def test_a_json_store_is_refused_by_name(tmp_path: Path, wire) -> None:
    cfg = _config(tmp_path, d1=False)
    wire(cfg)
    out = tmp_path / "out.html"

    result = _run(tmp_path, "2026-10-14", "evening", "--mode", "rerender", "--output", str(out))

    assert result.exit_code == 1
    assert "json" in result.stderr
    assert not out.exists()


def test_an_unknown_issue_names_itself(tmp_path: Path, wire) -> None:
    db = RecordingD1()
    cfg = _config(tmp_path)
    wire(cfg, _rerender_deps(tmp_path, db, cfg))

    result = _run(
        tmp_path,
        "2026-10-14",
        "evening",
        "--mode",
        "rerender",
        "--output",
        str(tmp_path / "out.html"),
    )

    assert result.exit_code == 1
    assert "2026-10-14 evening" in result.stderr


def test_a_later_issue_refuses_a_publishing_rerun_and_names_it(tmp_path: Path, wire) -> None:
    db = RecordingD1()
    cfg = _config(tmp_path)
    published: list[str] = []
    deps = _rerun_deps(tmp_path, db, cfg, [], publish_site=lambda *a: published.append("x"))
    deps.digest_store.save(_content(*ISSUE), raw_page=True)
    deps.digest_store.save(_content("2026-10-15", "morning"), raw_page=True)
    wire(cfg, deps)

    result = _run(tmp_path, "2026-10-14", "evening", "--mode", "rerun", "--publish")

    assert result.exit_code == 1
    assert "2026-10-15 morning" in result.stderr
    assert published == []


def test_publish_without_a_publisher_renders_nothing(tmp_path: Path, wire) -> None:
    db = RecordingD1()
    cfg = _config(tmp_path)
    llm = FakeLLM([_scores(), _sections()])
    deps = replace(_rerun_deps(tmp_path, db, cfg, []), llm=llm)
    wire(cfg, deps)

    result = _run(tmp_path, "2026-10-14", "evening", "--mode", "rerun", "--publish")

    assert result.exit_code == 1
    assert "publishing is not configured" in result.stderr
    assert llm.calls == []


def test_a_rerun_dry_run_writes_the_render_and_one_run_record(tmp_path: Path, wire) -> None:
    db = RecordingD1()
    cfg = _config(tmp_path)
    deps = _rerun_deps(tmp_path, db, cfg, [_scores(), _sections()])
    deps.store.save([_article()])
    D1DigestInputStore(db).save(*ISSUE, [_article().url])
    deps = replace(deps, record_run=D1RunLog(db, "").record)
    rendered: list[str] = []

    class RecordingWriter(HtmlDigestWriter):
        def render(self, *args, **kwargs) -> str:
            html = super().render(*args, **kwargs)
            rendered.append(html)
            return html

    deps = replace(deps, html_writer=RecordingWriter(tmp_path / "html"))
    wire(cfg, deps)
    out = tmp_path / "out.html"

    result = _run(tmp_path, "2026-10-14", "evening", "--mode", "rerun", "--output", str(out))

    assert result.exit_code == 0, result.output
    assert out.read_text(encoding="utf-8") == rendered[-1]
    rows = db.query("SELECT dry_run, summary FROM digest_runs").rows
    assert [row["dry_run"] for row in rows] == [1]
    assert json.loads(rows[0]["summary"])["rerun_of"] == "2026-10-14"


def test_the_regenerate_wiring_has_no_vote_sync(tmp_path: Path, monkeypatch) -> None:
    db = RecordingD1()
    cfg = _config(tmp_path, promote=True)
    monkeypatch.setenv("CYRIS_PROMOTE_TOKEN", "promote-tok")
    monkeypatch.setattr(bootstrap, "build_d1_client", lambda _cfg: db)
    monkeypatch.setattr(bootstrap, "load_effective_config", lambda *_: cfg)
    real_build_deps = bootstrap.build_deps
    built: list[Deps] = []

    def record(*args, **kwargs):
        built.append(real_build_deps(*args, **kwargs))
        return built[-1]

    monkeypatch.setattr(bootstrap, "build_deps", record)
    D1DigestStore(db).save(_content(*ISSUE), raw_page=True)

    _run(tmp_path, "2026-10-14", "evening", "--mode", "rerender", "--output", str(tmp_path / "o"))

    control = real_build_deps(cfg)
    assert built[0].sync_promotions is None
    assert control.sync_promotions is not None


def test_a_rerun_publish_is_refused_for_rerender_before_any_build(tmp_path: Path, wire) -> None:
    wire(_config(tmp_path))

    result = _run(tmp_path, "2026-10-14", "evening", "--mode", "rerender", "--publish")

    assert result.exit_code == 2
    assert "--publish is only available with --mode rerun" in result.stderr


def test_an_unpadded_date_is_a_usage_error(tmp_path: Path, wire) -> None:
    wire(_config(tmp_path))
    out = str(tmp_path / "out.html")

    results = [
        _run(tmp_path, "2026-1-4", "evening", "--mode", "rerender", "--output", out),
        _run(tmp_path, "2026-10-4", "evening", "--mode", "rerender", "--output", out),
    ]

    assert [r.exit_code for r in results] == [2, 2]
