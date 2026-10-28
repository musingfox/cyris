"""The CSS receipt tooling is the digest refactor's safety argument, so it is tested.

`scripts/css_computed.py` is not exercised end to end here: it needs Chromium
and a free debug port, which would make it a flaky non-hermetic gate rather than
a test. Its verification is a hand-run receipt. What is tested is the part that
needs no browser -- the probe set it ships and the breakpoints it samples.
`scripts/settings_probe.py` is the same kind of gate, so the same holds: its
fixtures and its registry of checks are tested here, its browser run is not.
The raw, digest, labels and onboarding probes are held to the same shape.
"""

import io
import json
import re
import sys
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from css_rules import parse_style_block, receipt_fixtures

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import cdp_probe  # noqa: E402
import digest_probe  # noqa: E402
import labels_probe  # noqa: E402
import onboarding_probe  # noqa: E402
import raw_probe  # noqa: E402
import settings_probe  # noqa: E402
from css_computed import (  # noqa: E402
    BREAKPOINTS,
    PROBES_PATH,
    WIDTHS,
    _page_socket,
    load_probes,
)
from css_receipt import (  # noqa: E402
    compare,
    compare_computed_page,
    compare_page,
    load_allowed,
    load_computed_allowed,
    snapshot,
)
from settings_probe import (  # noqa: E402
    CHECKS,
    EXPECTED_READINESS,
    build_fixture,
    probe_environment,
)

import cyris.entrypoints  # noqa: E402
from cyris.config import GRADE_D_KEYS  # noqa: E402

pytestmark = pytest.mark.integration


def _style(css: str) -> str:
    return f"<html><head><style>{css}</style></head><body></body></html>"


def test_body_keeps_declaration_order_and_other_rules_do_not():
    rules = parse_style_block(
        _style("body { background: red; background-image: none; } .x { b: 2; a: 1; }")
    )
    assert rules["body"] == ["background: red", "background-image: none"]
    assert rules[".x"] == {"a: 1", "b: 2"}


def test_a_reordered_background_shorthand_is_visible_in_the_body_receipt():
    first = parse_style_block(_style("body { background: red; background-image: none; }"))
    second = parse_style_block(_style("body { background-image: none; background: red; }"))
    assert first["body"] != second["body"]


def test_a_value_spanning_lines_normalizes_to_single_spaces():
    rules = parse_style_block(
        _style("body {\n  background-size:\n      100% 100%,\n      32px 32px;\n}")
    )
    assert rules["body"] == ["background-size: 100% 100%, 32px 32px"]


def test_the_real_digest_multi_line_values_normalize_to_single_spaces():
    _, digest_html, _ = receipt_fixtures()
    declarations = parse_style_block(digest_html)["body"]
    background = next(d for d in declarations if d.startswith("background-image:"))
    assert "\n" not in background
    assert "  " not in background


def test_a_semicolon_inside_parentheses_does_not_split_a_declaration():
    rules = parse_style_block(_style('.x { grid-template-areas: "a; b"; color: red; }'))
    assert rules[".x"] == {'grid-template-areas: "a; b"', "color: red"}


def test_nested_at_rules_keep_their_whole_ancestor_path():
    rules = parse_style_block(
        _style("@media (max-width: 880px) { @supports (d: grid) { .x { color: red; } } }")
    )
    assert rules == {"@media (max-width: 880px) | @supports (d: grid) | .x": {"color: red"}}


def test_a_rule_inside_a_media_query_never_collides_with_the_top_level_one():
    rules = parse_style_block(
        _style("body { padding: 24px; } @media (max-width: 880px) { body { padding: 16px; } }")
    )
    assert rules["body"] == ["padding: 24px"]
    assert rules["@media (max-width: 880px) | body"] == {"padding: 16px"}


def test_a_selector_declared_twice_at_one_path_keeps_both_blocks():
    rules = parse_style_block(_style(".footer a { color: red; } .footer a { padding: 4px; }"))
    assert rules[".footer a"] == {"color: red", "padding: 4px"}


def test_a_selector_redeclared_inside_the_same_media_query_keeps_both_blocks():
    css = "@media (max-width: 880px) { .x { color: red; } .x { gap: 8px; } }"
    rules = parse_style_block(_style(css))
    assert rules["@media (max-width: 880px) | .x"] == {"color: red", "gap: 8px"}


def test_a_second_body_block_extends_the_ordered_declarations():
    rules = parse_style_block(_style("body { background: red; } body { background-size: 32px; }"))
    assert rules["body"] == ["background: red", "background-size: 32px"]


def test_comments_are_stripped_before_parsing():
    rules = parse_style_block(_style("/* .ghost { color: red; } */ .x { color: blue; }"))
    assert rules == {".x": {"color: blue"}}


def test_the_fixture_renders_every_page_the_receipt_covers():
    index_html, digest_html, raw_html = receipt_fixtures()
    for html in (index_html, digest_html, raw_html):
        assert parse_style_block(html)
    assert "Featured Story" in digest_html
    assert "Pending Article" in raw_html


def test_snapshot_writes_the_pages_and_a_rules_receipt(tmp_path):
    snapshot(tmp_path)
    rules = json.loads((tmp_path / "rules.json").read_text())
    assert set(rules) == {"index", "digest", "raw"}
    for name in ("index", "digest", "raw"):
        assert (tmp_path / f"{name}.html").exists()
    assert rules["digest"]["body"][0].startswith("font-family")


def test_snapshot_is_reproducible(tmp_path):
    snapshot(tmp_path / "a")
    snapshot(tmp_path / "b")
    first = (tmp_path / "a" / "rules.json").read_text()
    assert first == (tmp_path / "b" / "rules.json").read_text()


def test_identical_pages_compare_clean_without_an_allow_list():
    assert compare_page({"a": ["x: 1"]}, {"a": ["x: 1"]}, {}) == []


def test_a_declaration_reordered_within_a_set_valued_rule_is_not_a_difference():
    assert compare_page({".x": ["a: 1", "b: 2"]}, {".x": ["b: 2", "a: 1"]}, {}) == []


def test_a_declaration_reordered_within_body_is_a_difference():
    problems = compare_page({"body": ["a: 1", "b: 2"]}, {"body": ["b: 2", "a: 1"]}, {})
    assert any("~ body" in line for line in problems)


def test_a_new_key_absent_from_added_is_a_failure_even_when_other_keys_are_allowed():
    allowance = {"added": [".brand-name"], "removed": [], "changed": {}}
    problems = compare_page({}, {".brand-name": ["a: 1"], ".masthead::after": ["b: 2"]}, allowance)
    assert problems == ["  + .masthead::after is not in this page's `added` list"]


def test_a_removed_key_must_be_named_in_removed():
    assert compare_page({".gone": ["a: 1"]}, {}, {}) == [
        "  - .gone is not in this page's `removed` list"
    ]
    assert compare_page({".gone": ["a: 1"]}, {}, {"removed": [".gone"]}) == []


def test_a_changed_key_is_checked_against_its_exact_after_value():
    before = {".brand": ["display: block", "font-size: 12px"]}
    after = {".brand": ["display: flex", "gap: 14px"]}
    allowance = {"changed": {".brand": ["display: flex", "gap: 14px"]}}
    assert compare_page(before, after, allowance) == []


def test_naming_a_changed_key_without_all_its_declarations_still_fails():
    before = {".brand": ["display: block"]}
    after = {".brand": ["display: flex", "gap: 14px"]}
    allowance = {"changed": {".brand": ["display: flex"]}}
    problems = compare_page(before, after, allowance)
    assert problems[0] == "  ~ .brand did not change to the value `changed` names"
    assert "      actual only:   gap: 14px" in problems


def test_a_changed_entry_reports_what_it_expected_and_what_it_found():
    problems = compare_page({".x": ["a: 1"]}, {".x": ["a: 2"]}, {"changed": {".x": ["a: 3"]}})
    assert "      expected only: a: 3" in problems
    assert "      actual only:   a: 2" in problems


def test_an_unlisted_change_reports_the_declarations_that_moved():
    problems = compare_page({".x": ["a: 1"]}, {".x": ["a: 2"]}, {})
    assert problems[0] == "  ~ .x changed and is not in this page's `changed` map"
    assert "      expected only: a: 1" in problems


def test_a_changed_allowance_for_body_respects_declaration_order():
    before = {"body": ["a: 1", "b: 2"]}
    after = {"body": ["b: 2", "a: 1"]}
    assert compare_page(before, after, {"changed": {"body": ["b: 2", "a: 1"]}}) == []
    assert compare_page(before, after, {"changed": {"body": ["a: 1", "b: 2"]}}) != []


