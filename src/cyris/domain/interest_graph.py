"""The interest graph: the tags of human-voted articles, joined where they co-occur.

A node's polarity is the sign of its net vote, and its brightness is the share of the
suppression clock its tag has left, so the map shows what is held back now.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import combinations
from typing import Literal

from cyris.domain.models import ArticleState

# The map is for the few tags a reader is asking about; past this many nodes the labels
# overlap and the table beside the map is the readable form. A display limit, graded A
# in docs/architecture.md §5.
MAX_INTEREST_NODES = 60


@dataclass(frozen=True)
class VotedTag:
    article_url: str
    tag: str
    state: ArticleState


@dataclass(frozen=True)
class TagClock:
    tag: str
    until: datetime


@dataclass(frozen=True)
class InterestNode:
    tag: str
    up: int
    down: int
    polarity: Literal["up", "down", "mixed"]
    brightness: float


@dataclass(frozen=True)
class InterestEdge:
    a: str
    b: str


@dataclass(frozen=True)
class InterestGraph:
    nodes: list[InterestNode]
    edges: list[InterestEdge]


def build_graph(
    voted: list[VotedTag],
    clocks: list[TagClock],
    now: datetime,
    *,
    clock_days: int,
    max_nodes: int,
) -> InterestGraph:
    votes: dict[tuple[str, str], ArticleState] = {}
    for row in voted:
        if row.state in (ArticleState.ACCEPTED, ArticleState.REJECTED):
            votes.setdefault((row.article_url, row.tag), row.state)

    up: Counter[str] = Counter()
    down: Counter[str] = Counter()
    tags_by_url: dict[str, set[str]] = defaultdict(set)
    for (url, tag), state in votes.items():
        (up if state == ArticleState.ACCEPTED else down)[tag] += 1
        tags_by_url[url].add(tag)

    ranked = sorted(up.keys() | down.keys(), key=lambda t: (-(up[t] + down[t]), t))
    kept = ranked[:max_nodes]
    kept_set = set(kept)

    span = timedelta(days=clock_days)
    brightness: dict[str, float] = {}
    for clock in clocks:
        share = min(max((clock.until - now) / span, 0.0), 1.0)
        brightness[clock.tag] = max(brightness.get(clock.tag, 0.0), share)

    nodes = [
        InterestNode(
            tag=tag,
            up=up[tag],
            down=down[tag],
            polarity="up" if up[tag] > down[tag] else "down" if down[tag] > up[tag] else "mixed",
            brightness=brightness.get(tag, 0.0),
        )
        for tag in kept
    ]

    pairs = {
        pair for tags in tags_by_url.values() for pair in combinations(sorted(tags & kept_set), 2)
    }
    edges = [InterestEdge(a, b) for a, b in sorted(pairs)]

    return InterestGraph(nodes=nodes, edges=edges)
