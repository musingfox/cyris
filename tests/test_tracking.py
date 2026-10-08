"""Topic tracking: a reader's sentence against every candidate title, no LLM."""

from datetime import UTC, datetime

import pytest

from cyris.domain.models import StoredArticle, Tier, TrackedTopic
from cyris.domain.similarity import normalize
from cyris.domain.tracking import hits
from cyris.service_layer.tracking import track_topics

pytestmark = pytest.mark.unit

MODEL = "gemini-embedding-001"


def article(url: str, title: str) -> StoredArticle:
    return StoredArticle(
        url=url,
        original_id=url,
        title=title,
        content="c",
        source_name="Src",
        source_tier=Tier.FILTER,
        published_at=datetime(2026, 10, 8, tzinfo=UTC),
        first_seen_at=datetime(2026, 10, 8, tzinfo=UTC),
    )


def topic(name: str = "Anthropic", **overrides) -> TrackedTopic:
    fields = {
        "name": name,
        "description": "The company that makes Claude and publishes on AI safety",
        "threshold": 0.8,
        "model": MODEL,
        **overrides,
    }
    return TrackedTopic(**fields)


class TableEmbedder:
    """Embeds by a lookup table, so a vector's closeness owes nothing to shared words."""

    VECTORS = {
        "The company that makes Claude and publishes on AI safety": [1.0, 0.0, 0.0],
        # No word in common with the description, and close to it.
        "那家做 Constitutional AI 的公司發表新模型": [0.95, 0.31, 0.0],
        # Shares "AI" and "company" with it, and far from it.
        "AI company quarterly results for a chip maker": [0.1, 0.0, 0.99],
        "Lottery draw": [0.0, 1.0, 0.0],
    }

    def __init__(self) -> None:
        self.payloads: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.payloads.append(list(texts))
        return [normalize(self.VECTORS[t]) for t in texts]

    def embedded(self) -> list[str]:
        return [text for payload in self.payloads for text in payload]


CLOSE = article("https://a.test/close", "那家做 Constitutional AI 的公司發表新模型")
WORDY = article("https://a.test/wordy", "AI company quarterly results for a chip maker")
FAR = article("https://a.test/far", "Lottery draw")


# ---- the domain rule -----------------------------------------------------------


def test_a_candidate_at_the_threshold_hits_and_one_below_does_not() -> None:
    seed = [1.0, 0.0]
    candidates = {"at": [0.8, 0.6], "below": [0.79, 0.613], "far": [0.0, 1.0]}

    assert [url for url, _ in hits(seed, candidates, 0.8)] == ["at"]


def test_hits_come_nearest_first() -> None:
    seed = [1.0, 0.0]
    candidates = {"near": normalize([0.9, 0.1]), "nearest": [1.0, 0.0]}

    assert [url for url, _ in hits(seed, candidates, 0.5)] == ["nearest", "near"]


def test_a_candidate_with_no_vector_is_never_a_hit() -> None:
    assert hits([1.0, 0.0], {"empty": []}, 0.0) == []


# ---- the pass ------------------------------------------------------------------


async def test_a_description_hits_a_title_close_in_meaning_not_in_words() -> None:
    report = await track_topics(TableEmbedder(), MODEL, [topic()], [CLOSE, WORDY, FAR])

    [section] = report.sections
    assert section.heading == "Anthropic"
    assert [item.urls for item in section.items] == [[CLOSE.url]]
    assert section.items[0].title == CLOSE.title
    assert report.hits == {"Anthropic": 1}
    assert report.skipped == {}


async def test_a_topic_set_for_another_model_never_hits_and_is_named() -> None:
    embedder = TableEmbedder()
    other = topic("Other", model="@cf/baai/bge-m3", threshold=0.1)

    report = await track_topics(embedder, MODEL, [other], [CLOSE, WORDY, FAR])

    assert report.sections == []
    assert report.hits == {}
    assert "@cf/baai/bge-m3" in report.skipped["Other"]
    assert MODEL in report.skipped["Other"]
    assert embedder.payloads == []


async def test_only_the_calibrated_topic_is_embedded_beside_a_skipped_one() -> None:
    embedder = TableEmbedder()
    other = topic("Other", model="@cf/baai/bge-m3", description="Lottery draw")

    report = await track_topics(embedder, MODEL, [topic(), other], [CLOSE])

    assert set(report.skipped) == {"Other"}
    assert report.hits == {"Anthropic": 1}
    assert embedder.embedded().count("Lottery draw") == 0


async def test_a_topic_with_no_hit_adds_no_section() -> None:
    report = await track_topics(TableEmbedder(), MODEL, [topic()], [WORDY, FAR])

    assert report.sections == []
    assert report.hits == {"Anthropic": 0}


async def test_titles_already_embedded_are_not_embedded_again() -> None:
    embedder = TableEmbedder()
    known = {CLOSE.url: normalize(TableEmbedder.VECTORS[CLOSE.title])}

    report = await track_topics(embedder, MODEL, [topic()], [CLOSE, FAR], known=known)

    assert report.hits == {"Anthropic": 1}
    assert embedder.embedded() == [topic().description, FAR.title]


async def test_descriptions_and_titles_go_out_in_one_payload() -> None:
    embedder = TableEmbedder()
    second = topic("Lotteries", description="Lottery draw", threshold=0.9)

    await track_topics(embedder, MODEL, [topic(), second], [CLOSE, WORDY])

    assert embedder.payloads == [[topic().description, "Lottery draw", CLOSE.title, WORDY.title]]


async def test_no_topics_embeds_nothing() -> None:
    embedder = TableEmbedder()

    report = await track_topics(embedder, MODEL, [], [CLOSE])

    assert (report.sections, report.hits, report.skipped) == ([], {}, {})
    assert embedder.payloads == []


async def test_a_failed_embedding_skips_every_topic_and_names_why() -> None:
    class Broken:
        async def embed(self, texts):
            raise RuntimeError("429 forever")

    report = await track_topics(Broken(), MODEL, [topic()], [CLOSE])

    assert report.sections == []
    assert report.skipped == {"Anthropic": "embedding failed: 429 forever"}


async def test_a_short_embedding_answer_skips_rather_than_mispairs() -> None:
    class Short(TableEmbedder):
        async def embed(self, texts):
            return (await super().embed(texts))[:-1]

    report = await track_topics(Short(), MODEL, [topic()], [CLOSE, FAR])

    assert report.sections == []
    assert report.skipped["Anthropic"].startswith("embedding failed")


# ---- the topic itself ------------------------------------------------------------


@pytest.mark.parametrize(
    "field,value",
    [("name", " "), ("description", ""), ("model", ""), ("threshold", 1.5), ("threshold", -0.1)],
)
def test_a_topic_needs_every_field_in_range(field: str, value) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        topic(**{field: value})


def test_a_topic_has_no_threshold_or_model_of_its_own() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as exc:
        TrackedTopic(name="A", description="B")

    assert {err["loc"][0] for err in exc.value.errors()} == {"threshold", "model"}
