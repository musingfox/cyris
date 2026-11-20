"""What a hosted tenant's run is wired with in place of a self-hosted deployment's channels.

A hosted run's alerts have two readers. The operator gets every alert with its raw error, on the
operator's own channel: the run posts it to `OPERATOR_ORIGIN`, which the hosted Worker intercepts
and answers with a gateway bound to this tenant, so the Worker, not the container, names the
tenant in what the operator reads, and the operator's webhook never reaches a container. The
reader gets the alert on the tenant's own channels with the error withheld
(`run_alert.reader_alert_message`). Why: docs/decisions/0035-a-hosted-runs-alerts-go-to-the-
operator-through-the-hosted-worker.md.

STUB, still. A hosted run's digest notification sends nothing, whatever the tenant's own `notify`
settings say: `bootstrap` wires `skip_digest_notification` in place of the Discord and mail
senders, so the decision is the composition root's and no setting can reopen that channel.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

import httpx

from cyris.adapters.notify import redact_webhook_tokens
from cyris.adapters.run_alert import alert_message
from cyris.i18n import ENGLISH
from cyris.service_layer.alerts import RunAlert

logger = logging.getLogger(__name__)

# The outcome `run_summary` records for each notification channel of a hosted run.
NOT_SENT = {"status": "skipped", "reason": "a hosted tenant's run sends no notification yet"}

# The host the hosted Worker intercepts for the operator's alerts. Plain http: the request is
# answered inside the Workers runtime and never crosses a network. `OPS_HOST` in
# workers/app/src/hosted_alerts.js is the same name; tests/test_hosted_config.py holds the two.
OPERATOR_ORIGIN = "http://ops.cyris"

# One post, no retry: an alert that cannot be sent is logged and never fails or delays the run.
OPERATOR_TIMEOUT_SECONDS = 10.0


async def skip_digest_notification(*_args: Any, **_kwargs: Any) -> dict[str, str]:
    """Stands in for `send_discord` and `send_digest_mail`: sends nothing and says so."""
    logger.info("Hosted tenant: no digest notification is sent")
    return dict(NOT_SENT)


async def send_operator_alert(
    tenant_id: str, alert: RunAlert, *, origin: str = OPERATOR_ORIGIN
) -> None:
    """Post `alert`, worded in English with its raw error, to the operator's gateway.

    The operator reads every tenant's alerts, so they are in one language whatever the tenant's
    `digest.output_language`. The gateway puts the tenant's id in front of them. Never raises.
    """
    subject, body = alert_message(replace(alert, language=ENGLISH))
    payload = {"subject": redact_webhook_tokens(subject), "body": redact_webhook_tokens(body)}
    try:
        async with httpx.AsyncClient(timeout=OPERATOR_TIMEOUT_SECONDS) as client:
            resp = await client.post(f"{origin}/t/{tenant_id}/alert", json=payload)
            resp.raise_for_status()
    except httpx.HTTPError as e:
        logger.warning("Operator alert failed: %s", redact_webhook_tokens(str(e)))
