"""The UI catalogs agree with themselves and with the code that words from them (ADR-0027).

Each check reads the catalog files and the source under `src/cyris/`. A key is used where
`t("key")` appears in a Python module, a template or a page script; a key built from a value,
`t(f"prefix.{value}")` in a Python module, `t("prefix." ~ value)` in a template or
``t(`prefix.${value}`)`` in a page script, is used for every value of its declared range in
`FAMILIES`. Any other first argument to `t(` is a key no check can see, so it fails.
"""

import ast
import asyncio
import json
import re
from pathlib import Path
from typing import get_args, get_type_hints

import jinja2
import jinja2.nodes
import langcodes
import pytest
from aiohttp.test_utils import make_mocked_request
from messageformat2 import Message
from messageformat2.datamodel import (
    DataModelVisitor,
    FunctionAnnotation,
    LocalDeclaration,
    VariableRef,
)

import cyris
from cyris.domain.areas import AreaStatus
from cyris.domain.interest_graph import InterestNode
from cyris.domain.models import ArticleState, Lane
from cyris.entrypoints.triage_server import (
    BROWSER_MESSAGES,
    STATIC_DIR,
    TEMPLATES_DIR,
    TriageServer,
    render_labels_page,
    render_onboarding_page,
    render_settings_page,
)
from cyris.service_layer.schedule import PERIOD_ORDER

pytestmark = [pytest.mark.unit, pytest.mark.guard]

SRC = Path(cyris.__file__).parent
LOCALES = SRC / "locales"
ENGLISH = "en"
# The functions both formatters ship as Stable and on by default.
FUNCTIONS = {"string", "number", "integer"}
# Each key family built from a value, with the value's closed range.
FAMILIES: dict[str, tuple[str, ...]] = {
    "period.": PERIOD_ORDER,
    "issue_period.": PERIOD_ORDER,
    "article_state.": tuple(map(str, ArticleState)),
    "lane.": tuple(map(str, Lane)),
    "area_status.": get_args(AreaStatus),
    "polarity.": get_args(get_type_hints(InterestNode)["polarity"]),
}
RENDERERS = {
    "settings.html.j2": render_settings_page,
    "labels.html.j2": render_labels_page,
    "onboarding.html.j2": render_onboarding_page,
}

JS_CALL = re.compile(r"(?<![\w$])t\(")
JS_FUNCTION_NAME = re.compile(r"\bfunction\s+$")
JS_LITERAL_ARG = re.compile(r"""\s*(["'])([^"'\\\n]+)\1\s*[,)]""")
JS_FAMILY_ARG = re.compile(r"\s*`([\w.]*\.)\$\{[^{}`]+\}`\s*[,)]")
SCRIPT_SRC = re.compile(r"""<script\b[^>]*\bsrc="/static/([\w./-]+\.js)\"""")
IMPORT = re.compile(r"""^\s*import\b[^;]*?\bfrom\s+["']\./([\w./-]+\.js)["']""", re.MULTILINE)
CATALOG_META = re.compile(r'<meta name="i18n-catalog" content="([^"]+)">')
# Any element naming the catalog: the page's one meta, or an inline block it must not carry.
NAMES_CATALOG = re.compile(r"<(\w+)\b[^>]*\bi18n-catalog\b")


def _catalogs() -> dict[str, dict]:
    return {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(LOCALES.glob("*.json"))
    }


def _english() -> dict[str, str]:
    return _catalogs()[ENGLISH]


def _texts(tag: str, entries: dict) -> dict[str, str]:
    return entries if tag == ENGLISH else {key: entry["text"] for key, entry in entries.items()}


def _every_message() -> list[tuple[str, str, str]]:
    return [
        (tag, key, text)
        for tag, entries in _catalogs().items()
        for key, text in _texts(tag, entries).items()
    ]


class _Reads(DataModelVisitor):
    """The functions a message calls, the variables it reads, and the ones `.local` declares."""

    def __init__(self) -> None:
        self.functions: set[str] = set()
        self.variables: set[str] = set()
        self.locals: set[str] = set()

    def visit(self, node) -> None:
        if isinstance(node, FunctionAnnotation):
            self.functions.add(node.name)
        elif isinstance(node, VariableRef):
            self.variables.add(node.name)
        elif isinstance(node, LocalDeclaration):
            self.locals.add(node.name)
        self.generic_visit(node)


def _reads(text: str) -> _Reads:
    reads = _Reads()
    reads.visit(Message(text).datamodel)
    return reads


def _inputs(text: str) -> set[str]:
    reads = _reads(text)
    return reads.variables - reads.locals


