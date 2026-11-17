"""The names the owner gave the archive, the third layer, a suppression and an issue.

Each renders in English and zh-Hant on the surfaces that show it; what the code stores, the
period, the file names and the links, keeps its English identifier.
"""

import json
import re
from datetime import UTC, datetime, timedelta, timezone
from html.parser import HTMLParser

import pytest

from cyris.adapters.mail import build_digest_mail
from cyris.adapters.notify import build_discord_embeds
from cyris.adapters.output.email_digest import render_digest_email
from cyris.adapters.output.html_digest import HtmlDigestWriter
from cyris.adapters.run_alert import alert_message
from cyris.domain.interest_graph import InterestEdge, InterestGraph, InterestNode
from cyris.domain.models import ArticleState, DigestContent, DigestItem, Lane, StoredArticle
from cyris.service_layer.alerts import RunAlert

pytestmark = pytest.mark.unit

ONLINE = "https://digest.test/2026-11-17-morning"


def _content(language: str, **overrides) -> DigestContent:
    fields = {
        "date": "2026-11-17",
        "period": "morning",
        "sources_processed": 2,
        "articles_received": 3,
        "articles_included": 1,
        "output_language": language,
    }
    return DigestContent(**(fields | overrides))


# ---- the archive -----------------------------------------------------------------------


@pytest.mark.parametrize(("language", "name"), [("en", "Archive"), ("zh-Hant", "過往精選")])
def test_the_archive_is_named_in_its_language(language, name, tmp_path) -> None:
    index = HtmlDigestWriter(tmp_path, language=language).render_index(["2026-11-17-morning.html"])
    digest = HtmlDigestWriter(tmp_path).render(_content(language))

    assert f"<title>CYRIS // {name}</title>" in index
    assert f'<span class="label">{name}</span>' in index
    assert f'aria-current="page">{name}</a>' in index
    assert f'href="index.html" aria-current="page">{name}</a>' in digest


@pytest.mark.parametrize(("language", "name"), [("en", "Archive"), ("zh-Hant", "過往精選")])
def test_the_mail_links_the_archive_by_its_name(language, name) -> None:
    html = render_digest_email(_content(language), ONLINE)

    assert f'<a href="https://digest.test/">{name}</a>' in html


# ---- the third layer -------------------------------------------------------------------


def _glance(n: int) -> list[DigestItem]:
    return [
        DigestItem(title=f"Row {i}", summary="", sources=["A"], urls=[f"https://g.test/{i}"])
        for i in range(n)
    ]


@pytest.mark.parametrize(
    ("language", "name", "one", "many"),
    [
        ("en", "Glance", "1 at a glance", "12 at a glance"),
        ("zh-Hant", "速覽", "1 則速覽", "12 則速覽"),
    ],
)
def test_the_third_layer_is_glance(language, name, one, many, tmp_path) -> None:
    writer = HtmlDigestWriter(tmp_path)

    html = writer.render(_content(language, filtered_headlines=_glance(12)))
    single = writer.render(_content(language, filtered_headlines=_glance(1)))

    assert f'<h2 class="section-tag"><span class="label">{name}</span></h2>' in html
    [words] = [json.loads(w) for w in re.findall(r"data-layer-words='([^']*)'", html)]
    assert words["count"] == many
    assert words["skip_label"].endswith(many)
    assert words["back"].endswith(name)
    [alone] = [json.loads(w) for w in re.findall(r"data-layer-words='([^']*)'", single)]
    assert alone["count"] == one
    for old in ("Headline", "快訊"):
        assert old not in html


# ---- a suppression on the interest map -------------------------------------------------


def _interest(language: str, tmp_path, *, lit: float, holds: bool) -> str:
    nodes = [InterestNode("代理", 2, 0, "up", lit), InterestNode("晶片", 0, 2, "down", 0.0)]
    graph = InterestGraph(nodes=nodes, edges=[InterestEdge("代理", "晶片")])
    return HtmlDigestWriter(tmp_path, language=language).render_interest(
        graph, holds_active=holds, generated_at="2026-11-17T08:00:00+08:00", clock_days=7
    )


def test_a_suppression_is_not_interested_in_zh_hant(tmp_path) -> None:
    lit = _interest("zh-Hant", tmp_path, lit=1.0, holds=True)
    elsewhere = _interest("zh-Hant", tmp_path, lit=0.0, holds=True)
    none = _interest("zh-Hant", tmp_path, lit=0.0, holds=False)

    assert "亮：標為沒興趣；越暗：剩餘時間越少；淡色：未標為沒興趣。" in lit
    assert "並標出每項沒興趣標記還剩多少時間。" in lit
    assert "目前有標為沒興趣的文章，但都不在這張地圖的標籤附近。" in elsewhere
    assert "目前沒有標為沒興趣的文章。" in none
    for page in (lit, elsewhere, none):
        assert "排除" not in page
        assert "壓制" not in page


