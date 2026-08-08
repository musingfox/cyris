"""The [rss] section and its bootstrap wiring (Cloudflare RSS Worker buffer)."""

from cyris.bootstrap import build_deps
from cyris.config import load_config

CONFIG = """
[miniflux]
url = "http://localhost:8085"

[agent_vault]
path = "{vault}"

[obsidian]
user_vault_path = "{vault}"
{rss}
"""

SOURCES = """
sources:
  - name: "A"
    url: "https://a.test/feed"
"""


def _config(tmp_path, monkeypatch, rss_section: str):
    monkeypatch.setenv("CYRIS_MINIFLUX_API_KEY", "key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")
    config_path = tmp_path / "cyris.toml"
    config_path.write_text(CONFIG.format(vault=tmp_path, rss=rss_section), encoding="utf-8")
    sources_path = tmp_path / "sources.yaml"
    sources_path.write_text(SOURCES, encoding="utf-8")
    return load_config(config_path=config_path, sources_path=sources_path)


def test_token_comes_from_the_environment(tmp_path, monkeypatch):
    """Secrets live in .env, never in cyris.toml — same rule as the other Workers."""
    monkeypatch.setenv("CYRIS_RSS_TOKEN", "from-env")
    cfg = _config(tmp_path, monkeypatch, '\n[rss]\nworker_url = "https://rss.test"\n')

    assert cfg.app.rss.worker_url == "https://rss.test"
    assert cfg.app.rss.token == "from-env"


def test_source_is_wired_only_when_url_and_token_are_both_set(tmp_path, monkeypatch):
    monkeypatch.setenv("CYRIS_RSS_TOKEN", "tok")
    cfg = _config(tmp_path, monkeypatch, '\n[rss]\nworker_url = "https://rss.test"\n')

    names = [type(s).__name__ for s in build_deps(cfg).fetch_sources]
    assert "CloudflareRssSource" in names
    # Miniflux keeps running alongside it; fetch_all_articles dedups by URL.
    assert "MinifluxSource" in names


def test_absent_section_leaves_the_buffer_unwired(tmp_path, monkeypatch):
    monkeypatch.delenv("CYRIS_RSS_TOKEN", raising=False)
    cfg = _config(tmp_path, monkeypatch, "")

    names = [type(s).__name__ for s in build_deps(cfg).fetch_sources]
    assert "CloudflareRssSource" not in names
