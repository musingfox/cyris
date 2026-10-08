"""`cyris labels draw|report`: the blind-label sample, drawn from D1 and scored back."""

import json
from types import SimpleNamespace

import pytest
from fakes import SqliteD1, settings_toml
from typer.testing import CliRunner

from cyris.domain.similarity import normalize
from cyris.entrypoints.cli import app

pytestmark = pytest.mark.integration

SINCE = "2026-09-18T10:00:00+00:00"
AFTER = "2026-09-19T00:00:00.000000+00:00"


def put(db, url, *, state="accepted", tags=(), reason=None, triaged_at=None, title=""):
    db.query(
        "INSERT INTO stored_articles (url, title, content, published_at, source_name, "
        "source_tier, source_tags, state, first_seen_at, rejection_reason, triaged_at) "
        "VALUES (?, ?, '', ?, 'Source', 'filter', ?, ?, ?, ?, ?)",
        [url, title or url, AFTER, json.dumps(list(tags)), state, AFTER, reason, triaged_at],
    )


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    (tmp_path / "cyris.toml").write_text(
        f'[agent_vault]\npath = "{tmp_path / "vault"}"\n\n' + settings_toml(), encoding="utf-8"
    )
    (tmp_path / "sources.yaml").write_text("sources: []")
    db = SqliteD1()
    for i in range(6):
        put(db, f"news-kept-{i}", tags=("news",))
        put(db, f"news-cut-{i}", tags=("news",), state="rejected", reason="filtered")
        put(db, f"blog-kept-{i}", state="pending")
        put(db, f"blog-cut-{i}", state="rejected", reason="filtered")
    monkeypatch.setattr("cyris.bootstrap.build_d1_client", lambda cfg: db)
    return tmp_path, db


def run(tmp_path, *args):
    return CliRunner().invoke(
        app,
        [
            "labels",
            *args,
            "--config",
            str(tmp_path / "cyris.toml"),
            "--sources",
            str(tmp_path / "sources.yaml"),
        ],
    )


def draw(tmp_path, *extra, min_rejected="4"):
    return run(
        tmp_path,
        "draw",
        *("--since", SINCE, "--seed", "3", "--size", "8", "--min-rejected", min_rejected),
        *extra,
    )


def sample(db) -> list[tuple]:
    return [
        tuple(r.values())
        for r in db.query(
            "SELECT url, position, news, pipeline_state FROM blind_labels ORDER BY position"
        ).rows
    ]


class TestDraw:
    def test_draws_two_from_each_stratum_and_says_how_many(self, deployment) -> None:
        tmp_path, db = deployment

        result = draw(tmp_path)

        assert result.exit_code == 0, result.output
        rows = db.query("SELECT news, pipeline_state FROM blind_labels").rows
        assert len(rows) == 8
        assert sum(r["pipeline_state"] == "rejected" for r in rows) == 4
        assert sum(r["news"] for r in rows) == 4
        assert "Drew 8 of 24 filter candidates" in result.output
        assert "/labels" in result.output

    def test_the_same_seed_redraws_the_same_sample(self, deployment) -> None:
        tmp_path, db = deployment
        draw(tmp_path)
        first = sample(db)

        result = draw(tmp_path, "--replace")

        assert result.exit_code == 0, result.output
        assert sample(db) == first

    def test_a_kept_sample_is_not_replaced_without_asking(self, deployment) -> None:
        tmp_path, db = deployment
        draw(tmp_path)
        db.query("UPDATE blind_labels SET label = 'up' WHERE position = 0")
        before = sample(db)

        result = draw(tmp_path)

        assert result.exit_code == 1
        assert "already drawn: 8 items, 1 answered" in result.output
        assert "--replace" in result.output
        assert sample(db) == before

    def test_a_cutoff_without_an_offset_is_refused(self, deployment) -> None:
        tmp_path, db = deployment

        result = run(tmp_path, "draw", "--since", "2026-09-18T10:00", "--seed", "3")

        assert result.exit_code == 1
        assert "UTC offset" in result.output
        assert sample(db) == []

    def test_a_pool_short_of_rejected_rows_draws_nothing(self, deployment) -> None:
        tmp_path, db = deployment

        result = draw(tmp_path, min_rejected="30")

        assert result.exit_code == 1
        assert "at least 30" in result.output
        assert sample(db) == []

    def test_a_deployment_without_d1_is_told_so(self, deployment, monkeypatch) -> None:
        tmp_path, _ = deployment
        monkeypatch.setattr("cyris.bootstrap.build_d1_client", lambda cfg: None)

        result = draw(tmp_path)

        assert result.exit_code == 1
        assert "backend" in result.output


