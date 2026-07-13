"""Tests for launchd schedule manager."""

from cyris.schedule.launchd import ScheduleManager


class TestScheduleManager:
    def test_generate_plist_utc_conversion(self, tmp_path):
        config = tmp_path / "cyris.toml"
        sources = tmp_path / "sources.yaml"
        config.touch()
        sources.touch()

        manager = ScheduleManager(config, sources)
        plist = manager.generate_plist(["08:00", "20:00"], "Asia/Taipei")

        # Asia/Taipei is UTC+8, so 08:00 TPE = 00:00 UTC, 20:00 TPE = 12:00 UTC
        assert "<integer>0</integer>" in plist
        assert "<integer>12</integer>" in plist

    def test_generate_plist_absolute_paths(self, tmp_path):
        config = tmp_path / "cyris.toml"
        sources = tmp_path / "sources.yaml"
        config.touch()
        sources.touch()

        manager = ScheduleManager(config, sources)
        plist = manager.generate_plist(["08:00"], "UTC")

        assert "~" not in plist
        assert str(tmp_path) in plist

    def test_generate_plist_contains_label(self, tmp_path):
        config = tmp_path / "cyris.toml"
        sources = tmp_path / "sources.yaml"
        config.touch()
        sources.touch()

        manager = ScheduleManager(config, sources, label="com.test.cyris")
        plist = manager.generate_plist(["08:00"], "UTC")

        assert "com.test.cyris" in plist

    def test_generate_plist_interval(self, tmp_path):
        config = tmp_path / "cyris.toml"
        sources = tmp_path / "sources.yaml"
        config.touch()
        sources.touch()

        manager = ScheduleManager(
            config, sources, subcommand="promote-sync", log_basename="promote-sync"
        )
        plist = manager.generate_plist_interval(3600)

        assert "<key>StartInterval</key>" in plist
        assert "<integer>3600</integer>" in plist
        assert "StartCalendarInterval" not in plist
        assert "<string>promote-sync</string>" in plist
        assert "promote-sync.out.log" in plist

    def test_status_not_installed(self, tmp_path):
        manager = ScheduleManager(
            tmp_path / "cyris.toml", tmp_path / "sources.yaml", label="com.test.cyris.nonexistent"
        )
        status = manager.status()

        assert status["installed"] is False
        assert status["loaded"] is False
        assert status["plist_path"] is None
