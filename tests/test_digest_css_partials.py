"""The shared CSS partials stay the one source of truth for the three digest pages.

These assertions are the milestone's invariant, not a snapshot of today's CSS: a
checked-in golden file would need regenerating on every legitimate edit and would
decay into a rubber stamp. Nothing here starts a browser, opens a socket, or
touches the filesystem, so it can run as an ordinary unit test.
"""

import re
from collections import Counter
from pathlib import Path

import pytest
from css_rules import (
    COMPONENT_SELECTORS,
    CSS_PARTIALS,
    UI_SPEC,
    include_sites,
    mirror_diff,
    parse_style_block,
    receipt_fixtures,
    root_declarations,
    rule_occurrences,
    spec_token_fence,
    split_selector_list,
)
from jinja2 import DebugUndefined, Environment, meta

import cyris.entrypoints
from cyris.adapters.output.html_digest import HtmlDigestWriter

pytestmark = pytest.mark.unit

PAGE_TEMPLATES = {
    "index": "index.html.j2",
    "digest": "digest.html.j2",
    "raw": "raw.html.j2",
}

# The partials each page must draw its shared rules from. Declared here rather
# than read back out of the templates: derived expectations would silently accept
# a page that stopped including a partial and inlined the rules again.
EXPECTED_INCLUDES = {
    # Archive and raw open with §4's `.page-head` of components, so no masthead.
    "index": ("_tokens.css.j2", "_components.css.j2", "_page.css.j2", "_footer.css.j2"),
    "digest": CSS_PARTIALS,
    "raw": tuple(partial for partial in CSS_PARTIALS if partial != "_masthead.css.j2"),
}

STYLE_CSS = Path(cyris.entrypoints.__file__).parent / "static" / "style.css"

INCLUDE_CASES = [
    (page, partial) for page, partials in EXPECTED_INCLUDES.items() for partial in partials
]


@pytest.fixture(scope="module")
def env() -> Environment:
    """The renderer's own Jinja environment, autoescape and filters included."""
    return HtmlDigestWriter("unused-by-these-tests").env


@pytest.fixture(scope="module")
def pages() -> dict[str, str]:
    index_html, digest_html, raw_html = receipt_fixtures()
    return {"index": index_html, "digest": digest_html, "raw": raw_html}


def _style_text(html: str) -> str:
    return "\n".join(
        block for block in html.split("<style>")[1:] for block in [block.split("</style>")[0]]
    )


@pytest.fixture(scope="module")
def spec_tokens() -> list[str]:
    return root_declarations(spec_token_fence(UI_SPEC.read_text()))


def test_the_spec_token_fence_is_the_full_token_set(spec_tokens: list[str]) -> None:
    assert len(spec_tokens) == 34
    assert spec_tokens[0] == "--bg: #07070a"
    assert spec_tokens[-1] == "--measure: 640px"
    assert "--type-scale: 1" in spec_tokens


@pytest.mark.parametrize("page", sorted(PAGE_TEMPLATES))
def test_every_page_declares_the_spec_tokens_in_spec_order(
    pages: dict[str, str], spec_tokens: list[str], page: str
) -> None:
    assert root_declarations(pages[page]) == spec_tokens


def test_the_static_stylesheet_declares_the_spec_tokens_in_spec_order(
    spec_tokens: list[str],
) -> None:
    assert root_declarations(STYLE_CSS.read_text()) == spec_tokens


def test_a_changed_spec_value_no_longer_matches_the_page(pages: dict[str, str]) -> None:
    fence = spec_token_fence(UI_SPEC.read_text())
    assert "--s-1: 4px" in fence
    changed = fence.replace("--s-1: 4px", "--s-1: 5px")
    assert root_declarations(changed) != root_declarations(pages["digest"])


