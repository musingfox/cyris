"""What this process has used so far: CPU seconds and peak resident memory."""

from __future__ import annotations

import sys

try:
    import resource
except ImportError:  # no POSIX `resource` on this platform; the run goes unmeasured
    resource = None

_KIB = 1024


def available() -> bool:
    return resource is not None


def peak_rss_bytes(ru_maxrss: int, platform: str) -> int:
    """`ru_maxrss` in bytes: macOS reports bytes, Linux reports KiB."""
    return ru_maxrss if platform == "darwin" else ru_maxrss * _KIB


def read_process_usage() -> dict[str, float | int]:
    """The keys `run_digest` adds to its summary: `cpu_seconds` and `peak_rss_bytes`."""
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return {
        "cpu_seconds": round(usage.ru_utime + usage.ru_stime, 2),
        "peak_rss_bytes": peak_rss_bytes(usage.ru_maxrss, sys.platform),
    }
