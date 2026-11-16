"""Each converted surface, rendered in English and in zh-Hant, says what that catalog says.

The skeleton of ADR-0027 converts one string on each of five surfaces: the site bar's
Settings on the digest page, the mail's Read online, the Discord message's period, the
/settings script's Discord check, and the triage server's refusal of a settings write with
no store. Every expected text is read from the catalog files, never from `cyris.i18n`.
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
from cyris.adapters.notify import build_discord_embeds
from cyris.adapters.output.email_digest import render_digest_email
from cyris.adapters.output.html_digest import HtmlDigestWriter
from cyris.domain.models import DigestContent
from cyris.entrypoints.triage_server import STATIC_DIR, TriageServer

pytestmark = pytest.mark.unit

LOCALES = Path(cyris.__file__).parent / "locales"
DATE = "2026-11-16"
DIGEST_URL = f"https://digest.test/{DATE}-morning"
# A requested language and the catalog its UI is worded from: Japanese has no catalog.
LANGUAGES = [("en", "en"), ("ja", "en"), ("zh-Hant", "zh-Hant")]
SURFACE_KEYS = [
    "site.settings",
    "email.read_online",
    "period.morning",
    "settings.checking_discord",
    "error.no_settings_store",
]


def _text(catalog: str, key: str) -> str:
    entry = json.loads((LOCALES / f"{catalog}.json").read_text(encoding="utf-8"))[key]
    return entry if catalog == "en" else entry["text"]


def _content(language: str) -> DigestContent:
    return DigestContent(
        date=DATE,
        period="morning",
        sources_processed=1,
        articles_received=0,
        articles_included=0,
        output_language=language,
    )


def _html_lang(html: str) -> str:
    return re.search(r'<html lang="([^"]*)">', html)[1]


def _settings_link(html: str) -> str:
    return re.search(r'<a class="label settings-link"[^>]*>([^<]*)</a>', html)[1]


def _page_catalog(html: str) -> dict:
    block = re.search(r'<script type="application/json" id="i18n-catalog">(.*?)</script>', html)
    return json.loads(block[1])


def _serve(server: TriageServer, path: str, headers: dict[str, str] | None = None) -> str:
    handler = {
        "/settings": server._handle_settings_page,
        "/labels": server._handle_labels_page,
    }[path]
    response = asyncio.run(handler(make_mocked_request("GET", path, headers=headers or {})))
    return response.body.decode()


def _refusal(server: TriageServer, headers: dict[str, str] | None = None) -> dict:
    request = make_mocked_request("POST", "/api/settings/values", headers=headers or {})
    response = asyncio.run(server._settings_body(request))
    assert response.status == 409
    return json.loads(response.body)


@pytest.mark.parametrize("key", SURFACE_KEYS)
def test_each_surface_string_has_a_chinese_text_unlike_its_english(key) -> None:
    assert _text("zh-Hant", key) != _text("en", key)


@pytest.mark.parametrize(("language", "catalog"), LANGUAGES)
def test_the_digest_pages_settings_link_follows_the_issue(tmp_path, language, catalog) -> None:
    html = HtmlDigestWriter(tmp_path, language="en").render(_content(language))

    assert _settings_link(html) == _text(catalog, "site.settings")
    assert _html_lang(html) == catalog


@pytest.mark.parametrize(("language", "catalog"), LANGUAGES)
def test_the_raw_page_follows_the_issue_it_belongs_to(tmp_path, language, catalog) -> None:
    html = HtmlDigestWriter(tmp_path, language="en").render_raw(DATE, "morning", [], language)

    assert _settings_link(html) == _text(catalog, "site.settings")
    assert _html_lang(html) == catalog


def test_an_issue_stored_with_no_language_takes_the_current_setting(tmp_path) -> None:
    html = HtmlDigestWriter(tmp_path, language="zh-Hant").render(_content(""))

    assert _settings_link(html) == _text("zh-Hant", "site.settings")
    assert _html_lang(html) == "zh-Hant"


@pytest.mark.parametrize(("language", "catalog"), LANGUAGES)
def test_the_archive_follows_the_current_setting(tmp_path, language, catalog) -> None:
    html = HtmlDigestWriter(tmp_path, language=language).render_index([f"{DATE}-morning.html"])

    assert _settings_link(html) == _text(catalog, "site.settings")
    assert _html_lang(html) == catalog


@pytest.mark.parametrize(("language", "catalog"), LANGUAGES)
def test_the_mails_read_online_follows_the_issue(language, catalog) -> None:
    html = render_digest_email(_content(language), digest_url=DIGEST_URL)

    button = re.search(r'<a class="btn primary" href="([^"]*)">([^<]*)</a>', html)
    assert button.groups() == (DIGEST_URL, _text(catalog, "email.read_online"))
    assert _html_lang(html) == catalog


def test_the_mails_design_tokens_still_reach_its_layout() -> None:
    html = render_digest_email(_content("zh-Hant"), digest_url=DIGEST_URL)

    assert re.search(r"\.wrap \{ max-width: [^;{}]+;", html)
    assert "{{" not in html


@pytest.mark.parametrize(("language", "catalog"), LANGUAGES)
def test_the_discord_period_follows_the_issue(language, catalog) -> None:
    stats = build_discord_embeds(_content(language), digest_url=DIGEST_URL)[-1]

    assert stats["title"] == f"{_text(catalog, 'period.morning')} {DATE}"


def test_the_discord_period_outside_the_schedules_labels_shows_as_stored() -> None:
    content = _content("zh-Hant").model_copy(update={"period": "noon"})

    assert build_discord_embeds(content)[-1]["title"] == f"noon {DATE}"


@pytest.mark.parametrize(("language", "catalog"), LANGUAGES)
def test_settings_carries_its_scripts_message_in_the_saved_language(language, catalog) -> None:
    html = _serve(TriageServer(values={"digest.output_language": language}), "/settings")

    assert _page_catalog(html) == {
        "settings.checking_discord": {
            "tag": catalog,
            "text": _text(catalog, "settings.checking_discord"),
        }
    }
    assert _settings_link(html) == _text(catalog, "site.settings")
    assert _html_lang(html) == catalog


@pytest.mark.parametrize(("language", "catalog"), LANGUAGES)
def test_a_write_with_no_store_is_refused_in_the_saved_language(language, catalog) -> None:
    body = _refusal(TriageServer(values={"digest.output_language": language}))

    assert body == {"ok": False, "error": _text(catalog, "error.no_settings_store")}


@pytest.mark.parametrize(
    ("header", "catalog"), [("zh-TW,zh;q=0.9,en;q=0.8", "zh-Hant"), ("fr", "en"), ("", "en")]
)
def test_with_no_saved_language_settings_follows_the_browser(header, catalog) -> None:
    server = TriageServer(values={})
    headers = {"Accept-Language": header} if header else {}

    html = _serve(server, "/settings", headers)

    assert _html_lang(html) == catalog
    assert _settings_link(html) == _text(catalog, "site.settings")
    assert _page_catalog(html)["settings.checking_discord"]["tag"] == catalog
    assert _refusal(server, headers)["error"] == _text(catalog, "error.no_settings_store")


def test_a_saved_language_outranks_the_browsers() -> None:
    server = TriageServer(values={"digest.output_language": "en"})

    html = _serve(server, "/settings", {"Accept-Language": "zh-TW"})

    assert _html_lang(html) == "en"


def test_a_legacy_language_name_is_worded_in_english_not_by_the_browser() -> None:
    server = TriageServer(values={"digest.output_language": "Traditional Chinese"})

    html = _serve(server, "/settings", {"Accept-Language": "zh-TW"})

    assert _html_lang(html) == "en"


class _Settings:
    def set(self, values) -> None:
        pass


def test_a_saved_language_takes_effect_on_the_next_request() -> None:
    server = TriageServer(settings=_Settings(), values={"digest.output_language": "en"})
    assert _html_lang(_serve(server, "/labels")) == "en"

    assert server._store({"digest.output_language": "zh-Hant"}) is None

    assert _html_lang(_serve(server, "/labels")) == "zh-Hant"
    assert _html_lang(_serve(server, "/settings")) == "zh-Hant"


# The browser formatter, run as the page runs it: `static/i18n.js` reads the page's block.
RUNNER = """
import { pathToFileURL } from "node:url";
const [module, catalog, calls] = process.argv.slice(2);
globalThis.document = {
  getElementById: (id) => (id === "i18n-catalog" ? { textContent: catalog } : null),
};
const { t } = await import(pathToFileURL(module).href);
const results = JSON.parse(calls).map(([key, values]) => {
  try {
    return { text: t(key, values) };
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


@pytest.mark.parametrize(("language", "catalog"), LANGUAGES)
def test_the_settings_script_formats_its_message_as_python_words_it(
    tmp_path, language, catalog
) -> None:
    html = _serve(TriageServer(values={"digest.output_language": language}), "/settings")

    results = _format_in_browser(tmp_path, _page_catalog(html), [["settings.checking_discord", {}]])

    assert results == [{"text": _text(catalog, "settings.checking_discord")}]


def test_the_browser_formatter_throws_on_a_missing_variable_and_isolates_nothing(
    tmp_path,
) -> None:
    catalog = {"a.hello": {"tag": "en", "text": "Hello {$name}"}}

    results = _format_in_browser(
        tmp_path, catalog, [["a.hello", {}], ["a.hello", {"name": "Ada"}], ["a.absent", {}]]
    )

    assert "name" in results[0]["error"]
    assert results[1] == {"text": "Hello Ada"}
    assert "a.absent" in results[2]["error"]
