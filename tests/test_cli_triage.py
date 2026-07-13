"""Tests for CLI triage command."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cyris.adapters.store.article_store import ArticleStore
from cyris.domain.models import Article, Tier
from cyris.entrypoints.cli import app

runner = CliRunner()


@pytest.fixture
def setup_triage(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Create config, store with articles, and digests with feedback."""
    # Create config files
    toml_content = f"""
[general]
timezone = "Asia/Taipei"

[miniflux]
url = "http://localhost"
api_key = "test"

[llm_provider]
api_key = "test"
model = "test"

[obsidian]
user_vault_path = "{tmp_path / "user-vault"}"
digest_folder = "Digests"

[agent_vault]
path = "{tmp_path / "agent-vault"}"
"""
    config_path = tmp_path / "cyris.toml"
    config_path.write_text(toml_content)
    sources_path = tmp_path / "sources.yaml"
    sources_path.write_text("sources: []")

    # Create directories
    user_vault = tmp_path / "user-vault"
    agent_vault = tmp_path / "agent-vault"
    user_vault.mkdir()
    agent_vault.mkdir()

    # Create store with articles
    store = ArticleStore(agent_vault)
    now = datetime.now(UTC)
    articles = [
        Article(
            id=1,
            title="Article 1",
            url="https://example.com/1",
            content="Content 1",
            published_at=now,
            source_name="Source A",
            source_tier=Tier.FILTER,
        ),
        Article(
            id=2,
            title="Article 2",
            url="https://example.com/2",
            content="Content 2",
            published_at=now,
            source_name="Source B",
            source_tier=Tier.SUMMARIZE,
        ),
    ]
    store.save(articles, now=now)

    # Create digests with feedback
    digest_path = user_vault / "Digests"
    digest_path.mkdir()

    # Digest 1 with 2 checked articles
    digest1 = digest_path / "2026-03-29_morning.md"
    digest1.write_text("""---
date: 2026-03-29
---

# Digest

## Section

- **[Article 1](https://example.com/1)** — Summary 1 (Source A)
  - [x] deep-read
- **[Article 2](https://example.com/2)** — Summary 2 (Source B)
  - [x] deep-read
""")

    # Digest 2 with 1 unchecked article
    digest2 = digest_path / "2026-03-28_morning.md"
    digest2.write_text("""---
date: 2026-03-28
---

# Digest

## Section

- **[Article 3](https://example.com/3)** — Summary 3 (Source C)
  - [ ] deep-read
""")

    return tmp_path, config_path, sources_path


def test_triage_full_flow(setup_triage: tuple[Path, Path, Path]) -> None:
    """Test full triage flow with 2 checked articles."""
    tmp_path, config_path, sources_path = setup_triage

    result = runner.invoke(
        app,
        [
            "articles",
            "triage",
            "--config",
            str(config_path),
            "--sources",
            str(sources_path),
        ],
    )

    assert result.exit_code == 0
    assert "已接受 2 篇文章" in result.stdout
    assert "已匯出 2 篇文章" in result.stdout


def test_triage_with_digest_limit(setup_triage: tuple[Path, Path, Path]) -> None:
    """Test triage with --digest-limit=1 scans only 1 digest."""
    tmp_path, config_path, sources_path = setup_triage

    result = runner.invoke(
        app,
        [
            "articles",
            "triage",
            "--digest-limit",
            "1",
            "--config",
            str(config_path),
            "--sources",
            str(sources_path),
        ],
    )

    assert result.exit_code == 0
    assert "掃描了 1 個 digest 檔案" in result.stdout


def test_triage_no_digests(tmp_path: Path) -> None:
    """Test triage with no digest files exits gracefully."""
    # Create minimal config with empty digest folder
    toml_content = f"""
[general]
timezone = "Asia/Taipei"

[miniflux]
url = "http://localhost"
api_key = "test"

[llm_provider]
api_key = "test"
model = "test"

[obsidian]
user_vault_path = "{tmp_path / "user-vault"}"
digest_folder = "Digests"

[agent_vault]
path = "{tmp_path / "agent-vault"}"
"""
    config_path = tmp_path / "cyris.toml"
    config_path.write_text(toml_content)
    sources_path = tmp_path / "sources.yaml"
    sources_path.write_text("sources: []")

    # Create empty digest folder
    user_vault = tmp_path / "user-vault"
    digest_path = user_vault / "Digests"
    digest_path.mkdir(parents=True)

    (tmp_path / "agent-vault").mkdir()

    result = runner.invoke(
        app,
        [
            "articles",
            "triage",
            "--config",
            str(config_path),
            "--sources",
            str(sources_path),
        ],
    )

    assert result.exit_code == 0
    assert "錯誤" in result.stdout or "digest" in result.stdout.lower()


def test_triage_with_custom_export_folder(setup_triage: tuple[Path, Path, Path]) -> None:
    """Test triage with custom export folder."""
    tmp_path, config_path, sources_path = setup_triage

    result = runner.invoke(
        app,
        [
            "articles",
            "triage",
            "--export-folder",
            "MyReading",
            "--config",
            str(config_path),
            "--sources",
            str(sources_path),
        ],
    )

    assert result.exit_code == 0
    assert "MyReading" in result.stdout

    # Verify files were exported to correct folder
    export_folder = tmp_path / "user-vault" / "MyReading"
    assert export_folder.exists()
    assert len(list(export_folder.glob("*.md"))) == 2
