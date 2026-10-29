"""L4: the remaining news, grouped into units and written as paragraphs per area."""

import json
import math
import re

import pytest
from fakes import FakeLLM

from cyris.domain.area_layout import layout_by_area
from cyris.domain.models import DigestContent, DigestItem, DigestSection, UsageStats
from cyris.domain.news_units import UNITS_PER_BATCH
from cyris.domain.selection import digest_urls
from cyris.service_layer.news_paragraphs import write_news_paragraphs

pytestmark = pytest.mark.unit

GEMINI = "gemini-embedding-001"


def _headline(key: str, area: str | None = "World", summary: str = "") -> DigestItem:
    return DigestItem(
        title=f"T-{key}",
        summary=summary or f"S-{key}.",
        sources=[f"Src-{key}"],
        urls=[f"https://news.test/{key}"],
        area=area,
    )


def _content(area_order=("World", "Tech"), **fields) -> DigestContent:
    return DigestContent(
        date="2026-10-27",
        period="morning",
        sources_processed=1,
        articles_received=0,
        articles_included=sum(len(i.urls) for i in fields.get("filtered_headlines", []))
        + sum(len(i.urls) for s in fields.get("news_clusters", []) for i in s.items),
        area_order=list(area_order),
        **fields,
    )


def _angle(degrees: float) -> list[float]:
    return [math.cos(math.radians(degrees)), math.sin(math.radians(degrees))]


class FakeEmbedder:
    def __init__(
        self, by_text: dict[str, list[float]] | None = None, error: Exception | None = None
    ):
        self.by_text = by_text or {}
        self.error = error
        self.usage = None
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if self.error:
            raise self.error
        return [self.by_text.get(t, _angle(90 + 7 * i)) for i, t in enumerate(texts)]


def _every_unit_in_one_paragraph(n: int, per: int = 2) -> str:
    return json.dumps(
        {
            "paragraphs": [
                {"text": f"para {start}", "units": list(range(start, min(start + per, n)))}
                for start in range(0, n, per)
            ]
        }
    )


def _unit_count(prompt: str) -> int:
    return len(re.findall(r"^\[\d+\] ", prompt, re.MULTILINE))


class Answering(FakeLLM):
    """Cites every unit of each prompt, two to a paragraph."""

    async def complete(self, prompt, **kwargs):
        self._responses = [_every_unit_in_one_paragraph(_unit_count(prompt))]
        return await super().complete(prompt, **kwargs)


async def _write(content, llm, **overrides):
    kwargs = {
        "embedder": None,
        "embedding_model": GEMINI,
        "known_vectors": {},
        "titles": {},
        "preference": None,
        "units_per_paragraph": 5,
        "output_language": "en",
        "style_prompt": "",
        "usage": UsageStats(),
    }
    kwargs.update(overrides)
    return await write_news_paragraphs(content, llm, **kwargs)


def _cited_urls(content: DigestContent) -> list[str]:
    return [u for p in content.news_paragraphs for unit in p.units for u in unit.urls]


# ---- criterion 1: every unit of an area is cited, before any cut ---------------


async def test_every_unit_is_cited_exactly_once_even_when_the_model_skips_or_repeats() -> None:
    content = _content(filtered_headlines=[_headline(k) for k in "abcd"])
    llm = FakeLLM(
        json.dumps(
            {
                "paragraphs": [
                    {"text": "one", "units": [0, 2, 2, 99, "x"]},
                    {"text": "again", "units": [0]},
                ]
            }
        )
    )
    out, report = await _write(content, llm)
    urls = _cited_urls(out)
    assert sorted(urls) == sorted(f"https://news.test/{k}" for k in "abcd")
    assert len(urls) == len(set(urls))
    # The model's paragraph first, then one of its own for each unit it left out.
    assert [p.text for p in out.news_paragraphs] == ["one", "S-b.", "S-d."]
    assert report.uncited == 2


