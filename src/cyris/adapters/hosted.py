"""What a hosted tenant's run is wired with in place of the channels it does not have yet.

STUB. Later cards give a hosted tenant its own pages (per-tenant serving) and the operator a
channel for a tenant's failures (operator alerts). Until they land, a hosted run publishes
nothing and notifies nobody, whatever the tenant's own `notify` settings say, and the finished
issue stays in that tenant's `digests` table
(docs/decisions/0028-one-sqlite-durable-object-per-hosted-tenant.md). `bootstrap` wires these in
place of the Discord and mail senders, so the decision is the composition root's and no setting
can reopen a channel.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# The outcome `run_summary` records for each notification channel of a hosted run.
NOT_SENT = {"status": "skipped", "reason": "a hosted tenant's run sends no notification yet"}


async def skip_digest_notification(*_args: Any, **_kwargs: Any) -> dict[str, str]:
    """Stands in for `send_discord` and `send_digest_mail`: sends nothing and says so."""
    logger.info("Hosted tenant: no digest notification is sent")
    return dict(NOT_SENT)


async def skip_alert(*_args: Any, **_kwargs: Any) -> None:
    """Stands in for `send_discord_alert` and `send_alert_mail`: sends nothing."""
    logger.info("Hosted tenant: no run alert is sent")