def test_a_refused_devtools_connection_is_retried_until_the_deadline(monkeypatch):
    attempts = []

    def fake_urlopen(url, timeout=None):
        attempts.append(url)
        if len(attempts) == 1:
            raise urllib.error.URLError("connection refused")
        target = [{"type": "page", "webSocketDebuggerUrl": "ws://127.0.0.1:9/page"}]
        return io.BytesIO(json.dumps(target).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert _page_socket(9, time.monotonic() + 5) == "ws://127.0.0.1:9/page"
    assert len(attempts) == 2


def test_a_pinned_key_reaching_one_snapshot_is_reported_once():
    pins = {"color": {"before": "red", "after": "blue"}}
    problems = compare_computed_page({"@1440 | .x": {"color": "red"}}, {}, {"@1440 | .x": pins})
    assert problems == ["  - @1440 | .x was measured only before the change"]


def test_a_pin_naming_a_key_in_neither_snapshot_still_fails():
    pins = {"color": {"before": "red", "after": "blue"}}
    problems = compare_computed_page({}, {}, {"@1440 | .x": pins})
    assert problems == ["  ! @1440 | .x `color` is pinned but no longer measured"]


def _write_snapshot(directory: Path, rules: dict, computed_receipt: bool = True) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "rules.json").write_text(json.dumps(rules), encoding="utf-8")
    # `compare` requires both receipts, so a rule-layer test still carries an
    # empty computed one unless it is the absence itself under test.
    if computed_receipt:
        (directory / "computed.json").write_text(json.dumps({}), encoding="utf-8")
    return directory


def test_the_computed_receipt_is_compared_strictly_when_both_snapshots_have_one(tmp_path, capsys):
    rules = {"digest": {".x": ["a: 1"]}}
    before = _write_snapshot(tmp_path / "before", rules)
    after = _write_snapshot(tmp_path / "after", rules)
    (before / "computed.json").write_text(json.dumps({"digest": {"@880 | .x": {"gap": "4px"}}}))
    (after / "computed.json").write_text(json.dumps({"digest": {"@880 | .x": {"gap": "8px"}}}))
    assert compare(before, after, None) == 1
    output = capsys.readouterr().out
    assert "computed.json :: digest" in output
    assert "      actual only:   gap: 8px" in output


def test_a_computed_receipt_present_in_only_one_snapshot_is_a_failure(tmp_path):
    rules = {"digest": {}}
    before = _write_snapshot(tmp_path / "before", rules)
    after = _write_snapshot(tmp_path / "after", rules, computed_receipt=False)
    (before / "computed.json").write_text(json.dumps({"digest": {}}))
    assert compare(before, after, None) == 1


def test_a_computed_receipt_absent_from_both_snapshots_is_a_failure(tmp_path, capsys):
    rules = {"digest": {".x": ["a: 1"]}}
    before = _write_snapshot(tmp_path / "before", rules, computed_receipt=False)
    after = _write_snapshot(tmp_path / "after", rules, computed_receipt=False)
    assert compare(before, after, None) == 1
    assert "absent from both snapshots" in capsys.readouterr().out


def test_an_allow_list_never_excuses_a_computed_difference(tmp_path):
    rules = {"digest": {}}
    before = _write_snapshot(tmp_path / "before", rules)
    after = _write_snapshot(tmp_path / "after", rules)
    (before / "computed.json").write_text(json.dumps({"digest": {"@880 | .x": {"gap": "4px"}}}))
    (after / "computed.json").write_text(json.dumps({"digest": {"@880 | .x": {"gap": "8px"}}}))
    allow = tmp_path / "allow.json"
    allow.write_text(json.dumps({"digest": {"changed": {"@880 | .x": {"gap": "8px"}}}}))
    assert compare(before, after, allow) == 1


def test_compare_exits_zero_on_identical_snapshots(tmp_path, capsys):
    rules = {"digest": {".x": ["a: 1"]}}
    before = _write_snapshot(tmp_path / "before", rules)
    after = _write_snapshot(tmp_path / "after", rules)
    assert compare(before, after, None) == 0
    output = capsys.readouterr().out
    assert "equal modulo the allow-list" in output
    assert "comparing rules.json and computed.json" in output


def test_compare_exits_non_zero_and_names_the_page_of_an_unallowed_change(tmp_path, capsys):
    before = _write_snapshot(tmp_path / "before", {"digest": {".x": ["a: 1"]}})
    after = _write_snapshot(tmp_path / "after", {"digest": {".x": ["a: 2"]}})
    assert compare(before, after, None) == 1
    output = capsys.readouterr().out
    assert output.startswith("rules.json :: digest\n")
    assert "1 unallowed difference(s)" in output


def test_compare_applies_each_pages_allowance_separately(tmp_path):
    before = _write_snapshot(tmp_path / "before", {"digest": {}, "raw": {}})
    after = _write_snapshot(tmp_path / "after", {"digest": {".new": ["a: 1"]}, "raw": {}})
    allow = tmp_path / "allow.json"
    allow.write_text(json.dumps({"raw": {"added": [".new"]}}), encoding="utf-8")
    assert compare(before, after, allow) == 1
    allow.write_text(json.dumps({"digest": {"added": [".new"]}}), encoding="utf-8")
    assert compare(before, after, allow) == 0


def test_an_allow_list_of_the_wrong_shape_is_rejected(tmp_path):
    path = tmp_path / "allow.json"
    path.write_text(json.dumps([".x"]), encoding="utf-8")
    with pytest.raises(SystemExit):
        load_allowed(path)
    path.write_text(json.dumps({"digest": {"added": {".x": []}}}), encoding="utf-8")
    with pytest.raises(SystemExit):
        load_allowed(path)
    path.write_text(json.dumps({"digest": {"changed": [".x"]}}), encoding="utf-8")
    with pytest.raises(SystemExit):
        load_allowed(path)


def test_a_real_snapshot_pair_compares_clean_and_catches_a_planted_change(tmp_path):
    snapshot(tmp_path / "before")
    snapshot(tmp_path / "after")
    for directory in (tmp_path / "before", tmp_path / "after"):
        (directory / "computed.json").write_text(json.dumps({}), encoding="utf-8")
    assert compare(tmp_path / "before", tmp_path / "after", None) == 0

    rules_path = tmp_path / "after" / "rules.json"
    rules = json.loads(rules_path.read_text())
    rules["digest"]["body"] = [d for d in rules["digest"]["body"] if "background-size" not in d]
    rules_path.write_text(json.dumps(rules), encoding="utf-8")
    assert compare(tmp_path / "before", tmp_path / "after", None) == 1


def test_a_pure_reorder_reports_the_two_orders_rather_than_an_empty_delta():
    problems = compare_page({"body": ["a: 1", "b: 2"]}, {"body": ["b: 2", "a: 1"]}, {})
    assert "      expected order: a: 1, b: 2" in problems
    assert "      actual order:   b: 2, a: 1" in problems


def test_the_sampled_widths_cover_every_breakpoint_the_rendered_pages_declare():
    pages = dict(zip(("index", "digest", "raw"), receipt_fixtures(), strict=True))
    declared = {
        name: {
            int(found.group(1))
            for key in parse_style_block(html)
            if (found := re.search(r"@media \(max-width: (\d+)px\)", key))
        }
        for name, html in pages.items()
    }
    assert declared == {name: set(widths) for name, widths in BREAKPOINTS.items()}
    assert {width for widths in BREAKPOINTS.values() for width in widths} <= set(WIDTHS)


def test_the_sampled_widths_are_every_breakpoint_between_a_desktop_and_a_phone():
    assert WIDTHS == (1440, 720, 375)


def test_every_probe_names_a_page_the_receipt_renders_and_at_least_one_property():
    probes = load_probes(PROBES_PATH)
    assert set(probes) == {"index", "digest", "raw"}
    for page, selectors in probes.items():
        assert selectors, page
        for selector, properties in selectors.items():
            assert properties, f"{page} | {selector}"


def test_the_raw_probe_set_watches_the_rows_and_panels_it_renders():
    raw = load_probes(PROBES_PATH)["raw"]
    assert {".raw-row", ".raw-row a", ".panel-head"} <= set(raw)
    assert ".article" not in raw
    assert ".article a" not in raw


def test_each_raw_source_is_a_panel_of_rows_in_state_score_title_vote_order():
    html = raw_probe.render_page()
    assert html.count('<section class="panel">') == 2
    assert '<span class="source-name">Source A</span><span class="label">3</span>' in html
    assert re.search(
        r'<div class="list-row raw-row">\s*<span class="state pending">pending</span>'
        r'\s*<span class="score">0.80</span>'
        r'\s*<a href="https://example.test/pending-two" target="_blank" rel="noopener" lang="">'
        r'Pending Two</a>\s*<span class="vote-group"',
        html,
    )
    assert re.search(
        r'<span class="score">—</span>\s*<a href="https://example.test/pending-three"', html
    )