async def test_cluster_sections_are_units_and_their_members_stay_together() -> None:
    cluster = DigestSection(
        heading="Quake",
        items=[
            DigestItem(
                title="Quake",
                summary="A quake.",
                sources=["A", "B"],
                urls=["https://news.test/q1", "https://news.test/q2"],
                area="World",
            )
        ],
    )
    content = _content(news_clusters=[cluster], filtered_headlines=[_headline("h")])
    out, _ = await _write(content, Answering())
    (paragraph,) = out.news_paragraphs
    assert [u.urls for u in paragraph.units] == [
        ["https://news.test/q1", "https://news.test/q2"],
        ["https://news.test/h"],
    ]


async def test_the_content_is_the_same_articles_with_no_row_or_cluster_left() -> None:
    cluster = DigestSection(
        heading="C",
        items=[DigestItem(title="C", summary="", sources=["A"], urls=["https://c"], area="Tech")],
    )
    content = _content(
        news_clusters=[cluster], filtered_headlines=[_headline(k, "World") for k in "ab"]
    )
    out, _ = await _write(content, Answering())
    assert out.filtered_headlines == []
    assert out.news_clusters == []
    assert digest_urls(out) == digest_urls(content)
    assert out.articles_included == content.articles_included
    # criterion 2: no section of the layout holds a row of the old headline form.
    assert all(
        row.kind != "wire" for section in layout_by_area(out).sections for row in section.rows
    )
    assert content.filtered_headlines, "the input is not changed in place"


async def test_each_area_is_written_apart_and_placed_in_its_own_section() -> None:
    content = _content(
        filtered_headlines=[_headline("w1", "World"), _headline("t1", "Tech"), _headline("w2")]
    )
    llm = Answering()
    out, report = await _write(content, llm)
    assert len(llm.calls) == 2
    world, tech = layout_by_area(out).sections
    assert [u.urls[0] for p in world.paragraphs for u in p.units] == [
        "https://news.test/w1",
        "https://news.test/w2",
    ]
    assert [u.urls[0] for p in tech.paragraphs for u in p.units] == ["https://news.test/t1"]
    assert report.areas == {
        "World": {"units": 2, "calls": 1, "uncited": 0},
        "Tech": {"units": 1, "calls": 1, "uncited": 0},
    }


# ---- criterion 4: calls per issue ≤ Σ ceil(units / 40) -------------------------


async def test_calls_are_one_per_forty_units_of_each_area() -> None:
    world = [_headline(f"w{n}", "World") for n in range(UNITS_PER_BATCH + 5)]
    tech = [_headline(f"t{n}", "Tech") for n in range(3)]
    llm = Answering()
    out, report = await _write(_content(filtered_headlines=world + tech), llm)
    assert len(llm.calls) == 3 == report.calls
    assert [_unit_count(c["prompt"]) for c in llm.calls] == [40, 5, 3]
    assert len(_cited_urls(out)) == 48


async def test_the_target_and_the_language_reach_the_prompt() -> None:
    llm = Answering()
    await _write(
        _content(filtered_headlines=[_headline("a")]),
        llm,
        units_per_paragraph=3,
        style_prompt="Be terse.",
    )
    (call,) = llm.calls
    assert "about 3 units per paragraph" in call["prompt"]
    assert "[0] (Src-a) T-a: S-a." in call["prompt"]
    assert "English" in call["system"] or "en" in call["system"]
    assert "Be terse." in call["system"]


# ---- the paragraph step without an answer --------------------------------------


async def test_a_failed_batch_gives_each_of_its_units_a_paragraph_and_others_still_run() -> None:
    class FailFirst(Answering):
        async def complete(self, prompt, **kwargs):
            if not self.calls:
                self.calls.append({"prompt": prompt})
                raise RuntimeError("boom")
            return await super().complete(prompt, **kwargs)

    content = _content(
        filtered_headlines=[_headline("w1", "World"), _headline("w2"), _headline("t1", "Tech")]
    )
    out, report = await _write(content, FailFirst())
    assert [p.text for p in out.news_paragraphs] == ["S-w1.", "S-w2.", "para 0"]
    assert report.failed_batches == 1