def test_a_reordered_spec_no_longer_matches_the_page(pages: dict[str, str]) -> None:
    lines = spec_token_fence(UI_SPEC.read_text()).splitlines()
    bg = next(i for i, line in enumerate(lines) if line.strip().startswith("--bg:"))
    elev = next(i for i, line in enumerate(lines) if line.strip().startswith("--bg-elev:"))
    lines[bg], lines[elev] = lines[elev], lines[bg]
    assert root_declarations("\n".join(lines)) != root_declarations(pages["digest"])


@pytest.mark.parametrize(
    ("page", "partial"), INCLUDE_CASES, ids=[f"{p}-{c}" for p, c in INCLUDE_CASES]
)
def test_partial_reaches_the_page_verbatim(
    env: Environment, pages: dict[str, str], page: str, partial: str
) -> None:
    template_name = PAGE_TEMPLATES[page]
    sites = include_sites(env, template_name)
    assert partial in sites, f"{template_name} no longer includes {partial}"

    parameters = sites[partial]
    partial_source = env.loader.get_source(env, partial)[0]
    read_by_partial = meta.find_undeclared_variables(env.parse(partial_source))
    unknown = sorted(set(parameters) - read_by_partial)
    # The environment has no StrictUndefined, so a misspelled parameter renders as
    # an empty value on both sides of the comparison below instead of failing.
    assert not unknown, f"{template_name} passes {unknown} to {partial}, which never reads them"

    # The complement: a parameter the partial reads but the page never passes
    # renders as an empty value the browser falls back on, and it would do so on
    # both sides of the comparison below. DebugUndefined leaves the placeholder
    # in the output instead, wherever in a value it sits, while still testing
    # false so an `{% if %}`-guarded parameter stays optional.
    marked = env.overlay(undefined=DebugUndefined).get_template(partial).render(**parameters)
    omitted = sorted(set(re.findall(r"\{\{ ?(\w+) ?\}\}", marked)))
    assert not omitted, f"{template_name} omits {omitted}, which {partial} reads"

    rendered = env.get_template(partial).render(**parameters)
    assert rendered in _style_text(pages[page]), (
        f"{partial} rendered with {template_name}'s parameters is not in {page}'s <style>"
    )


@pytest.mark.parametrize("page", sorted(PAGE_TEMPLATES))
def test_the_components_partial_takes_no_parameters(env: Environment, page: str) -> None:
    assert include_sites(env, PAGE_TEMPLATES[page])["_components.css.j2"] == {}


def test_the_omission_check_sees_a_dropped_parameter_and_not_a_guarded_one(
    env: Environment,
) -> None:
    parameters = include_sites(env, PAGE_TEMPLATES["digest"])["_page.css.j2"]
    assert "line_height" in parameters and "glow" in parameters
    strict = env.overlay(undefined=DebugUndefined)

    without_required = {k: v for k, v in parameters.items() if k != "line_height"}
    assert "{{ line_height }}" in strict.get_template("_page.css.j2").render(**without_required)

    without_guarded = {k: v for k, v in parameters.items() if k != "glow"}
    assert "{{" not in strict.get_template("_page.css.j2").render(**without_guarded)


def test_vote_buttons_are_styled_on_the_pages_that_carry_them(pages: dict[str, str]) -> None:
    for page in ("digest", "raw"):
        assert ".promote-btn.done" in parse_style_block(pages[page]), f"{page} lost .promote-btn"
    index_rules = [rule for rule in parse_style_block(pages["index"]) if "promote-btn" in rule]
    assert not index_rules, f"index styles vote buttons it never renders: {index_rules}"


@pytest.mark.parametrize("page", sorted(PAGE_TEMPLATES))
def test_body_background_shorthand_precedes_its_longhand(pages: dict[str, str], page: str) -> None:
    # `background` resets `background-image`. Emitted the other way round, the
    # radial glow and the 32px grid disappear and a set-based rule comparison
    # cannot see it.
    properties = [
        declaration.split(":", 1)[0].strip()
        for declaration in parse_style_block(pages[page])["body"]
    ]
    assert properties.index("background") < properties.index("background-image"), (
        f"{page}'s body emits background-image before the background shorthand"
    )


