"""The runbook's verdict on two hosted tenants: each one's runs, issues and articles are its own."""

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


def _report(tenant: str, sources: list[str], **overrides) -> dict:
    report = {
        "tenant": tenant,
        "sources": sources,
        "stored_articles": [{"source_name": sources[0], "articles": 3}],
        "digest_runs": [{"id": 1, "status": "ok", "dry_run": 0, "period": "morning"}],
        "digests": [{"date": "2026-11-18", "period": "morning", "articles_included": 3}],
    }
    return {**report, **overrides}


ALPHA = _report("alpha", ["Hacker News", "Ars Technica"])
BRAVO = _report("bravo", ["Quanta Magazine", "Aeon"])


def _files(tmp_path: Path, *reports: dict) -> list[str]:
    paths = []
    for report in reports:
        path = tmp_path / f"{report['tenant']}.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        paths.append(str(path))
    return paths


def test_two_tenants_holding_only_their_own_rows_pass(tmp_path: Path, capsys) -> None:
    assert check.main(_files(tmp_path, ALPHA, BRAVO)) == 0

    assert capsys.readouterr().out.splitlines() == [
        "PASS alpha: 1 run(s), 1 issue(s), only its own sources",
        "PASS bravo: 1 run(s), 1 issue(s), only its own sources",
    ]


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
