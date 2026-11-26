"""Is this hour a digest hour?

An hourly tick asks (supercronic in the container, or `cyris triage-ui`'s own tick
on a local store, which `--no-schedule` turns off); the answer comes from the effective
`digest_schedule`, which the settings page can change without a rebuild. A fixed
`0 8,20 * * *` crontab could not — it is baked into the image.

Hour granularity is the contract, not a rounding: an hourly tick cannot honour
`08:30`, so the write surface rejects it rather than silently firing at 08:00.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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


# How long after a digest hour begins a tick still runs that hour's issue when `digest_runs` holds
# no ok run of it, as `CATCH_UP_HOURS` in workers/app/src/schedule.js. A run the platform killed
# mid-way, or a machine that was off at the hour, leaves no ok row; the ticks after it retry, and a
# persistent failure is retried twice before it is left to a manual `cyris run --period`.
CATCH_UP_HOURS = 3


@dataclass(frozen=True)
class MissedSlot:
    """A digest hour that began within `CATCH_UP_HOURS` of now.

    `date` is the hour's own local date, which past midnight is not today's. `since` is the hour's
    start as `digest_runs` stores `finished_at`, UTC with a `+00:00` offset.
    """

    period: Period
    date: str
    since: str


def missed_slot(
    now: datetime, times: list[str], counts: frozenset[int] = SELF_HOSTED_TIMES
) -> MissedSlot | None:
    """The latest digest hour begun before `now`'s own hour and under `CATCH_UP_HOURS` before it.

    Stepping back whole hours from `now` lands once in every hour of that window, so the first
    step that lands in a digest hour finds the latest one. `now` must already be local; steps are
    absolute, so a daylight-saving change does not skip or repeat an hour.
    """
    for back in range(1, CATCH_UP_HOURS):
        probe = (now.astimezone(UTC) - timedelta(hours=back)).astimezone(now.tzinfo)
        period = due_period(probe, times, counts)
        if period is None:
            continue
        into_hour = timedelta(
            minutes=probe.minute, seconds=probe.second, microseconds=probe.microsecond
        )
        start = (probe.astimezone(UTC) - into_hour).replace(tzinfo=UTC)
        return MissedSlot(period, probe.date().isoformat(), start.isoformat(timespec="seconds"))
    return None


def catch_up(
    now: datetime,
    times: list[str],
    counts: frozenset[int],
    read_runs: Callable[[str, str], Iterable[Mapping] | None],
) -> MissedSlot | None:
    """The digest hour a tick off the hour should run, or None.

    Answers the same cases as `planTick` in workers/app/src/schedule.js, which the shared
    `schedule_cases.json` holds both to. A slot is caught up while it is under `CATCH_UP_HOURS` old
    and `digest_runs` holds no ok, non-dry run of its period since it began; an error row is not
    done, so a failed run is retried too. A tick on a digest hour catches up nothing: the regular
    run is that hour's. `read_runs(period, since)` answers the `digest_runs` rows (`status`,
    `dry_run`) of that period with a `finished_at` at or after `since`, or None when it cannot
    be read, which is also a None here: a missed catch-up loses nothing the lost run had not
    already lost.
    """
    if due_period(now, times, counts) is not None:
        return None
    slot = missed_slot(now, times, counts)
    if slot is None:
        return None
    runs = read_runs(slot.period, slot.since)
    if runs is None:
        return None
    done = any(row["status"] == "ok" and int(row["dry_run"]) == 0 for row in runs)
    return None if done else slot
