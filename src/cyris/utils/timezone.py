"""Timezone utilities for schedule conversion and timezone-aware datetime."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def now_in_timezone(tz_name: str) -> datetime:
    """Get current time in the specified timezone.

    Args:
        tz_name: IANA timezone name (e.g., "Asia/Taipei").

    Returns:
        Timezone-aware datetime in the specified timezone.

    Raises:
        ValueError: If timezone name is invalid.
    """
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, KeyError) as e:
        raise ValueError(f"Invalid timezone: {tz_name}") from e
    return datetime.now(tz=tz)


def convert_to_utc(local_time: str, tz_name: str) -> tuple[int, int]:
    """Convert a local HH:MM time to UTC hour and minute.

    Uses today's date for DST calculation.

    Args:
        local_time: Time string in HH:MM format.
        tz_name: IANA timezone name.

    Returns:
        (utc_hour, utc_minute) tuple.

    Raises:
        ValueError: If timezone or time format is invalid.
    """
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, KeyError) as e:
        raise ValueError(f"Invalid timezone: {tz_name}") from e

    hour, _, minute = local_time.partition(":")
    if not hour.isdigit() or not minute.isdigit():
        raise ValueError(f"Invalid time format: {local_time}")

    local_dt = datetime.now(tz=tz).replace(
        hour=int(hour), minute=int(minute), second=0, microsecond=0
    )
    utc_dt = local_dt.astimezone(UTC)
    return (utc_dt.hour, utc_dt.minute)
