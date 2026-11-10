"""`read_process_usage`: the unit `ru_maxrss` arrives in on each platform, normalized to bytes."""

import importlib.util
import resource
import sys
from types import SimpleNamespace

import pytest

from cyris.adapters import process_usage
from cyris.adapters.process_usage import peak_rss_bytes, read_process_usage

pytestmark = pytest.mark.unit


def test_macos_reports_ru_maxrss_in_bytes_already() -> None:
    assert peak_rss_bytes(150_000_000, "darwin") == 150_000_000


def test_linux_reports_ru_maxrss_in_kib() -> None:
    assert peak_rss_bytes(146_484, "linux") == 146_484 * 1024


@pytest.mark.parametrize(
    ("platform", "maxrss", "expected"),
    [("darwin", 2_097_152, 2_097_152), ("linux", 2_048, 2_097_152)],
)
def test_the_read_returns_both_keys_in_bytes_and_seconds(
    monkeypatch, platform: str, maxrss: int, expected: int
) -> None:
    fake = SimpleNamespace(ru_utime=1.5, ru_stime=0.25, ru_maxrss=maxrss)
    monkeypatch.setattr(process_usage.resource, "getrusage", lambda _who: fake)
    monkeypatch.setattr(process_usage.sys, "platform", platform)

    got = read_process_usage()

    assert got == {"cpu_seconds": 1.75, "peak_rss_bytes": expected}
    assert isinstance(got["cpu_seconds"], float)
    assert isinstance(got["peak_rss_bytes"], int)


def test_the_read_asks_for_this_process_only(monkeypatch) -> None:
    asked: list[int] = []

    def getrusage(who: int):
        asked.append(who)
        return SimpleNamespace(ru_utime=0.0, ru_stime=0.0, ru_maxrss=1)

    monkeypatch.setattr(process_usage.resource, "getrusage", getrusage)

    read_process_usage()

    assert asked == [resource.RUSAGE_SELF]


def test_the_module_imports_where_the_resource_module_does_not_exist(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "resource", None)
    spec = importlib.util.spec_from_file_location(
        "process_usage_without_resource", process_usage.__file__
    )
    module = importlib.util.module_from_spec(spec)

    spec.loader.exec_module(module)

    assert module.resource is None
    assert module.available() is False


def test_the_read_is_available_where_the_resource_module_is() -> None:
    assert process_usage.available() is True
