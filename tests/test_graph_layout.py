"""The interest graph layout: deterministic, inside the box, co-occurring tags close."""

import math

import pytest

from cyris.domain.graph_layout import layout
from cyris.domain.interest_graph import InterestEdge, InterestGraph, InterestNode

pytestmark = pytest.mark.unit

WIDTH, HEIGHT = 480.0, 360.0


def _graph(tags, edges):
    nodes = [InterestNode(tag, 1, 0, "up", 0.0) for tag in tags]
    return InterestGraph(nodes=nodes, edges=[InterestEdge(a, b) for a, b in edges])


def _two_pairs(tags=("a", "b", "c", "d")):
    return _graph(tags, [("a", "b"), ("c", "d")])


def _dist(points, a, b):
    (ax, ay), (bx, by) = points[a], points[b]
    return math.hypot(ax - bx, ay - by)


def test_the_same_graph_lays_out_the_same_way_twice() -> None:
    graph = _two_pairs()

    assert layout(graph, width=WIDTH, height=HEIGHT) == layout(graph, width=WIDTH, height=HEIGHT)


def test_tags_that_co_occur_sit_closer_than_tags_that_do_not() -> None:
    points = layout(_two_pairs(), width=WIDTH, height=HEIGHT)

    assert _dist(points, "a", "b") < _dist(points, "a", "c")
    assert _dist(points, "c", "d") < _dist(points, "b", "c")


def test_every_point_sits_inside_the_box() -> None:
    tags = [f"t{i:02d}" for i in range(60)]
    ring = [(tags[i], tags[(i + 1) % 60]) for i in range(60)]
    points = layout(_graph(tags, ring), width=WIDTH, height=HEIGHT)

    assert len(points) == 60
    for x, y in points.values():
        assert 0 <= x <= WIDTH
        assert 0 <= y <= HEIGHT


def test_one_node_sits_at_the_centre_and_an_empty_graph_has_no_points() -> None:
    assert layout(_graph(["a"], []), width=WIDTH, height=HEIGHT) == {"a": (WIDTH / 2, HEIGHT / 2)}
    assert layout(_graph([], []), width=WIDTH, height=HEIGHT) == {}


def test_node_order_in_the_graph_does_not_change_the_positions() -> None:
    forward = _two_pairs(("a", "b", "c", "d"))
    reversed_order = _two_pairs(("d", "c", "b", "a"))

    assert layout(forward, width=WIDTH, height=HEIGHT) == layout(
        reversed_order, width=WIDTH, height=HEIGHT
    )