@pytest.fixture(scope="module")
def shared_partials(env: Environment) -> str:
    return "".join(
        env.get_template(name).render() for name in ("_tokens.css.j2", "_components.css.j2")
    )


def test_the_static_stylesheet_is_the_shared_partials(shared_partials: str) -> None:
    assert mirror_diff(STYLE_CSS.read_text(), shared_partials) == []


def test_a_restyled_component_in_the_static_stylesheet_is_named(shared_partials: str) -> None:
    style = STYLE_CSS.read_text()
    assert style.count("padding: 2px 10px") == 1
    changed = style.replace("padding: 2px 10px", "padding: 2px 8px")
    assert mirror_diff(changed, shared_partials) == [".pill"]


def test_a_rule_only_the_static_stylesheet_has_is_named(shared_partials: str) -> None:
    extra = STYLE_CSS.read_text() + "\n.card { color: red; }\n"
    assert mirror_diff(extra, shared_partials) == [".card"]


def test_a_rule_only_the_partials_have_is_named(shared_partials: str) -> None:
    style = STYLE_CSS.read_text()
    start = style.index("@keyframes pulse")
    end = style.index("}\n}", start) + 3
    missing = mirror_diff(style[:start] + style[end:], shared_partials)
    assert missing and all(key.startswith("@keyframes pulse | ") for key in missing)


def _component_definition_problems(
    source: str, selectors: frozenset[str] = COMPONENT_SELECTORS
) -> list[str]:
    """Name each component selector a source defines other than as often as listed.

    A key may carry an at-rule prefix (``@media (...) | .x``); each key naming a
    bare selector is one block the source is expected to hold, so a listed media
    copy is expected and any other copy pushes the count past it.
    """
    counts = rule_occurrences(source)
    expected = Counter(
        part for key in selectors for part in split_selector_list(key.rsplit(" | ", 1)[-1])
    )
    return sorted(
        f"{selector} x{counts[selector]}"
        for selector, times in expected.items()
        if counts[selector] != times
    )


@pytest.mark.parametrize("page", sorted(PAGE_TEMPLATES))
def test_every_page_defines_each_component_once(pages: dict[str, str], page: str) -> None:
    assert _component_definition_problems(pages[page]) == []


def test_the_static_stylesheet_defines_each_component_once() -> None:
    assert _component_definition_problems(STYLE_CSS.read_text()) == []


def test_a_second_block_for_a_component_is_reported() -> None:
    page = "<style>.pill{a:1} .pill{b:2}</style>"
    assert rule_occurrences(page)[".pill"] == 2
    assert ".pill x2" in _component_definition_problems(page)


def test_a_component_repeated_inside_a_selector_list_is_counted() -> None:
    assert rule_occurrences("<style>.pill, .x{a:1} .pill{b:2}</style>")[".pill"] == 2


def test_a_component_repeated_inside_a_media_query_is_counted() -> None:
    page = "<style>.pill{a:1} @media (max-width: 880px){.pill{b:2}}</style>"
    assert rule_occurrences(page)[".pill"] == 2
    assert ".pill x2" in _component_definition_problems(page)


LISTED_TWICE = frozenset({".x", "@media (max-width: 720px) | .x"})


def test_a_media_copy_the_component_list_names_is_expected() -> None:
    page = "<style>.x{a:1} @media (max-width: 720px){.x{b:2}}</style>"
    assert _component_definition_problems(page, LISTED_TWICE) == []


def test_a_missing_listed_media_copy_is_reported() -> None:
    assert _component_definition_problems("<style>.x{a:1}</style>", LISTED_TWICE) == [".x x1"]


def test_a_media_copy_the_component_list_does_not_name_is_reported() -> None:
    page = (
        "<style>.x{a:1} @media (max-width: 720px){.x{b:2}}"
        " @media (max-width: 880px){.x{c:3}}</style>"
    )
    assert _component_definition_problems(page, LISTED_TWICE) == [".x x3"]
