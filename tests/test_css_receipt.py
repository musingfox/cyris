"""The CSS receipt tooling is the digest refactor's safety argument, so it is tested.

`scripts/css_computed.py` is not exercised end to end here: it needs Chromium
and a free debug port, which would make it a flaky non-hermetic gate rather than
a test. Its verification is a hand-run receipt. What is tested is the part that
needs no browser -- the probe set it ships and the breakpoints it samples.
"""

import io
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from css_rules import parse_style_block, receipt_fixtures

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

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
    assert WIDTHS == (1440, 880, 720, 640, 375)


def test_every_probe_names_a_page_the_receipt_renders_and_at_least_one_property():
    probes = load_probes(PROBES_PATH)
    assert set(probes) == {"index", "digest", "raw"}
    for page, selectors in probes.items():
        assert selectors, page
        for selector, properties in selectors.items():
            assert properties, f"{page} | {selector}"


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
