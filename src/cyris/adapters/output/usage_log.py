"""Usage log writer — appends each digest run's LLM stats to the `usage_log` table."""

import logging
from datetime import UTC, datetime

from cyris.adapters.store.d1 import D1Queryable
from cyris.domain.models import DigestContent

logger = logging.getLogger(__name__)


def usage_record(content: DigestContent) -> dict:
    """The one record a run's usage is."""
    return {
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


def _usage_row(record: dict) -> tuple[list[str], list]:
    """The `usage_log` columns and values of one `usage_record`."""
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
        record["timestamp"],
        record.get("date"),
        record.get("period"),
        record.get("articles_received", 0),
        record.get("articles_included", 0),
        record.get("model"),
        record.get("api_calls", 0),
        record.get("input_tokens", 0),
        record.get("output_tokens", 0),
    ]
    # `cost_usd` is NOT NULL DEFAULT 0, so an unpriced model leaves the column out
    # instead of sending NULL. `model` is in the same row, which is what separates
    # "no rate card for this vendor" from a genuine $0.
    cost = record.get("estimated_cost_usd")
    if cost is not None:
        columns.append("cost_usd")
        values.append(cost)
    return columns, values


def append_usage_d1(content: DigestContent, client: D1Queryable) -> None:
    """Append the run's record to `usage_log`."""
    if content.usage.api_calls == 0:
        return

    columns, values = _usage_row(usage_record(content))
    client.query(
        f"INSERT INTO usage_log ({', '.join(columns)}) VALUES ({', '.join('?' * len(values))})",
        values,
    )
    logger.info("Usage logged to D1")
