"""macOS launchd schedule manager for Cyris digest runs."""

import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

from cyris.utils.timezone import convert_to_utc

logger = logging.getLogger(__name__)

PLIST_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" \
"http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>

    <key>ProgramArguments</key>
    <array>
        <string>{python}</string>
        <string>-m</string>
        <string>cyris</string>
        <string>{subcommand}</string>
        <string>--config</string>
        <string>{config_path}</string>
        <string>--sources</string>
        <string>{sources_path}</string>
    </array>

    <key>WorkingDirectory</key>
    <string>{working_dir}</string>

{schedule_block}

    <key>StandardOutPath</key>
    <string>{log_dir}/{log_basename}.out.log</string>

    <key>StandardErrorPath</key>
    <string>{log_dir}/{log_basename}.err.log</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>{path_env}</string>
    </dict>
</dict>
</plist>
"""

INTERVAL_TEMPLATE = """\
        <dict>
            <key>Hour</key>
            <integer>{hour}</integer>
            <key>Minute</key>
            <integer>{minute}</integer>
        </dict>"""


class ScheduleManager:
    """Manage a macOS launchd plist for a scheduled cyris subcommand."""

    def __init__(
        self,
        config_path: Path,
        sources_path: Path,
        label: str = "com.cyris.digest",
        subcommand: str = "run",
        log_basename: str = "cyris",
    ) -> None:
        self.config_path = config_path.resolve()
        self.sources_path = sources_path.resolve()
        self.label = label
        self.subcommand = subcommand
        self.log_basename = log_basename
        self._plist_path = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"

    def _render(self, schedule_block: str) -> str:
        log_dir = Path.home() / "Library" / "Logs" / "cyris"
        return PLIST_TEMPLATE.format(
            label=self.label,
            python=sys.executable,
            subcommand=self.subcommand,
            config_path=self.config_path,
            sources_path=self.sources_path,
            working_dir=self.config_path.parent,
            schedule_block=schedule_block,
            log_dir=log_dir,
            log_basename=self.log_basename,
            path_env=str(Path(sys.executable).parent),
        )

    def generate_plist(self, schedule_times: list[str], timezone: str) -> str:
        """Generate launchd plist XML firing at UTC-converted clock times."""
        intervals = []
        for time_str in schedule_times:
            utc_hour, utc_minute = convert_to_utc(time_str, timezone)
            intervals.append(INTERVAL_TEMPLATE.format(hour=utc_hour, minute=utc_minute))
        block = "    <key>StartCalendarInterval</key>\n    <array>\n" + "\n".join(intervals)
        block += "\n    </array>"
        return self._render(block)

    def generate_plist_interval(self, seconds: int) -> str:
        """Generate launchd plist XML firing every `seconds`."""
        block = f"    <key>StartInterval</key>\n    <integer>{seconds}</integer>"
        return self._render(block)

    def _write_and_load(self, plist_content: str) -> Path:
        self._plist_path.parent.mkdir(parents=True, exist_ok=True)
        (Path.home() / "Library" / "Logs" / "cyris").mkdir(parents=True, exist_ok=True)

        # Unload first if already loaded
        if self._plist_path.exists():
            subprocess.run(["launchctl", "unload", str(self._plist_path)], capture_output=True)

        self._plist_path.write_text(plist_content)
        logger.info("Plist written to %s", self._plist_path)

        result = subprocess.run(
            ["launchctl", "load", str(self._plist_path)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"launchctl load failed: {result.stderr}")

        logger.info("Loaded launchd job: %s", self.label)
        return self._plist_path

    def install(self, schedule_times: list[str], timezone: str) -> Path:
        """Write the calendar-scheduled plist and load it. Returns plist path."""
        return self._write_and_load(self.generate_plist(schedule_times, timezone))

    def install_interval(self, seconds: int) -> Path:
        """Write the interval-scheduled plist and load it. Returns plist path."""
        return self._write_and_load(self.generate_plist_interval(seconds))

    def uninstall(self) -> None:
        """Unload and remove the plist."""
        if not self._plist_path.exists():
            raise FileNotFoundError(f"Plist not found: {self._plist_path}")

        result = subprocess.run(
            ["launchctl", "unload", str(self._plist_path)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.warning("launchctl unload returned: %s", result.stderr)

        self._plist_path.unlink()
        logger.info("Removed plist: %s", self._plist_path)

    def status(self) -> dict[str, Any]:
        """Check if the job is installed and loaded."""
        installed = self._plist_path.exists()
        loaded = False

        if installed:
            result = subprocess.run(
                ["launchctl", "list", self.label],
                capture_output=True,
                text=True,
            )
            loaded = result.returncode == 0

        return {
            "installed": installed,
            "loaded": loaded,
            "plist_path": str(self._plist_path) if installed else None,
            "label": self.label,
        }