def test_a_suppression_keeps_its_english_name(tmp_path) -> None:
    lit = _interest("en", tmp_path, lit=1.0, holds=True)

    assert "Lit = suppressed now; dimmer = less time left; half colour = not suppressed." in lit
    assert "No suppression is active." in _interest("en", tmp_path, lit=0.0, holds=False)


# ---- an issue is named by the hour its run started -------------------------------------

# 00:05 UTC is 08:05 in Asia/Taipei, the offset a run stores its start at.
STARTED = datetime(2026, 11, 17, 8, 5, tzinfo=timezone(timedelta(hours=8)))
# One form in every catalog: the day, a space, the hour rounded down with `00` after it.
NAMES = {"en": "11/17 0800", "zh-Hant": "11/17 0800"}
# The words a reader once knew an issue's period by, in every catalog.
PERIOD_WORDS = re.compile(r"morning|evening|早報|晚報", re.IGNORECASE)


def _raw_article() -> StoredArticle:
    at = datetime(2026, 11, 17, tzinfo=UTC)
    return StoredArticle(
        url="https://r.test/1",
        original_id=1,
        title="Kept",
        content="x",
        published_at=at,
        source_name="A",
        source_lane=Lane.SKIM,
        state=ArticleState.ACCEPTED,
        first_seen_at=at,
    )


def _pages(language: str, tmp_path, started_at: datetime | None) -> dict[str, str]:
    """Every page and message that names one issue, worded in `language`."""
    content = _content(language, started_at=started_at)
    writer = HtmlDigestWriter(tmp_path, language=language)
    subject, text = build_digest_mail(content, ONLINE, raw_page=True)
    return {
        "digest": writer.render(content, raw_page=True),
        "raw": writer.render_raw(
            content.date, content.period, [_raw_article()], language, started_at=started_at
        ),
        "archive": writer.render_index(
            ["2026-11-17-morning.html", "2026-11-17-morning-raw.html", "2026-11-16-evening.html"],
            content=content,
            starts={("2026-11-16", "evening"): STARTED - timedelta(hours=12)},
        ),
        "mail": render_digest_email(content, ONLINE, raw_page=True),
        "subject": subject,
        "text": text,
        "discord": build_discord_embeds(content, ONLINE)[-1]["title"],
    }


@pytest.mark.parametrize("language", ["en", "zh-Hant"])
def test_every_surface_names_the_issue_by_its_hour(language, tmp_path) -> None:
    name = NAMES[language]

    pages = _pages(language, tmp_path, STARTED)

    assert f"<title>CYRIS // {name}</title>" in pages["digest"]
    assert f"<title>CYRIS // {name} · " in pages["raw"]
    for page in ("digest", "raw"):
        assert '<span class="data">11/17</span><span class="label">0800</span>' in pages[page]
    assert '<span class="date">11/17</span><span class="label">0800</span>' in pages["archive"]
    assert (
        '<span class="date">11/16</span>\n                <span class="label">2000</span>'
        in (pages["archive"])
    )
    assert f"<title>CYRIS // {name}</title>" in pages["mail"]
    assert f'<h1 class="issue-title">{name}</h1>' in pages["mail"]
    assert pages["discord"] == name


def test_the_issue_is_named_in_each_catalogs_sentences(tmp_path) -> None:
    en, zh = _pages("en", tmp_path, STARTED), _pages("zh-Hant", tmp_path, STARTED)

    assert (
        'content="The 11/17 0800 issue of a Cyris digest: 1 article selected from 3."'
        in en["digest"]
    )
    assert 'content="Cyris 11/17 0800 精選：從 3 篇文章選出 1 篇。"' in zh["digest"]
    assert (
        'content="Every article the 11/17 0800 issue of a Cyris digest judged, grouped by'
        ' source."' in en["raw"]
    )
    assert 'content="Cyris 11/17 0800 精選評選過的所有文章，依來源分組。"' in zh["raw"]
    assert en["subject"] == "Digest for 11/17 0800"
    assert zh["subject"] == "11/17 0800 精選"


@pytest.mark.parametrize("language", ["en", "zh-Hant"])
def test_an_issue_stored_without_its_start_is_named_by_its_date_alone(language, tmp_path) -> None:
    pages = _pages(language, tmp_path, None)

    assert "<title>CYRIS // 11/17</title>" in pages["digest"]
    assert '<span class="data">11/17</span></div>' in pages["digest"]
    assert '<span class="date">11/17</span></div>' in pages["archive"]
    assert '<h1 class="issue-title">11/17</h1>' in pages["mail"]
    assert pages["discord"] == "11/17"
    assert pages["subject"] == {"en": "Digest for 11/17", "zh-Hant": "11/17 精選"}[language]


def _alert(language: str) -> RunAlert:
    return RunAlert(
        kind="run_failed",
        period="evening",
        started_at=datetime(2026, 11, 17, 12, 5, tzinfo=UTC),
        timezone="Asia/Taipei",
        language=language,
        error="RuntimeError: x",
    )


