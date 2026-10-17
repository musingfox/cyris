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
class Step:
    id: str
    statements: tuple[str, ...]


MIGRATIONS: tuple[Step, ...] = ()


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
            if step.statements:
                client.query(";\n".join(step.statements))
            client.query("INSERT OR IGNORE INTO schema_migrations (id) VALUES (?)", [step.id])
        except Exception as e:
            raise D1Error(f"migration {step.id} failed: {e}") from e
