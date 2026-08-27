"""Tests for CLI articles commands."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cyris.adapters.store import ArticleStore
from cyris.domain.models import Article, ArticleState, Tier
from cyris.entrypoints.cli import app

runner = CliRunner()


@pytest.fixture
def setup_store(tmp_path: Path) -> tuple[Path, Path, Path, ArticleStore]:
    """Create a store with test articles and a minimal config."""
    # Create config files
    toml_content = f'''
[general]
timezone = "Asia/Taipei"

[llm_provider]
api_key = "test"
model = "test"

[obsidian]
user_vault_path = "{tmp_path / "vault"}"
digest_folder = "Digests"

[agent_vault]
path = "{tmp_path / "agent-vault"}"
'''
    config_path = tmp_path / "cyris.toml"
    config_path.write_text(toml_content)
    sources_path = tmp_path / "sources.yaml"
    sources_path.write_text("sources: []")

    # Create directories
    (tmp_path / "vault").mkdir()
    (tmp_path / "agent-vault").mkdir()

    # Create store with articles
    store = ArticleStore(tmp_path / "agent-vault")
    now = datetime.now(UTC)
    articles = [
        Article(
            id=1,
            title="Pending Article",
            url="https://example.com/1",
            content="Content 1",
            published_at=now,
            source_name="Source A",
            source_tier=Tier.FILTER,
        ),
        Article(
            id=2,
            title="Another Pending",
            url="https://example.com/2",
            content="Content 2",
            published_at=now,
            source_name="Source B",
            source_tier=Tier.SUMMARIZE,
        ),
    ]
    store.save(articles, now=now)

    return tmp_path, config_path, sources_path, store


def test_articles_list_pending(setup_store: tuple[Path, Path, Path, ArticleStore]) -> None:
    """Test articles list with pending state filter."""
    tmp_path, config_path, sources_path, store = setup_store
    result = runner.invoke(
        app,
        [
            "articles",
            "list",
            "--state",
            "pending",
            "--config",
            str(config_path),
            "--sources",
            str(sources_path),
        ],
    )
    assert result.exit_code == 0
    assert "Pending Article" in result.stdout or "PENDING" in result.stdout


def test_articles_list_json(setup_store: tuple[Path, Path, Path, ArticleStore]) -> None:
    """Test articles list with JSON format."""
    tmp_path, config_path, sources_path, store = setup_store
    result = runner.invoke(
        app,
        [
            "articles",
            "list",
            "--format",
            "json",
            "--config",
            str(config_path),
            "--sources",
            str(sources_path),
        ],
    )
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert isinstance(data, list)


def test_articles_list_urls(setup_store: tuple[Path, Path, Path, ArticleStore]) -> None:
    """Test articles list with URLs format."""
    tmp_path, config_path, sources_path, store = setup_store
    result = runner.invoke(
        app,
        [
            "articles",
            "list",
            "--format",
            "urls",
            "--config",
            str(config_path),
            "--sources",
            str(sources_path),
        ],
    )
    assert result.exit_code == 0
    lines = [line for line in result.stdout.strip().split("\n") if line.startswith("http")]
    assert len(lines) == 2


def test_articles_accept(setup_store: tuple[Path, Path, Path, ArticleStore]) -> None:
    """Test accepting articles by URL."""
    tmp_path, config_path, sources_path, store = setup_store
    result = runner.invoke(
        app,
        [
            "articles",
            "accept",
            "https://example.com/1",
            "--config",
            str(config_path),
            "--sources",
            str(sources_path),
        ],
    )
    assert result.exit_code == 0
    assert "1" in result.stdout
    # Verify state updated
    articles = store.get_by_urls(["https://example.com/1"])
    assert len(articles) == 1
    assert articles[0].state == ArticleState.ACCEPTED


def test_articles_accept_without_vault(setup_store: tuple[Path, Path, Path, ArticleStore]) -> None:
    """Test accepting articles without vault_path configured."""
    tmp_path, config_path, sources_path, store = setup_store
    # Create config without vault_path
    toml_content = f'''
[general]
timezone = "Asia/Taipei"

[llm_provider]
api_key = "test"
model = "test"

[obsidian]
digest_folder = "Digests"

[agent_vault]
path = "{tmp_path / "agent-vault"}"
'''
    config_path.write_text(toml_content)

    result = runner.invoke(
        app,
        [
            "articles",
            "accept",
            "https://example.com/1",
            "--config",
            str(config_path),
            "--sources",
            str(sources_path),
        ],
    )
    assert result.exit_code == 0
    assert "Accepted 1" in result.stdout
    assert "Exported" not in result.stdout
    # Verify state still updated
    articles = store.get_by_urls(["https://example.com/1"])
    assert len(articles) == 1
    assert articles[0].state == ArticleState.ACCEPTED


def test_articles_accept_not_in_store(setup_store: tuple[Path, Path, Path, ArticleStore]) -> None:
    """Test accepting article URL not in store."""
    tmp_path, config_path, sources_path, store = setup_store
    result = runner.invoke(
        app,
        [
            "articles",
            "accept",
            "https://example.com/nonexistent",
            "--config",
            str(config_path),
            "--sources",
            str(sources_path),
        ],
    )
    assert result.exit_code == 0
    assert "Accepted 0" in result.stdout
    assert "Exported" not in result.stdout


def test_articles_reject(setup_store: tuple[Path, Path, Path, ArticleStore]) -> None:
    """Test rejecting articles by URL with reason."""
    tmp_path, config_path, sources_path, store = setup_store
    result = runner.invoke(
        app,
        [
            "articles",
            "reject",
            "https://example.com/1",
            "--reason",
            "spam",
            "--config",
            str(config_path),
            "--sources",
            str(sources_path),
        ],
    )
    assert result.exit_code == 0
    assert "1" in result.stdout


def test_articles_clean(setup_store: tuple[Path, Path, Path, ArticleStore]) -> None:
    """Test cleaning old rejected articles."""
    tmp_path, config_path, sources_path, store = setup_store
    store.update_article_state("https://example.com/1", ArticleState.REJECTED, reason="x")
    result = runner.invoke(
        app,
        [
            "articles",
            "clean",
            "--confirm",
            "--older-than",
            "0",
            "--config",
            str(config_path),
            "--sources",
            str(sources_path),
        ],
    )
    assert result.exit_code == 0
    assert "1" in result.stdout or "Deleted" in result.stdout
