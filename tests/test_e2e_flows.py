"""End-to-end tests for Cyris workflows."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from cyris.adapters.store.article_store import ArticleStore
from cyris.domain.models import Article, Tier
from cyris.entrypoints.cli import app

runner = CliRunner()


def _make_claude_response(text: str, input_tokens: int = 100, output_tokens: int = 50):
    """Create a mock anthropic Message response."""
    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(text=text)]
    mock_resp.usage = MagicMock(input_tokens=input_tokens, output_tokens=output_tokens)
    return mock_resp


@pytest.fixture
def e2e_config(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """Create minimal config files and directories for E2E tests."""
    # Create cyris.toml
    toml_content = f'''
[general]
timezone = "Asia/Taipei"
digest_window_hours = 12
digest_schedule = ["08:00", "20:00"]

[general.notify]
discord_webhook_url = ""

[miniflux]
url = "http://localhost:8080"
api_key = "test-key"

[llm_provider]
api_key = "test-anthropic-key"
model = "claude-sonnet-4-6"

[digest]
max_articles_per_digest = 200

[obsidian]
user_vault_path = "{tmp_path / "user-vault"}"
digest_folder = "Digests"

[agent_vault]
path = "{tmp_path / "agent-vault"}"

[paywall]
use_browser_cookies = false
browser = "chrome"
cookie_domains = []

[email]
webhook_secret = ""
webhook_host = "0.0.0.0"
webhook_port = 8765
webhook_path = "/webhook/email"
'''
    config_path = tmp_path / "cyris.toml"
    config_path.write_text(toml_content)

    # Create sources.yaml
    sources_content = """
sources:
  - name: "TechCrunch"
    url: "https://techcrunch.com/feed/"
    tier: filter
    tags: [tech, startup]
  - name: "Reuters"
    url: "https://reuters.com/feed/"
    tier: filter
    tags: [international, news]
  - name: "Stratechery"
    url: "https://stratechery.com/feed/"
    tier: summarize
    tags: [tech, business-strategy]