async def test_with_no_llm_each_unit_is_its_own_paragraph_and_no_call_is_made() -> None:
    out, report = await _write(_content(filtered_headlines=[_headline("a"), _headline("b")]), None)
    assert [p.text for p in out.news_paragraphs] == ["S-a.", "S-b."]
    assert report.calls == 0


async def test_an_issue_with_no_remaining_news_is_returned_as_it_was() -> None:
    content = _content()
    llm = Answering()
    out, report = await _write(content, llm)
    assert out == content
    assert llm.calls == []
    assert report.units == 0


# ---- grouping ------------------------------------------------------------------


async def test_close_titles_become_one_unit_on_the_measured_model() -> None:
    known = {
        "https://news.test/a": _angle(0),
        "https://news.test/b": _angle(80),
        "https://news.test/c": _angle(20),
    }
    llm = Answering()
    out, report = await _write(
        _content(filtered_headlines=[_headline(k) for k in "abc"]), llm, known_vectors=known
    )
    assert _unit_count(llm.calls[0]["prompt"]) == 2
    (paragraph,) = out.news_paragraphs
    assert [u.urls for u in paragraph.units] == [
        ["https://news.test/a", "https://news.test/c"],
        ["https://news.test/b"],
    ]
    assert report.grouping == "cosine"
    assert report.units == 2


async def test_titles_the_run_did_not_embed_are_embedded_once_by_their_own_title() -> None:
    embedder = FakeEmbedder({"orig-a": _angle(0), "orig-b": _angle(5)})
    llm = Answering()
    await _write(
        _content(filtered_headlines=[_headline("a"), _headline("b"), _headline("c")]),
        llm,
        embedder=embedder,
        known_vectors={"https://news.test/c": _angle(90)},
        titles={"https://news.test/a": "orig-a", "https://news.test/b": "orig-b"},
    )
    assert embedder.calls == [["orig-a", "orig-b"]]
    assert _unit_count(llm.calls[0]["prompt"]) == 2


async def test_another_embedding_model_groups_nothing_and_says_why() -> None:
    known = {"https://news.test/a": _angle(0), "https://news.test/b": _angle(1)}
    llm = Answering()
    _, report = await _write(
        _content(filtered_headlines=[_headline("a"), _headline("b")]),
        llm,
        known_vectors=known,
        embedding_model="@cf/baai/bge-m3",
    )
    assert _unit_count(llm.calls[0]["prompt"]) == 2
    assert report.grouping == "no grouping cosine measured for @cf/baai/bge-m3"


async def test_a_failed_embedding_groups_nothing_and_the_paragraphs_still_come() -> None:
    llm = Answering()
    out, report = await _write(
        _content(filtered_headlines=[_headline("a"), _headline("b")]),
        llm,
        embedder=FakeEmbedder(error=RuntimeError("down")),
        titles={"https://news.test/a": "a", "https://news.test/b": "b"},
    )
    assert report.grouping == "embedding failed: down"
    assert len(_cited_urls(out)) == 2


async def test_with_no_embedder_and_no_vectors_nothing_is_grouped() -> None:
    _, report = await _write(_content(filtered_headlines=[_headline("a")]), Answering())
    assert report.grouping == "no embedder"


# ---- the vote order over units (ADR-0022) ---------------------------------------


async def test_units_are_ordered_by_their_best_members_preference_before_batching() -> None:
    known = {
        "https://news.test/a": _angle(0),
        "https://news.test/b": _angle(80),
        "https://news.test/c": _angle(20),
    }
    llm = Answering()
    _, report = await _write(
        _content(filtered_headlines=[_headline(k) for k in "abc"]),
        llm,
        known_vectors=known,
        # c pulls its unit {a, c} up only through its own score; b is judged lower.
        preference={
            "https://news.test/a": -0.5,
            "https://news.test/b": 0.1,
            "https://news.test/c": 0.4,
        },
    )
    prompt = llm.calls[0]["prompt"]
    assert prompt.index("T-a") < prompt.index("T-b")
    assert report.moved == 0

    llm = Answering()
    _, report = await _write(
        _content(filtered_headlines=[_headline(k) for k in "abc"]),
        llm,
        known_vectors=known,
        preference={
            "https://news.test/a": -0.5,
            "https://news.test/b": 0.3,
            "https://news.test/c": 0.1,
        },
    )
    prompt = llm.calls[0]["prompt"]
    assert prompt.index("T-b") < prompt.index("T-a")
    assert report.moved == 2


