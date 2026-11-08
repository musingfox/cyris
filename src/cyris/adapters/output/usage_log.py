"""Usage log writer — appends each digest run's LLM stats to a JSONL file."""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from cyris.adapters.store.d1 import D1Queryable
from cyris.domain.models import DigestContent

logger = logging.getLogger(__name__)


def usage_record(content: DigestContent) -> dict:
    """The one record a run's usage is, as `usage.jsonl` keeps it and D1 is given it."""
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


def append_usage(content: DigestContent, log_path: Path) -> None:
    """Append a usage record to the JSONL log file.

    Args:
        content: Digest content with usage stats.
        log_path: Path to the usage.jsonl file.
    """
    if content.usage.api_calls == 0:
        return

    record = usage_record(content)

    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a") as f:
        f.write(json.dumps(record) + "\n")

    logger.info("Usage logged to %s", log_path)


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


def usage_log_row(record: dict) -> dict:
    """The `usage_log` row one record becomes, every column, as the table then holds it."""
    columns, values = _usage_row(record)
    row = dict(zip(columns, values, strict=True))
    row.setdefault("cost_usd", 0)  # the column's default, which an unpriced record leaves
    return row


def usage_rows_d1(client: D1Queryable) -> list[dict]:
    """Every `usage_log` row as stored, for a migration to compare with."""
    return client.query(
        "SELECT logged_at, digest_date, period, articles_received, articles_included, model, "
        "api_calls, input_tokens, output_tokens, cost_usd FROM usage_log"
    ).rows


def append_usage_d1(content: DigestContent, client: D1Queryable) -> None:
    """Append the same record to D1 instead of a local file."""
    if content.usage.api_calls == 0:
        return

    columns, values = _usage_row(usage_record(content))
    client.query(
        f"INSERT INTO usage_log ({', '.join(columns)}) VALUES ({', '.join('?' * len(values))})",
        values,
    )
    logger.info("Usage logged to D1")


def import_usage_d1(records: list[dict], client: D1Queryable) -> int:
    """Copy `usage.jsonl` records into `usage_log`, each one at most once.

    The table has no key, so a record counts as already there when a row holds its
    run: the same stamp, issue and model. A run logs one record, stamped to the
    microsecond, so that never merges two runs, and a second copy writes nothing.
    """
    written = 0
    for record in records:
        columns, values = _usage_row(record)
        same_run = [record["timestamp"], record.get("date"), record.get("period")]
        written += client.query(
            f"INSERT INTO usage_log ({', '.join(columns)}) SELECT {', '.join('?' * len(values))} "
            "WHERE NOT EXISTS (SELECT 1 FROM usage_log WHERE logged_at = ? "
            "AND digest_date IS ? AND period IS ? AND model IS ?)",
            [*values, *same_run, record.get("model")],
        ).changes
    return written