DIGEST_ITEM_PROBES = {
    ".section-tag",
    ".meta",
    ".featured-item",
    ".news-cluster",
    ".news-paragraph",
    ".article-item",
    ".headline-item",
    ".lead-story h2",
}


def test_the_digest_probe_set_watches_every_item_row_like_the_lead():
    digest = load_probes(PROBES_PATH)["digest"]
    assert set(digest) >= DIGEST_ITEM_PROBES
    for selector in DIGEST_ITEM_PROBES:
        assert digest[selector] == digest[".lead-story"], selector


def test_the_raw_probe_set_watches_the_page_head_not_the_masthead_it_replaced():
    raw = load_probes(PROBES_PATH)["raw"]
    assert ".page-head" in raw
    assert ".masthead" not in raw
    assert ".subtitle" not in raw


def test_the_index_probe_set_watches_the_page_head_not_the_masthead_it_replaced():
    index = load_probes(PROBES_PATH)["index"]
    assert ".page-head" in index
    assert [key for key in (".masthead", ".subtitle", "h1") if key in index] == []


def test_the_probe_set_watches_the_masthead_name_on_every_page_that_has_one():
    probes = load_probes(PROBES_PATH)
    for page in ("index", "digest", "raw"):
        assert ".brand-name" in probes[page], page


def _pin(before: str, after: str) -> dict[str, str]:
    return {"before": before, "after": after}


def test_a_pinned_cell_passes_only_on_the_exact_pair_it_names():
    before = {"@880 | .brand": {"color": "rgb(1, 1, 1)"}}
    after = {"@880 | .brand": {"color": "rgb(2, 2, 2)"}}
    pins = {"@880 | .brand": {"color": _pin("rgb(1, 1, 1)", "rgb(2, 2, 2)")}}
    assert compare_computed_page(before, after, pins) == []


def test_a_pinned_cell_that_lands_on_another_after_value_fails():
    before = {"@880 | .brand": {"color": "rgb(1, 1, 1)"}}
    after = {"@880 | .brand": {"color": "rgb(9, 9, 9)"}}
    pins = {"@880 | .brand": {"color": _pin("rgb(1, 1, 1)", "rgb(2, 2, 2)")}}
    problems = compare_computed_page(before, after, pins)
    assert problems[0] == "  ~ @880 | .brand `color` is not the before -> after pair pinned for it"
    assert "      measured: rgb(1, 1, 1) -> rgb(9, 9, 9)" in problems


def test_a_pin_whose_before_no_longer_holds_fails_too():
    before = {"@880 | .brand": {"color": "rgb(8, 8, 8)"}}
    after = {"@880 | .brand": {"color": "rgb(2, 2, 2)"}}
    pins = {"@880 | .brand": {"color": _pin("rgb(1, 1, 1)", "rgb(2, 2, 2)")}}
    assert compare_computed_page(before, after, pins) != []


def test_a_pin_keeps_the_cell_measured_rather_than_dropping_it():
    unchanged = {"@880 | .brand": {"color": "rgb(1, 1, 1)"}}
    pins = {"@880 | .brand": {"color": _pin("rgb(1, 1, 1)", "rgb(2, 2, 2)")}}
    assert compare_computed_page(unchanged, unchanged, pins) != []


def test_a_pin_on_one_property_does_not_cover_its_neighbours():
    before = {"@880 | .brand": {"color": "rgb(1, 1, 1)", "font-size": "13px"}}
    after = {"@880 | .brand": {"color": "rgb(2, 2, 2)", "font-size": "16px"}}
    pins = {"@880 | .brand": {"color": _pin("rgb(1, 1, 1)", "rgb(2, 2, 2)")}}
    problems = compare_computed_page(before, after, pins)
    assert problems[0].endswith("is not pinned in this page's computed allowance")
    assert "      actual only:   font-size: 16px" in problems


def test_a_probe_key_on_only_one_side_is_a_failure_no_pin_can_excuse():
    assert compare_computed_page({}, {"@880 | .new": {"color": "red"}}, {}) == [
        "  + @880 | .new was measured only after the change"
    ]
    assert compare_computed_page({"@880 | .gone": {"color": "red"}}, {}, {}) == [
        "  - @880 | .gone was measured only before the change"
    ]


def test_a_computed_allowance_weaker_than_a_pinned_pair_is_rejected(tmp_path):
    path = tmp_path / "allow-computed.json"
    for payload in (
        ["@880 | .brand"],
        {"raw": ["@880 | .brand"]},
        {"raw": {"@880 | .brand": ["color"]}},
        {"raw": {"@880 | .brand": {"color": "rgb(2, 2, 2)"}}},
        {"raw": {"@880 | .brand": {"color": {"after": "rgb(2, 2, 2)"}}}},
        {"raw": {"@880 | .brand": {"color": _pin("rgb(1, 1, 1)", "rgb(1, 1, 1)")}}},
    ):
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(SystemExit):
            load_computed_allowed(path)


def test_the_computed_allow_list_is_what_clears_a_computed_difference(tmp_path):
    rules = {"digest": {}}
    before = _write_snapshot(tmp_path / "before", rules)
    after = _write_snapshot(tmp_path / "after", rules)
    (before / "computed.json").write_text(json.dumps({"digest": {"@880 | .x": {"gap": "4px"}}}))
    (after / "computed.json").write_text(json.dumps({"digest": {"@880 | .x": {"gap": "8px"}}}))
    pins = tmp_path / "allow-computed.json"
    pins.write_text(json.dumps({"digest": {"@880 | .x": {"gap": _pin("4px", "8px")}}}))
    assert compare(before, after, None, pins) == 0

    (after / "computed.json").write_text(json.dumps({"digest": {"@880 | .x": {"gap": "12px"}}}))
    assert compare(before, after, None, pins) == 1


def test_a_pin_whose_cell_left_the_probe_set_fails_rather_than_going_quiet():
    cell = {"@880 | .brand": {"font-size": "13px"}}
    pins = {"@880 | .brand": {"color": _pin("rgb(1, 1, 1)", "rgb(2, 2, 2)")}}
    assert compare_computed_page(cell, cell, pins) == [
        "  ! @880 | .brand `color` is pinned but no longer measured"
    ]


def test_the_probe_set_watches_the_site_bar_and_not_the_meta_strip():
    probes = load_probes(PROBES_PATH)
    assert ".meta-strip" not in probes["digest"]
    for page in ("index", "digest", "raw"):
        for selector in (".site-bar", ".site-nav a", ".brand-name"):
            assert selector in probes[page], f"{page} | {selector}"


def test_the_probe_set_watches_no_footer_link():
    probes = load_probes(PROBES_PATH)
    assert [page for page, selectors in probes.items() if ".footer a" in selectors] == []