SEEDS = {"Liked": [1.0, 0.0], "Disliked": [0.0, 1.0]}


class FakeEmbedder:
    """Titles of items labeled up sit near the liked seed, the rest near the disliked one."""

    def __init__(self, ups: set[str]) -> None:
        self.ups = ups
        self.payloads: list[list[str]] = []
        self.usage = SimpleNamespace(as_dict=lambda: {"texts": 10})

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.payloads.append(list(texts))
        return [
            normalize(SEEDS.get(t) or ([1.0, 0.2] if t in self.ups else [0.2, 1.0])) for t in texts
        ]


@pytest.fixture
def labeled(deployment, monkeypatch):
    """A drawn sample of eight, each stratum answered once up and once down, and one
    human vote either way outside the sample."""
    tmp_path, db = deployment
    assert draw(tmp_path).exit_code == 0
    rows = db.query("SELECT url, news, pipeline_state FROM blind_labels ORDER BY position").rows
    seen: set[tuple] = set()
    ups = set()
    for r in rows:
        key = (r["news"], r["pipeline_state"])
        label = "down" if key in seen else "up"
        seen.add(key)
        if label == "up":
            ups.add(r["url"])
        db.query("UPDATE blind_labels SET label = ? WHERE url = ?", [label, r["url"]])
        # The label's own vote, which must never seed its score.
        db.query(
            "UPDATE stored_articles SET triaged_at = ?, state = ? WHERE url = ?",
            [AFTER, "accepted" if label == "up" else "rejected", r["url"]],
        )
    put(db, "seed-up", title="Liked", triaged_at=AFTER)
    put(db, "seed-down", title="Disliked", state="rejected", triaged_at=AFTER)
    embedder = FakeEmbedder(ups)
    monkeypatch.setattr("cyris.bootstrap.make_embedder", lambda provider, model: embedder)
    return tmp_path, db, embedder


class TestReport:
    def test_scores_the_filter_and_the_preference_over_the_labels(self, labeled) -> None:
        tmp_path, _, _ = labeled

        result = run(tmp_path, "report")

        assert result.exit_code == 0, result.output
        out = result.output
        assert "8 labeled (4 up, 4 down), 0 skipped, 0 unanswered of 8" in out
        assert "seed 3" in out and "2026-09-18T10:00:00" in out
        for name in ("news", "non-news", "overall"):
            line = next(line for line in out.splitlines() if line.startswith(f"{name} "))
            assert line.split()[-2:] == ["0.500", "0.500"], line
        assert "Kept at draw: 2 accepted, 2 pending" in out
        assert "Seeds: 1 up, 1 down" in out
        assert "AUC 1.000" in out
        assert 'Embedding spend: {"texts": 10}' in out

    def test_no_label_seeds_its_own_score(self, labeled) -> None:
        tmp_path, db, embedder = labeled

        run(tmp_path, "report")

        sampled = {r["url"] for r in db.query("SELECT url FROM blind_labels").rows}
        assert len(embedder.payloads) == 2
        assert sorted(embedder.payloads[0]) == ["Disliked", "Liked"]
        assert sorted(embedder.payloads[1]) == sorted(sampled)

    def test_skips_are_counted_and_left_out(self, labeled) -> None:
        tmp_path, db, embedder = labeled
        db.query("UPDATE blind_labels SET label = 'skip' WHERE position = 0")

        result = run(tmp_path, "report")

        assert "7 labeled" in result.output
        assert "1 skipped" in result.output
        assert len(embedder.payloads[1]) == 7

    def test_nothing_labeled_yet_is_said_and_embeds_nothing(self, deployment, monkeypatch) -> None:
        tmp_path, _ = deployment
        draw(tmp_path)
        embedder = FakeEmbedder(set())
        monkeypatch.setattr("cyris.bootstrap.make_embedder", lambda provider, model: embedder)

        result = run(tmp_path, "report")

        assert result.exit_code == 1
        assert "No item is labeled up or down yet" in result.output
        assert embedder.payloads == []
