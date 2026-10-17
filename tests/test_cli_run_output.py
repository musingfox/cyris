"""`cyris run --dry-run --output PATH` leaves the previewed page as a file, and says so."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fakes import FakeLLM, make_config
from test_run_digest import FakeSource, make_deps
from typer.testing import CliRunner

from cyris import bootstrap
from cyris.adapters.output.html_digest import HtmlDigestWriter
from cyris.config import AgentVaultConfig, SourceConfig, StoreConfig
from cyris.domain.models import Article, Lane
from cyris.entrypoints.cli import app

pytestmark = pytest.mark.integration

runner = CliRunner()


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


def _llm() -> FakeLLM:
    return FakeLLM(
        [
            json.dumps({"scores": [{"id": 1, "score": 85, "language": "en"}]}),
            json.dumps(
                {
                    "sections": [
                        {
                            "heading": "AI 趨勢",
                            "summary": "企業加速導入 AI",
                            "articles": [
                                {
                                    "id": "0",
                                    "title": "Enterprise AI Adoption",
                                    "source": "TechSource",
                                }
                            ],
                        }
                    ]
                }
            ),
        ]
    )


def _config(tmp_path: Path, store: StoreConfig | None = None):
    kwargs = {"store": store} if store else {}
    return make_config(
        agent_vault=AgentVaultConfig(path=tmp_path / "agent-vault"),
        sources={"feedA": SourceConfig(name="feedA", url="x")},
        **kwargs,
    )


@pytest.fixture
def wire(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Route the command at a config and a Deps built over one pending article."""
    rendered: list[str] = []

    class RecordingWriter(HtmlDigestWriter):
        def render(self, *args, **kwargs) -> str:
            html = super().render(*args, **kwargs)
            rendered.append(html)
            return html

    def install(cfg, articles=None, llm=None) -> list[str]:
        monkeypatch.setattr(bootstrap, "load_effective_config", lambda *_: cfg)
        root = tmp_path / f"deps-{len(rendered)}-{id(cfg)}"
        articles = articles if articles is not None else [_article()]
        deps, _ = make_deps(root, llm or _llm(), FakeSource(articles))
        # A dry run fetches but saves nothing, so the stored rows are seeded here.
        deps.store.save(articles)
        deps = replace(deps, html_writer=RecordingWriter(root / "html"))
        monkeypatch.setattr(bootstrap, "build_deps", lambda *a, **k: deps)
        return rendered

    return install


def _run(tmp_path: Path, *extra: str):
    args = ["run", "--config", str(tmp_path / "c.toml"), "--sources", str(tmp_path / "s.yaml")]
    return runner.invoke(app, [*args, *extra])


def test_the_preview_file_holds_the_writers_render(tmp_path: Path, wire) -> None:
    rendered = wire(_config(tmp_path))
    out = tmp_path / "out.html"

    result = _run(tmp_path, "--dry-run", "--output", str(out))

    assert result.exit_code == 0, result.output
    assert out.read_text(encoding="utf-8") == rendered[-1]
    assert "<html" not in result.stdout


def test_output_without_dry_run_is_a_usage_error_before_any_config_loads(
    tmp_path: Path, monkeypatch
) -> None:
    def must_not_run(*_args, **_kwargs):
        raise AssertionError("config or deps were built")

    monkeypatch.setattr(bootstrap, "load_effective_config", must_not_run)
    monkeypatch.setattr(bootstrap, "build_deps", must_not_run)
    out = tmp_path / "out.html"

    result = _run(tmp_path, "--output", str(out))

    assert result.exit_code == 2
    assert not out.exists()


def test_an_empty_preview_fails_and_leaves_the_file_as_it_was(
    tmp_path: Path, wire, monkeypatch
) -> None:
    wire(_config(tmp_path), articles=[], llm=FakeLLM([]))
    out = tmp_path / "out.html"
    out.write_text("old", encoding="utf-8")

    result = _run(tmp_path, "--dry-run", "--output", str(out))

    assert result.exit_code == 1
    assert out.read_text(encoding="utf-8") == "old"


def test_the_first_stdout_line_names_the_resolved_backend(tmp_path: Path, wire) -> None:
    wire(_config(tmp_path))
    result = _run(tmp_path, "--dry-run", "--output", str(tmp_path / "a.html"))
    assert result.stdout.splitlines()[0] == "Store backend: json"

    d1 = StoreConfig(backend="d1", database_id="db-ID-1", account_id="acct", api_token="tok")
    wire(_config(tmp_path, store=d1))
    result = _run(tmp_path, "--dry-run", "--output", str(tmp_path / "b.html"))
    assert result.stdout.splitlines()[0] == "Store backend: d1"


def test_the_backend_line_is_not_printed_without_output(tmp_path: Path, wire) -> None:
    wire(_config(tmp_path))

    result = _run(tmp_path, "--dry-run")

    assert "Store backend" not in result.stdout


def test_no_secret_or_database_id_reaches_the_output(tmp_path: Path, wire, monkeypatch) -> None:
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "tok-SECRET-1")
    d1 = StoreConfig(backend="d1", database_id="db-ID-1", account_id="acct")
    wire(_config(tmp_path, store=d1))
    out = tmp_path / "out.html"

    result = _run(tmp_path, "--dry-run", "--output", str(out))

    assert result.stdout.splitlines()[0] == "Store backend: d1"
    assert "tok-SECRET-1" not in result.output
    assert "db-ID-1" not in result.output
    assert "tok-SECRET-1" not in out.read_text(encoding="utf-8")
    assert "db-ID-1" not in out.read_text(encoding="utf-8")


def test_a_dry_run_without_output_still_echoes_the_html(tmp_path: Path, wire) -> None:
    wire(_config(tmp_path))

    result = _run(tmp_path, "--dry-run")

    assert result.exit_code == 0
    assert "<html" in result.stdout, result.output


def test_a_preview_at_an_unscheduled_hour_writes_no_file(tmp_path: Path, wire, monkeypatch) -> None:
    wire(_config(tmp_path))
    monkeypatch.setattr("cyris.service_layer.schedule.due_period", lambda now, times: None)
    out = tmp_path / "out.html"

    result = _run(tmp_path, "--dry-run", "--if-due", "--output", str(out))

    assert result.exit_code == 1
    assert not out.exists()
