"""Receipts that the reader-facing pages follow docs/design/ui-language.md."""

import functools
import re
from collections.abc import AsyncIterator
from html.parser import HTMLParser
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from css_rules import (
    COMPONENT_SELECTORS,
    PROTOTYPE,
    UI_SPEC,
    box_shadows,
    canonical_colour,
    colour_literals,
    font_stack_literals,
    include_sites,
    off_spec_transitions,
    off_token_radii,
    parse_style_block,
    receipt_fixtures,
    rem_values,
    spacing_literals,
    spec_colour_exceptions,
    style_attributes,
    unscaled_font_sizes,
)

import cyris.entrypoints
from cyris.adapters.output.html_digest import HtmlDigestWriter
from cyris.entrypoints.triage_server import TriageServer, render_settings_page

STYLE = Path(cyris.entrypoints.__file__).parent / "static" / "style.css"


def _source(name: str) -> str:
    """A page or stylesheet the literal guards read; settings is the page as served."""
    if name == "style.css":
        return STYLE.read_text()
    if name == "settings":
        return render_settings_page()
    return dict(zip(("index", "digest", "raw"), receipt_fixtures(), strict=True))[name]


GUARDED = ["index", "digest", "raw", "style.css", "settings"]


def _render_partial(name: str) -> str:
    return HtmlDigestWriter("unused-by-these-tests").env.get_template(name).render()


FOCUS = {"outline: 1px solid var(--accent)", "outline-offset: 2px"}

REDUCED_MOTION = "@media (prefers-reduced-motion: reduce) | *, *::before, *::after"
STILL = {
    "transition: none !important",
    "animation: none !important",
    "scroll-behavior: auto !important",
}

# Section 4's interaction rules, which the components partial carries alongside
# the components themselves.
INTERACTION_KEYS = frozenset({":focus-visible", REDUCED_MOTION})


def _component_key_problems(css: str) -> list[str]:
    keys = set(parse_style_block(f"<style>{css}</style>"))
    expected = COMPONENT_SELECTORS | INTERACTION_KEYS
    return [f"extra {key}" for key in sorted(keys - expected)] + [
        f"missing {key}" for key in sorted(expected - keys)
    ]


def _focus_problems(html: str) -> list[str]:
    declarations = parse_style_block(html).get(":focus-visible")
    if declarations is None:
        return ["missing :focus-visible"]
    return [] if declarations == FOCUS else [f":focus-visible is {sorted(declarations)}"]


@pytest.fixture
async def triage() -> AsyncIterator[TestClient]:
    client = TestClient(TestServer(TriageServer()._app))
    await client.start_server()
    yield client
    await client.close()


async def _stylesheet_hrefs(client: TestClient, path: str) -> list[str]:
    response = await client.get(path)
    assert response.status == 200
    links = re.findall(r"<link\b[^>]*>", await response.text())
    hrefs = [
        match.group(1)
        for link in links
        if 'rel="stylesheet"' in link and (match := re.search(r'href="(/static/[^"]+)"', link))
    ]
    return hrefs


async def _served_rules(client: TestClient, href: str) -> dict[str, set[str] | list[str]]:
    response = await client.get(href)
    assert response.status == 200, f"{href} answered {response.status}"
    return parse_style_block(f"<style>{await response.text()}</style>")


async def test_settings_links_only_the_shared_stylesheet(triage: TestClient) -> None:
    assert await _stylesheet_hrefs(triage, "/settings") == ["/static/style.css"]


async def test_the_shared_stylesheet_carries_no_deck_rule(triage: TestClient) -> None:
    rules = await _served_rules(triage, "/static/style.css")
    deck_only = {".card", "#toast", "#undo-toast", ".nav-btn", "#filter-tabs .tab", ".hidden"}
    assert sorted(deck_only & set(rules)) == []


async def test_the_shared_body_rule_has_no_deck_layout(triage: TestClient) -> None:
    body = (await _served_rules(triage, "/static/style.css"))["body"]
    assert {"background: var(--bg)", "color: var(--text)"} <= set(body)
    properties = {declaration.split(":", 1)[0] for declaration in body}
    assert not properties & {"display", "overflow", "touch-action", "min-height", "font-size"}


def test_the_allowed_colours_are_the_spec_exceptions() -> None:
    assert spec_colour_exceptions(UI_SPEC.read_text()) == {
        "rgba(198,255,61,.45)",
        "rgba(7,7,10,.88)",
        "#d4ff66",
    }


@pytest.mark.parametrize("source", GUARDED)
def test_no_colour_literal_outside_the_spec_exceptions(source: str) -> None:
    assert colour_literals(_source(source)) == []


@pytest.mark.parametrize(
    ("css", "reported"),
    [
        (".x{color:#e06c75}", ["#e06c75"]),
        (".x{background:rgba(198, 255, 61, 0.5)}", ["rgba(198,255,61,.5)"]),
        (".x{border:1px solid var(--x, #fff)}", ["#fff"]),
        (".x{box-shadow:0 0 12px rgba(198, 255, 61, 0.45)}", []),
        (":root{--bg:#07070a}", []),
        (".x{border-color:transparent}", []),
        (".x{color:white}", ["white"]),
        (".x{border:1px solid Red}", ["red"]),
        (".x{outline:2px solid black}", ["black"]),
        (".x{color:currentColor;background:inherit}", []),
        (".x{color:var(--red-ish)}", []),
    ],
)
def test_colour_literals_are_reported_in_canonical_form(css: str, reported: list[str]) -> None:
    assert colour_literals(css) == reported


