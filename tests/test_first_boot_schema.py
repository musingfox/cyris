"""First boot against a clean Cloudflare account, whose D1 has no tables.

Nothing used to create them: `schema.sql` shipped in the repo and was applied by
hand with `wrangler d1 execute`, so a one-click deployment reached the settings
read — the first D1 touch in every entrypoint — and aborted there.
"""

from datetime import UTC, datetime

from fakes import SqliteD1

from cyris.adapters.store.d1 import D1Error, apply_schema
from cyris.adapters.store.d1_store import D1ArticleStore
from cyris.domain.models import Article, Tier


def _article(url: str) -> Article:
    return Article(
        id=url,
        title="t",
        content="c",
        url=url,
        published_at=datetime(2026, 9, 4, tzinfo=UTC),
        source_name="s",
        source_tier=Tier.FILTER,
    )


def test_a_clean_database_gets_its_tables() -> None:
    db = SqliteD1(with_schema=False)

    apply_schema(db)

    assert D1ArticleStore(db).count_by_state() == {}


def test_the_settings_read_finds_its_table_on_first_boot(tmp_path, monkeypatch) -> None:
    """The seam that mattered: `settings` is read before anything else touches D1."""
    from cyris import bootstrap

    db = SqliteD1(with_schema=False)
    monkeypatch.setenv("CYRIS_STORE_BACKEND", "d1")
    monkeypatch.setenv("CYRIS_STORE_DATABASE_ID", "abc")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "tok")
    monkeypatch.setattr(bootstrap, "build_d1_client", lambda _cfg: db)

    cfg = bootstrap.load_effective_config(tmp_path / "nope.toml", tmp_path / "nope.yaml")

    assert cfg.settings_from_d1 == []
    assert db.query("SELECT name FROM sqlite_master WHERE name = 'settings'").rows


def test_doctor_reports_an_unusable_d1_as_a_check_line(monkeypatch) -> None:
    """Creating the tables here means a D1Error is D1 itself, not a missing table."""
    from typer.testing import CliRunner

    from cyris.entrypoints.cli import app

    def boom(*_args, **_kwargs):
        raise D1Error("HTTP 403: Authentication error")

    monkeypatch.setattr("cyris.bootstrap.load_effective_config", boom)

    result = CliRunner().invoke(app, ["doctor"])

    assert result.exit_code == 1
    assert "✗ d1 — HTTP 403: Authentication error" in result.stdout
    assert "CLOUDFLARE_API_TOKEN" in result.stdout


def test_the_second_boot_is_a_no_op() -> None:
    """AC: applying it again on a populated database must not fail or drop data."""
    db = SqliteD1(with_schema=False)
    apply_schema(db)
    D1ArticleStore(db).save([_article("https://example.com/b")])

    apply_schema(db)

    assert sum(D1ArticleStore(db).count_by_state().values()) == 1
