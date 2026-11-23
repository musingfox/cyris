"""Is this hour a digest hour?

An hourly tick asks (supercronic in the container, or `cyris triage-ui`'s own tick
on a local store, which `--no-schedule` turns off); the answer comes from the effective
`digest_schedule`, which the settings page can change without a rebuild. A fixed
`0 8,20 * * *` crontab could not — it is baked into the image.

Hour granularity is the contract, not a rounding: an hourly tick cannot honour
`08:30`, so the write surface rejects it rather than silently firing at 08:00.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Literal, cast, get_args

Period = Literal["morning", "evening"]

# The order a day's issues are published in: `due_period` gives the first label to
# the earlier hour. The archive sorts a day's issues by it.
PERIOD_ORDER: tuple[str, ...] = get_args(Period)

# How many digest times a schedule may hold, by edition, as `SELF_HOSTED_TIMES` and
# `HOSTED_TIMES` in workers/app/src/schedule.js. A self-hosted deployment publishes both
# periods every day. A hosted tenant publishes one, its morning, or two when the hosted
# Worker's registry allows them; the registry holds that maximum, so a run takes either
# (ADR-0042). Never more times than periods.
SELF_HOSTED_TIMES: frozenset[int] = frozenset({2})
HOSTED_TIMES: frozenset[int] = frozenset({1, 2})


def schedule_counts(*, hosted: bool) -> frozenset[int]:
    """How many digest times the edition's schedule may hold."""
    return HOSTED_TIMES if hosted else SELF_HOSTED_TIMES


def validate_schedule(times: list[str], counts: frozenset[int] = SELF_HOSTED_TIMES) -> list[str]:
    """As many distinct whole hours as `counts` allows, earliest first. Raises ValueError."""
    if len(times) not in counts:
        if counts == SELF_HOSTED_TIMES:
            raise ValueError("the digest schedule is exactly two times: a morning and an evening")
        raise ValueError("a hosted digest schedule is one time, or two: a morning and an evening")
    hours = []
    for t in times:
        try:
            hour, minute = (int(part) for part in t.split(":", 1))
        except ValueError:
            raise ValueError(f"{t!r} is not a HH:MM time") from None
        if not 0 <= hour <= 23 or minute != 0:
            raise ValueError(f"{t!r} must be a whole hour — the schedule tick is hourly")
        hours.append(hour)
    if len(set(hours)) != len(hours):
        raise ValueError("the two digest times must be different hours")
    return [f"{h:02d}:00" for h in sorted(hours)]


def due_period(
    now: datetime, times: list[str], counts: frozenset[int] = SELF_HOSTED_TIMES
) -> Period | None:
    """Which digest `now` is due for, or None. `now` must already be local.

    The earliest hour is the morning's, so a schedule of one hour has only a morning.
    """
    try:
        hours = validate_schedule(times, counts)
    except ValueError:
        return None
    current = f"{now.hour:02d}:00"
    if current not in hours:
        return None
    return cast(Period, PERIOD_ORDER[hours.index(current)])


def first_issue_key(now: datetime, times: list[str]) -> tuple[str, Period]:
    """The (date, period) a run started at `now` can take without a later tick replacing it.

    An issue is keyed by (date, period), and the run `due_period` starts at a
    publish hour stores its issue over any other with that key. So the first
    issue takes today's latest publish hour that has already begun: every tick
    left today falls on a later hour, and none of them gives that period again
    before the date changes. While both of today's hours are still to come,
    any key dated today is one a tick will take, so it is yesterday's evening.
    The run is started with this date, so a run that ends after midnight keeps
    it. `now` must already be local. Raises ValueError on a schedule
    `validate_schedule` refuses.
    """
    earlier, later = (int(time.split(":")[0]) for time in validate_schedule(times))
    today = now.date()
    if now.hour >= later:
        return today.isoformat(), "evening"
    if now.hour >= earlier:
        return today.isoformat(), "morning"
    return (today - timedelta(days=1)).isoformat(), "evening"


def is_latest_issue(key: tuple[str, str], issues: Iterable[tuple[str, str]]) -> bool:
    """True when no issue ranks after `key` by (date, PERIOD_ORDER index). Raises ValueError."""

    def rank(issue: tuple[str, str]) -> tuple[str, int]:
        date, period = issue
        return date, PERIOD_ORDER.index(period)

    key_rank = rank(key)
    return all(rank(issue) <= key_rank for issue in issues)