@pytest.mark.parametrize("page", [0, 1, 2], ids=["index", "digest", "raw"])
def test_every_digest_page_shows_keyboard_focus(page: int) -> None:
    assert _focus_problems(receipt_fixtures()[page]) == []


async def test_settings_shows_keyboard_focus(triage: TestClient) -> None:
    assert (await _served_rules(triage, "/static/style.css"))[":focus-visible"] == FOCUS


def test_a_page_without_a_focus_rule_is_reported() -> None:
    assert _focus_problems("<style>a { color: var(--text); }</style>") == ["missing :focus-visible"]


@pytest.mark.parametrize("page", [0, 1, 2], ids=["index", "digest", "raw"])
def test_every_digest_page_stops_motion_on_request(page: int) -> None:
    assert parse_style_block(receipt_fixtures()[page])[REDUCED_MOTION] == STILL


async def test_settings_stops_motion_on_request(triage: TestClient) -> None:
    assert (await _served_rules(triage, "/static/style.css"))[REDUCED_MOTION] == STILL


# Section 4 allows one transform: the raw triage card's drag and fly-out.
RAW_CARD_MOTION = frozenset(
    {(".card", "transition: border-color var(--t-fast), transform var(--t-fast)")}
)


@pytest.mark.parametrize("page", ["index", "digest", "raw", "settings"])
def test_every_transition_changes_colour_over_the_fast_token(page: str) -> None:
    exempt = RAW_CARD_MOTION if page == "raw" else frozenset()
    assert off_spec_transitions(_source(page), exempt) == []


CARD_MOTION = ".card{transition:border-color var(--t-fast), transform var(--t-fast)}"


def test_the_card_transform_is_off_spec_without_its_exemption() -> None:
    assert len(off_spec_transitions(CARD_MOTION)) == 1


def test_the_card_transform_passes_only_with_its_exemption() -> None:
    assert off_spec_transitions(CARD_MOTION, RAW_CARD_MOTION) == []


@pytest.mark.parametrize(
    "css",
    [
        ".card{transition:all var(--t-fast)}",
        ".card{transition:border-color 150ms, transform 150ms}",
        ".card.dragging{transition:transform var(--t-fast)}",
    ],
)
def test_the_card_exemption_covers_no_other_transition(css: str) -> None:
    assert len(off_spec_transitions(css, RAW_CARD_MOTION)) == 1


@pytest.mark.parametrize("page", [0, 1, 2], ids=["index", "digest", "raw"])
def test_no_hover_moves_anything(page: int) -> None:
    rules = parse_style_block(receipt_fixtures()[page])
    moved = [
        key
        for key, declarations in rules.items()
        if ":hover" in key and _declared(declarations, "transform")
    ]
    assert moved == []


@pytest.mark.parametrize(
    ("css", "reported"),
    [
        (".x{transition:color 0.15s}", 1),
        (".x{transition:color var(--t-fast), transform var(--t-fast)}", 1),
        (".x{transition:all var(--t-fast)}", 1),
        (".x{transition-duration:200ms}", 1),
        (".x{transition:background-color var(--t-fast), border-color var(--t-fast)}", 0),
        ("*{transition:none !important}", 0),
    ],
)
def test_an_off_spec_transition_is_reported(css: str, reported: int) -> None:
    assert len(off_spec_transitions(css)) == reported


def test_no_keyframes_override_is_left_under_reduced_motion() -> None:
    _, digest, _ = receipt_fixtures()
    prefix = "@media (prefers-reduced-motion: reduce) | @keyframes"
    assert [key for key in parse_style_block(digest) if key.startswith(prefix)] == []


PROMOTE_KEYS = (
    ".promote-btn.done",
    ".promote-btn.error",
    ".vote-group",
    ".settings-link",
)


@pytest.mark.parametrize("page", ["digest", "raw"])
def test_a_failed_vote_is_marked_in_warn(page: str) -> None:
    html = dict(zip(("index", "digest", "raw"), receipt_fixtures(), strict=True))[page]
    assert parse_style_block(html)[".promote-btn.error"] == {
        "color: var(--warn)",
        "border-color: var(--warn)",
        "background: var(--warn-tint)",
    }
    assert "#e06c75" not in html
    assert "224, 108, 117" not in html


@pytest.mark.parametrize("page", ["digest", "raw"])
@pytest.mark.parametrize("state", ["done", "error"])
def test_hover_keeps_a_voted_buttons_state_colour(page: str, state: str) -> None:
    # .btn.secondary:hover outranks .promote-btn.done by specificity alone, so the
    # state has to name :hover itself or hovering repaints a cast vote as unvoted.
    html = dict(zip(("index", "digest", "raw"), receipt_fixtures(), strict=True))[page]
    rules = parse_style_block(html)
    colour = next(d for d in rules[f".promote-btn.{state}"] if d.startswith("color:"))
    assert rules[f".promote-btn.{state}:hover"] == {colour}


@pytest.mark.parametrize("key", PROMOTE_KEYS)
def test_digest_and_raw_style_votes_identically(key: str) -> None:
    _, digest, raw = receipt_fixtures()
    assert parse_style_block(digest)[key] == parse_style_block(raw)[key]


@pytest.mark.parametrize("page", [1, 2], ids=["digest", "raw"])
def test_vote_buttons_are_small_secondary_buttons(page: int) -> None:
    html = receipt_fixtures()[page]
    buttons = re.findall(r"<button\b[^>]*data-vote=[^>]*>", html)
    assert buttons
    assert all('class="btn sm secondary promote-btn"' in button for button in buttons)
    copied = {".promote-btn", ".promote-btn:hover", ".promote-btn:disabled"}
    assert sorted(copied & set(parse_style_block(html))) == []


