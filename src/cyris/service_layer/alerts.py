"""An alert to the reader's channels, as structure: the senders word it (ADR-0027).

The core says which alert it is and what fills it, never how it reads: the wording is the UI
catalogs', and only an adapter reads them.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

# `run_failed` and `fetched_nothing` end a run, `could_not_start` is a scheduled run that
# stopped before `run_digest`, `vote_similarity_rejected` is a warning beside a published issue.
AlertKind = Literal["run_failed", "could_not_start", "fetched_nothing", "vote_similarity_rejected"]


@dataclass(frozen=True)
class RunAlert:
    """One alert: its kind, the run it is about, and the values its body names."""

    kind: AlertKind
    period: str
    # Aware; the subject shows it as a minute in `timezone`.
    started_at: datetime
    timezone: str
    # The current `digest.output_language`: an alert is about the deployment, not one issue.
    language: str
    # `run_failed`, `could_not_start`: the raw `Type: message` of the error.
    error: str = ""
    # `fetched_nothing`: the sources whose fetch failed.
    sources: tuple[str, ...] = ()
    # `vote_similarity_rejected`: the embedding API's HTTP status, as text so no locale
    # groups its digits, and its own message.
    status: str = ""
    detail: str = ""
