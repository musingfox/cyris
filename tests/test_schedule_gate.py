"""The hourly tick's question: is this a digest hour?"""

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from cyris.service_layer.schedule import (
    HOSTED_TIMES,
    PERIOD_ORDER,
    SELF_HOSTED_TIMES,
    catch_up,
    due_period,
    schedule_counts,
    validate_schedule,
)

pytestmark = pytest.mark.unit

TAIPEI = ZoneInfo("Asia/Taipei")


def _at(hour: int) -> datetime:
    return datetime(2026, 8, 27, hour, 0, tzinfo=TAIPEI)


def test_the_earlier_hour_is_the_morning_digest():
    assert due_period(_at(8), ["08:00", "20:00"]) == "morning"


def test_the_later_hour_is_the_evening_digest():
    assert due_period(_at(20), ["08:00", "20:00"]) == "evening"


def test_the_period_order_is_the_order_the_schedule_fires_them_in():
    """The archive sorts a day's issues by this; the earlier hour's label comes first."""
    fired = (due_period(_at(8), ["08:00", "20:00"]), due_period(_at(20), ["08:00", "20:00"]))

    assert PERIOD_ORDER == fired == ("morning", "evening")


def test_order_in_the_list_does_not_decide_which_is_which():
    assert due_period(_at(8), ["20:00", "08:00"]) == "morning"


@pytest.mark.parametrize("hour", [0, 7, 9, 19, 21, 23])
def test_every_other_hour_is_not_due(hour):
    assert due_period(_at(hour), ["08:00", "20:00"]) is None


def test_an_unusable_schedule_never_fires_rather_than_guessing():
    """A malformed D1 row must not make every tick a digest hour."""
    assert due_period(_at(8), ["08:00"]) is None
    assert due_period(_at(8), []) is None


@pytest.mark.parametrize("times", [["08:30", "20:00"], ["08:00"], ["8", "20"], ["08:00", "08:00"]])
def test_the_write_surface_refuses_what_the_tick_cannot_honour(times):
    with pytest.raises(ValueError):
        validate_schedule(times)


def test_hours_are_normalised_and_sorted():
    assert validate_schedule(["9:00", "06:00"]) == ["06:00", "09:00"]


SHARED_CASES = Path(__file__).parent.parent / "workers/app/test/schedule_cases.json"


ALL_CASES = json.loads(SHARED_CASES.read_text())


@pytest.mark.parametrize("case", ALL_CASES, ids=lambda c: str(c))
def test_due_period_agrees_with_the_app_workers_cron_check(case):
    """workers/app/src/schedule.js answers the same cases before it wakes the container."""
    now = datetime.fromisoformat(case["at"]).astimezone(ZoneInfo(case["timezone"]))
    counts = schedule_counts(hosted=case.get("edition") == "hosted")
    assert due_period(now, case["schedule"], counts) == case["due"]


def test_a_hosted_schedule_is_one_hour_whose_issue_is_the_morning():
    """A hosted tenant's one daily issue is stored as `morning` (ADR-0042)."""
    assert validate_schedule(["13:00"], HOSTED_TIMES) == ["13:00"]
    assert due_period(_at(13), ["13:00"], HOSTED_TIMES) == "morning"
    assert due_period(_at(14), ["13:00"], HOSTED_TIMES) is None


def test_a_hosted_schedule_takes_two_hours_and_never_three():
    assert validate_schedule(["20:00", "8:00"], HOSTED_TIMES) == ["08:00", "20:00"]
    for times in ([], ["08:00", "12:00", "20:00"], ["08:00", "08:00"], ["08:30"]):
        with pytest.raises(ValueError):
            validate_schedule(times, HOSTED_TIMES)


def test_a_self_hosted_schedule_is_still_exactly_two_hours():
    assert schedule_counts(hosted=False) == SELF_HOSTED_TIMES == {2}
    with pytest.raises(ValueError, match="exactly two times"):
        validate_schedule(["08:00"])


def _digest_runs(case: dict):
    """What `D1RunLog.since` answers for a case's `digest_runs` table: the period's rows since."""
    if case["runs"] is None:
        return lambda period, since: None
    return lambda period, since: [
        row for row in case["runs"] if row["period"] == period and row["finished_at"] >= since
    ]


@pytest.mark.parametrize("case", [c for c in ALL_CASES if "catch_up" in c], ids=lambda c: str(c))
def test_catch_up_agrees_with_the_app_workers_plan_tick(case):
    """workers/app/src/schedule.js `planTick` answers the same cases off the hour."""
    now = datetime.fromisoformat(case["at"]).astimezone(ZoneInfo(case["timezone"]))
    counts = schedule_counts(hosted=case.get("edition") == "hosted")

    slot = catch_up(now, case["schedule"], counts, _digest_runs(case))

    expected = case["catch_up"]
    assert (None if slot is None else {"period": slot.period, "date": slot.date}) == expected
