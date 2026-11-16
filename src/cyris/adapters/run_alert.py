"""A run alert's subject and body, worded from the UI catalogs in the alert's language.

The core hands each sender a `RunAlert`, structure only (ADR-0027). Discord and mail both
word it here, and only as they send it, so a run that sends no alert reads no catalog.
"""

from typing import assert_never
from zoneinfo import ZoneInfo

from cyris.i18n import Translator, issue_name, translator
from cyris.service_layer.alerts import RunAlert


def _headline(t: Translator, alert: RunAlert) -> str:
    match alert.kind:
        case "run_failed":
            return t("alert.run_failed_headline")
        case "could_not_start":
            return t("alert.could_not_start_headline")
        case "fetched_nothing":
            return t("alert.fetched_nothing_headline")
        case "vote_similarity_rejected":
            return t("alert.vote_similarity_headline")
        case _:
            assert_never(alert.kind)


def _body(t: Translator, alert: RunAlert) -> str:
    match alert.kind:
        case "run_failed" | "could_not_start":
            return alert.error
        case "fetched_nothing":
            return t("alert.fetched_nothing_body", sources=", ".join(alert.sources))
        case "vote_similarity_rejected":
            return t("alert.vote_similarity_body", status=alert.status, detail=alert.detail)
        case _:
            assert_never(alert.kind)


def alert_message(alert: RunAlert) -> tuple[str, str]:
    """`(subject, body)`.

    The subject is the headline, then the run's issue named as every surface names it, by the
    hour the run started in `general.timezone`, then the zone.
    """
    t = translator(alert.language)
    when = alert.started_at.astimezone(ZoneInfo(alert.timezone))
    subject = t(
        "alert.subject",
        headline=_headline(t, alert),
        issue=issue_name(t, when.date().isoformat(), when),
        timezone=alert.timezone,
    )
    return subject, _body(t, alert)
