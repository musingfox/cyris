"""Integration tests for ArticleStore with pipeline and CLI flow."""


def test_dedup_window_matches_rss_worker_retention() -> None:
    """The store's dedup scan and the Worker's prune must span the same days.

    Shortening the scan re-ingests what the buffer still holds; shortening the
    prune drops articles the scan expects to recognise. Neither side errors.
    """
    import re
    from pathlib import Path

    from cyris.adapters.store.article_store import _DEDUP_SCAN_DAYS

    index_js = Path(__file__).resolve().parents[1] / "workers" / "rss" / "src" / "index.js"
    match = re.search(r"const RETENTION_DAYS = (\d+)", index_js.read_text())
    assert match, "RETENTION_DAYS not found in workers/rss/src/index.js"
    assert int(match.group(1)) == _DEDUP_SCAN_DAYS


def test_both_backends_order_languages_the_same_way() -> None:
    """The D1 CASE and the JSON key function come from one tuple.

    A reader switching `[store] backend` must not see the deck reorder, so the
    SQL is generated rather than transcribed; this pins the generated form.
    """
    from cyris.adapters.store.d1_store import _SORT_EXPRESSIONS
    from cyris.domain.language import LANGUAGE_SORT_ORDER, language_sort_key

    sql = _SORT_EXPRESSIONS["language"].format(dir="ASC")
    for code in LANGUAGE_SORT_ORDER:
        assert f"WHEN '{code}' THEN {language_sort_key(code)}" in sql
    assert f"ELSE {language_sort_key('an-unknown-code')}" in sql
