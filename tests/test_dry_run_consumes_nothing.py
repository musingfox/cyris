"""`--dry-run` previews the run; it must not consume what the run consumes.

Skipping the writes inside `run_digest` is not enough. Two edges wired in
`bootstrap` take rather than read — the newsletter queue deletes what it hands
over, and a vote sync stamps human verdicts in the store before draining its own
queue. A preview that touched either would destroy state no later run can
rebuild, which is the opposite of previewing.
"""

from pathlib import Path

import pytest

from cyris.bootstrap import build_deps
from cyris.config import load_config

CONFIG = """
[agent_vault]
path = "{vault}"

[newsletter]
worker_url = "https://newsletter.test"

[promote]
worker_url = "https://promote.test"
"""

SOURCES = """
sources:
  - name: "A"
    url: "https://a.test/feed"
"""


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")
    monkeypatch.setenv("CYRIS_WORKER_TOKEN", "tok")
    monkeypatch.setenv("CYRIS_PROMOTE_TOKEN", "promote-tok")
    config_path = tmp_path / "cyris.toml"
    config_path.write_text(CONFIG.format(vault=tmp_path), encoding="utf-8")
    sources_path = tmp_path / "sources.yaml"
    sources_path.write_text(SOURCES, encoding="utf-8")
    return load_config(config_path=config_path, sources_path=sources_path)


def _newsletter_source(deps):
    return next(s for s in deps.fetch_sources if type(s).__name__ == "CloudflareNewsletterSource")


def test_a_run_acks_the_newsletter_queue(cfg) -> None:
    assert _newsletter_source(build_deps(cfg))._ack_enabled is True


def test_a_preview_leaves_the_newsletter_queue_alone(cfg) -> None:
    assert _newsletter_source(build_deps(cfg, dry_run=True))._ack_enabled is False


def test_a_preview_still_fetches_newsletters(cfg) -> None:
    """Not by dropping the source: what the preview shows must be what the run
    would digest, and email-only sources are half of some windows."""
    names = [type(s).__name__ for s in build_deps(cfg, dry_run=True).fetch_sources]

    assert "CloudflareNewsletterSource" in names


def test_a_run_syncs_votes(cfg) -> None:
    assert build_deps(cfg).sync_promotions is not None


def test_a_preview_does_not_sync_votes(cfg) -> None:
    """`sync_promotions` accepts, rejects and stamps `triaged_at` before acking —
    every one of those a human decision the pipeline may not invent."""
    assert build_deps(cfg, dry_run=True).sync_promotions is None
