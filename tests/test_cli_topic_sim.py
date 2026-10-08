"""`cyris topic-sim`: a window's titles ranked against a description, to read a threshold off."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fakes import settings_toml
from typer.testing import CliRunner

from cyris.adapters.store import ArticleStore
from cyris.domain.models import Article, Tier
from cyris.domain.similarity import normalize
from cyris.entrypoints.cli import app

pytestmark = pytest.mark.integration

DESCRIPTION = "The company that makes Claude"
VECTORS = {
    DESCRIPTION: [1.0, 0.0],
    "Claude gets a new model": [0.9, 0.436],
    "Chip export rules tighten": [0.5, 0.866],
    "Lottery draw": [0.0, 1.0],
}


class FakeEmbedder:
    def __init__(self) -> None:
        self.payloads: list[list[str]] = []
        self.usage = SimpleNamespace(as_dict=dict)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.payloads.append(list(texts))
        return [normalize(VECTORS[t]) for t in texts]


def _article(n: int, title: str) -> Article:
    return Article(
        id=n,
        title=title,
        url=f"https://a.test/{n}",
        content="c",
        published_at=datetime.now(UTC) - timedelta(hours=2),
        source_name="Src",
        source_tier=Tier.FILTER,
    )


def _write_config(tmp_path):
    vault = tmp_path / "vault"
    (tmp_path / "cyris.toml").write_text(
        f'[agent_vault]\npath = "{vault}"\n\n' + settings_toml(), encoding="utf-8"
    )
    return vault


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    ArticleStore(_write_config(tmp_path)).save(
        [
            _article(1, "Lottery draw"),
            _article(2, "Claude gets a new model"),
            _article(3, "Chip export rules tighten"),
        ]
    )
    embedder = FakeEmbedder()
    built: list[tuple] = []

    def make_embedder(provider, model):
        built.append((provider, model))
        return embedder

    monkeypatch.setattr("cyris.bootstrap.make_embedder", make_embedder)
    return tmp_path, embedder, built


def _run(tmp_path, *args):
    return CliRunner().invoke(
        app,
        [
            "topic-sim",
            DESCRIPTION,
            "--config",
            str(tmp_path / "cyris.toml"),
            "--sources",
            str(tmp_path / "sources.yaml"),
            *args,
        ],
    )


def test_titles_print_nearest_first_with_the_model_to_save(deployment) -> None:
    tmp_path, embedder, built = deployment

    result = _run(tmp_path)

    assert result.exit_code == 0, result.output
    lines = [line for line in result.output.splitlines() if "[Src" in line]
    assert [line.split("] ")[1] for line in lines] == [
        "Claude gets a new model",
        "Chip export rules tighten",
        "Lottery draw",
    ]
    assert "@cf/baai/bge-m3" in result.output
    assert built == [("workers_ai", "")]
    assert len(embedder.payloads) == 1


def test_a_threshold_marks_where_it_cuts_and_counts_the_hits(deployment) -> None:
    tmp_path, _, _ = deployment

    result = _run(tmp_path, "--threshold", "0.7")

    assert result.exit_code == 0, result.output
    out = result.output.splitlines()
    cut = next(i for i, line in enumerate(out) if "0.70 ---" in line)
    assert "Claude gets a new model" in out[cut - 1]
    assert "Chip export rules tighten" in out[cut + 1]
    assert "At 0.70: 1 hit(s)" in result.output


def test_an_empty_window_says_so_and_embeds_nothing(deployment) -> None:
    tmp_path, embedder, _ = deployment
    empty = tmp_path / "empty"
    empty.mkdir()
    _write_config(empty)

    result = _run(empty)

    assert result.exit_code == 1
    assert "No stored article in the last 168h" in result.output
    assert embedder.payloads == []
