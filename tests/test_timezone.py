"""Tests for timezone utilities."""

import pytest

from cyris.utils.timezone import convert_to_utc, now_in_timezone


class TestConvertToUtc:
    def test_taipei_morning(self):
        # Asia/Taipei is UTC+8 (no DST)
        hour, minute = convert_to_utc("08:00", "Asia/Taipei")
        assert (hour, minute) == (0, 0)

    def test_taipei_evening(self):
        hour, minute = convert_to_utc("20:00", "Asia/Taipei")
        assert (hour, minute) == (12, 0)

    def test_utc_passthrough(self):
        hour, minute = convert_to_utc("15:30", "UTC")
        assert (hour, minute) == (15, 30)

    def test_invalid_timezone(self):
        with pytest.raises(ValueError, match="Invalid timezone"):
            convert_to_utc("08:00", "Fake/Timezone")

    def test_invalid_time_format(self):
        with pytest.raises(ValueError, match="Invalid time format"):
            convert_to_utc("abc", "UTC")


class TestNowInTimezone:
    def test_returns_aware_datetime(self):
        dt = now_in_timezone("Asia/Taipei")
        assert dt.tzinfo is not None

    def test_invalid_timezone(self):
        with pytest.raises(ValueError, match="Invalid timezone"):
            now_in_timezone("Fake/Zone")
