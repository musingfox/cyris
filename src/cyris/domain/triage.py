"""Article triage semantics: canonical rejection reasons."""

from enum import StrEnum


class RejectReason(StrEnum):
    """Canonical rejection reasons; free-text reasons remain allowed at the CLI."""

    FILTERED = "filtered"
    MANUAL = "manual"
    MANUAL_TRIAGE = "manual_triage"
    ALREADY_KNOWN = "already_known"
    NOT_INTERESTED = "not_interested"
