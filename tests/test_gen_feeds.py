"""Tests for workers/rss/gen-feeds.py source-file fallback."""

import importlib.util
from pathlib import Path

import pytest

_GEN = Path(__file__).resolve().parents[1] / "workers" / "rss" / "gen-feeds.py"
_spec = importlib.util.spec_from_file_location("gen_feeds", _GEN)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
sources_path = _mod.sources_path


def test_prefers_sources_yaml_when_both_exist(tmp_path: Path) -> None:
    (tmp_path / "sources.yaml").write_text("sources: []\n", encoding="utf-8")
    (tmp_path / "sources.example.yaml").write_text("sources: []\n", encoding="utf-8")

    assert str(sources_path(tmp_path)).endswith("sources.yaml")


def test_falls_back_to_example_when_sources_yaml_missing(tmp_path: Path) -> None:
    (tmp_path / "sources.example.yaml").write_text("sources: []\n", encoding="utf-8")

    assert str(sources_path(tmp_path)).endswith("sources.example.yaml")


def test_missing_both_raises_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="sources.yaml") as exc:
        sources_path(tmp_path)
    assert "sources.example.yaml" in str(exc.value)