async def test_with_no_votes_the_units_keep_their_incoming_order() -> None:
    llm = Answering()
    await _write(_content(filtered_headlines=[_headline(k) for k in "cab"]), llm, preference={})
    prompt = llm.calls[0]["prompt"]
    assert prompt.index("T-c") < prompt.index("T-a") < prompt.index("T-b")


async def test_the_order_decides_which_units_share_the_first_batch() -> None:
    headlines = [_headline(f"h{n:02}") for n in range(UNITS_PER_BATCH + 1)]
    first, last = headlines[0].urls[0], headlines[-1].urls[0]
    llm = Answering()
    # Unjudged units keep their slots; the two judged ones trade theirs.
    await _write(_content(filtered_headlines=headlines), llm, preference={first: 0.0, last: 1.0})
    assert "T-h40" in llm.calls[0]["prompt"]
    assert "T-h00" in llm.calls[1]["prompt"]


# ---- the order survives to the quota (ADR-0022) ----------------------------------


class Reversing(FakeLLM):
    """One paragraph per unit, most important first by its own lights: the last unit
    leads. With `skip_first` it leaves the first unit uncited."""

    def __init__(self, skip_first: bool = False):
        super().__init__()
        self.skip_first = skip_first

    async def complete(self, prompt, **kwargs):
        n = _unit_count(prompt)
        cited = range(1 if self.skip_first else 0, n)
        self._responses = [
            json.dumps({"paragraphs": [{"text": f"p{i}", "units": [i]} for i in reversed(cited)]})
        ]
        return await super().complete(prompt, **kwargs)


def _eight_with_h7_liked() -> tuple[DigestContent, dict[str, float]]:
    headlines = [_headline(f"h{n}") for n in range(8)]
    preference = {h.urls[0]: 0.0 for h in headlines}
    preference[headlines[7].urls[0]] = 1.0
    return _content(filtered_headlines=headlines), preference


def _paragraph_titles(content: DigestContent) -> list[list[str]]:
    return [[u.title for u in p.units] for p in content.news_paragraphs]


@pytest.mark.parametrize("skip_first", [False, True], ids=["model-reorders", "model-skips"])
async def test_a_liked_unit_leads_whatever_order_the_model_writes(skip_first) -> None:
    content, preference = _eight_with_h7_liked()
    out, _ = await _write(content, Reversing(skip_first), preference=preference)

    assert _paragraph_titles(out) == [[f"T-h{n}"] for n in (7, 0, 1, 2, 3, 4, 5, 6)]


async def test_with_no_votes_the_paragraphs_keep_the_incoming_unit_order() -> None:
    content, _ = _eight_with_h7_liked()
    out, _ = await _write(content, Reversing(), preference={})
    assert _paragraph_titles(out) == [[f"T-h{n}"] for n in range(8)]


async def test_a_paragraph_counts_the_digest_items_its_units_stand_for() -> None:
    """A cluster is one item however many reports it holds; a cosine group is one per headline."""
    cluster = DigestSection(
        heading="Quake",
        items=[
            DigestItem(
                title="Quake",
                summary="s",
                sources=["A", "B"],
                urls=["https://news.test/qa", "https://news.test/qb"],
                area="World",
            )
        ],
    )
    known = {"https://news.test/a": _angle(0), "https://news.test/c": _angle(20)}
    out, _ = await _write(
        _content(news_clusters=[cluster], filtered_headlines=[_headline("a"), _headline("c")]),
        None,
        known_vectors=known,
    )
    assert [(len(p.units[0].urls), p.items) for p in out.news_paragraphs] == [(2, 1), (2, 2)]
