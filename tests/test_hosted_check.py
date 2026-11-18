"""The runbook's verdict on two hosted tenants: runs, issues, articles and pages are their own."""

import hashlib
import importlib
import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_SCRIPTS = str(Path(__file__).parent.parent / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)
check = importlib.import_module("hosted_check")


def _page(tenant: str, slug: str = "2026-11-18-morning") -> bytes:
    """A digest page as the Worker serves it: its receipt names the issue, its body the tenant's."""
    return (
        f'<html><head><meta name="cyris-issue" content="{slug}"></head><body>{tenant}</body></html>'
    ).encode()


def _sha(page: bytes) -> str:
    return hashlib.sha256(page).hexdigest()


def _report(tenant: str, sources: list[str], **overrides) -> dict:
    report = {
        "tenant": tenant,
        "sources": sources,
        "stored_articles": [{"source_name": sources[0], "articles": 3}],
        "digest_runs": [{"id": 1, "status": "ok", "dry_run": 0, "period": "morning"}],
        "digests": [
            {
                "date": "2026-11-18",
                "period": "morning",
                "saved_at": "2026-11-18T08:05:00+00:00",
                "articles_included": 3,
            }
        ],
        "latest_page": {"path": "/2026-11-18-morning.html", "sha256": _sha(_page(tenant))},
    }
    return {**report, **overrides}


ALPHA = _report("alpha", ["Hacker News", "Ars Technica"])
BRAVO = _report("bravo", ["Quanta Magazine", "Aeon"])


def _files(tmp_path: Path, *reports: dict, pages: dict | None = None) -> list[str]:
    """Each report, and beside it the page its tenant was served unless `pages` says otherwise."""
    pages = pages or {}
    paths = []
    for report in reports:
        tenant = report["tenant"]
        path = tmp_path / f"{tenant}.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        page = pages.get(tenant, _page(tenant))
        if page is not None:
            path.with_suffix(".html").write_bytes(page)
        paths.append(str(path))
    return paths


def test_two_tenants_holding_only_their_own_rows_pass(tmp_path: Path, capsys) -> None:
    assert check.main(_files(tmp_path, ALPHA, BRAVO)) == 0

    assert capsys.readouterr().out.splitlines() == [
        "PASS alpha: 1 run(s), 1 issue(s), only its own sources, "
        "and its latest page is its own newest issue",
        "PASS bravo: 1 run(s), 1 issue(s), only its own sources, "
        "and its latest page is its own newest issue",
    ]


def test_a_page_carrying_the_readers_type_size_is_still_its_own() -> None:
    sized = _page("alpha").replace(
        b"</head>", b"<style>html:root{--type-scale:1.125}</style></head>"
    )

    assert check.page_problems(ALPHA, [BRAVO], sized) == []


@pytest.mark.parametrize(
    ("page", "problems"),
    [
        (None, ["its latest page did not answer 200"]),
        (
            _page("bravo"),
            [
                "its latest page is not the one its own database holds",
                "its latest page is bravo's",
            ],
        ),
        (
            _page("alpha", "2026-11-17-evening"),
            [
                "its latest page is issue 2026-11-17-evening, not its newest 2026-11-18-morning",
                "its latest page is not the one its own database holds",
            ],
        ),
        (
            b"<html>an archive with no receipt</html>",
            [
                "its latest page is issue none, not its newest 2026-11-18-morning",
                "its latest page is not the one its own database holds",
            ],
        ),
    ],
    ids=["no page", "the other tenant's page", "an older issue", "no digest page at all"],
)
def test_a_latest_page_that_is_not_the_tenants_newest_issue_fails(page, problems) -> None:
    assert check.page_problems(ALPHA, [BRAVO], page) == problems


def test_the_newest_issue_is_the_one_saved_last_whatever_its_date() -> None:
    """A rerun of an older issue saves it last, and the site root then leads with it."""
    rerun = _report(
        "alpha",
        ALPHA["sources"],
        digests=[
            {"date": "2026-11-18", "period": "morning", "saved_at": "2026-11-18T08:05:00+00:00"},
            {"date": "2026-11-17", "period": "evening", "saved_at": "2026-11-18T09:00:00+00:00"},
        ],
        latest_page={"sha256": _sha(_page("alpha", "2026-11-17-evening"))},
    )

    assert check.page_problems(rerun, [BRAVO], _page("alpha", "2026-11-17-evening")) == []


def test_a_tenant_whose_page_did_not_come_back_fails_in_main(tmp_path: Path, capsys) -> None:
    assert check.main(_files(tmp_path, ALPHA, BRAVO, pages={"bravo": None})) == 1

    out = capsys.readouterr().out
    assert "PASS alpha" in out
    assert "FAIL bravo: its latest page did not answer 200" in out


def test_an_article_from_the_other_tenants_source_fails_the_tenant_that_stored_it() -> None:
    leaked = _report(
        "alpha",
        ALPHA["sources"],
        stored_articles=[{"source_name": "Aeon", "articles": 1}],
    )

    assert check.problems(leaked, [BRAVO]) == [
        "articles from sources it was never given: Aeon",
    ]


@pytest.mark.parametrize(
    ("overrides", "problem"),
    [
        ({"digest_runs": []}, "no finished ok run (runs: none)"),
        ({"digest_runs": [{"status": "error", "dry_run": 0}]}, "no finished ok run (runs: error)"),
        ({"digest_runs": [{"status": "ok", "dry_run": 1}]}, "no finished ok run (runs: ok)"),
        ({"digests": []}, "no issue in digests"),
        ({"stored_articles": [], "sources": []}, "it has no sources: seed it first"),
    ],
)
def test_a_tenant_that_has_not_run_or_kept_an_issue_fails(overrides: dict, problem: str) -> None:
    assert check.problems({**ALPHA, **overrides}, [BRAVO]) == [problem]


def test_two_tenants_seeded_with_one_source_cannot_be_told_apart(tmp_path: Path, capsys) -> None:
    shared = _report("bravo", ["Aeon", "Ars Technica"])

    assert check.main(_files(tmp_path, ALPHA, shared)) == 1

    out = capsys.readouterr().out
    assert "FAIL alpha: shares sources with bravo: Ars Technica" in out


def test_no_report_is_a_usage_error(capsys) -> None:
    assert check.main([]) == 2
    assert "Usage: python3 scripts/hosted_check.py" in capsys.readouterr().err