def _sources() -> list[Path]:
    """Every file under `src/cyris/` a `t` call can sit in, vendored code aside."""
    return sorted(
        path
        for pattern in ("*.py", "*.j2", "*.js")
        for path in SRC.rglob(pattern)
        if "vendor" not in path.parts
    )


def _family(prefix: str) -> set[str]:
    return {f"{prefix}{value}" for value in FAMILIES[prefix]}


def _python_calls(text: str) -> list[tuple[int, set[str] | None]]:
    calls = []
    for node in ast.walk(ast.parse(text)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (
            (isinstance(func, ast.Name) and func.id == "t")
            or (isinstance(func, ast.Attribute) and func.attr == "t")
        ):
            continue
        first = node.args[0] if node.args else None
        keys = None
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            keys = {first.value}
        elif (
            isinstance(first, ast.JoinedStr)
            and len(first.values) == 2
            and isinstance(first.values[0], ast.Constant)
            and isinstance(first.values[1], ast.FormattedValue)
            and first.values[0].value in FAMILIES
        ):
            keys = _family(first.values[0].value)
        calls.append((node.lineno, keys))
    return calls


def _template_calls(text: str) -> list[tuple[int, set[str] | None]]:
    calls = []
    for node in jinja2.Environment().parse(text).find_all(jinja2.nodes.Call):
        if not (isinstance(node.node, jinja2.nodes.Name) and node.node.name == "t"):
            continue
        first = node.args[0] if node.args else None
        keys = None
        if isinstance(first, jinja2.nodes.Const) and isinstance(first.value, str):
            keys = {first.value}
        elif (
            isinstance(first, jinja2.nodes.Concat)
            and len(first.nodes) == 2
            and isinstance(first.nodes[0], jinja2.nodes.Const)
            and first.nodes[0].value in FAMILIES
        ):
            keys = _family(first.nodes[0].value)
        calls.append((node.lineno, keys))
    return calls


def _script_calls(text: str) -> list[tuple[int, set[str] | None]]:
    calls = []
    for call in JS_CALL.finditer(text):
        if JS_FUNCTION_NAME.search(text[: call.start()]):
            continue
        line = text.count("\n", 0, call.start()) + 1
        keys = None
        if arg := JS_LITERAL_ARG.match(text, call.end()):
            keys = {arg[2]}
        elif (arg := JS_FAMILY_ARG.match(text, call.end())) and arg[1] in FAMILIES:
            keys = _family(arg[1])
        calls.append((line, keys))
    return calls


CALL_FINDERS = {".py": _python_calls, ".j2": _template_calls, ".js": _script_calls}


def _calls(path: Path) -> list[tuple[int, set[str] | None]]:
    """Each `t(` call in `path` with its line, and the keys it names; None where it names none."""
    return CALL_FINDERS[path.suffix](path.read_text(encoding="utf-8"))


def _keys_in(path: Path) -> set[str]:
    """The keys `path` calls `t` with, each one a literal or a family `FAMILIES` declares."""
    calls = _calls(path)
    opaque = [f"{path.relative_to(SRC)}:{line}" for line, keys in calls if keys is None]
    assert not opaque, f"t( takes a quoted literal or a declared f-string family: {opaque}"
    return set().union(*(keys for _, keys in calls))


def _used_keys() -> dict[str, set[Path]]:
    used: dict[str, set[Path]] = {}
    for path in _sources():
        for key in _keys_in(path):
            used.setdefault(key, set()).add(path)
    return used


def _script_keys(script: Path) -> set[str]:
    """The keys a page script formats, through the modules it imports from `static/` too."""
    keys = _keys_in(script)
    for imported in IMPORT.findall(script.read_text(encoding="utf-8")):
        if "vendor" not in Path(imported).parts:
            keys |= _script_keys(script.parent / imported)
    return keys


def _page_scripts(template: str) -> list[Path]:
    return [
        STATIC_DIR / name
        for name in SCRIPT_SRC.findall((TEMPLATES_DIR / template).read_text(encoding="utf-8"))
    ]


def _named_file(page: str) -> str:
    return CATALOG_META.search(page)[1]


async def _get(url: str) -> dict[str, dict]:
    """The browser messages a bare server answers `url` with, through its own router."""
    router = TriageServer()._app.router
    match = await router.resolve(make_mocked_request("GET", url))
    response = await match.handler(make_mocked_request("GET", url, match_info=match))
    assert response.status == 200, url
    return json.loads(response.body)


def _served(page: str) -> dict[str, dict]:
    """The messages in the file `page` names, as the server serves it."""
    return asyncio.run(_get(_named_file(page)))


# ---- the five checks ADR-0027 names ----------------------------------------------------


@pytest.mark.parametrize(("tag", "key", "text"), _every_message(), ids=lambda v: str(v)[:40])
def test_every_message_parses_and_calls_only_the_shared_functions(tag, key, text) -> None:
    functions = _reads(text).functions

    assert functions <= FUNCTIONS, f"{tag} {key} calls {functions - FUNCTIONS}"


def test_every_key_the_code_uses_is_in_english() -> None:
    missing = {
        key: sorted(str(path.relative_to(SRC)) for path in paths)
        for key, paths in _used_keys().items()
    }
    for key in _english():
        missing.pop(key, None)

    assert missing == {}


def test_every_translation_repeats_the_current_english_as_its_source() -> None:
    english = _english()
    stale = {
        (tag, key): entry["source"]
        for tag, entries in _catalogs().items()
        if tag != ENGLISH
        for key, entry in entries.items()
        if key in english and entry["source"] != english[key]
    }

    assert stale == {}


def test_every_translation_reads_the_inputs_its_english_reads() -> None:
    english = _english()
    differ = {
        (tag, key): (sorted(_inputs(entry["text"])), sorted(_inputs(english[key])))
        for tag, entries in _catalogs().items()
        if tag != ENGLISH
        for key, entry in entries.items()
        if key in english and _inputs(entry["text"]) != _inputs(english[key])
    }

    assert differ == {}


@pytest.mark.parametrize("tag", sorted(_catalogs()))
@pytest.mark.parametrize("template", sorted(RENDERERS))
def test_the_file_a_page_names_carries_each_message_its_scripts_format(template, tag) -> None:
    needed = set().union(*map(_script_keys, _page_scripts(template)))

    served = _served(RENDERERS[template](tag))

    assert needed - set(served) == set()


@pytest.mark.parametrize("tag", sorted(_catalogs()))
@pytest.mark.parametrize("template", sorted(RENDERERS))
def test_no_page_carries_messages_inline(template, tag) -> None:
    assert NAMES_CATALOG.findall(RENDERERS[template](tag)) == ["meta"]


@pytest.mark.parametrize("path", _sources(), ids=lambda path: str(path.relative_to(SRC)))
def test_every_t_call_takes_a_literal_key_or_a_declared_family(path) -> None:
    assert [line for line, keys in _calls(path) if keys is None] == []


@pytest.mark.parametrize(
    ("finder", "source", "keys"),
    [
        (_python_calls, 't("a.b")', {"a.b"}),
        (_python_calls, "t('a.b', n=1)", {"a.b"}),
        (_python_calls, 't(f"period.{p}")', {f"period.{p}" for p in PERIOD_ORDER}),
        (_python_calls, "x.split(1)\nrequests.post(2)", set()),
        (_python_calls, "t(key)", None),
        (_python_calls, 't("lane." + x)', None),
        (_python_calls, 't(f"nowhere.{x}")', None),
        (_python_calls, "t()", None),
        (_template_calls, '{{ t("a.b") }}', {"a.b"}),
        (_template_calls, "{{ t('a.b', n=1) | safe }}", {"a.b"}),
        (_template_calls, "{{ x|format(1) }}", set()),
        (_template_calls, "{{ t(key) }}", None),
        (_template_calls, '{{ t("period." ~ p) }}', {f"period.{p}" for p in PERIOD_ORDER}),
        (_template_calls, '{{ t("nowhere." ~ x) }}', None),
        (_template_calls, '{{ t("period." ~ p ~ ".x") }}', None),
        (_template_calls, '{{ t("a" if x else "b") }}', None),
        (_script_calls, 't("a.b")', {"a.b"}),
        (_script_calls, "t('a.b', { n })", {"a.b"}),
        (_script_calls, "x.split(','); post(1); format(2)", set()),
        (_script_calls, "export function t(key, values = {}) {", set()),
        (_script_calls, "t(`a.b`)", None),
        (_script_calls, "t(`period.${p}`)", {f"period.{p}" for p in PERIOD_ORDER}),
        (_script_calls, "t(`nowhere.${x}`)", None),
        (_script_calls, "t(`period.${p}.x`)", None),
        (_script_calls, "t(key)", None),
        (_script_calls, "t('lane.' + x)", None),
        (_script_calls, "t(cond ? 'a' : 'b')", None),
    ],
)
def test_a_t_call_is_read_for_the_key_it_names(finder, source, keys) -> None:
    calls = finder(source)

    assert [call_keys for _, call_keys in calls] == ([] if keys == set() else [keys])


def test_a_call_is_reported_at_its_line() -> None:
    assert _script_calls("a;\n\n  t(`x`)") == [(3, None)]


def _family_keys_missing_from(english: dict[str, str], families: dict) -> dict[str, list[str]]:
    """Each family's keys, one per value of its range, that `english` lacks."""
    return {
        prefix: missing
        for prefix in families
        if (missing := sorted({f"{prefix}{value}" for value in families[prefix]} - english.keys()))
    }


def test_every_value_of_a_family_has_an_english_key() -> None:
    assert _family_keys_missing_from(_english(), FAMILIES) == {}


def test_a_value_its_family_has_no_key_for_is_found() -> None:
    families = {**FAMILIES, "period.": (*PERIOD_ORDER, "noon")}

    assert _family_keys_missing_from(_english(), families) == {"period.": ["period.noon"]}


# ---- cheaper checks beside them ----------------------------------------------------------


def test_the_file_carries_no_message_no_page_script_formats() -> None:
    needed = set().union(
        *(_script_keys(script) for template in RENDERERS for script in _page_scripts(template))
    )

    assert sorted(BROWSER_MESSAGES) == sorted(needed)


@pytest.mark.parametrize("template", sorted(RENDERERS))
def test_each_page_loads_one_script(template) -> None:
    assert len(_page_scripts(template)) == 1


@pytest.mark.parametrize("tag", sorted(_catalogs()))
def test_every_page_in_one_language_names_one_file(tag) -> None:
    assert len({_named_file(render(tag)) for render in RENDERERS.values()}) == 1


@pytest.mark.parametrize("tag", sorted(_catalogs()))
def test_a_served_message_is_its_catalogs_text_under_its_catalogs_tag(tag) -> None:
    catalogs = {t: _texts(t, entries) for t, entries in _catalogs().items()}
    for key, message in _served(render_settings_page(tag)).items():
        assert set(message) == {"tag", "text"}, key
        assert message["tag"] in (tag, ENGLISH), key
        assert message["text"] == catalogs[message["tag"]][key], key


def test_every_english_key_is_used() -> None:
    assert set(_english()) - set(_used_keys()) == set()


def test_every_translated_key_is_an_english_key() -> None:
    english = set(_english())
    extra = {tag: set(entries) - english for tag, entries in _catalogs().items()}

    assert {tag: keys for tag, keys in extra.items() if keys} == {}


@pytest.mark.parametrize("tag", sorted(_catalogs()))
def test_a_catalog_is_named_by_its_canonical_tag(tag) -> None:
    assert langcodes.standardize_tag(tag) == tag


def _templates() -> list[Path]:
    return sorted(SRC.rglob("*.j2"))


@pytest.mark.parametrize("path", _templates(), ids=lambda path: path.name)
def test_no_template_binds_t(path) -> None:
    tree = jinja2.Environment().parse(path.read_text(encoding="utf-8"))
    bound = [
        node.lineno
        for node in tree.find_all(jinja2.nodes.Name)
        if node.name == "t" and node.ctx in ("store", "param")
    ]

    assert bound == []


def _render_calls_passing_t(source: str) -> list[int]:
    """The lines of each Jinja render call in `source` that binds `t` by keyword."""
    lines = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr not in ("render", "render_async", "stream", "generate"):
            continue
        names = {keyword.arg for keyword in node.keywords}
        for keyword in node.keywords:
            if keyword.arg is None and isinstance(keyword.value, ast.Dict):
                names |= {k.value for k in keyword.value.keys if isinstance(k, ast.Constant)}
        if "t" in names:
            lines.append(node.lineno)
    return lines


def test_a_render_call_passing_t_is_found() -> None:
    assert _render_calls_passing_t("env.render(a=1, t=2)\nx.render(**{'t': 1})") == [1, 2]


def test_no_render_call_passes_t() -> None:
    passing = {
        str(path.relative_to(SRC)): lines
        for path in SRC.rglob("*.py")
        if (lines := _render_calls_passing_t(path.read_text(encoding="utf-8")))
    }

    assert passing == {}


def test_only_the_environment_factory_binds_t() -> None:
    binders = [
        str(path.relative_to(SRC))
        for path in SRC.rglob("*.py")
        if 'globals["t"] =' in path.read_text(encoding="utf-8")
    ]

    assert binders == ["adapters/output/html_digest.py"]


@pytest.mark.parametrize("path", _templates(), ids=lambda path: path.name)
def test_tojson_never_sits_in_a_double_quoted_attribute(path) -> None:
    assert re.findall(r'="[^"]*\|\s*tojson', path.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize("path", _templates(), ids=lambda path: path.name)
def test_a_message_marked_safe_has_no_placeholder(path) -> None:
    english = _english()
    marked = re.findall(
        r"""\bt\(\s*["']([^"']+)["'][^)]*\)\s*\|\s*safe""", path.read_text(encoding="utf-8")
    )

    assert [key for key in marked if _inputs(english[key])] == []
