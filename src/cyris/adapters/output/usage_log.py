"""Usage log writer — appends each digest run's LLM stats to a JSONL file."""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from cyris.adapters.store.d1 import D1Queryable
from cyris.domain.models import DigestContent

logger = logging.getLogger(__name__)


def append_usage(content: DigestContent, log_path: Path) -> None:
    """Append a usage record to the JSONL log file.

    Args:
        content: Digest content with usage stats.
        log_path: Path to the usage.jsonl file.
    """
    if content.usage.api_calls == 0:
        return

    record = {
        "timestamp": datetime.now(tz=UTC).isoformat(),
        "date": content.date,
        "period": content.period,
        "articles_received": content.articles_received,
        "articles_included": content.articles_included,
        "model": content.usage.model,
        "api_calls": content.usage.api_calls,
        "input_tokens": content.usage.input_tokens,
        "output_tokens": content.usage.output_tokens,
        "total_tokens": content.usage.total_tokens,
        # null, not 0, when the model has no rate card here — the two mean
        # different things to whatever reads this log back.
        "estimated_cost_usd": (
            round(cost, 6) if (cost := content.usage.estimated_cost) is not None else None
        ),
    }

    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a") as f:
        f.write(json.dumps(record) + "\n")

    logger.info("Usage logged to %s", log_path)


def append_usage_d1(content: DigestContent, client: D1Queryable) -> None:
    """Append the same record to D1 instead of a local file."""
    if content.usage.api_calls == 0:
        return

    columns = [
        "logged_at",
        "digest_date",
        "period",
        "articles_received",
        "articles_included",
        "model",
        "api_calls",
        "input_tokens",
        "output_tokens",
    ]
    values = [
        datetime.now(tz=UTC).isoformat(),
        content.date,
        content.period,
        content.articles_received,
        content.articles_included,
        content.usage.model,
        content.usage.api_calls,
        content.usage.input_tokens,
        content.usage.output_tokens,
    ]
    # `cost_usd` is NOT NULL DEFAULT 0, so an unpriced model leaves the column out
    # instead of sending NULL. `model` is in the same row, which is what separates
    # "no rate card for this vendor" from a genuine $0.
    cost = content.usage.estimated_cost
    if cost is not None:
        columns.append("cost_usd")
        values.append(round(cost, 6))

    client.query(
        f"INSERT INTO usage_log ({', '.join(columns)}) VALUES ({', '.join('?' * len(values))})",
        values,
    )
    logger.info("Usage logged to D1")
