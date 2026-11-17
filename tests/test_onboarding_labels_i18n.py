"""/labels and /onboarding, worded in English and in zh-Hant from the catalogs (ADR-0027).

Every expected text is read from the catalog files, never from `cyris.i18n`. A span a
message marks up, `{#em}…{/em}`, reaches a page script as the element the script supplies;
a template draws it with `t(key, elements=…)`.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from cyris.entrypoints.triage_server import STATIC_DIR

pytestmark = pytest.mark.unit


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