def test_votes_stay_hidden_until_authorized() -> None:
    _, digest, _ = receipt_fixtures()
    assert ".vote-group { display: none;" in digest
    assert ".settings-link { display: none;" in digest


@pytest.mark.parametrize("template", ["digest.html.j2", "raw.html.j2"])
def test_the_vote_partial_takes_no_parameters(template: str) -> None:
    env = HtmlDigestWriter("unused-by-these-tests").env
    assert include_sites(env, template)["_promote.css.j2"] == {}


def test_the_components_partial_defines_exactly_the_listed_components() -> None:
    assert _component_key_problems(_render_partial("_components.css.j2")) == []


@pytest.mark.parametrize("key", sorted(COMPONENT_SELECTORS))
def test_each_component_matches_the_prototype(key: str) -> None:
    partial = parse_style_block(f"<style>{_render_partial('_components.css.j2')}</style>")
    assert partial[key] == parse_style_block(PROTOTYPE.read_text())[key]


def test_a_rule_outside_the_component_list_is_reported_as_extra() -> None:
    planted = _render_partial("_components.css.j2") + "\n.row { padding: 0; }"
    assert _component_key_problems(planted) == ["extra .row"]


def test_the_digest_keeps_its_stats_rows_to_the_stats_card() -> None:
    # The stats card writes <div class="row">, so a shared `.row` would restyle it.
    rules = parse_style_block(receipt_fixtures()[1])
    assert ".row" not in rules
    assert ".stats-card .row" in rules


def test_the_digest_score_pill_is_the_component() -> None:
    _, digest, _ = receipt_fixtures()
    assert parse_style_block(digest)[".pill.score"] == {
        "color: var(--accent)",
        "border-color: var(--accent-dim)",
        "background: var(--accent-tint)",
    }


def test_raw_states_are_the_component() -> None:
    _, _, raw = receipt_fixtures()
    rules = parse_style_block(raw)
    assert rules[".state.rejected"] == {"color: var(--text-faint)"}
    assert rules[".state.pending"] == {"color: var(--text-dim)"}
    assert "#6b4a4a" not in raw


async def _served(client: TestClient, path: str) -> str:
    response = await client.get(path)
    assert response.status == 200, f"{path} answered {response.status}"
    return await response.text()


def _site_bar(html: str) -> str:
    match = re.search(r'<header class="site-bar">.*?</header>', html, re.DOTALL)
    assert match, "no site bar"
    return match.group(0)


async def test_settings_opens_with_the_digest_site_bar_marking_settings(
    triage: TestClient,
) -> None:
    env = HtmlDigestWriter("unused-by-these-tests").env
    expected = env.get_template("_site_bar.html.j2").render(current="settings")
    page = await _served(triage, "/settings")
    assert page.count('<header class="site-bar">') == 1
    assert _site_bar(page) == _site_bar(expected)
    assert '<script src="/static/settings.js"></script>' in page


async def test_settings_links_nowhere_near_triage(triage: TestClient) -> None:
    assert "/triage" not in await _served(triage, "/settings")


async def test_the_settings_template_is_not_served_as_a_static_file(triage: TestClient) -> None:
    assert (await triage.get("/static/settings.html")).status == 404


async def test_settings_never_forces_a_smooth_scroll(triage: TestClient) -> None:
    assert 'scrollIntoView({behavior: "smooth"})' not in await _served(
        triage, "/static/settings.js"
    )


def _motion_without_preference(script: str) -> list[str]:
    """Name each line that sets a scroll behaviour without asking about reduced motion."""
    return [
        line.strip()
        for line in script.splitlines()
        if "behavior" in line and "prefers-reduced-motion: reduce" not in line
    ]


def test_a_scroll_that_ignores_reduced_motion_is_reported() -> None:
    planted = 'el.scrollIntoView({behavior: "smooth"});'
    assert _motion_without_preference(planted) == [planted]
    assert _motion_without_preference("route();") == []


async def test_every_settings_scroll_asks_for_the_reduced_motion_preference(
    triage: TestClient,
) -> None:
    script = await _served(triage, "/static/settings.js")
    assert _motion_without_preference(script) == []


async def test_each_settings_form_names_its_category(triage: TestClient) -> None:
    ids = set(re.findall(r'\bid="([^"]+)"', await _served(triage, "/settings")))
    for category in ("model", "digest", "notify"):
        assert {f"{category}-form", f"save-{category}", f"{category}-result"} <= ids
    assert ids.isdisjoint({"form", "save", "result"})


def _literal_scroll_offsets(script: str) -> list[str]:
    """Name each line that scrolls by a pixel count written into the script (§1.6)."""
    return [line.strip() for line in script.splitlines() if re.search(r"\bscroll\w*\s*=.*\d", line)]


def test_a_literal_scroll_offset_is_reported() -> None:
    planted = "nav.scrollLeft = link.offsetLeft - 16;"
    assert _literal_scroll_offsets(planted) == [planted]
    assert _literal_scroll_offsets("nav.scrollLeft = link.offsetLeft - gutter;") == []


async def test_settings_scrolls_by_the_spacing_tokens(triage: TestClient) -> None:
    assert _literal_scroll_offsets(await _served(triage, "/static/settings.js")) == []


def _uses_confirm(script: str) -> bool:
    return re.search(r"\bconfirm\(", script) is not None


