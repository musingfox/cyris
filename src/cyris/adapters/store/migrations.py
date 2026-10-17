"""Versioned steps that change a database built by an earlier schema.

`schema.sql` creates tables on a fresh database and cannot change one that exists.
Each step here runs once per database, in order, and is recorded in
`schema_migrations` by id. The steps run after `schema.sql` on every boot, so a
step may assume the table shapes `schema.sql` declares.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from cyris.adapters.store.d1 import D1Queryable


@dataclass(frozen=True)
class Requires:
    """A column the table must have (`present`) or must lack, before the step runs."""

    table: str
    column: str
    present: bool


@dataclass(frozen=True)
class Step:
    id: str
    statements: tuple[str, ...]
    requires: Requires | None = None


LANE_STEP = Step(
    "0001_tier_to_lane",
    (
        "UPDATE stored_articles SET source_tier = CASE source_tier "
        "WHEN 'filter' THEN 'skim' WHEN 'summarize' THEN 'read' WHEN 'fan' THEN 'follow' "
        "ELSE source_tier END WHERE source_tier IN ('filter', 'summarize', 'fan')",
        "ALTER TABLE stored_articles RENAME COLUMN source_tier TO source_lane",
    ),
    Requires("stored_articles", "source_tier", present=True),
)

MIGRATIONS: tuple[Step, ...] = (LANE_STEP,)


def _holds(client: D1Queryable, requires: Requires) -> bool:
    names = {row["name"] for row in client.query(f"PRAGMA table_info('{requires.table}')").rows}
    return bool(names) and (requires.column in names) == requires.present


def apply_steps(client: D1Queryable, steps: Sequence[Step]) -> None:
    """Run each step whose id is not recorded, in order, and record it.

    A step that fails stops the run: it is not recorded, and the steps after it
    wait for the next boot. An id recorded but unknown to `steps` is left alone.
    """
    from cyris.adapters.store.d1 import D1Error

    applied = {row["id"] for row in client.query("SELECT id FROM schema_migrations").rows}
    for step in steps:
        if step.id in applied:
            continue
        try:
            if step.requires is None or _holds(client, step.requires):
                try:
                    client.query(";\n".join(step.statements))
                except Exception:
                    # The ui and run containers can cold-start together: both see the
                    # step pending, and the one that loses the race fails on a column
                    # the winner already renamed. A precondition that no longer holds
                    # means the step is in place, so record it instead of aborting boot.
                    if step.requires is None or _holds(client, step.requires):
                        raise
            client.query("INSERT OR IGNORE INTO schema_migrations (id) VALUES (?)", [step.id])
        except Exception as e:
            raise D1Error(f"migration {step.id} failed: {e}") from e
