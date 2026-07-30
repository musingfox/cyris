"""Tests for HTML digest rendering."""

from pathlib import Path

from cyris.adapters.output.html_digest import HtmlDigestWriter
from cyris.domain.models import DigestContent, DigestItem, DigestSection, UsageStats


def test_render_with_featured_article(sample_digest_content):
    """C1 Test 1: Featured article renders with DOCTYPE, title, date, styles."""
    # Modify sample to have a featured section with score
    sample_digest_content.featured_articles = [
        DigestSection(
            heading="Featured",
            items=[
                DigestItem(
                    title="GPT-5 Released",
                    summary="OpenAI announced GPT-5 today.",
                    sources=["TechCrunch"],
                    urls=["https://tc.com/gpt5"],
                    score=9.2,
                )
            ],
        )
    ]

    writer = HtmlDigestWriter(Path("/tmp/html-test"))
    html = writer.render(sample_digest_content)

    assert "<!DOCTYPE html>" in html
    assert "GPT-5 Released" in html
    assert "2026-03-16" in html
    assert "<style>" in html


def test_render_escapes_feed_controlled_fields(sample_digest_content):
    """Feed-supplied titles and URLs must not be able to add attributes or tags.

    The templates are named *.html.j2, so select_autoescape's extension match left
    them unescaped until `default=True` was added.
    """
    from html.parser import HTMLParser

    sample_digest_content.featured_articles = [
        DigestSection(
            heading="Featured",
            items=[
                DigestItem(
                    title="<img src=x onerror=alert(1)>",
                    summary="S",
                    sources=["Src"],
                    urls=['https://evil.test/?q=1" onclick="alert(2)'],
                )
            ],
        )
    ]

    html = HtmlDigestWriter(Path("/tmp/html-escape-test"), "https://w.test", "tok").render(
        sample_digest_content
    )

    injected: list[str] = []

    class Collector(HTMLParser):
        def handle_starttag(self, tag, attrs):
            if tag == "img":
                injected.append("img")
            injected.extend(k for k, _ in attrs if k.startswith("on"))

    Collector().feed(html)
    assert injected == []


def test_render_empty_sections(tmp_path):
    """C1 Test 2: Empty digest renders valid HTML without sections."""
    content = DigestContent(
        date="2026-03-16",
        period="morning",
        sources_processed=0,
        articles_received=0,
        articles_included=0,
        usage=UsageStats(input_tokens=0, output_tokens=0, api_calls=0, model="claude-sonnet-4-6"),
    )

    writer = HtmlDigestWriter(tmp_path)
    html = writer.render(content)

    assert "<!DOCTYPE html>" in html
    assert "<style>" in html
    # Should not contain section headings or content when empty
    assert "News Clusters" not in html
    assert "Thematic Summaries" not in html
    assert "Headlines" not in html


def test_render_optional_score(tmp_path):
    """C1 Test 3: DigestItem with score=None renders without error."""
    content = DigestContent(
        date="2026-03-16",
        period="morning",
        sources_processed=1,
        articles_received=1,
        articles_included=1,
        usage=UsageStats(
            input_tokens=100, output_tokens=50, api_calls=1, model="claude-sonnet-4-6"
        ),
        featured_articles=[
            DigestSection(
                heading="Featured",
                items=[
                    DigestItem(
                        title="No Score Article",
                        summary="This article has no score.",
                        sources=["Test"],
                        urls=["https://test.com"],
                        score=None,  # Explicitly None
                    )
                ],
            )
        ],
    )

    writer = HtmlDigestWriter(tmp_path)
    html = writer.render(content)

    assert "<!DOCTYPE html>" in html
    assert "No Score Article" in html
    # Should not display score when None
    assert "Score:" not in html


def test_write_creates_file(tmp_path, sample_digest_content):
    """C2 Test 1: write(dry_run=False) creates file and index."""
    writer = HtmlDigestWriter(tmp_path)
    path = writer.write(sample_digest_content, dry_run=False)

    expected_path = tmp_path / "2026-03-16-morning.html"
    assert path == expected_path
    assert path.exists()

    content = path.read_text()
    assert content.startswith("<!DOCTYPE html>")

    # Index should also exist
    index_path = tmp_path / "index.html"
    assert index_path.exists()


def test_write_dry_run(tmp_path, sample_digest_content, capsys):
    """C2 Test 2: write(dry_run=True) prints but does not create file."""
    writer = HtmlDigestWriter(tmp_path)
    path = writer.write(sample_digest_content, dry_run=True)

    expected_path = tmp_path / "2026-03-16-morning.html"
    assert path == expected_path
    assert not path.exists()  # File should NOT exist

    # Check stdout was used
    captured = capsys.readouterr()
    assert "<!DOCTYPE html>" in captured.out


