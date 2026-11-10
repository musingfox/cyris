"""The approach the feasibility rounds left standing (ADR-0023, *Known weakness*).

Topic assignment is the LLM's alone, over a recent-topics list. Handing it embedding
candidates dropped pairwise precision to 0.34-0.69, and an end-of-issue call to merge
synonymous topics returned nothing in 8 of 9 calls. Recall is `topic-assignment-eval`'s to
improve, which may also put the owner's granularity examples in the prompt: until then a
change that brings any of these back fails here, and has to answer those numbers.
"""

import ast
import importlib.util
import inspect
import json
from pathlib import Path

import pytest

from cyris.adapters.store import interest
from cyris.domain import topics
from cyris.domain.models import ProcessResult
from cyris.service_layer import digest_pipeline, filtering, prompts, run_digest, scoring

pytestmark = [pytest.mark.unit, pytest.mark.guard]

ASSIGNMENT_MODULES = [filtering, topics, digest_pipeline]
ROOT = Path(__file__).parent.parent
ARCHITECTURE = (ROOT / "docs/architecture.md").read_text(encoding="utf-8")
ONBOARDING_FEEDS = ROOT / "src/cyris/onboarding_feeds.json"


def _imported_modules(module) -> set[str]:
    tree = ast.parse(inspect.getsource(module))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            names |= {node.module or ""}
            names |= {f"{node.module}.{alias.name}" for alias in node.names}
    return names


@pytest.mark.parametrize("module", ASSIGNMENT_MODULES, ids=lambda m: m.__name__)
def test_assignment_reaches_no_embedding_or_similarity_code(module) -> None:
    for name in _imported_modules(module):
        assert "embed" not in name.casefold(), name
        assert "similarity" not in name.casefold(), name


@pytest.mark.parametrize(
    "func",
    [
        filtering.filter_articles,
        filtering.sections_by_topic,
        digest_pipeline.DigestPipeline.process,
    ],
    ids=lambda f: f.__qualname__,
)
def test_assignment_is_handed_no_embedder_or_candidates(func) -> None:
    for name in inspect.signature(func).parameters:
        assert "embed" not in name
        assert "candidate" not in name
        assert "vector" not in name


def test_the_filter_has_one_model_call_so_no_merge_step_sits_after_its_batches() -> None:
    assert inspect.getsource(filtering).count("complete_json(") == 1


def test_no_prompt_asks_the_model_to_merge_topics() -> None:
    for name, value in vars(prompts).items():
        if isinstance(value, str) and not name.startswith("__"):
            assert "synonym" not in value.casefold(), name


def test_clustering_is_retired() -> None:
    assert importlib.util.find_spec("cyris.service_layer.cluster_news") is None
    for name in ("NEWS_CLUSTER_SYSTEM", "build_news_cluster_prompt"):
        assert not hasattr(prompts, name), name
    assert not hasattr(topics, "stand_in_topics")


@pytest.mark.parametrize("word", ["Taiwan", "台灣", "TWD", "central bank"])
def test_the_prompt_leaves_out_the_owners_granularity_example(word: str) -> None:
    assert word not in prompts.FILTER_SYSTEM


def test_the_run_offers_the_list_an_upvote_extends_and_not_the_plain_lifetime() -> None:
    source = inspect.getsource(run_digest)

    assert "recent_topic_list(" in source
    assert ".recent_topics(" not in source


def test_the_ceilings_row_says_it_bounds_the_list_whatever_the_votes() -> None:
    (row,) = [line for line in ARCHITECTURE.splitlines() if "`MAX_TOPIC_LIFETIMES` = " in line]

    assert "independently of how much the reader votes" in row
    assert "a starting point, not a measurement" in row
    (cutoff,) = [line for line in ARCHITECTURE.splitlines() if "`RECENT_VOTE_CUTOFF` = " in line]
    assert "nothing reads that list" not in cutoff


def test_the_feed_list_routes_news_where_the_news_tag_is_read() -> None:
    comment = json.loads(ONBOARDING_FEEDS.read_text(encoding="utf-8"))["_comment"]

    assert "cluster_news" not in comment
    assert "keeps the article from the scorer" in comment
    assert "NEWS_TAG in a.source_tags" in inspect.getsource(scoring)


def test_topic_tags_have_no_path_to_article_tags() -> None:
    assert "url_to_tags" not in ProcessResult.model_fields
    assert "topic" not in inspect.getsource(interest).casefold()