def test_a_browser_confirm_dialog_is_reported() -> None:
    assert _uses_confirm("if (!confirm(x)) return;")
    assert not _uses_confirm('retire.textContent = "Confirm retire";')


@pytest.mark.parametrize("path", ["/settings", "/static/settings.js"])
async def test_settings_never_asks_the_browser_to_confirm(triage: TestClient, path: str) -> None:
    assert not _uses_confirm(await _served(triage, path))


SETTINGS_HASHES = ["#model", "#digest", "#notifications", "#sources"]


def _hash_targets(html: str) -> set[str]:
    """The element ids a category hash would scroll to, which it must never do."""
    ids = set(re.findall(r'\bid="([^"]+)"', html))
    return ids & {category.lstrip("#") for category in SETTINGS_HASHES}


def test_an_element_named_like_a_category_is_reported() -> None:
    assert _hash_targets('<input id="model">') == {"model"}


async def test_the_category_list_links_the_four_hashes(triage: TestClient) -> None:
    page = await _served(triage, "/settings")
    nav = re.search(r'<nav class="settings-nav".*?</nav>', page, re.DOTALL)
    assert nav, "no category list"
    assert re.findall(r'href="([^"]+)"', nav.group(0)) == SETTINGS_HASHES


async def test_no_settings_element_is_a_category_hash_target(triage: TestClient) -> None:
    assert _hash_targets(await _served(triage, "/settings")) == set()


async def test_a_hidden_settings_field_stays_hidden_whatever_its_display(
    triage: TestClient,
) -> None:
    assert await _stylesheet_hrefs(triage, "/settings") == ["/static/style.css"]
    rules = await _served_rules(triage, "/static/style.css")
    assert rules["[hidden]"] == {"display: none !important"}


async def test_settings_loads_the_display_font(triage: TestClient) -> None:
    page = await _served(triage, "/settings")
    fonts = re.search(r'href="(https://fonts\.googleapis\.com/css2[^"]+)"', page)
    assert fonts and "Instrument+Serif" in fonts.group(1)


def _declared(rules: set[str] | list[str], prop: str) -> list[str]:
    return [d.split(":", 1)[1].strip() for d in rules if d.split(":", 1)[0] == prop]


def test_the_page_glow_is_the_accent_tint() -> None:
    (image,) = _declared(parse_style_block(receipt_fixtures()[1])["body"], "background-image")
    tint = "radial-gradient(ellipse 80% 60% at 50% -10%, var(--accent-tint), transparent 70%)"
    assert tint in image
    assert "rgba(" not in image


def test_the_archive_has_no_page_glow_like_raw() -> None:
    """Spec section 1.2: the only glow is the brand mark."""
    index, _, raw = receipt_fixtures()
    images = _declared(parse_style_block(index)["body"], "background-image")
    assert images == _declared(parse_style_block(raw)["body"], "background-image")
    assert [image for image in images if "radial-gradient(" in image] == []


@pytest.mark.parametrize("page", [0, 1, 2], ids=["index", "digest", "raw"])
def test_the_brand_mark_glow_is_the_spec_exception(page: int) -> None:
    rules = parse_style_block(receipt_fixtures()[page])
    assert [canonical_colour(v) for v in _declared(rules[".brand-mark"], "box-shadow")] == [
        canonical_colour("0 0 12px rgba(198,255,61,.45)")
    ]


@pytest.mark.parametrize("page", ["index", "digest", "raw"])
def test_only_the_brand_mark_glows(page: str) -> None:
    """Spec section 1.2: the only glow is the brand mark."""
    assert box_shadows(_source(page)) == []


@pytest.mark.parametrize(
    ("css", "reported"),
    [
        (".x{box-shadow:0 0 24px var(--accent)}", 1),
        ("@media (max-width: 720px){.x{box-shadow:0 1px 0 red}}", 1),
        (".brand-mark{box-shadow:0 0 12px rgba(198,255,61,.45)}", 0),
        (".x{box-shadow:none}", 0),
    ],
)
def test_a_shadow_outside_the_brand_mark_is_reported(css: str, reported: int) -> None:
    assert len(box_shadows(css)) == reported


@pytest.mark.parametrize("page", [0, 1, 2], ids=["index", "digest", "raw"])
def test_no_page_rings_the_brand_mark(page: int) -> None:
    assert "0 0 0 4px" not in receipt_fixtures()[page]


@functools.cache
def _parsed(page: str) -> dict[str, set[str] | list[str]]:
    index, digest, raw = receipt_fixtures()
    return parse_style_block({"index": index, "digest": digest, "raw": raw}[page])


