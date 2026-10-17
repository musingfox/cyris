"""Stable positions for the interest graph: tags that co-occur sit close together.

A fixed-iteration Fruchterman-Reingold layout. It starts from points on a circle in
tag order and uses no randomness, so the same graph and size always lay out the same.
"""

from __future__ import annotations

import math

from cyris.domain.interest_graph import InterestGraph

# Fixed, not run to convergence, so the layout's cost and output do not depend on the
# graph beyond its size. A tuned constant, graded A in docs/architecture.md §5.
ITERATIONS = 300
# Keeps every node this fraction of the box's shorter side from its edges, so a label
# at the border is not clipped by the frame. A display choice, graded A in
# docs/architecture.md §5.
MARGIN = 0.1
EPSILON = 1e-9


def layout(graph: InterestGraph, *, width: float, height: float) -> dict[str, tuple[float, float]]:
    tags = sorted(node.tag for node in graph.nodes)
    count = len(tags)
    if count == 0:
        return {}
    if count == 1:
        return {tags[0]: (width / 2, height / 2)}

    index = {tag: i for i, tag in enumerate(tags)}
    pairs = [(index[e.a], index[e.b]) for e in graph.edges]
    margin = MARGIN * min(width, height)
    x_low, x_high = margin, width - margin
    y_low, y_high = margin, height - margin

    radius_x, radius_y = (x_high - x_low) / 2, (y_high - y_low) / 2
    pos = [
        [
            width / 2 + radius_x * math.cos(2 * math.pi * i / count),
            height / 2 + radius_y * math.sin(2 * math.pi * i / count),
        ]
        for i in range(count)
    ]

    area = (x_high - x_low) * (y_high - y_low)
    k = math.sqrt(area / count)
    for step in range(ITERATIONS):
        temperature = max(width, height) / 10 * (1 - step / ITERATIONS)
        disp = [[0.0, 0.0] for _ in range(count)]
        for i in range(count):
            for j in range(i + 1, count):
                dx, dy = pos[i][0] - pos[j][0], pos[i][1] - pos[j][1]
                dist = max(math.hypot(dx, dy), EPSILON)
                force = k * k / dist / dist
                disp[i][0] += dx * force
                disp[i][1] += dy * force
                disp[j][0] -= dx * force
                disp[j][1] -= dy * force
        for a, b in pairs:
            dx, dy = pos[a][0] - pos[b][0], pos[a][1] - pos[b][1]
            dist = max(math.hypot(dx, dy), EPSILON)
            force = dist / k
            disp[a][0] -= dx * force
            disp[a][1] -= dy * force
            disp[b][0] += dx * force
            disp[b][1] += dy * force
        for i in range(count):
            length = max(math.hypot(*disp[i]), EPSILON)
            step_len = min(length, temperature)
            pos[i][0] = min(max(pos[i][0] + disp[i][0] / length * step_len, x_low), x_high)
            pos[i][1] = min(max(pos[i][1] + disp[i][1] / length * step_len, y_low), y_high)

    return {tag: (pos[index[tag]][0], pos[index[tag]][1]) for tag in tags}
