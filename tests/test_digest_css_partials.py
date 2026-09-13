"""The shared CSS partials stay the one source of truth for the three digest pages.

These assertions are the milestone's invariant, not a snapshot of today's CSS: a
checked-in golden file would need regenerating on every legitimate edit and would
decay into a rubber stamp. Nothing here starts a browser, opens a socket, or
touches the filesystem, so it can run as an ordinary unit test.
"""

import re
from pathlib import Path

import pytest
from css_rules import parse_style_block, receipt_fixtures
from jinja2 import DebugUndefined, Environment, meta, nodes

import cyris.entrypoints
from cyris.adapters.output.html_digest import HtmlDigestWriter

CSS_PARTIALS = (
    "_tokens.css.j2",
    "_page.css.j2",
    "_masthead.css.j2",
    "_footer.css.j2",
    "_promote.css.j2",
)

PAGE_TEMPLATES = {
    "index": "index.html.j2",
    "digest": "digest.html.j2",
    "raw": "raw.html.j2",
}

# The partials each page must draw its shared rules from. Declared here rather
# than read back out of the templates: derived expectations would silently accept
# a page that stopped including a partial and inlined the rules again.
EXPECTED_INCLUDES = {
    "index": ("_tokens.css.j2", "_page.css.j2", "_masthead.css.j2", "_footer.css.j2"),
    "digest": CSS_PARTIALS,
    "raw": CSS_PARTIALS,
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


def _root_tokens(css_source: str) -> dict[str, str]:
    """Return the ``:root`` custom properties declared in an HTML page's styles."""
    declarations = parse_style_block(css_source)[":root"]
    tokens = {}
    for declaration in declarations:
        name, value = declaration.split(":", 1)
        tokens[name.strip()] = value.strip()
    return tokens


def _style_text(html: str) -> str:
    return "\n".join(
        block for block in html.split("<style>")[1:] for block in [block.split("</style>")[0]]
    )


def _include_sites(env: Environment, template_name: str) -> dict[str, dict[str, object]]:
    """Map each CSS partial a template includes to the parameters it passes.

    Parameters are read from the template's own syntax tree, so a renamed or
    retyped one is seen exactly as Jinja sees it.
    """
    source = env.loader.get_source(env, template_name)[0]
    tree = env.parse(source)
    sites: dict[str, dict[str, object]] = {}
    wrapped: set[int] = set()
    for with_node in tree.find_all(nodes.With):
        parameters = {
            target.name: value.as_const()
            for target, value in zip(with_node.targets, with_node.values, strict=True)
        }
        for include in with_node.find_all(nodes.Include):
            wrapped.add(id(include))
            if (name := include.template.as_const()) in CSS_PARTIALS:
                sites[name] = parameters
    for include in tree.find_all(nodes.Include):
        if id(include) not in wrapped and (name := include.template.as_const()) in CSS_PARTIALS:
            sites[name] = {}
    return sites


def test_every_page_and_the_triage_ui_declare_the_same_tokens(pages: dict[str, str]) -> None:
    sources = {name: _root_tokens(html) for name, html in pages.items()}
    sources["style.css"] = _root_tokens(f"<style>{STYLE_CSS.read_text()}</style>")

    reference = sources["digest"]
    problems = []
    for name, tokens in sources.items():
        if name == "digest":
            continue
        extra = sorted(set(tokens) - set(reference))
        missing = sorted(set(reference) - set(tokens))
        differing = sorted(
            token for token in set(tokens) & set(reference) if tokens[token] != reference[token]
        )
        if extra or missing or differing:
            problems.append(
                f"{name}: only in {name} {extra}, absent from {name} {missing},"
                f" different value {differing}"
            )
    assert not problems, "token sets diverged from digest's: " + "; ".join(problems)


@pytest.mark.parametrize(
    ("page", "partial"), INCLUDE_CASES, ids=[f"{p}-{c}" for p, c in INCLUDE_CASES]
)
def test_partial_reaches_the_page_verbatim(
    env: Environment, pages: dict[str, str], page: str, partial: str
) -> None:
    template_name = PAGE_TEMPLATES[page]
    sites = _include_sites(env, template_name)
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


def test_the_omission_check_sees_a_dropped_parameter_and_not_a_guarded_one(
    env: Environment,
) -> None:
    parameters = _include_sites(env, PAGE_TEMPLATES["digest"])["_page.css.j2"]
    assert "line_height" in parameters and "glow" in parameters
    strict = env.overlay(undefined=DebugUndefined)

    without_required = {k: v for k, v in parameters.items() if k != "line_height"}
    assert "{{ line_height }}" in strict.get_template("_page.css.j2").render(**without_required)

    without_guarded = {k: v for k, v in parameters.items() if k != "glow"}
    assert "{{" not in strict.get_template("_page.css.j2").render(**without_guarded)


def test_vote_buttons_are_styled_on_the_pages_that_carry_them(pages: dict[str, str]) -> None:
    for page in ("digest", "raw"):
        assert ".promote-btn" in parse_style_block(pages[page]), f"{page} lost .promote-btn"
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