# The checks `scripts/settings_probe.py` must carry. Each later change to the
# settings page adds the ids of the checks that hold it.
EXPECTED_IDS = {
    "site-bar-current",
    "digest-language-offers-the-listed-languages-by-name",
    "digest-keeps-an-unlisted-stored-value",
    "digest-save-keeps-an-unlisted-value-it-did-not-change",
    "digest-timezone-lists-the-browsers-zones-and-utc",
    "digest-timezone-with-none-stored-opens-on-the-browsers",
    "digest-timezone-detected-is-saved-as-it-stands",
    "editor-opens-on-enter",
    "editor-opens-on-space",
    "editor-cancel-focuses-row",
    "editor-row-close-focuses-row",
    "editor-new-cancel-focuses-add",
    "source-save-failure-focuses-save",
    "retire-failure-focuses-retire",
    "retire-focuses-add",
    "site-bar-links-align",
    "no-credential-in-dom",
    "hash-direct",
    "hash-refresh",
    "hash-default",
    "hash-nav-click",
    "model-readiness",
    "unavailable-state-opaque",
    "model-no-keys",
    "model-placeholder",
    "save-disabled-while-loading",
    "dirty-enables-save",
    "revert-disables-save",
    "dirty-radio",
    "sources-columns",
    "sources-feed-health",
    "sources-filter",
    "sources-empty-filter",
    "editor-opens-under-row",
    "editor-cancel",
    "editor-dirty",
    "editor-heading-escapes",
    "editor-add-at-top",
    "editor-type-fields",
    "source-save-nulls-hidden-fields",
    "source-save-tags",
    "notice-ok-beside-save",
    "notice-err-beside-save",
    "notice-hides-on-edit",
    "model-notice-ok",
    "notify-notice-masked",
    "notice-unreachable",
    "notice-unexplained-refusal",
    "source-notice-beside-save",
    "source-notice-outside-filter",
    "settings-load-failure",
    "sources-load-failure",
    "digest-posts-featured-only",
    "digest-posts-hours-only",
    "digest-partial-failure",
    "retire-arms",
    "retire-reverts",
    "retire-deletes",
    "retire-last-allowed",
    "sources-empty-all",
    "readonly-settings",
    "readonly-sources",
    "areas-lists",
    "areas-empty-no-missing-mark",
    "areas-readonly-json",
    "areas-read-failure",
    "areas-rename",
    "areas-rename-conflict",
    "areas-dismiss-arms",
    "areas-dismiss-confirms",
    "areas-position-local",
    "areas-save-order",
    "areas-save-order-refused",
    "areas-source-options",
    "areas-sources-both-reads",
    "areas-empty-sources-link",
    "areas-editor-groups",
    "areas-side-by-side-1300",
    "areas-proposal-picked",
    "areas-reload-keeps-unsaved",
    "areas-reload-source-gone",
    "areas-reload-activated",
    "areas-dot-combined",
    "areas-assign-save",
    "areas-assign-refused",
    "areas-assign-two-proposals",
    "areas-assign-order",
    "areas-assign-partial",
    "areas-propose-notice",
    "areas-propose-no-source",
    "areas-assign-unconfirmed",
    "areas-tap-375",
    "areas-threshold-saves",
    "areas-threshold-refused",
    "areas-threshold-dirty",
    "areas-threshold-json",
    "areas-propose-refused",
    "areas-propose-busy",
    "areas-propose-prefill",
    "areas-propose-prefill-edited",
    "areas-propose-suggests",
    "areas-propose-suggests-same",
    "areas-propose-two-entries-one-area",
    "areas-rename-gone",
    "areas-save-inert",
    "areas-merge-empty",
    "areas-read-failure-keeps-source-area",
    "areas-pending-keeps-source-area",
    "areas-suggest-no-active",
    "areas-suggest-busy",
    "areas-suggest-tag",
    "areas-source-keeps-area",
    "areas-suggest-selects",
    "areas-suggest-failure-keeps-area",
    "areas-source-json",
    "tracking-rows",
    "tracking-empty-is-no-missing-mark",
    "tracking-add",
    "tracking-no-threshold-refused",
    "tracking-remove",
    "fits-400",
    "fits-1440",
    "save-held-in-flight-model",
    "save-held-in-flight-digest",
    "save-held-in-flight-notifications",
    "edit-during-save-stays-dirty-model",
    "edit-during-save-stays-dirty-notifications",
    "editor-save-held-in-flight",
    "editor-locked-while-saving",
    "missing-marked",
    "missing-clears-on-save",
    "missing-readonly",
    "missing-none-when-set",
    "digest-posts-new-fields",
    "digest-style-empty",
    "digest-bad-timezone",
    "hash-pipeline",
    "pipeline-posts-changed-only",
    "pipeline-refusal",
    "model-posts-vote-only",
    "model-vote-checking",
    "embedding-readiness",
    "model-vote-refusal",
    "model-none-posts",
    "model-none-loaded",
    "model-missing-unchecked",
    "notify-off-arms",
    "notify-off-stores",
    "notify-off-hidden-when-off",
    "type-scale-saves",
    "type-scale-unset",
    "type-scale-unset-clears-on-save",
    "type-scale-applies-on-save",
    "type-scale-kept-on-refusal",
    "fits-largest-400",
    "fits-largest-1440",
    "fits-375-choice-states",
    "fits-375-source-editor",
    "fits-375-tab-hint",
    "fits-largest-375-choice-states",
    "fits-largest-375-source-editor",
    "fits-largest-375-tab-hint",
    "fits-375-tab-hint-taps-through",
    "fits-largest-375-tab-hint-taps-through",
    "tap-tabs-375",
    "tap-folds-375",
    "digest-order-waits-for-vote-similarity",
    "digest-order-follows-vote-similarity",
    "digest-order-unset-saves-off",
    "digest-clef-waits-for-its-token",
    "digest-clef-offered-with-its-token",
    "digest-order-reason-describes-it",
    "digest-clef-reason-describes-it",
    "digest-order-reason-leaves-with-vote-similarity",
}


async def _get_json(fixture, path: str, method: str = "GET", body=None) -> tuple[int, dict]:
    client = TestClient(TestServer(fixture.app))
    await client.start_server()
    try:
        response = await client.request(method, path, json=body)
        return response.status, await response.json()
    finally:
        await client.close()


async def test_the_readonly_probe_fixture_can_write_neither_settings_nor_sources():
    _, settings = await _get_json(build_fixture("readonly"), "/api/settings")
    _, sources = await _get_json(build_fixture("readonly"), "/api/sources")
    assert settings["writable"] is False
    assert settings["missing"] == []
    assert sources["writable"] is False
    assert "origin" not in sources


async def test_the_writable_probe_fixture_serves_its_four_sources_from_the_table():
    _, settings = await _get_json(build_fixture("writable"), "/api/settings")
    _, sources = await _get_json(build_fixture("writable"), "/api/sources")
    assert settings["writable"] is True
    assert settings["missing"] == []
    assert sources["writable"] is True
    assert "origin" not in sources
    assert len(sources["sources"]) == 4


async def test_the_probe_environment_resolves_the_provider_readiness_it_promises():
    with probe_environment():
        _, settings = await _get_json(build_fixture("writable"), "/api/settings")
    resolved = {p["name"]: p["configured"] for p in settings["providers"]}
    assert (
        resolved
        == EXPECTED_READINESS
        == {
            "anthropic": True,
            "gemini": True,
            "openai": False,
            "workers_ai": False,
            "ai_gateway": False,
            "openai_compatible": True,
            "none": True,
        }
    )


async def test_a_webhook_saved_through_the_probe_fixture_is_recorded_without_a_network():
    url = "https://discord.com/api/webhooks/9/NEWTOKEN"
    fixture = build_fixture("writable")
    with probe_environment() as probes:
        status, _ = await _get_json(
            fixture, "/api/settings/notify", "POST", {"discord_webhook_url": url}
        )
    assert status == 200
    assert fixture.settings.calls == [{"notify.discord_webhook_url": url}]
    assert probes.discord.await_count == 1


def test_every_probe_check_is_named_once_and_can_be_sabotaged():
    ids = [check.id for check in CHECKS]
    assert len(ids) == len(set(ids))
    assert [c.id for c in CHECKS if not (c.sabotage.strip() or c.sabotage_preload)] == []
    assert {c.fixture for c in CHECKS} <= {
        "readonly",
        "writable",
        "writable-largest",
        "writable-health",
        "writable-topics",
        "writable-areas",
        "writable-areas-empty",
        "writable-areas-unset",
        "writable-areas-polish",
    }
    assert set(ids) >= EXPECTED_IDS


def test_the_settings_probe_runs_on_the_shared_core():
    assert settings_probe.Check is cdp_probe.Check
    assert getattr(settings_probe, "DRIVER", cdp_probe.DRIVER) is cdp_probe.DRIVER


def test_the_shared_probe_core_knows_no_page():
    source = Path(cdp_probe.__file__).read_text()
    for word in ("TriageServer", "FakeSettings", "providersLoaded", ".settings-nav"):
        assert word not in source, word


DRAG = ({"press": "#c", "pointer": "mouse"}, {"move": 200}, {"release": True})


def test_a_probe_job_carries_its_gestures_in_declared_order():
    check = cdp_probe.Check(id="x", fixture="k", path="/p", script="", gestures=DRAG)
    assert cdp_probe.build_job(check, "http://h", False, "")["gestures"] == list(DRAG)


def test_a_probe_job_without_gestures_carries_none():
    check = cdp_probe.Check(id="x", fixture="k", path="/p", script="")
    assert cdp_probe.build_job(check, "http://h", False, "")["gestures"] == []


def test_a_probe_job_carries_a_mouse_button_and_a_vertical_move():
    gestures = (
        {"press": "#c", "pointer": "mouse", "button": "right"},
        {"move": 0, "dy": 60},
        {"release": True},
    )
    check = cdp_probe.Check(id="x", fixture="k", path="/p", script="", gestures=gestures)
    assert cdp_probe.build_job(check, "http://h", False, "")["gestures"] == list(gestures)


def test_a_probe_job_carries_its_key_presses():
    gestures = (
        {"key": "Tab"},
        {"key": "Enter"},
        {"key": " "},
        {"key": "ArrowLeft"},
        {"key": "ArrowRight"},
    )
    check = cdp_probe.Check(id="x", fixture="k", path="/p", script="", gestures=gestures)
    assert cdp_probe.build_job(check, "http://h", False, "")["gestures"] == list(gestures)


@pytest.mark.parametrize(
    "gestures",
    [
        ({"fling": 1},),
        ({"key": "Escape"},),
        ({"key": "Enter", "pointer": "mouse"},),
        ({"move": 10},),
        ({"release": True},),
        ({"press": "#c", "pointer": "touch", "button": "right"},),
        ({"press": "#c", "pointer": "mouse", "button": "back"},),
        ({"press": "#c", "pointer": "mouse"}, {"move": 0, "dy": "60"}),
        ({"press": "#c", "pointer": "mouse"}, {"dy": 60}),
    ],
)
def test_a_gesture_step_that_is_unknown_or_has_no_press_is_refused(gestures):
    with pytest.raises(ValueError):
        cdp_probe.Check(id="x", fixture="k", path="/p", script="", gestures=gestures)