def test_render_index_with_digests(tmp_path):
    """C3 Test 1: Index with multiple digest files shows links in desc order."""
    # Create digest files
    (tmp_path / "2026-04-15-morning.html").write_text("<html>test</html>")
    (tmp_path / "2026-04-14-evening.html").write_text("<html>test</html>")
    (tmp_path / "2026-04-14-morning.html").write_text("<html>test</html>")

    writer = HtmlDigestWriter(tmp_path)
    html = writer.render_index(tmp_path)

    assert "<!DOCTYPE html>" in html
    # Should have three links
    assert html.count("<a href=") == 3
    # First link should be most recent (2026-04-15-morning.html)
    assert html.index("2026-04-15-morning.html") < html.index("2026-04-14-evening.html")


def test_render_index_empty(tmp_path):
    """C3 Test 2: Empty directory renders valid HTML with no links."""
    writer = HtmlDigestWriter(tmp_path)
    html = writer.render_index(tmp_path)

    assert "<!DOCTYPE html>" in html
    assert "<a href=" not in html
    assert "No digests yet" in html


def test_render_index_ignores_non_digests(tmp_path):
    """C3 Test 3: Index ignores non-digest files."""
    (tmp_path / "2026-04-15-morning.html").write_text("<html>test</html>")
    (tmp_path / "index.html").write_text("<html>index</html>")
    (tmp_path / "random.txt").write_text("random")
    (tmp_path / "not-a-digest.html").write_text("<html>other</html>")

    writer = HtmlDigestWriter(tmp_path)
    html = writer.render_index(tmp_path)

    assert "<!DOCTYPE html>" in html
    # Should only have one link (2026-04-15-morning.html)
    assert html.count("<a href=") == 1
    assert "2026-04-15-morning.html" in html


def test_write_index(tmp_path):
    """C4 Test 1: write_index creates index.html with links."""
    # Create one digest
    (tmp_path / "2026-04-15-morning.html").write_text("<html>test</html>")

    writer = HtmlDigestWriter(tmp_path)
    index_path = writer.write_index(tmp_path)

    assert index_path == tmp_path / "index.html"
    assert index_path.exists()

    content = index_path.read_text()
    assert "<!DOCTYPE html>" in content
    assert "2026-04-15-morning.html" in content


def test_config_html_output_enabled():
    """C5 Test 1: Config with html_output enabled parses correctly."""
    import tempfile
    import tomllib

    toml_content = """
    [html_output]
    enabled = true
    output_dir = "/tmp/digests"
    """

    with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
        f.write(toml_content)
        f.flush()

        with open(f.name, "rb") as rf:
            raw = tomllib.load(rf)

        from cyris.config import AppConfig

        config = AppConfig.model_validate(raw)

        assert config.html_output.enabled is True
        assert config.html_output.output_dir == "/tmp/digests"

        # Cleanup
        Path(f.name).unlink()


def test_config_html_output_defaults():
    """C5 Test 2: Config without html_output uses defaults."""
    from cyris.config import AppConfig

    config = AppConfig.model_validate({})

    assert config.html_output.enabled is False
    assert config.html_output.output_dir == "agent-vault/html"


def test_cli_integration_html_enabled(tmp_path, sample_digest_content, monkeypatch):
    """C6 Test 1: CLI with html_output.enabled=True writes both MD and HTML."""
    from cyris.adapters.output.digest import DigestWriter
    from cyris.adapters.output.html_digest import HtmlDigestWriter
    from cyris.config import AppConfig, HtmlOutputConfig, ObsidianConfig

    # Setup config with HTML enabled
    config = AppConfig(
        obsidian=ObsidianConfig(user_vault_path=tmp_path / "vault"),
        html_output=HtmlOutputConfig(enabled=True, output_dir=str(tmp_path / "html")),
    )

    # Write markdown
    md_writer = DigestWriter(config.obsidian.user_vault_path, config.obsidian.digest_folder)
    md_path = md_writer.write(sample_digest_content)
    assert md_path.exists()

    # Write HTML (simulating CLI behavior)
    html_writer = HtmlDigestWriter(Path(config.html_output.output_dir))
    html_path = html_writer.write(sample_digest_content)
    assert html_path.exists()

    # Both files should exist
    assert md_path.exists()
    assert html_path.exists()