"""
    sources_path = tmp_path / "sources.yaml"
    sources_path.write_text(sources_content)

    # Create vault directories
    user_vault = tmp_path / "user-vault"
    user_vault.mkdir()
    (user_vault / "Digests").mkdir()
    (user_vault / "Reading").mkdir()

    agent_vault = tmp_path / "agent-vault"
    agent_vault.mkdir()
    (agent_vault / "daily" / "newsletters").mkdir(parents=True)

    return config_path, sources_path, user_vault, agent_vault


class TestRunPipelineE2E:
    """E2E tests for run CLI command (full pipeline)."""

    def test_run_happy_path(self, e2e_config: tuple[Path, Path, Path, Path]) -> None:
        """Test full run pipeline with mocked external services."""
        config_path, sources_path, user_vault, agent_vault = e2e_config

        # Real clock throughout: run_digest must pick up articles saved within
        # the same run (regression coverage for the reload end bound).
        now = datetime.now(UTC) - timedelta(hours=1)
        sample_entries = {
            "entries": [
                {
                    "id": 101,
                    "title": "Breaking News Story 1",
                    "url": "https://reuters.com/article1",
                    "content": "First breaking news content",
                    "published_at": now,
                    "feed": {"title": "Reuters"},
                },
                {
                    "id": 102,
                    "title": "Breaking News Story 2",
                    "url": "https://reuters.com/article2",
                    "content": "Second breaking news content",
                    "published_at": now,
                    "feed": {"title": "Reuters"},
                },
                {
                    "id": 103,
                    "title": "Weekly Tech Trends",
                    "url": "https://stratechery.com/weekly",
                    "content": "Deep analysis of tech trends",
                    "published_at": now,
                    "feed": {"title": "Stratechery"},
                },
            ],
            "total": 3,
        }

        # Mock Miniflux
        with patch("cyris.adapters.fetch.miniflux.miniflux.Client") as mock_miniflux_cls:
            mock_miniflux = MagicMock()
            mock_miniflux.get_entries.return_value = sample_entries
            mock_miniflux.update_entries = MagicMock()
            mock_miniflux_cls.return_value = mock_miniflux

            # Mock Claude API for news clustering
            cluster_response = _make_claude_response(
                json.dumps(
                    {
                        "clusters": [
                            {
                                "heading": "Breaking News Cluster",
                                "summary": "Two related breaking news stories",
                                "article_ids": [101, 102],
                            }
                        ],
                        "unclustered_ids": [],
                    }
                )
            )

            # Mock Claude API for summarize
            summarize_response = _make_claude_response(
                json.dumps(
                    {
                        "sections": [
                            {
                                "heading": "Tech Analysis",
                                "summary": "Weekly technology trends and insights",
                                "articles": [
                                    {
                                        "id": 103,
                                        "title": "Weekly Tech Trends",
                                        "source": "Stratechery",
                                    }
                                ],
                            }
                        ]
                    }
                )
            )

            # Mock Claude API for scoring (article 103 is the only non-news scorable)
            score_response = _make_claude_response(
                json.dumps({"scores": [{"id": 103, "score": 85, "language": "en"}]})
            )

            # All LLM calls go through the single AnthropicClient adapter;
            # pipeline order is score -> cluster -> summarize (filter pool
            # is empty here since all filter-tier articles are news).
            with patch("cyris.adapters.anthropic_client.anthropic.AsyncAnthropic") as mock_llm_cls:
                mock_llm_client = AsyncMock()
                mock_llm_client.messages.create = AsyncMock(
                    side_effect=[score_response, cluster_response, summarize_response]
                )
                mock_llm_cls.return_value = mock_llm_client

                # Run full pipeline
                result = runner.invoke(
                    app,
                    [
                        "run",
                        "--config",
                        str(config_path),
                        "--sources",
                        str(sources_path),
                    ],
                )

        # Assertions
        assert result.exit_code == 0, f"Run failed: {result.stdout}"
        assert "Digest written to" in result.stdout

        # Verify digest file exists (date comes from the real clock)
        digest_files = list((user_vault / "Digests").glob("*-morning.md"))
        assert len(digest_files) == 1, f"Expected one digest, found {digest_files}"
        digest_file = digest_files[0]

        # Verify digest content
        digest_content = digest_file.read_text()
        assert "---" in digest_content  # Frontmatter
        assert "新聞聚合" in digest_content or "主題摘要" in digest_content  # Section headings

        # Verify article store was updated
        store = ArticleStore(agent_vault)
        articles = store.list_articles(limit=10)
        assert len(articles) == 3

    def test_run_no_articles(self, e2e_config: tuple[Path, Path, Path, Path]) -> None:
        """Test run command when no articles are found."""
        config_path, sources_path, user_vault, agent_vault = e2e_config

        # Mock Miniflux to return empty
        with patch("cyris.adapters.fetch.miniflux.miniflux.Client") as mock_miniflux_cls:
            mock_miniflux = MagicMock()
            mock_miniflux.get_entries.return_value = {"entries": [], "total": 0}
            mock_miniflux_cls.return_value = mock_miniflux

            # Run full pipeline
            result = runner.invoke(
                app,
                ["run", "--config", str(config_path), "--sources", str(sources_path)],
            )

        # Assertions
        assert result.exit_code == 0
        assert "No articles found" in result.stdout


class TestArticlesLifecycleE2E:
    """E2E tests for articles lifecycle commands."""

    @pytest.fixture
    def articles_config(self, tmp_path: Path) -> tuple[Path, Path, Path, Path, ArticleStore]:
        """Create config and store with test articles."""
        # Create config files
        toml_content = f'''
[general]
timezone = "Asia/Taipei"

[miniflux]
url = "http://localhost"
api_key = "test"

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
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "Reading").mkdir()
        (vault / "Digests").mkdir()
        agent_vault = tmp_path / "agent-vault"
        agent_vault.mkdir()

        # Create store with articles
        store = ArticleStore(agent_vault)
        now = datetime.now(UTC)
        articles = [
            Article(
                id=1,
                title="Test Article 1",
                url="https://example.com/1",
                content="Content 1",
                published_at=now,
                source_name="Source A",
                source_tier=Tier.FILTER,
            ),
            Article(
                id=2,
                title="Test Article 2",
                url="https://example.com/2",
                content="Content 2",
                published_at=now,
                source_name="Source B",
                source_tier=Tier.SUMMARIZE,
            ),
            Article(
                id=3,
                title="Test Article 3",
                url="https://example.com/3",
                content="Content 3",
                published_at=now,
                source_name="Source C",
                source_tier=Tier.FILTER,
            ),
        ]
        store.save(articles, now=now)

        return config_path, sources_path, vault, agent_vault, store

    def test_accept_and_export_lifecycle(
        self, articles_config: tuple[Path, Path, Path, Path, ArticleStore]
    ) -> None:
        """Test accepting and exporting articles."""
        config_path, sources_path, vault, agent_vault, store = articles_config

        # Step 1: Accept 2 articles
        result = runner.invoke(
            app,
            [
                "articles",
                "accept",
                "https://example.com/1",
                "https://example.com/2",
                "--config",
                str(config_path),
                "--sources",
                str(sources_path),
            ],
        )
        assert result.exit_code == 0
        assert "2" in result.stdout

        # Step 2: Export accepted articles
        result = runner.invoke(
            app,
            [
                "articles",
                "export",
                "--state",
                "accepted",
                "--config",
                str(config_path),
                "--sources",
                str(sources_path),
            ],
        )
        assert result.exit_code == 0
        assert "2" in result.stdout or "Exported" in result.stdout

        # Verify files in Reading folder
        reading_folder = vault / "Reading"
        markdown_files = list(reading_folder.glob("*.md"))
        assert len(markdown_files) == 2

        # Verify frontmatter
        for md_file in markdown_files:
            content = md_file.read_text()
            assert "---" in content
            assert "url:" in content

    def test_triage_lifecycle(
        self, articles_config: tuple[Path, Path, Path, Path, ArticleStore]
    ) -> None:
        """Test triage command that processes digest feedback."""
        config_path, sources_path, vault, agent_vault, store = articles_config

        # Create a digest file with deep-read checkboxes marked
        digest_content = """---
date: 2026-03-31
period: morning
---

# 早報 2026-03-31

## 主題摘要

### [Test Article 1](https://example.com/1)
Summary of article 1
`Sources: Source A`
- [x] deep-read
- [ ] track

### [Test Article 2](https://example.com/2)
Summary of article 2
`Sources: Source B`
- [x] deep-read
- [ ] track

---
"""
        digest_file = vault / "Digests" / "2026-03-31-morning.md"
        digest_file.write_text(digest_content)

        # Run triage command
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

        # Verify exported files
        reading_folder = vault / "Reading"
        markdown_files = list(reading_folder.glob("*.md"))
        assert len(markdown_files) == 2
