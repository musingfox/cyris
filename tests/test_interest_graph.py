"""The interest graph: nodes, polarity, clock brightness and co-occurrence edges."""

from datetime import datetime, timedelta

import pytest

from cyris.domain.interest_graph import TagClock, VotedTag, build_graph
from cyris.domain.models import ArticleState

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 17, 8, 0, 0)
DAYS = 7


def _graph(voted=(), clocks=(), max_nodes=60):
    return build_graph(list(voted), list(clocks), NOW, clock_days=DAYS, max_nodes=max_nodes)


def _node(graph, tag):
    (node,) = [n for n in graph.nodes if n.tag == tag]
    return node


def test_nodes_and_edges_follow_the_votes() -> None:
    graph = _graph(
        [
            VotedTag("X", "a", ArticleState.ACCEPTED),
            VotedTag("X", "b", ArticleState.ACCEPTED),
            VotedTag("Y", "b", ArticleState.REJECTED),
            VotedTag("Y", "c", ArticleState.REJECTED),
        ]
    )

    assert [(n.tag, n.up, n.down, n.polarity) for n in graph.nodes] == [
        ("b", 1, 1, "mixed"),
        ("a", 1, 0, "up"),
        ("c", 0, 1, "down"),
    ]
    assert [(e.a, e.b) for e in graph.edges] == [("a", "b"), ("b", "c")]


def test_brightness_is_the_largest_remaining_share_of_the_clock() -> None:
    voted = [VotedTag("X", "c", ArticleState.REJECTED)]
    half = _graph(voted, [TagClock("c", NOW + timedelta(days=3.5))])
    expired = _graph(voted, [TagClock("c", NOW)])
    two = _graph(
        voted,
        [TagClock("c", NOW + timedelta(days=1)), TagClock("c", NOW + timedelta(days=6))],
    )
    none = _graph(voted)

    assert _node(half, "c").brightness == pytest.approx(0.5)
    assert _node(expired, "c").brightness == 0.0
    assert _node(two, "c").brightness == pytest.approx(6 / 7)
    assert _node(none, "c").brightness == 0.0


def test_max_nodes_keeps_the_top_tags_and_only_their_edges() -> None:
    voted = [
        VotedTag("1", "a", ArticleState.ACCEPTED),
        VotedTag("2", "a", ArticleState.ACCEPTED),
        VotedTag("3", "a", ArticleState.ACCEPTED),
        VotedTag("1", "b", ArticleState.ACCEPTED),
        VotedTag("2", "b", ArticleState.ACCEPTED),
        VotedTag("3", "c", ArticleState.ACCEPTED),
    ]
    graph = _graph(voted, max_nodes=2)

    assert [n.tag for n in graph.nodes] == ["a", "b"]
    assert all("c" not in (e.a, e.b) for e in graph.edges)


def test_a_duplicate_vote_counts_once() -> None:
    graph = _graph(
        [VotedTag("X", "a", ArticleState.ACCEPTED), VotedTag("X", "a", ArticleState.ACCEPTED)]
    )

    assert _node(graph, "a").up == 1


def test_a_pending_row_is_ignored() -> None:
    graph = _graph([VotedTag("X", "a", ArticleState.PENDING)])

    assert graph.nodes == []
    assert graph.edges == []


def test_empty_input_gives_an_empty_graph() -> None:
    graph = _graph()

    assert graph.nodes == []
    assert graph.edges == []
