"""Is this hour a digest hour?

The container's cron fires hourly and asks; the answer comes from the effective
`digest_schedule`, which the settings page can change without a rebuild. A fixed
`0 8,20 * * *` crontab could not — it is baked into the image.

Hour granularity is the contract, not a rounding: an hourly tick cannot honour
`08:30`, so the write surface rejects it rather than silently firing at 08:00.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

Period = Literal["morning", "evening"]


def validate_schedule(times: list[str]) -> list[str]:
    """Exactly two distinct whole hours, earliest first. Raises ValueError."""
    if len(times) != 2:
        raise ValueError("the digest schedule is exactly two times: a morning and an evening")
    hours = []
    for t in times:
        try:
            hour, minute = (int(part) for part in t.split(":", 1))
        except ValueError:
            raise ValueError(f"{t!r} is not a HH:MM time") from None
        if not 0 <= hour <= 23 or minute != 0:
            raise ValueError(f"{t!r} must be a whole hour — the schedule tick is hourly")
        hours.append(hour)
    if hours[0] == hours[1]:
        raise ValueError("the two digest times must be different hours")
    return [f"{h:02d}:00" for h in sorted(hours)]


def due_period(now: datetime, times: list[str]) -> Period | None:
    """Which digest `now` is due for, or None. `now` must already be local."""
    try:
        earlier, later = validate_schedule(times)
    except ValueError:
        return None
    current = f"{now.hour:02d}:00"
    if current == earlier:
        return "morning"
    if current == later:
        return "evening"
    return None