def test_a_probe_job_carries_the_media_features_it_emulates():
    reduced = (("prefers-reduced-motion", "reduce"),)
    check = cdp_probe.Check(id="x", fixture="k", path="/p", script="", media=reduced)
    job = cdp_probe.build_job(check, "http://h", False, "")
    assert job["media"] == [{"name": "prefers-reduced-motion", "value": "reduce"}]
    plain = cdp_probe.Check(id="x", fixture="k", path="/p", script="")
    assert cdp_probe.build_job(plain, "http://h", False, "")["media"] == []


def test_the_driver_emulates_media_before_it_loads_the_page():
    driver = cdp_probe.DRIVER
    assert driver.index("Emulation.setEmulatedMedia") < driver.index(
        "call('Page.navigate', { url })"
    )


# The checks `scripts/raw_probe.py` must carry. Each raw-page change adds the ids
# of the checks that hold it.
EXPECTED_RAW_IDS = {
    "list-vote-marks-done",
    "deck-tab-then-enter-opens-article",
    "deck-arrow-left-votes-down",
    "deck-arrow-right-votes-up",
    "deck-arrow-on-a-button-votes",
    "deck-held-arrow-votes-once",
    "deck-last-arrow-focuses-the-switch",
    "list-arrow-no-vote",
    "list-arrow-after-switch-no-vote",
    "deck-inherited-key-no-vote",
    "deck-modified-arrow-no-vote",
    "list-vote-failure-says-why",
    "list-vote-failure-signed-out-says-sign-in",
    "list-vote-failure-unconfigured-names-the-url",
    "list-vote-failure-passed-through-503-gives-status",
    "list-vote-failure-times-out",
    "list-vote-lands-without-abort-signal-timeout",
    "list-vote-times-out-without-abort-signal-timeout",
    "list-vote-retry-clears-notice",
    "list-vote-inflight-disables-group",
    "list-vote-up-shows-state",
    "list-vote-down-shows-state",
    "list-stored-vote-shows-state",
    "list-stored-vote-keeps-rendered-verdict",
    "list-vote-keeps-focus",
    "row-title-wraps-400",
    "fits-400-list",
    "gate-signed-out",
    "gate-no-worker",
    "gate-signed-in",
    "default-list",
    "switch-to-triage",
    "switch-back-to-list",
    "fits-400-triage",
    "fits-largest-400-list",
    "fits-largest-400-triage",
    "deck-pending-only",
    "deck-skips-voted",
    "deck-title-is-text",
    "list-vote-leaves-deck",
    "deck-empty",
    "button-up-votes",
    "button-down-votes",
    "vote-post-carries-no-credential",
    "double-press-one-vote",
    "vote-failure-stays",
    "deck-signed-out-says-sign-in",
    "deck-enter-votes-each-card",
    "deck-last-vote-focuses-the-switch",
    "vote-retry-clears-notice",
    "switch-away-during-vote",
    "inflight-disabled",
    "swipe-during-button-vote",
    "swipe-right-up",
    "swipe-left-down",
    "swipe-touch",
    "short-drag-no-vote",
    "lean-up-drag",
    "lean-down-drag",
    "lean-button-hover",
    "tap-opens-article",
    "tap-touch-opens-article",
    "right-click-no-open",
    "middle-click-no-open",
    "vertical-drag-no-open",
    "reduced-motion-still",
    "reduced-motion-advances",
    "reduced-motion-swipe-advances",
    "tap-votes-375",
    "tap-votes-apart-375",
    "tap-site-bar-375",
    "tap-smallest-votes-375",
    "tap-smallest-votes-apart-375",
}

PENDING_TWO_UP = {
    "url": "https://example.test/pending-two",
    "vote": "up",
    "digest_date": "2026-01-02",
}


async def _raw_answers(fixture, requests: list[tuple[str, str]]) -> list[tuple[int, str]]:
    client = TestClient(TestServer(fixture.app))
    await client.start_server()
    try:
        answers = []
        for method, path in requests:
            body = PENDING_TWO_UP if method == "POST" else None
            response = await client.request(method, path, json=body, allow_redirects=False)
            answers.append((response.status, await response.text()))
        return answers
    finally:
        await client.close()


@pytest.mark.parametrize(
    ("kind", "status", "authorized"),
    [
        ("signed-in", 200, True),
        ("signed-in-largest", 200, True),
        ("signed-in-smallest", 200, True),
        ("signed-out", 302, None),
        ("worker-refused", 403, None),
        ("no-worker", 404, None),
        ("slow-vote", 200, True),
        ("vote-signed-out", 200, True),
        ("vote-unconfigured", 200, True),
        ("vote-hangs", 200, True),
        ("promote-503", 200, True),
    ],
)
async def test_the_raw_probe_fixture_answers_the_vote_probe_per_kind(kind, status, authorized):
    [(answered, body)] = await _raw_answers(raw_probe.build_fixture(kind), [("GET", "/api/vote")])
    assert answered == status
    if authorized is not None:
        assert json.loads(body) == {"authorized": authorized}


async def test_the_raw_probe_fixture_keeps_each_vote_it_takes_without_a_credential():
    fixture = raw_probe.build_fixture("signed-in")
    [(status, _)] = await _raw_answers(fixture, [("POST", "/api/vote")])
    assert status == 200
    assert fixture.posts == [PENDING_TWO_UP]
    assert "authorization" not in {key.lower() for key in fixture.post_headers[0]}


async def test_the_raw_probe_fixture_can_refuse_every_vote_or_only_the_first():
    refusing = raw_probe.build_fixture("vote-fails")
    [(status, _)] = await _raw_answers(refusing, [("POST", "/api/vote")])
    assert status == 502
    assert refusing.posts == [PENDING_TWO_UP]
    once = raw_probe.build_fixture("fails-once")
    answers = await _raw_answers(once, [("POST", "/api/vote"), ("POST", "/api/vote")])
    assert [status for status, _ in answers] == [502, 200]


@pytest.mark.parametrize(
    ("kind", "status", "body"),
    [
        ("vote-signed-out", 302, None),
        ("vote-unconfigured", 503, {"error": "promote worker not configured"}),
        ("promote-503", 503, {"error": "unavailable"}),
    ],
)
async def test_the_raw_probe_fixture_refuses_a_vote_as_the_app_worker_would(kind, status, body):
    fixture = raw_probe.build_fixture(kind)
    [(answered, text)] = await _raw_answers(fixture, [("POST", "/api/vote")])
    assert answered == status
    if body is not None:
        assert json.loads(text) == body
    assert fixture.posts == [PENDING_TWO_UP]


async def test_the_raw_probe_fixture_serves_the_rendered_raw_page():
    [(status, html)] = await _raw_answers(
        raw_probe.build_fixture("signed-in"), [("GET", "/2026-01-02-morning-raw.html")]
    )
    assert status == 200
    assert "Pending Two" in html
    assert "&lt;b&gt;Six&lt;/b&gt;" in html
    assert html.count('class="vote-group"') == 6


def test_the_raw_probe_fixture_refuses_an_unknown_kind():
    with pytest.raises(ValueError):
        raw_probe.build_fixture("bogus")


def test_every_raw_probe_check_is_named_once_and_can_be_sabotaged():
    unsabotaged = cdp_probe.Check(id="a", fixture="signed-in", path="/p", script="", sabotage="")
    assert cdp_probe.registry_problems([unsabotaged], raw_probe.KINDS) == ["a: no sabotage"]
    assert cdp_probe.registry_problems(raw_probe.CHECKS, raw_probe.KINDS) == []
    assert {check.id for check in raw_probe.CHECKS} >= EXPECTED_RAW_IDS


