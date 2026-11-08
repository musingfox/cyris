"""Facts the store must agree on with its own domain and with the RSS Worker."""

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.guard]


def test_the_language_order_is_generated_from_one_tuple() -> None:
    """The D1 CASE and the domain's key function come from one tuple.

    The SQL is generated rather than transcribed; this pins the generated form.
    """
    from cyris.adapters.store.d1_store import _SORT_EXPRESSIONS
    from cyris.domain.language import LANGUAGE_SORT_ORDER, language_sort_key

    sql = _SORT_EXPRESSIONS["language"].format(dir="ASC")
    for code in LANGUAGE_SORT_ORDER:
        assert f"WHEN '{code}' THEN {language_sort_key(code)}" in sql
    assert f"ELSE {language_sort_key('an-unknown-code')}" in sql


def _feed_health_create(text: str) -> str:
    import re

    match = re.search(r"CREATE TABLE IF NOT EXISTS feed_health \(.*?\);", text, re.S)
    assert match, "feed_health's CREATE TABLE not found"
    collapsed = re.sub(r"\s+", " ", match.group(0))
    return re.sub(r"\( | \)", lambda m: m.group(0).strip(), collapsed)


def test_feed_health_is_created_identically_by_the_worker_and_the_app() -> None:
    """Either side may create `feed_health` first, so the two must be one table.

    The rss Worker can poll before cyris ever boots on a clean account, and
    `CREATE ... IF NOT EXISTS` keeps whichever came first: a column one side
    lacks would never appear, and nothing would say so.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    worker = (root / "workers" / "rss" / "src" / "index.js").read_text()
    app = (root / "src" / "cyris" / "adapters" / "store" / "schema.sql").read_text()
    assert _feed_health_create(worker) == _feed_health_create(app)