# The signed-off role table: (row, pages, key, base). Every base is multiplied by
# --type-scale on the page.
TYPE_ROLES = [
    (1, "index digest raw", "body", "16px"),
    (2, "index digest raw", ".brand-name", "16px"),
    (3, "digest", ".subtitle", "14px"),
    (4, "index raw", ".display", "clamp(52px, 8vw, 96px)"),
    (5, "index digest raw", ".footer", "14px"),
    (6, "index digest raw", ".btn", "14px"),
    (9, "digest", ".issue-title", "clamp(64px, 10vw, 136px)"),
    (10, "digest", ".stats-card", "16px"),
    (12, "digest", ".section-tag", "14px"),
    (14, "digest", ".section-description", "20px"),
    (17, "digest", ".lead-story h2", "clamp(32px, 4.5vw, 52px)"),
    (18, "digest", ".lead-story .summary", "20px"),
    (19, "digest", ".meta", "14px"),
    (20, "digest", ".item-title.lg", "22px"),
    (21, "digest", ".featured-item .summary", "20px"),
    (23, "digest", ".pill", "14px"),
    (25, "digest", ".news-cluster .summary", "20px"),
    (27, "digest", ".thematic-block h3::before", "14px"),
    (28, "digest", ".item-title", "20px"),
    (29, "digest", ".article-item .summary", "20px"),
    (32, "digest", ".attention-item .snippet", "20px"),
    (34, "digest", ".headline-item", "16px"),
    (35, "digest", ".headline-item .idx", "16px"),
    (36, "index", ".archive-row .date, .front-card .date", "22px"),
    (39, "index", ".empty-message", "16px"),
    (40, "raw", ".source-name", "20px"),
    (42, "raw", ".state", "13px"),
    (43, "raw", ".score", "16px"),
    (46, "raw", ".card h2", "30px"),
    (47, "index", ".front-card h2", "22px"),
    (44, "index digest raw", ".label", "14px"),
    (45, "index digest raw", ".data", "16px"),
]
TYPE_CASES = [
    (row, page, key, base) for row, pages, key, base in TYPE_ROLES for page in pages.split()
]


@pytest.mark.parametrize(
    ("row", "page", "key", "base"), TYPE_CASES, ids=[f"{r}-{p}-{k}" for r, p, k, _ in TYPE_CASES]
)
def test_each_font_size_follows_the_role_table(row: int, page: str, key: str, base: str) -> None:
    assert f"font-size: calc({base} * var(--type-scale))" in _parsed(page)[key], f"row {row}"


def test_the_issue_title_takes_the_issue_title_role() -> None:
    assert (
        "font-size: calc(clamp(64px, 10vw, 136px) * var(--type-scale))"
        in _parsed("digest")[".issue-title"]
    )


@pytest.mark.parametrize("page", ["index", "raw"])
def test_the_page_title_is_the_display_component(page: str) -> None:
    assert {
        "font-size: calc(clamp(52px, 8vw, 96px) * var(--type-scale))",
        "line-height: 1",
        "letter-spacing: -.03em",
    } <= set(_parsed(page)[".display"])


def test_the_raw_page_head_is_the_prototype_page_head() -> None:
    assert _parsed("raw")[".page-head"] == {
        "display: grid",
        "gap: var(--s-4)",
        "margin-bottom: var(--s-12)",
    }


@pytest.mark.parametrize(
    "key",
    [
        "@media (max-width: 720px) | .lead-story",
        "@media (max-width: 720px) | .site-nav .label",
    ],
)
def test_labels_keep_one_size_on_narrow_screens(key: str) -> None:
    declarations = _parsed("digest")[key]
    assert [d for d in declarations if d.startswith("font-size")] == []


def test_a_section_tag_is_a_label_with_no_number_or_heading_beside_it() -> None:
    digest = _parsed("digest")
    assert [key for key in digest if ".section-heading" in key or ".section-tag .id" in key] == []


def test_every_item_title_takes_the_title_role_from_one_rule() -> None:
    digest = _parsed("digest")
    assert {
        "font-weight: 600",
        "line-height: 1.3",
        "font-size: calc(20px * var(--type-scale))",
    } <= set(digest[".item-title"])
    assert digest[".item-title.lg"] == {"font-size: calc(22px * var(--type-scale))"}
    assert {".item-title a", ".item-title a:hover"} <= set(digest)
    typeset = ("font-size", "font-weight", "line-height", "letter-spacing", "color")
    per_kind = [
        f"{key} | {declaration}"
        for key, declarations in digest.items()
        if key.endswith((" h3", " h4", " h5", " h3 a", " h4 a", " h5 a"))
        for declaration in declarations
        if declaration.split(":", 1)[0] in typeset
    ]
    assert per_kind == []


def test_every_meta_row_is_one_base_rule_and_the_lead_names_its_variant() -> None:
    digest = _parsed("digest")
    assert sorted(key for key in digest if ".meta" in key) == [".meta", ".meta.ruled"]
    assert {
        "display: flex",
        "flex-wrap: wrap",
        "align-items: center",
        "gap: var(--s-3)",
        "font-family: var(--font-mono)",
        "font-size: calc(14px * var(--type-scale))",
        "text-transform: uppercase",
        "letter-spacing: 0.08em",
        "color: var(--text-faint)",
    } <= set(digest[".meta"])
    assert digest[".meta.ruled"] == {
        "margin-top: var(--s-6)",
        "padding-top: var(--s-5)",
        "border-top: 1px dashed var(--border-strong)",
    }
    assert '<div class="meta ruled">' in _source("digest")


def test_the_masthead_is_the_prototype_digest_head() -> None:
    digest = _parsed("digest")
    assert {
        "display: grid",
        "grid-template-columns: minmax(0, 1fr) 320px",
        "gap: var(--s-12)",
        "align-items: end",
    } <= set(digest[".headline-block"])
    assert digest["@media (max-width: 720px) | .headline-block"] == {
        "grid-template-columns: minmax(0, 1fr)",
        "gap: var(--s-6)",
    }
    # A title wider than a phone's column, as it is before the webfont lands, is
    # clipped rather than scrolling the page sideways.
    assert "overflow-x: clip" in digest[".headline-block"]
    assert "padding: var(--s-4) var(--s-5)" in digest[".stats-card"]
    assert "padding: var(--s-2) 0" in digest[".stats-card .row"]
    assert ".stats-card .label" not in digest
    narrow = "@media (max-width: 880px) | "
    assert [k for k in digest if k in (narrow + ".headline-block", narrow + ".stats-card")] == []