# The checks `scripts/digest_probe.py` must carry. Each digest-page change adds the
# ids of the checks that hold it.
EXPECTED_DIGEST_IDS = {
    *(f"votes-one-row-{width}" for width in (360, 880, 1000, 1440)),
    *(f"votes-beside-meta-{width}" for width in (360, 880, 1000, 1440)),
    *(
        f"vote-click-{kind}-{width}"
        for kind in (
            "lead-story",
            "featured-item",
            "news-cluster",
            "news-paragraph",
            "article-item",
            "headline-item",
        )
        for width in (360, 1440)
    ),
    "vote-failure-says-why",
    "vote-failure-fits-360-signed-in",
    "vote-failure-fits-360-signed-in-largest",
    *(f"fits-{width}" for width in (360, 880, 1000, 1440)),
    *(f"head-fits-{width}" for width in (721, 740, 760, 800, 880, 1000, 1440)),
    "head-fits-largest-721",
    "title-whole-360",
    *(f"fits-largest-{width}" for width in (360, 880, 1000, 1440)),
    *(
        f"tap-{name}-375"
        for name in (
            "votes",
            "votes-apart",
            "original",
            "original-clear",
            "site-bar",
            "archive",
        )
    ),
    *(
        f"tap-smallest-{name}-375"
        for name in ("votes", "votes-apart", "original", "original-clear")
    ),
}
# The Top story as a group: its members' ↗ and votes, and the page width around them.
EXPECTED_DIGEST_IDS |= {
    *(f"grouped-lead-fits-{width}" for width in (360, 880, 1000, 1440)),
    *(f"grouped-lead-fits-largest-{width}" for width in (360, 880, 1000, 1440)),
    *(
        f"grouped-lead-tap-{name}-375"
        for name in ("votes", "votes-apart", "original", "original-clear", "site-bar")
    ),
    *(
        f"grouped-lead-tap-smallest-{name}-375"
        for name in ("votes", "votes-apart", "original", "original-clear")
    ),
}

# The Wire's summaries: drawn below each title, and wrapping inside the row on a phone.
EXPECTED_DIGEST_IDS |= {
    "wire-summary-below-title-375",
    "wire-summary-below-title-1440",
    "wire-fits-375",
    "wire-fits-largest-375",
    "wire-fits-smallest-375",
}

_VOID_TAGS = {"meta", "link", "br", "img", "input", "hr"}


