"""/labels and /onboarding, worded in English and in zh-Hant from the catalogs (ADR-0027).

Every expected text is read from the catalog files, never from `cyris.i18n`. A span a
message marks up, `{#em}…{/em}`, reaches a page script as the element the script supplies;
a template draws it with `t(key, elements=…)`.
"""

import asyncio
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from aiohttp.test_utils import make_mocked_request

import cyris
from cyris import i18n
from cyris.entrypoints.triage_server import STATIC_DIR, TriageServer

pytestmark = pytest.mark.unit

LOCALES = Path(cyris.__file__).parent / "locales"
CATALOGS = ["en", "zh-Hant"]


def _text(catalog: str, key: str) -> str:
    entry = json.loads((LOCALES / f"{catalog}.json").read_text(encoding="utf-8"))[key]
    return entry if catalog == "en" else entry["text"]


def _html_lang(html: str) -> str:
    return re.search(r'<html lang="([^"]*)">', html)[1]


def _named_file(html: str) -> str:
    return re.search(r'<meta name="i18n-catalog" content="([^"]*)">', html)[1]


def _serve(server: TriageServer, path: str, headers: dict[str, str] | None = None) -> str:
    handler = {
        "/labels": server._handle_labels_page,
        "/onboarding": server._handle_onboarding_page,
    }[path]
    response = asyncio.run(handler(make_mocked_request("GET", path, headers=headers or {})))
    return response.body.decode()


def _served(server: TriageServer, html: str) -> dict:
    """The browser messages in the file `html` names, as the server serves it."""

    async def get():
        url = _named_file(html)
        router = server._app.router
        match = await router.resolve(make_mocked_request("GET", url))
        return await match.handler(make_mocked_request("GET", url, match_info=match))

    response = asyncio.run(get())
    assert response.status == 200
    return json.loads(response.body)


# ---- a marked-up span ----------------------------------------------------------------


# The browser formatter, as a page runs it, with a document that only builds nodes:
# each call names the elements it supplies, and a node prints as <name>…</name>.
RUNNER = """
import { pathToFileURL } from "node:url";
const [module, catalog, calls] = process.argv.slice(2);
const node = (name) => ({ name, children: [], append(...xs) { this.children.push(...xs); } });
const print = (x) => typeof x === "string" ? x
  : x.name === "#fragment" ? x.children.map(print).join("")
  : `<${x.name}>${x.children.map(print).join("")}</${x.name}>`;
globalThis.document = {
  querySelector: () => ({ content: "/static/i18n/en.json" }),
  createDocumentFragment: () => node("#fragment"),
};
globalThis.fetch = async () => new Response(catalog);
const { t } = await import(pathToFileURL(module).href);
const results = JSON.parse(calls).map(([key, values, names]) => {
  try {
    const elements = names && Object.fromEntries(names.map((name) => [name, node(name)]));
    const out = t(key, values, elements);
    return { text: typeof out === "string" ? out : print(out) };
  } catch (error) {
    return { error: String(error.message) };
  }
});
console.log(JSON.stringify(results));
"""


