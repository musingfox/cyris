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
        "estimated_cost_usd": round(content.usage.estimated_cost, 6),
    }

    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a") as f:
        f.write(json.dumps(record) + "\n")

    logger.info("Usage logged to %s", log_path)


def append_usage_d1(content: DigestContent, client: D1Queryable) -> None:
    """Append the same record to D1 instead of a local file."""
    if content.usage.api_calls == 0:
        return

    client.query(
        "INSERT INTO usage_log (logged_at, digest_date, period, articles_received, "
        "articles_included, model, api_calls, input_tokens, output_tokens, cost_usd) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            datetime.now(tz=UTC).isoformat(),
            content.date,
            content.period,
            content.articles_received,
            content.articles_included,
            content.usage.model,
            content.usage.api_calls,
            content.usage.input_tokens,
            content.usage.output_tokens,
            round(content.usage.estimated_cost, 6),
        ],
    )
    logger.info("Usage logged to D1")