class _Placement(HTMLParser):
    """For each element the digest probe looks for, the classes of the elements around it."""

    def __init__(self) -> None:
        super().__init__()
        self._open: list[set[str]] = []
        self.found: list[tuple[str, set[str]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = set((dict(attrs).get("class") or "").split())
        around = set().union(*self._open)
        if "vote-group" in classes:
            self.found.append(("vote-group", around))
        if tag == "details" and "src-fold" in classes:
            self.found.append(("src-fold", around))
        if tag not in _VOID_TAGS:
            self._open.append(classes)

    def handle_endtag(self, tag: str) -> None:
        if tag not in _VOID_TAGS:
            self._open.pop()


def _placed_in(html: str, what: str) -> set[str]:
    parser = _Placement()
    parser.feed(html)
    return set().union(*(around for name, around in parser.found if name == what))


def test_the_digest_probe_page_shows_a_vote_group_in_every_item_kind():
    items = {selector.lstrip(".") for selector in digest_probe.ITEM_SELECTORS}
    assert len(items) == 6
    assert items <= _placed_in(digest_probe.render_page(), "vote-group")
    # KINDS names the fixtures; the page-side list of item selectors is another name.
    assert "KINDS" not in digest_probe.DIGEST_PRELUDE


def test_the_digest_probe_grouped_page_leads_with_two_articles_each_with_its_link():
    page = digest_probe.render_page(grouped_lead=True)
    lead = page[
        page.index('class="lead-story"') : page.index(
            "</section>", page.index('class="lead-story"')
        )
    ]
    assert lead.count('class="article-item"') == 2
    assert lead.count('class="orig"') == 2
    single = digest_probe.render_page()
    assert 'class="article-item"' not in single[: single.index('class="lead-story"')]


def test_the_digest_probe_wire_has_a_long_summary_a_short_one_and_none():
    summaries = [item.summary for item in digest_probe.content().filtered_headlines]
    assert [bool(summary) for summary in summaries] == digest_probe.WIRE_SUMMARIES
    assert digest_probe.WIRE_SUMMARIES == [True, True, False]
    assert len(summaries[0]) > 3 * len(summaries[1])
    assert summaries[0] in digest_probe.render_page()


def test_the_digest_probe_page_folds_a_cluster_and_a_headline():
    assert {"news-cluster", "headline-item"} <= _placed_in(digest_probe.render_page(), "src-fold")


async def test_the_digest_probe_fixture_serves_an_archive_with_small_buttons():
    [(status, html)] = await _raw_answers(
        digest_probe.build_fixture("signed-in"), [("GET", digest_probe.ARCHIVE)]
    )
    assert status == 200
    assert html.count('class="btn secondary sm') == 4
    assert html.count('class="btn secondary sm digest-link"') == 2


@pytest.mark.parametrize("kind", digest_probe.KINDS)
async def test_the_digest_probe_fixture_signs_every_kind_in(kind):
    [(status, body)] = await _raw_answers(digest_probe.build_fixture(kind), [("GET", "/api/vote")])
    assert status == 200
    assert json.loads(body) == {"authorized": True}


async def test_the_digest_probe_fixture_keeps_a_refused_vote():
    fixture = digest_probe.build_fixture("vote-fails")
    [(status, _)] = await _raw_answers(fixture, [("POST", "/api/vote")])
    assert status == 502
    assert fixture.posts == [PENDING_TWO_UP]


def test_the_digest_probe_clicks_a_cluster_that_votes_on_three_urls():
    assert len(digest_probe.first_vote_urls()[".news-cluster"]) == 3


def test_the_digest_probe_fixture_refuses_an_unknown_kind():
    with pytest.raises(ValueError):
        digest_probe.build_fixture("bogus")


def test_the_digest_probe_runs_on_the_shared_core():
    assert digest_probe.Check is cdp_probe.Check


@pytest.mark.parametrize("probe", [raw_probe, digest_probe], ids=["raw", "digest"])
def test_a_vote_probe_takes_its_scaffolding_from_the_core(probe):
    source = Path(probe.__file__).read_text()
    for copied in ("class Fixture", "def registry_problems", 'Authorization: "x"'):
        assert copied not in source, copied
    assert isinstance(probe.build_fixture("signed-in"), cdp_probe.VoteFixture)


def test_every_digest_probe_check_is_named_once_and_can_be_sabotaged():
    unsabotaged = cdp_probe.Check(id="a", fixture="signed-in", path="/p", script="", sabotage="")
    assert cdp_probe.registry_problems([unsabotaged], digest_probe.KINDS) == ["a: no sabotage"]
    assert cdp_probe.registry_problems(digest_probe.CHECKS, digest_probe.KINDS) == []
    assert {check.id for check in digest_probe.CHECKS} >= EXPECTED_DIGEST_IDS


# The checks `scripts/labels_probe.py` must carry: the card is blind, each answer is
# sent as given, and the page fits a phone.
EXPECTED_LABELS_IDS = {
    "card-shows-the-first-item",
    "card-shows-no-verdict",
    *(f"{label}-sends-its-answer-and-deals-the-next" for label in ("up", "down", "skip")),
    "swipe-up-answers-up",
    "swipe-down-answers-down",
    "refused-answer-says-why",
    "refused-answer-says-sign-in-again",
    "fits-375",
    "fits-largest-375",
    "tap-answers-375",
}


def test_every_labels_probe_check_is_named_once_and_can_be_sabotaged():
    assert cdp_probe.registry_problems(labels_probe.CHECKS, labels_probe.KINDS) == []
    assert {check.id for check in labels_probe.CHECKS} >= EXPECTED_LABELS_IDS


def test_the_labels_probe_runs_on_the_shared_core():
    assert labels_probe.Check is cdp_probe.Check


# The checks `scripts/onboarding_probe.py` must carry: the frame walks, each
# settings step writes what it was given, the language and timezone are selects
# that keep a stored value neither lists and open on the browser's zone when none
# is stored, the sources step shows its topics as toggle tiles on one screen and
# only the chosen topics' feeds, each once, on the next, with Next, Back and Skip
# moving between them, counts the picks and warns past twelve, keeps a pick whose
# topic is dropped and the topics over a reload, and stores each pick as listed
# with no `topics` in its body, the votes step deals its cards, names a feed
# with none, shows the wait and writes only the marked cards, the first-issue
# step turns vote similarity on with no vote and starts its run though that is
# refused, starts one run for the key it picked, today's
# or yesterday's evening, shows it while it is out, says where its issue is or
# why it failed, and starts none when skipped, a skip writes nothing but that switch, only the
# right deployment sees the wizard and its list, and the page fits a phone.
EXPECTED_ONBOARDING_IDS = {
    "opens-on-the-first-step",
    "the-step-list-names-every-step",
    "next-walks-to-the-next-step",
    "back-returns-to-the-previous-step",
    "skip-walks-to-the-last-step",
    "skip-writes-nothing",
    "an-untouched-step-writes-nothing",
    "language-step-saves-both-values",
    "language-step-sends-only-what-changed",
    "hours-step-saves-the-schedule",
    "hours-step-refuses-an-empty-hour",
    "a-refused-value-is-marked-in-its-own-field",
    "language-step-offers-the-listed-languages-by-name",
    "language-step-keeps-an-unlisted-stored-value",
    "language-step-leaves-an-unlisted-value-it-did-not-change",
    "timezone-lists-the-browsers-zones-and-utc",
    "timezone-opens-on-the-stored-zone-not-the-browsers",
    "timezone-with-none-stored-opens-on-the-browsers-marked-detected",
    "timezone-detected-is-saved-by-next",
    "timezone-detected-is-not-saved-by-skip",
    "notify-step-saves-the-webhook",
    "notify-step-saves-the-mail-pair",
    "notify-step-refuses-a-sender-without-a-recipient",
    "notify-step-left-empty-writes-nothing",
    "finish-leaves-for-settings",
    "an-empty-deployment-is-sent-from-settings-to-the-wizard",
    "sources-step-shows-no-feed-before-a-topic",
    "sources-step-one-topic-lists-its-feeds",
    "sources-step-lists-feeds-when-storage-refuses-the-topics",
    "sources-step-two-topics-list-a-shared-feed-once",
    "sources-step-keeps-a-pick-when-its-topic-is-dropped",
    "sources-step-a-reload-keeps-the-topics",
    "sources-step-posts-no-topics",
    "sources-step-topics-are-toggle-buttons",
    "sources-step-a-pressed-topic-shows-it",
    "sources-step-a-topic-answers-the-keyboard",
    "sources-step-next-opens-the-feeds-screen",
    "sources-step-back-returns-to-the-topics",
    "sources-step-skip-on-the-topics-screen-leaves-the-step",
    "sources-step-counts-the-picked-feeds",
    "sources-step-warns-past-twelve-picks",
    "sources-step-twelve-picks-carry-no-warning",
    "fits-topics-375",
    "fits-largest-topics-375",
    "sources-step-lists-every-entry",
    "sources-step-stores-the-picked-entries",
    "sources-step-stores-the-listed-lane",
    "sources-step-skipped-writes-no-source",
    "sources-step-with-nothing-picked-writes-nothing",
    "sources-step-names-a-feed-it-could-not-add",
    "sources-step-retries-only-what-it-could-not-add",
    "votes-step-deals-the-drawn-cards",
    "votes-step-names-a-feed-with-no-article",
    "votes-step-shows-the-wait",
    "votes-step-deals-the-feeds-just-added",
    "votes-step-records-the-marked-cards",
    "votes-step-keeps-the-votes-when-one-article-is-gone",
    "votes-step-skipped-writes-nothing",
    "votes-step-with-no-vote-writes-nothing",
    "votes-step-a-second-press-takes-the-vote-back",
    "votes-step-with-no-feed-says-so",
    "has-sources-opens-the-wizard-without-the-list",
    "has-sources-keeps-the-step-a-reload-opens",
    "json-has-no-wizard",
    "fits-375",
    "fits-largest-375",
    "fits-sources-375",
    "fits-largest-sources-375",
    "fits-votes-375",
    "fits-largest-votes-375",
    "tap-votes-375",
    "tap-feeds-375",
    "tap-topics-375",
    "tap-steps-375",
    "first-issue-starts-a-run-no-later-tick-replaces",
    "first-issue-says-where-to-read-it",
    "first-issue-shows-the-run-while-it-is-out",
    "first-issue-says-why-its-run-failed",
    "first-issue-tries-again-after-a-failure",
    "first-issue-skipped-starts-no-run",
    "first-issue-turns-on-vote-similarity-with-no-vote",
    "first-issue-starts-though-vote-similarity-is-refused",
    "first-issue-before-the-morning-takes-yesterdays-evening",
    "first-issue-with-no-source-says-so",
    "fits-first-issue-375",
    "own-feed-joins-your-feeds-checked",
    "own-feed-needs-no-topic",
    "own-feed-is-posted-by-next",
    "own-feed-pressed-next-during-the-check-is-posted",
    "own-feed-refused-says-why-and-adds-nothing",
    "own-feed-shows-the-check-while-it-is-out",
    "own-feed-already-listed-checks-its-row",
    "fits-own-feed-375",
    "tap-own-feed-375",
}


def test_every_onboarding_probe_check_is_named_once_and_can_be_sabotaged():
    assert cdp_probe.registry_problems(onboarding_probe.CHECKS, onboarding_probe.KINDS) == []
    assert {check.id for check in onboarding_probe.CHECKS} >= EXPECTED_ONBOARDING_IDS


def test_the_onboarding_probe_runs_on_the_shared_core():
    assert onboarding_probe.Check is cdp_probe.Check


def test_the_onboarding_probe_fixture_refuses_an_unknown_kind():
    with pytest.raises(ValueError):
        onboarding_probe.build_fixture("bogus")


def test_the_onboarding_probe_sets_every_runtime_setting():
    """No step may meet a missing-value marker it did not ask for."""
    assert set(onboarding_probe.PROBE_VALUES) == set(GRADE_D_KEYS)
    assert onboarding_probe.PROBE_VALUES["notify.discord_webhook_url"] == ""


async def test_the_onboarding_probe_serves_the_real_wizard_and_its_script():
    fixture = onboarding_probe.build_fixture("empty")
    client = TestClient(TestServer(fixture.app))
    await client.start_server()
    try:
        page = await (await client.get(onboarding_probe.PAGE)).text()
        script = await client.get("/static/onboarding.js")
        probe = await (await client.get("/api/vote")).json()
    finally:
        await client.close()
    assert 'id="o-steps"' in page
    assert script.status == 200
    # The app Worker's route, which the site bar's links wait on.
    assert probe == {"authorized": True}


@pytest.mark.parametrize(
    ("kind", "path", "location"),
    [
        ("empty", "/settings", "/onboarding"),
        ("json", "/onboarding", "/settings"),
    ],
)
async def test_the_onboarding_probe_fixtures_redirect_as_the_container_does(
    kind: str, path: str, location: str
):
    fixture = onboarding_probe.build_fixture(kind)
    client = TestClient(TestServer(fixture.app))
    await client.start_server()
    try:
        response = await client.get(path, allow_redirects=False)
    finally:
        await client.close()
    assert (response.status, response.headers["Location"]) == (302, location)


@pytest.mark.parametrize("kind", ["empty", "has-sources"])
async def test_the_onboarding_probe_fixtures_with_a_table_serve_the_wizard(kind: str):
    fixture = onboarding_probe.build_fixture(kind)
    client = TestClient(TestServer(fixture.app))
    await client.start_server()
    try:
        response = await client.get(onboarding_probe.PAGE, allow_redirects=False)
        feeds = await (await client.get("/api/onboarding/feeds")).json()
    finally:
        await client.close()
    assert response.status == 200
    assert len(feeds["feeds"]) == (len(onboarding_probe.FEEDS) if kind == "empty" else 0)


async def test_the_onboarding_probe_fixture_records_each_source_it_stored():
    fixture = onboarding_probe.build_fixture("empty")
    status, _ = await _get_json(fixture, "/api/sources", "POST", onboarding_probe.PICKS[0])
    assert status == 200
    assert onboarding_probe._sources(onboarding_probe.PICKS[0])(fixture) is None
    assert onboarding_probe._sources()(fixture) is not None


async def test_the_onboarding_probe_fixture_records_each_body_as_posted():
    """The bodies are read before the route drops `topics`, so one sent with them shows."""
    fixture = onboarding_probe.build_fixture("empty")
    pick = onboarding_probe.PICKS[0]
    status, _ = await _get_json(fixture, "/api/sources", "POST", pick)
    assert status == 200
    assert fixture.posted == [pick]
    assert onboarding_probe._posted(pick)(fixture) is not None
    assert onboarding_probe._sources(pick)(fixture) is None


def test_the_onboarding_probe_topic_fixtures_hold_what_their_checks_need():
    """Each topic check's fixture holds: two topics differ, two share a feed, a pick has one."""
    probe = onboarding_probe
    one = probe.shown_feeds([probe.ONE_TOPIC])
    assert set(probe.shown_feeds([probe.OTHER_TOPIC])) - set(one)
    both = probe.shown_feeds(probe.TWO_TOPICS)
    assert both.count(probe.SHARED) == 1
    assert len(both) == len(set(both))
    assert len(probe.KEPT_PICK["topics"]) == 1
    every = probe.shown_feeds([topic["id"] for topic in probe.TOPICS])
    assert sorted(every) == sorted(entry["name"] for entry in probe.FEEDS)
    assert all("topics" not in probe.as_posted(entry) for entry in probe.FEEDS)
    script = (Path(cyris.entrypoints.__file__).parent / "static" / "onboarding.js").read_text()
    assert f"const KEPT = {json.dumps(probe.KEPT)};" in script
    assert f"const TOPICS_KEY = {json.dumps(probe.TOPICS_KEY)};" in script


async def test_the_onboarding_probe_refusing_table_turns_its_feed_down_once():
    fixture = onboarding_probe.build_fixture("refuses-one")
    refused = onboarding_probe.REFUSED
    first, body = await _get_json(fixture, "/api/sources", "POST", refused)
    second, _ = await _get_json(fixture, "/api/sources", "POST", refused)
    assert (first, body["error"], second) == (500, onboarding_probe.REFUSAL, 200)
    assert onboarding_probe._sources(refused)(fixture) is None


def test_the_onboarding_probe_picks_carry_more_than_one_tag():
    """A body that lost a tag, or reordered two, must differ from the entry it was sent for."""
    assert len(onboarding_probe.PICKS) == 2
    assert all(entry["tags"] for entry in onboarding_probe.PICKS)
    assert any(len(entry["tags"]) > 1 for entry in onboarding_probe.PICKS)
    assert onboarding_probe.REFUSED != onboarding_probe.TAKEN


async def test_the_onboarding_probe_fixture_records_what_a_step_wrote():
    fixture = onboarding_probe.build_fixture("empty")
    status, _ = await _get_json(
        fixture, "/api/settings/schedule", "POST", {"times": ["07:00", "19:00"]}
    )
    assert status == 200
    assert fixture.calls == [{"general.digest_schedule": ["07:00", "19:00"]}]


class _Body:
    """A request as an onboarding handler reads it: the JSON body alone."""

    def __init__(self, body: dict) -> None:
        self._body = body

    async def json(self) -> dict:
        return self._body


def _onboarding_server(fixture):
    """The `TriageServer` an onboarding fixture's app was built from, for its handlers."""
    [route] = [
        r for r in fixture.app.router.routes() if r.resource.canonical == "/api/onboarding/cards"
    ]
    return route.handler.__self__


async def test_the_onboarding_probe_own_feed_fixture_answers_each_address_offline():
    """The home page leads to `OWN_NAME`, the bare page is refused, a listed feed is named."""
    probe = onboarding_probe
    server = _onboarding_server(probe.build_fixture("empty"))
    taken = [{"name": e["name"], "url": e["url"]} for e in probe.FEEDS]

    async def check(url: str) -> tuple[int, dict]:
        response = await server._handle_post_feed_check(_Body({"url": url, "taken": taken}))
        return response.status, json.loads(response.text)

    assert await check(probe.OWN_HOME) == (
        200,
        {"ok": True, "name": probe.OWN_NAME, "url": probe.OWN_FEED},
    )
    status, body = await check(probe.BARE_HOME)
    assert (status, body["error"]) == (422, probe.NO_FEED_LINK)
    status, body = await check(probe.LISTED["url"])
    assert (status, body["name"]) == (409, probe.LISTED["name"])
    assert probe.ONE_TOPIC not in probe.LISTED["topics"]
    assert probe.OWN_NAME not in {e["name"] for e in probe.FEEDS}


async def test_the_onboarding_probe_cards_fixture_deals_its_cards_and_names_the_quiet_feed():
    server = _onboarding_server(onboarding_probe.build_fixture("cards"))

    body = json.loads((await server._handle_post_onboarding_cards(_Body({}))).text)

    assert [card["url"] for card in body["cards"]] == [a.url for a in onboarding_probe.CARDS]
    assert body["empty"] == [onboarding_probe.QUIET["name"]]
    assert len(onboarding_probe.CARDS) >= 3


async def test_the_onboarding_probe_votes_receipt_reads_what_the_votes_wrote():
    fixture = onboarding_probe.build_fixture("cards")
    server = _onboarding_server(fixture)
    cards = onboarding_probe.CARDS
    votes = [{"url": cards[0].url, "vote": "up"}, {"url": cards[1].url, "vote": "down"}]
    assert onboarding_probe._NOTHING_VOTED(fixture) is None

    response = await server._handle_post_onboarding_votes(_Body({"votes": votes}))

    assert response.status == 200
    assert onboarding_probe._votes(up=(0,), down=(1,))(fixture) is None
    assert onboarding_probe._votes(up=(1,), down=(0,))(fixture) is not None
    assert onboarding_probe._NOTHING_VOTED(fixture) is not None


def test_the_onboarding_probe_slow_fixture_outlasts_a_look_at_the_wait():
    assert onboarding_probe.SLOW_SECONDS >= 2


class _Query:
    """A request as the probe's `POST /run` stand-in reads it: the query alone."""

    def __init__(self, period: str, date: str | None = None) -> None:
        self.query = {"period": period} | ({"date": date} if date else {})


def _start_run(fixture):
    [route] = [r for r in fixture.app.router.routes() if r.resource.canonical == "/run"]
    return route.handler


async def _first_issue(fixture) -> dict:
    server = _onboarding_server(fixture)
    return json.loads((await server._handle_get_first_issue(None)).text)


@pytest.mark.parametrize(
    ("kind", "run"),
    [
        ("first-issue", onboarding_probe.ISSUE_RUN),
        ("first-issue-early", onboarding_probe.EARLY_RUN),
    ],
)
async def test_the_onboarding_probe_first_issue_clock_picks_the_key_the_checks_expect(
    kind: str, run: tuple[str, str]
):
    body = await _first_issue(onboarding_probe.build_fixture(kind))

    assert (body["period"], body["date"]) == run
    assert body["today"] == onboarding_probe.ISSUE_DATE
    assert (body["sources"], body["published"]) == (1, False)
    assert body["hours"][0] == "08:00"


async def test_the_onboarding_probe_run_records_its_period_and_ends_as_listed():
    fixture = onboarding_probe.build_fixture("first-issue-fails")
    start = _start_run(fixture)
    before = (await _first_issue(fixture))["run"]["id"]

    await start(_Query("morning", "2026-10-20"))
    failed = (await _first_issue(fixture))["run"]
    await start(_Query("morning", "2026-10-20"))
    published = (await _first_issue(fixture))["run"]

    assert fixture.runs == [onboarding_probe.ISSUE_RUN, onboarding_probe.ISSUE_RUN]
    assert (failed["id"], failed["status"], failed["error"]) == (
        before + 1,
        "error",
        onboarding_probe.FAILURE,
    )
    assert (published["status"], published["digest_url"]) == ("ok", onboarding_probe.DIGEST_URL)
    run = onboarding_probe.ISSUE_RUN
    assert onboarding_probe._runs(run, run)(fixture) is None
    assert onboarding_probe._runs(run)(fixture) is not None


async def test_the_onboarding_probe_slow_run_never_records_a_row():
    fixture = onboarding_probe.build_fixture("first-issue-slow")
    before = (await _first_issue(fixture))["run"]

    await _start_run(fixture)(_Query("morning", "2026-10-20"))

    assert (await _first_issue(fixture))["run"] == before
    assert fixture.runs == [onboarding_probe.ISSUE_RUN]


@pytest.mark.parametrize(
    ("period", "date"), [("noon", None), ("evening", "2026-10-18"), ("evening", "2026-10-21")]
)
async def test_the_onboarding_probe_run_refuses_what_the_worker_refuses(period, date):
    fixture = onboarding_probe.build_fixture("first-issue")

    response = await _start_run(fixture)(_Query(period, date))

    assert response.status == 400
    assert fixture.runs == []


async def test_the_onboarding_probe_early_run_is_yesterdays_evening_and_publishes_it():
    fixture = onboarding_probe.build_fixture("first-issue-early")

    await _start_run(fixture)(_Query("evening", "2026-10-19"))
    run = (await _first_issue(fixture))["run"]

    assert fixture.runs == [onboarding_probe.EARLY_RUN]
    assert (run["status"], run["digest_url"]) == ("ok", "https://cyris.example/2026-10-19-evening")


def test_the_onboarding_probe_waits_longer_than_the_page_reads():
    js = (Path(__file__).parents[1] / "src/cyris/entrypoints/static/onboarding.js").read_text()
    poll = int(re.search(r"const POLL_MS = (\d+);", js)[1])

    assert 2 * poll <= onboarding_probe.RUN_READ_MS


def test_the_labels_probe_fixture_refuses_an_unknown_kind():
    with pytest.raises(ValueError):
        labels_probe.build_fixture("bogus")


async def test_the_labels_probe_serves_the_real_page_and_script_and_records_answers():
    fixture = labels_probe.build_fixture("sample")
    client = TestClient(TestServer(fixture.app))
    await client.start_server()
    try:
        page = await (await client.get(labels_probe.PAGE)).text()
        script = await client.get("/static/labels.js")
        first = await (await client.get("/api/labels")).json()
        after = await (await client.post("/api/labels", json={"url": "u", "label": "up"})).json()
    finally:
        await client.close()
    assert 'id="l-card"' in page
    assert script.status == 200
    assert first["item"]["title"] == "Title One"
    assert after["item"]["title"] == "Title Two"
    assert fixture.posts == [{"url": "u", "label": "up"}]
