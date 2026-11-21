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

Every line a hosted run writes names its tenant (`tag_output`): the container's stdout is the only
log, and every tenant's runs land in the one Workers Logs stream.

What no tenant decides is pinned as its settings load (`pin_tenant_settings`): the provider, the
model and the notify targets, and the article cap clamped, from the hosted Worker's own policy
file. The tenant's reader writes the rest on the hosted Worker (ADR-0040), and the container can
write any row of its own database, so the run does not take those rows from there.
"""

from __future__ import annotations

import functools
import io
import json
import logging
import os
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, TextIO

import httpx

from cyris.adapters.notify import redact_webhook_tokens
from cyris.adapters.run_alert import alert_message
from cyris.config import HOSTED_B_GRADE_ENV_VARS, TENANT_ID, validate_setting
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

# What starts every line a hosted run writes. `tenant=` so a Workers Logs search for one
# tenant is one substring.
LINE_TAG = "tenant={tenant} "


# The hosted Worker's policy file, which the run reads where the Worker does: the image copies it
# to the same place beside `src` (Dockerfile), so the two read one file.
POLICY_PATH = Path(__file__).resolve().parents[3] / "workers/app/src/hosted_settings_policy.json"

ARTICLE_CAP_KEY = "digest.max_articles_per_digest"


@functools.cache
def hosted_policy() -> dict[str, Any]:
    """The hosted policy file, read once per process; a missing file raises, so no run goes on."""
    return json.loads(POLICY_PATH.read_text(encoding="utf-8"))


def pin_tenant_settings(stored: dict[str, Any]) -> dict[str, Any]:
    """`stored`, a tenant's `settings` rows, with what the tenant does not decide put back.

    The provider and model are the policy's and the notify targets blank, whatever the rows say,
    and an article cap above the policy's is the policy's. A cap the rule refuses is left as it
    is, so the load reports it as it reports any refused row.
    """
    policy = hosted_policy()
    pinned = {
        **stored,
        "llm_provider.provider": policy["provider"],
        "llm_provider.model": policy["model"],
        **dict.fromkeys(policy["blank_settings"], ""),
    }
    try:
        cap = validate_setting(ARTICLE_CAP_KEY, stored[ARTICLE_CAP_KEY])
    except (KeyError, ValueError):
        return pinned
    if cap > policy["max_articles_per_digest"]:
        pinned[ARTICLE_CAP_KEY] = policy["max_articles_per_digest"]
    return pinned


def tenant_from_environment() -> str | None:
    """The tenant id the hosted Worker set on this container, or None outside hosted mode.

    For what must work before any setting loads, or when none can: the config load refuses
    a malformed id, and so this answers None for one.
    """
    tenant = os.environ.get(HOSTED_B_GRADE_ENV_VARS["store.tenant_id"], "")
    return tenant if TENANT_ID.fullmatch(tenant) else None


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
    # `kind` lets the gateway tell an alert that ends the run, after which the Worker's own
    # alert about the container's stop would only repeat it (ENDING_KINDS in hosted_alerts.js).
    payload = {
        "kind": alert.kind,
        "subject": redact_webhook_tokens(subject),
        "body": redact_webhook_tokens(body),
    }
    try:
        async with httpx.AsyncClient(timeout=OPERATOR_TIMEOUT_SECONDS) as client:
            resp = await client.post(f"{origin}/t/{tenant_id}/alert", json=payload)
            resp.raise_for_status()
    except httpx.HTTPError as e:
        logger.warning("Operator alert failed: %s", redact_webhook_tokens(str(e)))


def _tag(chunk, tag, at_line_start: bool):
    """`chunk`, str or bytes, with `tag` before each line it starts; and whether it ended one."""
    newline = b"\n" if isinstance(chunk, bytes) else "\n"
    out = []
    for i, line in enumerate(chunk.split(newline)):
        if i:
            out.append(newline)
            at_line_start = True
        if line:
            if at_line_start:
                out.append(tag)
            out.append(line)
            at_line_start = False
    return chunk[:0].join(out), at_line_start


class TaggedLines(io.TextIOBase):
    """A text stream that starts each line written through it with `tag`.

    Its `buffer`, when the stream has one, tags the bytes written there the same way, so a
    command that writes bytes (`typer.echo(b"...")`, `sys.stdout.buffer`) still works.
    """

    def __init__(self, stream: TextIO, tag: str) -> None:
        self._stream = stream
        self._tag = tag
        # Shared with `buffer`: a line begun as text and ended as bytes is tagged once.
        self.at_line_start = True

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return self._stream.encoding

    @property
    def errors(self) -> str | None:  # type: ignore[override]
        return self._stream.errors

    @property
    def buffer(self) -> _TaggedBytes:
        # AttributeError without one, as a stream with no binary side answers.
        return _TaggedBytes(self, self._stream.buffer)

    def writable(self) -> bool:
        return True

    def isatty(self) -> bool:
        return self._stream.isatty()

    def fileno(self) -> int:
        return self._stream.fileno()

    def write(self, text: str) -> int:
        out, self.at_line_start = _tag(text, self._tag, self.at_line_start)
        self._stream.write(out)
        return len(text)

    def flush(self) -> None:
        self._stream.flush()


class _TaggedBytes(io.RawIOBase):
    """The binary side of a `TaggedLines`, tagging each line in the text side's encoding."""

    def __init__(self, text: TaggedLines, raw: Any) -> None:
        self._text = text
        self._raw = raw

    def writable(self) -> bool:
        return True

    def write(self, data: Any) -> int:
        chunk = bytes(data)
        # Text written before these bytes goes out before them.
        self._text.flush()
        tag = self._text._tag.encode(self._text.encoding or "utf-8")
        out, self._text.at_line_start = _tag(chunk, tag, self._text.at_line_start)
        self._raw.write(out)
        return len(chunk)

    def flush(self) -> None:
        self._raw.flush()


def tag_output(tenant_id: str, *, stdout: bool = True) -> None:
    """Start every line this process writes to stderr, and to stdout unless `stdout` is
    False, with the tenant's id.

    At the stream, not the logging formatter, so a progress line, a typer message and a
    traceback are tagged as a log record is. A command whose stdout is machine-readable
    (`--json`) keeps it untagged, so it still parses; its log lines go to stderr, tagged.
    """
    tag = LINE_TAG.format(tenant=tenant_id)
    if stdout and not isinstance(sys.stdout, TaggedLines):
        sys.stdout = TaggedLines(sys.stdout, tag)
    if not isinstance(sys.stderr, TaggedLines):
        sys.stderr = TaggedLines(sys.stderr, tag)