def test_a_run_alert_names_its_issue_in_its_catalogs_punctuation() -> None:
    assert alert_message(_alert("en"))[0] == "Digest run failed: 11/17 2000 Asia/Taipei"
    assert alert_message(_alert("zh-Hant"))[0] == "精選產生失敗：11/17 2000 Asia/Taipei"


@pytest.mark.parametrize(
    ("hour", "minute", "named"), [(8, 5, "0800"), (20, 59, "2000"), (0, 0, "0000"), (23, 1, "2300")]
)
@pytest.mark.parametrize("language", ["en", "zh-Hant"])
def test_the_name_rounds_the_start_down_to_its_hour(
    language, hour, minute, named, tmp_path
) -> None:
    started = datetime(2026, 11, 17, hour, minute, 30, tzinfo=timezone(timedelta(hours=8)))
    content = _content(language, started_at=started)

    subject, _ = build_digest_mail(content)

    assert build_discord_embeds(content)[-1]["title"] == f"11/17 {named}"
    assert (
        subject == {"en": f"Digest for 11/17 {named}", "zh-Hant": f"11/17 {named} 精選"}[language]
    )
    assert DigestContent.model_validate_json(content.model_dump_json()).started_at == started


class _Read(HTMLParser):
    """What a reader reads or hears on a page: text outside scripts and styles, the worded
    attributes, the description and the layer script's words. Never a link or a receipt."""

    WORDED = {"title", "aria-label", "alt", "data-layer-words"}

    def __init__(self) -> None:
        super().__init__()
        self.runs: list[str] = []
        self.inside: list[str] = []

    def handle_starttag(self, tag, attrs) -> None:
        self.runs += [value for name, value in attrs if name in self.WORDED and value]
        values = dict(attrs)
        if tag == "meta" and values.get("name") == "description":
            self.runs.append(values["content"])
        if tag in ("script", "style"):
            self.inside.append(tag)

    def handle_endtag(self, tag) -> None:
        if self.inside and tag == self.inside[-1]:
            self.inside.pop()

    def handle_data(self, data) -> None:
        if not self.inside:
            self.runs.append(data)


def _read(page: str) -> str:
    if not page.lstrip().startswith("<"):
        return re.sub(r"https://\S+", "", page)
    parser = _Read()
    parser.feed(page)
    return "\n".join(parser.runs)


@pytest.mark.parametrize("started_at", [STARTED, None], ids=["with-start", "without"])
@pytest.mark.parametrize("language", ["en", "zh-Hant"])
def test_no_surface_shows_a_reader_the_period(language, started_at, tmp_path) -> None:
    pages = _pages(language, tmp_path, started_at)
    alert, _ = alert_message(_alert(language))

    shown = {name: PERIOD_WORDS.findall(_read(page)) for name, page in pages.items()}

    assert shown == dict.fromkeys(pages, [])
    assert PERIOD_WORDS.findall(alert) == []


def test_the_period_word_check_finds_one_on_a_page() -> None:
    page = (
        '<meta name="description" content="a morning issue">'
        '<meta name="cyris-issue" content="x-evening">'
        '<p>Evening</p><a href="x-morning.html">晚報</a>'
    )

    assert PERIOD_WORDS.findall(_read(page)) == ["morning", "Evening", "晚報"]


# ---- what the code stores keeps its English identifier ---------------------------------


@pytest.mark.parametrize("language", ["en", "zh-Hant"])
def test_the_period_still_files_and_links_the_issue(language, tmp_path) -> None:
    content = _content(language, started_at=STARTED)
    pages = _pages(language, tmp_path, STARTED)

    assert HtmlDigestWriter.digest_filename(content.date, content.period) == (
        "2026-11-17-morning.html"
    )
    assert HtmlDigestWriter.raw_filename(content.date, content.period) == (
        "2026-11-17-morning-raw.html"
    )
    for page in ("digest", "raw"):
        assert '<a href="2026-11-17-morning.html"' in pages[page]
        assert '<a href="2026-11-17-morning-raw.html"' in pages[page]
    assert 'href="2026-11-17-morning.html"' in pages["archive"]
    assert 'href="2026-11-16-evening.html"' in pages["archive"]
    assert "https://digest.test/2026-11-17-morning-raw" in pages["mail"]
    stored = json.loads(content.model_dump_json())
    assert stored["period"] == "morning"
    assert stored["started_at"] == "2026-11-17T08:05:00+08:00"
    assert DigestContent.model_validate_json(content.model_dump_json()).started_at == STARTED


def test_an_issue_stored_before_its_start_was_kept_still_loads() -> None:
    older = json.dumps(
        {
            "date": "2026-11-16",
            "period": "evening",
            "sources_processed": 1,
            "articles_received": 1,
            "articles_included": 1,
        }
    )

    loaded = DigestContent.model_validate_json(older)

    assert loaded.started_at is None
    assert loaded.period == "evening"
