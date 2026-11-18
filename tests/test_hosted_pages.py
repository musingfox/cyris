"""A hosted tenant's site: the pages a run publishes, written into the tenant's own database.

`HostedPages` fills the seam the Pages publisher fills (`publish_site`, `page_serves`,
`site_filenames`), so a hosted run publishes through the same calls a self-hosted one does and
the pages land in `hosted_pages`, a table only the hosted schema creates (ADR-0029).
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fakes import SqliteD1

from cyris.adapters.output.hosted_pages import SITE_PATH, HostedPages, tenant_site
from cyris.adapters.store.tenant_gateway import HOSTED_SCHEMA_PATH, apply_hosted_schema

pytestmark = pytest.mark.unit

AT = datetime(2026, 11, 18, 8, 5, tzinfo=UTC)
DIGEST = "/2026-11-18-morning.html"
RAW = "/2026-11-18-morning-raw.html"


def _site() -> tuple[HostedPages, SqliteD1]:
    db = SqliteD1()
    apply_hosted_schema(db)
    return HostedPages(db, now=lambda: AT), db


def _pages(raw: str = "<html>raw</html>") -> dict[str, bytes]:
    return {
        DIGEST: b'<html><meta name="cyris-issue" content="2026-11-18-morning"></html>',
        RAW: raw.encode("utf-8"),
        "/index.html": b"<html>archive</html>",
        "/favicon.svg": b"<svg/>",
    }


def test_a_publish_writes_every_page_with_the_type_it_is_served_as() -> None:
    site, db = _site()

    assert site.publish(_pages(), "2026-11-18-morning")

    rows = db.query("SELECT path, content_type, updated_at FROM hosted_pages ORDER BY path").rows
    assert [(r["path"], r["content_type"]) for r in rows] == [
        (RAW, "text/html; charset=utf-8"),
        (DIGEST, "text/html; charset=utf-8"),
        ("/favicon.svg", "image/svg+xml"),
        ("/index.html", "text/html; charset=utf-8"),
    ]
    assert {r["updated_at"] for r in rows} == {"2026-11-18T08:05:00+00:00"}
    assert site.filenames() == [RAW[1:], DIGEST[1:], "favicon.svg", "index.html"]


def test_the_digest_page_is_written_last_so_its_raw_page_and_archive_precede_it() -> None:
    db = SqliteD1()
    apply_hosted_schema(db)
    written: list[str] = []
    real = db.query

    def query(sql, params=None):
        if sql.startswith("INSERT INTO hosted_pages"):
            written.append(params[0])
        return real(sql, params)

    db.query = query  # type: ignore[method-assign]

    assert HostedPages(db, now=lambda: AT).publish(_pages(), "2026-11-18-morning")
    assert written[-1] == DIGEST


def test_publishing_again_replaces_a_page_and_keeps_the_others() -> None:
    site, db = _site()
    site.publish(_pages(), "2026-11-18-morning")

    assert site.publish({RAW: b"<html>raw, again</html>"}, "2026-11-18-morning")

    assert site.serves(RAW, b"<html>raw, again</html>")
    assert site.serves(DIGEST, _pages()[DIGEST])
    assert db.query("SELECT count(*) AS n FROM hosted_pages").rows[0]["n"] == 4


def test_serves_compares_the_whole_stored_body() -> None:
    site, _ = _site()
    site.publish(_pages(), "2026-11-18-morning")

    assert site.serves(DIGEST, _pages()[DIGEST])
    assert not site.serves(DIGEST, b"<html>an older render</html>")
    assert not site.serves("/2026-11-17-evening.html", b"")


def test_a_raw_page_over_a_statements_100_kb_is_bound_not_written_into_the_statement() -> None:
    site, db = _site()
    seen: list[str] = []
    real = db.query

    def query(sql, params=None):
        seen.append(sql)
        return real(sql, params)

    db.query = query  # type: ignore[method-assign]
    raw = "<html>" + "x" * 150_000 + "</html>"

    assert site.publish(_pages(raw), "2026-11-18-morning")

    assert max(len(sql.encode("utf-8")) for sql in seen) < 1_000
    assert site.serves(RAW, raw.encode("utf-8"))


@pytest.mark.parametrize(
    "pages",
    [{"/notes.txt": b"text"}, {DIGEST: b"\xff\xfe not utf-8"}],
    ids=["a file the site does not serve", "bytes that are not UTF-8"],
)
def test_a_page_the_site_cannot_serve_fails_the_publish_and_writes_nothing(pages) -> None:
    site, db = _site()

    assert not site.publish({**_pages(), **pages}, "2026-11-18-morning")
    assert db.query("SELECT count(*) AS n FROM hosted_pages").rows[0]["n"] == 0


def test_a_refused_write_fails_the_publish_rather_than_raise() -> None:
    db = SqliteD1()  # no hosted schema: the write is refused as an unseeded object refuses it

    assert not HostedPages(db, now=lambda: AT).publish(_pages(), "2026-11-18-morning")


def test_the_tenants_site_is_its_path_on_the_hosted_domain() -> None:
    assert tenant_site("cyris-hosted.example.workers.dev", "alpha") == (
        "cyris-hosted.example.workers.dev/t/alpha"
    )
    assert SITE_PATH == "/t/{tenant}"


def test_the_pages_table_is_the_hosted_schemas_and_never_a_self_hosted_stores() -> None:
    schema = (Path(HOSTED_SCHEMA_PATH).parent / "schema.sql").read_text(encoding="utf-8")
    self_hosted = SqliteD1()
    tables = {r["name"] for r in self_hosted.query("SELECT name FROM sqlite_master").rows}

    assert "hosted_pages" not in schema
    assert "hosted_pages" not in tables
    assert "CREATE TABLE IF NOT EXISTS hosted_pages" in HOSTED_SCHEMA_PATH.read_text("utf-8")