def test_a_short_last_row_of_features_leaves_its_empty_cells_blank() -> None:
    digest = _parsed("digest")
    grid = digest[".featured-grid"]
    assert [d for d in grid if d.startswith("background")] == []
    assert "gap: 1px" not in grid
    assert {"border-top: 1px solid var(--border)", "border-left: 1px solid var(--border)"} <= grid
    assert {
        "border-right: 1px solid var(--border)",
        "border-bottom: 1px solid var(--border)",
    } <= digest[".featured-item"]


BODY_TRACKS = "grid-template-columns: repeat(auto-fit, minmax(min(100%, 320px), 1fr))"


def test_features_and_the_radar_list_share_one_fluid_track_rule() -> None:
    digest = _parsed("digest")
    assert BODY_TRACKS in digest[".featured-grid"]
    assert BODY_TRACKS in digest[".attention-list"]
    narrowed = [
        key
        for key in digest
        if key.startswith("@media") and ("featured-grid" in key or "attention-list" in key)
    ]
    assert narrowed == []


def test_the_digest_breaks_only_at_the_spec_breakpoint() -> None:
    digest = _parsed("digest")
    widths = {key.split(" | ")[0] for key in digest if key.startswith("@media (max-width")}
    assert widths == {"@media (max-width: 720px)"}
    assert digest["@media (max-width: 720px) | .section"] == {"margin-bottom: var(--s-12)"}


# Section 4 sets the pill's padding by value; it is the one spacing literal on the page.
PILL_PADDING = frozenset({(".pill", "padding: 2px 10px")})


def test_every_digest_spacing_is_on_the_scale() -> None:
    assert spacing_literals(_source("digest"), PILL_PADDING) == []


@pytest.mark.parametrize(
    ("css", "reported"),
    [
        (".x{padding:28px 0}", 1),
        ("@media (max-width: 720px){.x{padding:16px 12px 60px}}", 1),
        (".x{gap:1px}", 1),
        (".x{margin:var(--s-8) 0 var(--s-6)}", 0),
        (".x{margin:0 auto}", 0),
        (".x{width:24px;min-width:36px;left:0}", 0),
    ],
)
def test_a_spacing_off_the_scale_is_reported(css: str, reported: int) -> None:
    assert len(spacing_literals(css)) == reported


def test_the_pill_padding_passes_only_with_its_exemption() -> None:
    assert len(spacing_literals(".pill{padding:2px 10px}")) == 1
    assert spacing_literals(".pill{padding:2px 10px}", PILL_PADDING) == []


def test_the_digest_passes_its_masthead_and_footer_scale_spacing() -> None:
    sites = include_sites(HtmlDigestWriter("unused-by-these-tests").env, "digest.html.j2")
    assert sites["_masthead.css.j2"] == {
        "masthead_padding": "var(--s-8) 0 var(--s-6)",
        "masthead_margin": "var(--s-12)",
        "masthead_rule": True,
        "subtitle_margin": "var(--s-5)",
    }
    assert sites["_footer.css.j2"] == {
        "layout": "block",
        "justify": False,
        "align_center": False,
        "margin_top": "var(--s-20)",
        "padding_top": "var(--s-8)",
        "gap": None,
    }


def test_the_lead_story_is_the_prototype_lead_card() -> None:
    digest = _parsed("digest")
    assert [key for key in digest if ".lead-story::before" in key] == []
    assert _declared(digest[".lead-story::after"], "box-shadow") == []
    assert "padding: var(--s-8) var(--s-12)" in digest[".lead-story"]
    assert _declared(digest[".lead-story"], "margin-bottom") == []
    assert digest["@media (max-width: 720px) | .lead-story"] == {"padding: var(--s-6) var(--s-5)"}
    title = digest[".lead-story h2"]
    assert _declared(title, "max-width") == []
    assert {"line-height: 1.1", "margin: var(--s-3) 0 var(--s-5)"} <= set(title)
    assert {"color: var(--text-dim)", "line-height: 1.65"} <= set(digest[".lead-story .summary"])


def test_archive_rows_are_the_prototype_rows_and_wrap_them_on_phones() -> None:
    index = _parsed("index")
    # The row itself is the shared list row; the archive adds only its columns.
    assert 'class="list-row archive-row"' in _source("index")
    assert index[".archive-row"] == {"grid-template-columns: 170px 110px 1fr auto"}
    assert ".archive-row:hover" not in index
    # A row without a count keeps its buttons in the last column, right-aligned.
    assert "grid-column: 4" in index[".archive-row .actions"]
    phone = index["@media (max-width: 720px) | .archive-row"]
    assert {"grid-template-columns: 1fr auto", "row-gap: var(--s-2)"} <= phone
    wrapped = index["@media (max-width: 720px) | .archive-row .small, .archive-row .actions"]
    assert "grid-column: 1 / -1" in wrapped
    assert [key for key in index if "digest-item" in key] == []
    widths = {key.split(" | ")[0] for key in index if key.startswith("@media (max-width")}
    assert widths == {"@media (max-width: 720px)"}


def test_the_headline_card_is_the_prototype_card() -> None:
    index = _parsed("index")
    card = index[".front-card"]
    assert {
        "background: var(--surface)",
        "border: 1px solid var(--border-strong)",
        "padding: var(--s-6)",
        "display: grid",
        "gap: var(--s-4)",
        "margin-bottom: var(--s-5)",
    } <= card
    assert [d for d in card if d.startswith(("border-radius", "box-shadow"))] == []
    assert index[".latest"] == {"color: var(--accent)"}
    assert "font-weight: 600" in index[".front-card h2"]


