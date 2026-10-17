"""No per-model cosine cutoff is read by suppression, and a leftover toml line changes nothing."""

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fakes import settings_toml

from cyris.config import load_config
from cyris.domain.models import ArticleState, StoredArticle, Tier
from cyris.service_layer.vote_similarity import judge_by_votes

pytestmark = [pytest.mark.unit, pytest.mark.guard]

ROOT = Path(__file__).parent.parent
SRC = ROOT / "src" / "cyris"


def _toml_with_threshold(tmp_path: Path, threshold: float | None) -> Path:
    body = settings_toml()
    if threshold is not None:
        body = body.replace(
            "[vote_similarity]\n", f"[vote_similarity]\nthreshold = {threshold}\n", 1
        )
    path = tmp_path / "cyris.toml"
    path.write_text(body)
    return path


def _sources(tmp_path: Path) -> Path:
    path = tmp_path / "sources.yaml"
    path.write_bytes((ROOT / "sources.example.yaml").read_bytes())
    return path


def test_provider_defaults_carry_only_the_model() -> None:
    embedding = json.loads((SRC / "provider_defaults.json").read_text())["embedding"]

    assert embedding["gemini"] == {"model": "gemini-embedding-001"}
    assert embedding["workers_ai"] == {"model": "@cf/baai/bge-m3"}


def test_a_leftover_threshold_line_still_parses(tmp_path: Path) -> None:
    with_line = load_config(_with(tmp_path / "a", 0.9), _sources(tmp_path / "a"))
    without = load_config(_with(tmp_path / "b", None), _sources(tmp_path / "b"))

    assert with_line.app.vote_similarity.threshold == 0.9
    assert without.app.vote_similarity.threshold is None


def _with(directory: Path, threshold: float | None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    return _toml_with_threshold(directory, threshold)


def test_only_config_and_doctor_read_the_threshold() -> None:
    pattern = re.compile(r"vote\.threshold|vote_similarity\.threshold")
    hit_files = {
        str(path.relative_to(SRC)) for path in SRC.rglob("*.py") if pattern.search(path.read_text())
    }

    assert hit_files <= {"config.py", "diagnostics/doctor.py"}


def _article(url: str, title: str, state: ArticleState = ArticleState.PENDING, voted=False):
    return StoredArticle(
        url=url,
        original_id=url,
        title=title,
        content="c",
        source_name="Src",
        source_tier=Tier.FILTER,
        published_at=datetime(2026, 10, 1, tzinfo=UTC),
        first_seen_at=datetime(2026, 10, 1, tzinfo=UTC),
        state=state,
        triaged_at=datetime(2026, 10, 1, tzinfo=UTC) if voted else None,
    )


class _Store:
    def __init__(self, rows: list[StoredArticle]) -> None:
        self._rows = rows

    def list_articles(self, state=None, limit=100, **_kw) -> list[StoredArticle]:
        return [a for a in self._rows if state is None or a.state == state][:limit]


class _Embedder:
    """Puts the seed on [1, 0] and each candidate at a cosine written in its title."""

    def __init__(self, cosines: dict[str, float]) -> None:
        self._cosines = cosines

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            if text == "Seed":
                out.append([1.0, 0.0])
            else:
                c = self._cosines[text]
                out.append([c, (1 - c * c) ** 0.5])
        return out


@pytest.mark.parametrize("threshold", [0.9, 0.1])
async def test_one_window_suppresses_the_same_url_whatever_the_leftover_threshold(
    tmp_path: Path, threshold: float
) -> None:
    cfg = load_config(_with(tmp_path, threshold), _sources(tmp_path))
    assert cfg.app.vote_similarity.threshold == threshold
    cosines = {f"u{i}": 0.40 + 0.01 * i for i in range(9)}
    cosines["u58"] = 0.58
    store = _Store([_article("seed", "Seed", ArticleState.REJECTED, voted=True)])
    candidates = [_article(url, url) for url in cosines]
    embedder = _Embedder({**cosines})

    report = await judge_by_votes(store, embedder, candidates, max_seeds=200)

    assert report.suppressed_urls == ["u58"]


def test_no_source_names_the_deleted_constants() -> None:
    hits = [
        str(path.relative_to(SRC))
        for path in SRC.rglob("*")
        if path.is_file()
        and path.suffix in {".py", ".json", ".sql"}
        and re.search(r"DEFAULT_THRESHOLD|embedding_threshold", path.read_text())
    ]

    assert hits == []
