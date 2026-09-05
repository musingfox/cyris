"""Runtime-mutable settings in D1 — grade D in docs/architecture.md §5.

`cyris.toml` is baked into the image and mounted `:ro`, so the settings page
cannot write it. These keys live in D1 instead, and the file keeps the same
keys as the fallback a fresh deployment starts from.

The read order is fixed and not negotiable: **D1 first, file second.** A host
run and a container run reading different settings is the 2026-08-25→27 split,
and this table only removes that risk if every reader resolves it the same way.
For the same reason a D1 read error propagates — falling back to the file on
error would reintroduce exactly the divergence the order exists to prevent.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from cyris.adapters.store.d1 import D1Queryable

logger = logging.getLogger(__name__)

# Dotted paths into AppConfig. Only keys with a writer are listed: a key nobody
# can change from the settings page has nothing to gain from a second home.
WRITABLE_KEYS = (
    "llm_provider.provider",
    "llm_provider.model",
    "general.digest_schedule",
    "general.timezone",
    "digest.max_featured",
)


class D1Settings:
    """Key/value settings over Cloudflare D1."""

    def __init__(self, client: D1Queryable) -> None:
        self._db = client

    def all(self) -> dict[str, Any]:
        """Every stored setting, JSON-decoded. Unknown keys are ignored."""
        rows = self._db.query("SELECT key, value FROM settings").rows
        out: dict[str, Any] = {}
        for row in rows:
            if row["key"] not in WRITABLE_KEYS:
                continue
            try:
                out[row["key"]] = json.loads(row["value"])
            except json.JSONDecodeError:
                logger.warning("Ignoring un-decodable setting %r", row["key"])
        return out

    def set(self, values: dict[str, Any]) -> None:
        """Upsert several settings. Rejects anything not on the whitelist."""
        unknown = sorted(set(values) - set(WRITABLE_KEYS))
        if unknown:
            raise ValueError(f"not a settings key: {', '.join(unknown)}")
        now = datetime.now(UTC).isoformat(timespec="seconds")
        for key, value in values.items():
            self._db.query(
                "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                [key, json.dumps(value), now],
            )
        logger.info("Settings updated: %s", ", ".join(sorted(values)))


def apply_to(cfg: Any, stored: dict[str, Any]) -> list[str]:
    """Overlay stored settings onto a loaded Config. Returns the keys applied."""
    applied = []
    overrides: dict[str, dict[str, Any]] = {}
    for key, value in stored.items():
        table, field = key.split(".", 1)
        overrides.setdefault(table, {})[field] = value
        applied.append(key)
    # Rebuild each table rather than setattr-ing into it: a plain assignment
    # skips the model validators, and some of them derive one field from
    # another. `llm_provider.api_key` is read from the env var named by
    # `provider`, so a D1 row that flips the provider must re-run that
    # injection — otherwise the key stays empty and the run dies naming an
    # environment variable that is in fact set.
    for table, fields in overrides.items():
        current = getattr(cfg.app, table)
        setattr(cfg.app, table, type(current).model_validate(current.model_dump() | fields))
    return applied
