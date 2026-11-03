"""Only a digest run is wired to read teaser articles' pages.

`build_deps` leaves `lengthen_articles` unset unless asked, and the CLI asks for
it on `run` (a `--dry-run` too) and on a rerun alone: a rerender and the
comparison commands read no page.
"""

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx
from fakes import make_config
from typer.testing import CliRunner

from cyris import bootstrap
from cyris.config import AgentVaultConfig, HtmlOutputConfig, SourceConfig, StoreConfig
from cyris.domain.models import Article, Lane
from cyris.entrypoints.cli import app

pytestmark = pytest.mark.integration

runner = CliRunner()
FEED = SourceConfig(name="Teaser Feed", url="https://pjwu.test/rss.xml")
PAGE = "https://pjwu.test/writing/others-standards/"
BODY = " ".join(f"Paragraph {i} of the article's own page goes on at length." for i in range(30))


def _config(tmp_path: Path, *, d1: bool = False):
    store = (
        StoreConfig(backend="d1", database_id="db-ID-1", account_id="acct", api_token="tok")
        if d1
        else StoreConfig()
    )
    return make_config(
        store=store,
        agent_vault=AgentVaultConfig(path=tmp_path / "vault"),
        html_output=HtmlOutputConfig(enabled=True, output_dir=str(tmp_path / "html")),
        sources={FEED.name: FEED},
    )


def _teaser() -> Article:
    return Article(
        id=PAGE,
        title="Others' standards",
        url=PAGE,
        content="One sentence.",
        published_at=datetime(2026, 11, 3, tzinfo=UTC),
        source_name=FEED.name,
        source_lane=Lane.READ,
    )


def test_a_deps_built_without_asking_reads_no_page(tmp_path: Path) -> None:
    assert bootstrap.build_deps(_config(tmp_path)).lengthen_articles is None


async def test_a_deps_asked_for_pages_reads_a_teaser_of_the_configured_feeds(
    tmp_path: Path,
) -> None:
    lengthen = bootstrap.build_deps(_config(tmp_path), fetch_pages=True).lengthen_articles
    async with respx.mock:
        page = respx.get(PAGE).mock(
            return_value=httpx.Response(
                200,
                text=f"<html><body><article><p>{BODY}</p></article></body></html>",
                headers={"Content-Type": "text/html"},
            )
        )
        (out,) = await lengthen([_teaser()], summarize_snippet_length=1000)

    assert page.call_count == 1
    assert "Paragraph 29" in out.content


class _DepsBuiltError(Exception):
    pass


@pytest.fixture
def asked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """The keywords each command passes `build_deps`, which then stops the command."""
    calls: list[dict] = []
    cfg = _config(tmp_path, d1=True)
    monkeypatch.setattr(bootstrap, "load_effective_config", lambda *_: cfg)

    def record(*_args, **kwargs):
        calls.append(kwargs)
        raise _DepsBuiltError

    monkeypatch.setattr(bootstrap, "build_deps", record)
    return calls


def _invoke(tmp_path: Path, *args: str):
    files = ["--config", str(tmp_path / "c.toml"), "--sources", str(tmp_path / "s.yaml")]
    return runner.invoke(app, [*args, *files])


@pytest.mark.parametrize(
    "args",
    [
        ["run"],
        ["run", "--dry-run"],
        ["regenerate", "2026-11-03", "morning", "--mode", "rerun", "--output", "o.html"],
    ],
    ids=["run", "dry-run", "rerun"],
)
def test_a_digest_run_asks_for_pages(tmp_path: Path, asked: list[dict], args) -> None:
    result = _invoke(tmp_path, *args)

    assert isinstance(result.exception, _DepsBuiltError), result.output
    assert [call.get("fetch_pages") for call in asked] == [True]


@pytest.mark.parametrize(
    "args",
    [
        ["regenerate", "2026-11-03", "morning", "--mode", "rerender", "--output", "o.html"],
        ["vote-sim"],
        ["embed-compare"],
        ["llm-compare", "--arm", "gemini:gemini-test"],
    ],
    ids=["rerender", "vote-sim", "embed-compare", "llm-compare"],
)
def test_no_other_command_asks_for_pages(tmp_path: Path, asked: list[dict], args) -> None:
    result = _invoke(tmp_path, *args)

    assert isinstance(result.exception, _DepsBuiltError), result.output
    assert [bool(call.get("fetch_pages")) for call in asked] == [False]
