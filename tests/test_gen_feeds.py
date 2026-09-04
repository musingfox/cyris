"""Tests for workers/rss/gen-feeds.py source-file selection."""

import importlib.util
from pathlib import Path

import pytest

_GEN = Path(__file__).resolve().parents[1] / "workers" / "rss" / "gen-feeds.py"
_spec = importlib.util.spec_from_file_location("gen_feeds", _GEN)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
sources_path = _mod.sources_path


def test_ignores_sources_yaml_even_when_it_exists(tmp_path: Path) -> None:
    """The generated file is committed to a public repo and shipped to every fork.

    Reading the author's gitignored `sources.yaml` would publish their reading
    list and make a stranger's Worker buffer feeds nobody chose.
    """
    (tmp_path / "sources.yaml").write_text("sources: []\n", encoding="utf-8")
    (tmp_path / "sources.example.yaml").write_text("sources: []\n", encoding="utf-8")

    assert str(sources_path(tmp_path)).endswith("sources.example.yaml")


def test_missing_example_raises_file_not_found(tmp_path: Path) -> None:
    (tmp_path / "sources.yaml").write_text("sources: []\n", encoding="utf-8")

    with pytest.raises(FileNotFoundError, match="sources.example.yaml"):
        sources_path(tmp_path)
