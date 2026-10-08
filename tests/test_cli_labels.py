"""`cyris labels draw|report`: the blind-label sample, drawn from D1 and scored back."""

import json

import pytest
from fakes import SqliteD1, settings_toml
from typer.testing import CliRunner

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