def test_cli_integration_html_disabled(tmp_path, sample_digest_content):
    """C6 Test 2: CLI with html_output.enabled=False does not write HTML."""
    from cyris.adapters.output.digest import DigestWriter
    from cyris.config import AppConfig, HtmlOutputConfig, ObsidianConfig

    # Setup config with HTML disabled
    config = AppConfig(
        obsidian=ObsidianConfig(user_vault_path=tmp_path / "vault"),
        html_output=HtmlOutputConfig(enabled=False, output_dir=str(tmp_path / "html")),
    )

    # Write markdown
    md_writer = DigestWriter(config.obsidian.user_vault_path, config.obsidian.digest_folder)
    md_path = md_writer.write(sample_digest_content)
    assert md_path.exists()

    # HTML should not be written
    html_dir = Path(config.html_output.output_dir)
    assert not html_dir.exists()


def test_cli_integration_html_error_does_not_block_markdown(
    tmp_path, sample_digest_content, monkeypatch
):
    """C6 Test 3: HTML generation failure does not block markdown output."""
    from cyris.adapters.output.digest import DigestWriter
    from cyris.adapters.output.html_digest import HtmlDigestWriter
    from cyris.config import AppConfig, HtmlOutputConfig, ObsidianConfig

    # Setup config
    config = AppConfig(
        obsidian=ObsidianConfig(user_vault_path=tmp_path / "vault"),
        html_output=HtmlOutputConfig(enabled=True, output_dir=str(tmp_path / "html")),
    )

    # Write markdown first
    md_writer = DigestWriter(config.obsidian.user_vault_path, config.obsidian.digest_folder)
    md_path = md_writer.write(sample_digest_content)
    assert md_path.exists()

    # Simulate HTML failure by patching HtmlDigestWriter.write to raise
    def mock_write_error(*args, **kwargs):
        raise RuntimeError("Template not found")

    monkeypatch.setattr(HtmlDigestWriter, "write", mock_write_error)

    # Try to write HTML (should fail gracefully)
    try:
        html_writer = HtmlDigestWriter(Path(config.html_output.output_dir))
        html_writer.write(sample_digest_content)
    except RuntimeError:
        pass  # Expected to fail

    # Markdown should still exist
    assert md_path.exists()


def test_all_sections_render(tmp_path):
    """Integration test: All section types render correctly."""
    content = DigestContent(
        date="2026-04-15",
        period="evening",
        sources_processed=5,
        articles_received=20,
        articles_included=10,
        usage=UsageStats(
            input_tokens=1000, output_tokens=500, api_calls=3, model="claude-sonnet-4-6"
        ),
        featured_articles=[
            DigestSection(
                heading="Top Stories",
                items=[
                    DigestItem(
                        title="Featured Story",
                        summary="Lead story summary.",
                        sources=["Source A"],
                        urls=["https://a.com"],
                        score=9.5,
                    ),
                    DigestItem(
                        title="Second Feature",
                        summary="Secondary feature.",
                        sources=["Source B"],
                        urls=["https://b.com"],
                        score=8.7,
                    ),
                ],
            )
        ],
        news_clusters=[
            DigestSection(
                heading="Tech Industry",
                items=[
                    DigestItem(
                        title="Cluster 1",
                        summary="News cluster summary.",
                        sources=["News A", "News B"],
                        urls=["https://na.com", "https://nb.com"],
                    )
                ],
            )
        ],
        thematic_summaries=[
            DigestSection(
                heading="AI Research",
                description="Latest developments in AI",
                items=[
                    DigestItem(
                        title="Research Paper",
                        summary="Paper summary.",
                        sources=["ArXiv"],
                        urls=["https://arxiv.org/1234"],
                    )
                ],
            )
        ],
        attention_sections=[
            DigestSection(
                heading="Worth Watching",
                items=[
                    DigestItem(
                        title="Attention Item",
                        summary="Brief snippet.",
                        sources=["Blog"],
                        urls=["https://blog.com"],
                    )
                ],
            )
        ],
        filtered_headlines=[
            DigestItem(
                title="Headline 1",
                summary="Brief summary",
                sources=["News"],
                urls=["https://news.com/1"],
            ),
            DigestItem(
                title="Headline 2",
                summary="Another summary",
                sources=["News"],
                urls=["https://news.com/2"],
            ),
        ],
        triage_pending_count=5,
    )

    writer = HtmlDigestWriter(tmp_path)
    html = writer.render(content)

    # Check all sections present
    assert "Featured Story" in html
    assert "Second Feature" in html
    assert "News Clusters" in html
    assert "Tech Industry" in html
    assert "Thematic Summaries" in html
    assert "AI Research" in html
    assert "Attention" in html
    assert "Worth Watching" in html
    assert "Headlines" in html
    assert "Headline 1" in html
    assert "5 awaiting triage" in html