def test_a_same_day_row_dims_its_date() -> None:
    assert _parsed("index")[".archive-row.same-day .date"] == {"color: var(--text-faint)"}


def test_the_receipt_archive_carries_the_card_title() -> None:
    assert "<h2>Featured Story</h2>" in _source("index")


def test_the_empty_archive_message_takes_the_small_role() -> None:
    assert "font-size: calc(16px * var(--type-scale))" in _parsed("index")[".empty-message"]


def test_the_raw_state_column_fits_the_larger_state_label() -> None:
    raw = _parsed("raw")
    assert "grid-template-columns: 96px 56px 1fr auto" in raw[".raw-row"]
    assert "grid-template-columns: 96px 1fr auto" in raw["@media (max-width: 720px) | .raw-row"]


def test_the_triage_card_is_the_prototype_card() -> None:
    raw = _parsed("raw")
    card = raw[".card"]
    assert {
        "background: var(--surface)",
        "border: 1px solid var(--border-strong)",
        "touch-action: pan-y",
        "transition: border-color var(--t-fast), transform var(--t-fast)",
    } <= card
    assert [d for d in card if d.startswith(("border-radius", "box-shadow"))] == []
    assert raw[".card.lean-up"] == {"border-color: var(--accent)"}
    assert raw[".card.lean-down"] == {"border-color: var(--warn)"}
    assert raw[".card:hover"] == {"border-color: var(--text-faint)"}
    assert "height: 56px" in raw[".deck-actions .btn"]
    assert "max-width: 600px" in raw[".deck-wrap"]


def test_the_triage_card_stays_still_under_reduced_motion() -> None:
    raw = _parsed("raw")
    assert raw["@media (prefers-reduced-motion: reduce) | .card"] == {"transform: none !important"}
    assert raw[REDUCED_MOTION] == STILL


def test_the_card_transform_is_the_only_one_the_raw_page_needs_let_through() -> None:
    assert off_spec_transitions(_source("raw")) == [
        ".card | transition: border-color var(--t-fast), transform var(--t-fast)"
    ]


def test_raw_rows_put_the_title_on_its_own_line_at_the_one_breakpoint() -> None:
    raw = _parsed("raw")
    assert "grid-column: 1 / -1" in raw["@media (max-width: 720px) | .raw-row a"]
    assert [key for key in raw if key.startswith("@media (max-width: 640px)")] == []


def test_raw_rows_are_the_prototype_rows() -> None:
    raw = _parsed("raw")
    # The row itself is the shared list row; raw adds its columns and baseline.
    assert 'class="list-row raw-row"' in _source("raw")
    assert raw[".raw-row"] == {
        "grid-template-columns: 96px 56px 1fr auto",
        "align-items: baseline",
    }
    assert ".raw-row:hover" not in raw
    old = {".source-group", ".source-head", ".source-count", ".article", ".article a"}
    assert sorted(old & set(raw)) == []


@pytest.mark.parametrize("page", ["index", "digest", "raw"])
def test_every_page_scales_its_inherited_text(page: str) -> None:
    body = _parsed(page)["body"]
    assert "font-size: calc(16px * var(--type-scale))" in body
    assert isinstance(body, list) and body[0].startswith("font-family")


@pytest.mark.parametrize("page", ["index", "digest", "raw"])
def test_the_body_has_no_gutter_so_a_bar_can_span_the_viewport(page: str) -> None:
    assert [d for d in _parsed(page)["body"] if d.startswith("padding")] == []


@pytest.mark.parametrize("page", ["index", "digest", "raw"])
def test_the_container_carries_the_page_gutters_outside_its_width(page: str) -> None:
    assert {
        "padding: var(--s-6) var(--s-4) var(--s-20)",
        "box-sizing: content-box",
    } <= set(_parsed(page)[".container"])


def test_the_raw_page_is_as_wide_as_the_spec_sets_it() -> None:
    assert "max-width: 960px" in _parsed("raw")[".container"]


@pytest.mark.parametrize("page", ["index", "digest", "raw"])
def test_a_link_takes_its_parents_colour_by_default(page: str) -> None:
    assert _parsed(page)["a"] == {"color: inherit"}


def test_only_digest_content_links_take_the_accent() -> None:
    digest = _parsed("digest")
    assert digest[":where(main) a"] == {
        "color: var(--accent)",
        "text-decoration: none",
        "background: none",
        "border: none",
    }
    assert digest[":where(main) a:hover"] == {"color: var(--text)"}


def test_the_digest_keeps_the_page_gutters_at_every_width() -> None:
    assert [key for key in _parsed("digest") if key.endswith("| .container")] == []


@pytest.mark.parametrize("source", GUARDED)
def test_no_font_size_escapes_the_type_scale(source: str) -> None:
    assert unscaled_font_sizes(_source(source)) == []


@pytest.mark.parametrize(
    "css",
    [
        ".x{font-size:11px}",
        ".x{font-size:clamp(28px, 3.5vw, 40px)}",
        "@media (max-width: 880px){.x{font-size:9px}}",
        ".x{font:600 14px Geist}",
        ".x{font-size:calc(14px * 1)}",
    ],
)
def test_an_unscaled_font_size_is_reported(css: str) -> None:
    assert len(unscaled_font_sizes(css)) == 1