def _format_in_browser(tmp_path: Path, catalog: dict, calls: list) -> list[dict]:
    bun = shutil.which("bun")
    if bun is None:
        assert not os.environ.get("CI"), "CI must provide bun, which scripts/check.sh runs"
        pytest.skip("bun is not installed")
    runner = tmp_path / "runner.mjs"
    runner.write_text(RUNNER, encoding="utf-8")
    result = subprocess.run(
        [bun, str(runner), str(STATIC_DIR / "i18n.js"), json.dumps(catalog), json.dumps(calls)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_the_browser_puts_each_marked_span_in_the_element_the_script_supplies(tmp_path) -> None:
    catalog = {
        "a.done": {
            "tag": "en",
            "text": "{$n} done. {#read}Read it{/read}, or see the {#archive}archive{/archive}.",
        }
    }

    results = _format_in_browser(
        tmp_path,
        catalog,
        [["a.done", {"n": 2}, ["read", "archive"]], ["a.done", {"n": 2}, ["read"]]],
    )

    marked = "2 done. <read>Read it</read>, or see the <archive>archive</archive>."
    assert results[0] == {"text": marked}
    assert "archive" in results[1]["error"]


def test_the_browser_words_a_marked_message_as_plain_text_without_elements(tmp_path) -> None:
    catalog = {"a.title": {"tag": "en", "text": "Set up {#em}cyris{/em}"}}

    assert _format_in_browser(tmp_path, catalog, [["a.title", {}, None]]) == [
        {"text": "Set up cyris"}
    ]


# ---- /labels ---------------------------------------------------------------------------


def _buttons(html: str) -> list[tuple[str, str]]:
    return re.findall(r'data-label="(\w+)">([^<]*)</button>', html)


@pytest.mark.parametrize("catalog", CATALOGS)
def test_labels_is_worded_in_the_saved_language(catalog) -> None:
    server = TriageServer(values={"digest.output_language": catalog})

    html = _serve(server, "/labels")

    assert _html_lang(html) == catalog
    assert re.search(r"<title>([^<]*)</title>", html)[1] == _text(catalog, "labels.page_title")
    assert _buttons(html) == [
        ("down", _text(catalog, "labels.down")),
        ("skip", _text(catalog, "labels.skip")),
        ("up", _text(catalog, "labels.up")),
    ]
    heading = re.search(r'<h1 class="display">(.*?)</h1>', html)[1]
    assert heading == re.sub(r"\{#em\}(.*?)\{/em\}", r"<em>\1</em>", _text(catalog, "labels.title"))
    assert _text(catalog, "labels.hint") in html


@pytest.mark.parametrize("catalog", CATALOGS)
def test_the_labels_script_gets_its_messages_in_the_pages_language(catalog) -> None:
    server = TriageServer(values={"digest.output_language": catalog})
    html = _serve(server, "/labels")

    served = _served(server, html)

    assert served["labels.progress"] == {"tag": catalog, "text": _text(catalog, "labels.progress")}
    assert served["labels.refused_because"]["tag"] == catalog


@pytest.mark.parametrize("catalog", CATALOGS)
def test_the_labels_progress_line_formats_in_the_pages_language(tmp_path, catalog) -> None:
    server = TriageServer(values={"digest.output_language": catalog})
    served = _served(server, _serve(server, "/labels"))
    values = {"answered": 1, "total": 3, "remaining": 2}

    results = _format_in_browser(tmp_path, served, [["labels.progress", values, None]])

    expected = _text(catalog, "labels.progress")
    for name, value in values.items():
        expected = expected.replace(f"{{${name}}}", str(value))
    assert results == [{"text": expected}]


def test_labels_is_a_module_that_formats_through_the_shared_formatter() -> None:
    html = _serve(TriageServer(values={"digest.output_language": "en"}), "/labels")
    script = (STATIC_DIR / "labels.js").read_text(encoding="utf-8")

    assert '<script type="module" src="/static/labels.js"></script>' in html
    assert 'import { t } from "./i18n.js";' in script


# ---- /onboarding's feed list -----------------------------------------------------------


class _EmptySources:
    """A `sources` table with nothing in it yet, so the list is offered."""

    def list_sources(self) -> dict:
        return {}


def _feed_list(server: TriageServer, headers: dict[str, str] | None = None) -> dict:
    request = make_mocked_request("GET", "/api/onboarding/feeds", headers=headers or {})
    return json.loads(asyncio.run(server._handle_get_onboarding_feeds(request)).body)


@pytest.mark.parametrize("catalog", CATALOGS)
def test_the_feed_lists_topics_are_labelled_in_the_saved_language(catalog) -> None:
    server = TriageServer(values={"digest.output_language": catalog}, source_store=_EmptySources())

    topics = _feed_list(server)["topics"]

    assert topics and all(
        topic["label"] == _text(catalog, f"onboarding_topic.{topic['id']}") for topic in topics
    )


def test_with_no_saved_language_the_topics_follow_the_browser() -> None:
    server = TriageServer(values={}, source_store=_EmptySources())

    topics = _feed_list(server, {"Accept-Language": "zh-TW"})["topics"]

    assert [topic["label"] for topic in topics] == [
        _text("zh-Hant", f"onboarding_topic.{topic['id']}") for topic in topics
    ]


# ---- /onboarding -----------------------------------------------------------------------


def _wizard(**kwargs) -> TriageServer:
    """A deployment with a writable source table, which the wizard needs to open."""
    return TriageServer(source_store=_EmptySources(), **kwargs)


def _steps(html: str) -> list[str]:
    return re.findall(r'<li class="label" data-step="[^"]+">([^<]*)</li>', html)


def _nav(html: str) -> list[str]:
    nav = re.search(r'<div class="step-nav">(.*?)</div>', html, re.S)[1]
    return re.findall(r'id="o-(?:back|skip|next|start|finish)"[^>]*>([^<]*)<', nav)


@pytest.mark.parametrize("catalog", CATALOGS)
def test_onboarding_is_worded_in_the_saved_language(catalog) -> None:
    server = _wizard(values={"digest.output_language": catalog})

    html = _serve(server, "/onboarding")

    assert _html_lang(html) == catalog
    assert _named_file(html) == f"/static/i18n/{catalog}.json"
    assert re.search(r"<title>([^<]*)</title>", html)[1] == _text(catalog, "onboarding.page_title")
    step_keys = ["language", "schedule", "sources", "votes", "notify", "model", "first_issue"]
    assert _steps(html) == [_text(catalog, f"onboarding.step_{step}") for step in step_keys]
    assert _nav(html) == [
        _text(catalog, f"onboarding.{key}") for key in ("back", "skip", "next", "start", "finish")
    ]
    progress = _text(catalog, "onboarding.progress").replace("{$step}", "1")
    assert re.search(r'id="o-progress">([^<]*)<', html)[1] == progress.replace("{$steps}", "7")
    heading = re.search(r'<h1 class="display">(.*?)</h1>', html)[1]
    assert heading == re.sub(
        r"\{#em\}(.*?)\{/em\}", r"<em>\1</em>", _text(catalog, "onboarding.title")
    )
    assert _text(catalog, "settings_help.digest.topic_lifetime_days") in html


@pytest.mark.parametrize(("header", "catalog"), [("zh-TW,zh;q=0.9", "zh-Hant"), ("fr", "en")])
def test_with_no_saved_language_onboarding_follows_the_browser(header, catalog) -> None:
    server = _wizard(values={})

    html = _serve(server, "/onboarding", {"Accept-Language": header})

    assert _html_lang(html) == catalog
    assert _steps(html)[0] == _text(catalog, "onboarding.step_language")
    assert _served(server, html)["onboarding.progress"]["tag"] == catalog


def test_onboarding_is_a_module_that_formats_through_the_shared_formatter() -> None:
    html = _serve(_wizard(values={"digest.output_language": "en"}), "/onboarding")
    script = (STATIC_DIR / "onboarding.js").read_text(encoding="utf-8")

    assert '<script type="module" src="/static/onboarding.js"></script>' in html
    assert 'import { t } from "./i18n.js";' in script


def test_the_onboarding_script_names_the_none_provider_from_the_shared_key() -> None:
    script = (STATIC_DIR / "onboarding.js").read_text(encoding="utf-8")

    assert 't("provider.none")' in script
    assert _text("en", "provider.none") not in script


def test_the_onboarding_script_names_no_issue_by_its_period() -> None:
    """An issue is named by the hour its run started; the period is only the key sent."""
    script = (STATIC_DIR / "onboarding.js").read_text(encoding="utf-8")
    english = json.loads((LOCALES / "en.json").read_text(encoding="utf-8"))

    assert "issue_period." not in script
    assert not re.search(r"\$\{(?:data|run|saved)\.period\}", script)
    assert not re.search(r"\bt\(\"[\w.]+\",\s*\{[^}]*\bperiod\b", script)
    assert [key for key, text in english.items() if "{$period}" in text] == []


# Each plural or select message the script formats, with values on each side of its rule.
FORMATTED = [
    ("onboarding.picked", {"count": 1}),
    ("onboarding.picked", {"count": 12}),
    ("onboarding.added_some", {"added": 1, "picked": 2, "failed": 1}),
    ("onboarding.added_some", {"added": 0, "picked": 3, "failed": 3}),
    ("onboarding.hour_needed", {"which": "morning"}),
    ("onboarding.hour_needed", {"which": "both"}),
    ("onboarding.recorded_votes", {"count": 1, "up": 1, "down": 0}),
    ("onboarding.saved_advanced", {"count": 2, "note": "Saved."}),
]


@pytest.mark.parametrize("catalog", CATALOGS)
def test_the_onboarding_script_formats_as_python_words_it(tmp_path, catalog) -> None:
    server = _wizard(values={"digest.output_language": catalog})
    served = _served(server, _serve(server, "/onboarding"))
    t = i18n.translator(catalog)

    results = _format_in_browser(
        tmp_path, served, [[key, values, None] for key, values in FORMATTED]
    )

    assert results == [{"text": t(key, **values)} for key, values in FORMATTED]


class _Request:
    """What the settings route reads of a request: its JSON body and its headers."""

    def __init__(self, body: dict, headers: dict[str, str] | None = None) -> None:
        self._body = body
        self.headers = headers or {}

    async def json(self) -> dict:
        return self._body


class _Settings:
    def set(self, values) -> None:
        pass


@pytest.mark.parametrize(
    ("before", "saved", "catalog"),
    [
        ({}, "zh-Hant", "zh-Hant"),
        ({"digest.output_language": "zh-Hant"}, "en", "en"),
        ({}, "ja", "en"),
    ],
)
def test_a_saved_language_answers_with_the_catalog_the_pages_now_take(
    before, saved, catalog
) -> None:
    """onboarding.js reloads when this differs from the catalog its page was worded from."""
    server = _wizard(settings=_Settings(), values=before)
    body = {"values": {"digest.output_language": saved}}

    response = asyncio.run(server._handle_post_values(_Request(body, {"Accept-Language": "fr"})))

    assert json.loads(response.body)["ui_lang"] == catalog
    assert _html_lang(_serve(server, "/onboarding")) == catalog


# ---- the terms main's catalogs already have ------------------------------------------------


@pytest.mark.parametrize("catalog", CATALOGS)
def test_the_wizards_snippet_label_names_the_third_layer_glance(catalog) -> None:
    html = _serve(_wizard(values={"digest.output_language": catalog}), "/onboarding")

    label = re.search(r'<label class="label" for="o-filter-snippet">([^<]*)</label>', html)[1]
    assert label == _text(catalog, "settings_field.digest.filter_snippet_length")
    assert _text(catalog, "layer.glance") in label


def test_no_onboarding_or_labels_message_names_the_old_third_layer() -> None:
    english = json.loads((LOCALES / "en.json").read_text(encoding="utf-8"))

    assert [
        key
        for key, text in english.items()
        if key.startswith(("onboarding.", "labels.", "feed_check.")) and "Headlines" in text
    ] == []


@pytest.mark.parametrize("key", ["onboarding.advanced", "onboarding.model_hint_none"])
def test_a_digest_is_called_by_the_issue_pages_word_in_zh_hant(key) -> None:
    text = _text("zh-Hant", key)

    assert _text("zh-Hant", "issue.digest") in text
    assert "摘要" not in text