def test_a_scaled_clamp_is_not_reported() -> None:
    assert (
        unscaled_font_sizes(".x{font-size:calc(clamp(52px, 8vw, 96px) * var(--type-scale))}") == []
    )


@pytest.mark.parametrize("source", GUARDED)
def test_every_font_stack_is_a_token(source: str) -> None:
    assert font_stack_literals(_source(source)) == []


@pytest.mark.parametrize(
    ("css", "reported"),
    [
        (".x{font-family:'Geist Mono', monospace}", 1),
        (".x{font:400 1em 'Instrument Serif', serif}", 1),
        (".x{font-family:var(--font-mono)}", 0),
        (":root{--font-sans:'Geist', system-ui, sans-serif}", 0),
    ],
)
def test_a_hand_written_font_stack_is_reported(css: str, reported: int) -> None:
    assert len(font_stack_literals(css)) == reported


@pytest.mark.parametrize(
    ("css", "reported"),
    [
        (".x{padding:.5rem}", 1),
        (".x{margin:0 1.2rem 0 0}", 1),
        (".x{letter-spacing:.1em}", 0),
        (".x{padding:var(--s-2)}", 0),
    ],
)
def test_a_rem_size_is_reported(css: str, reported: int) -> None:
    assert len(rem_values(css)) == reported


@pytest.mark.parametrize(
    ("css", "reported"),
    [
        (".x{border-radius:8px}", 1),
        (".x{border-top-left-radius:4px}", 1),
        (".x{border-radius:var(--r-control)}", 0),
        (".x{border-radius:var(--r-tag)}", 0),
        (".d{border-radius:50%}", 0),
    ],
)
def test_a_radius_off_the_tokens_is_reported(css: str, reported: int) -> None:
    assert len(off_token_radii(css)) == reported


def test_a_style_attribute_is_reported() -> None:
    assert len(style_attributes('<p style="margin:8px">x</p>')) == 1
    assert style_attributes("<style>p { margin: 0; }</style><p>x</p>") == []


@pytest.mark.parametrize("source", ["style.css", "settings"])
def test_no_size_is_written_in_rem(source: str) -> None:
    assert rem_values(_source(source)) == []


@pytest.mark.parametrize("source", ["style.css", "settings"])
def test_every_radius_is_a_shape_token(source: str) -> None:
    assert off_token_radii(_source(source)) == []


def test_the_settings_page_styles_nothing_through_an_attribute() -> None:
    assert style_attributes(_source("settings")) == []


def test_the_settings_page_breaks_only_where_the_spec_does() -> None:
    queries = {key.split(" | ")[0] for key in parse_style_block(_source("settings")) if "@" in key}
    assert queries <= {"@media (max-width: 720px)", "@media (prefers-reduced-motion: reduce)"}


def test_the_settings_page_scales_its_text_without_a_body_gutter() -> None:
    body = parse_style_block(_source("settings"))["body"]
    assert "font-size: calc(16px * var(--type-scale))" in body
    assert [d for d in body if d.startswith("padding")] == []


class _VisibleSmallText(HTMLParser):
    """The text of every small paragraph a reader sees without opening a `details`."""

    def __init__(self) -> None:
        super().__init__()
        self.texts: list[str] = []
        self._details = 0
        self._text: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "details":
            self._details += 1
        elif tag == "p" and ("class", "small") in attrs and not self._details:
            self._text = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "details":
            self._details -= 1
        elif tag == "p" and self._text is not None:
            self.texts.append("".join(self._text).strip())
            self._text = None

    def handle_data(self, data: str) -> None:
        if self._text is not None:
            self._text.append(data)


def _longer_than_a_sentence(html: str) -> list[str]:
    parser = _VisibleSmallText()
    parser.feed(html)
    return [text for text in parser.texts if len(re.findall(r"[.!?](?:\s|$)", text)) > 1]


def test_a_second_visible_sentence_is_reported() -> None:
    planted = (
        '<p class="small">One. Two.</p><p class="small">Three.</p>'
        '<details class="more"><summary>More</summary><p class="small">Four. Five.</p></details>'
    )
    assert _longer_than_a_sentence(planted) == ["One. Two."]


def test_settings_says_one_sentence_in_view_and_folds_the_rest() -> None:
    """§4 and §6: one small sentence under a heading or field, the rest under More."""
    assert _longer_than_a_sentence(_source("settings")) == []


def test_a_settings_source_name_takes_the_title_role() -> None:
    """§3 gives source names the title role, 20–22px."""
    name = parse_style_block(_source("settings"))["table.src .name"]
    assert "font-size: calc(20px * var(--type-scale))" in name


# The old page's inline form and table rules, which the ticket moved to shared components.
OLD_SETTINGS_RULES = {
    "fieldset",
    "legend",
    "label.provider",
    "input[type=text]",
    "select",
    "button",
    "button[disabled]",
    "table.sources",
}


def _inline_rules_shared_with_the_stylesheet(page: str) -> list[str]:
    inline = set(parse_style_block(page)) - {"body"}
    shared = set(parse_style_block(f"<style>{STYLE.read_text()}</style>"))
    return sorted(inline & (shared | OLD_SETTINGS_RULES))


def test_an_inline_copy_of_a_shared_rule_is_reported() -> None:
    planted = _source("settings") + "<style>.btn { color: var(--text); }</style>"
    assert _inline_rules_shared_with_the_stylesheet(planted) == [".btn"]


def test_the_settings_page_keeps_only_its_own_layout_inline() -> None:
    assert _inline_rules_shared_with_the_stylesheet(_source("settings")) == []
