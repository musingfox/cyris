"""Tests for HTML digest rendering."""

from datetime import UTC, datetime
from pathlib import Path

from cyris.adapters.output.html_digest import HtmlDigestWriter
from cyris.domain.models import (
    ArticleState,
    DigestContent,
    DigestItem,
    DigestSection,
    StoredArticle,
    Tier,
    UsageStats,
)


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
                    # Both quote styles: href is double-quoted, data-urls single-quoted.
                    urls=["https://evil.test/?q=1\" onclick=\"alert(2)&x=' onmouseover='alert(3)"],
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
    assert "In Focus" not in html
    assert "On the Radar" not in html
    assert "The Wire" not in html


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
    assert "In Focus" in html
    assert "Tech Industry" in html
    # Thematic summaries render inside the Features stream, not as their own section
    assert "Research Paper" in html
    assert "Thematic Summaries" not in html
    assert "On the Radar" in html
    assert "Worth Watching" in html
    assert "The Wire" in html
    assert "Headline 1" in html
    assert "5 awaiting triage" in html


def test_promote_buttons_on_every_section(tmp_path):
    """Every rendered item is votable, and a cluster's vote carries all its articles."""
    item = lambda n: DigestItem(  # noqa: E731
        title=f"Item {n}", summary="s", sources=["Src"], urls=[f"https://x.com/{n}"]
    )
    content = DigestContent(
        date="2026-04-15",
        period="evening",
        sources_processed=1,
        articles_received=1,
        articles_included=1,
        usage=UsageStats(),
        featured_articles=[DigestSection(heading="Top", items=[item(1), item(2)])],
        news_clusters=[
            DigestSection(
                heading="Tech",
                items=[
                    DigestItem(
                        title="Cluster",
                        summary="s",
                        sources=["A", "B"],
                        urls=["https://na.com", "https://nb.com"],
                    )
                ],
            )
        ],
        fan_sections=[DigestSection(heading="Fan", items=[item(3)])],
        thematic_summaries=[DigestSection(heading="Theme", items=[item(4)])],
        attention_sections=[DigestSection(heading="Watch", items=[item(5)])],
        filtered_headlines=[item(6)],
    )

    writer = HtmlDigestWriter(tmp_path, promote_worker_url="https://w.dev", promote_token="t")
    html = writer.render(content)

    # lead + featured + cluster + fan + thematic + attention + headline
    assert html.count('class="vote-group"') == 7
    assert 'data-urls=\'["https://na.com", "https://nb.com"]\'' in html
    # The deep-read queue is gone: Obsidian Clipper covers saving, cyris only filters.
    assert "深讀" not in html
    assert 'data-vote="deep"' not in html
    # Every article in the cluster stays individually openable.
    assert '<a href="https://na.com" target="_blank" rel="noopener">A</a>' in html
    assert '<a href="https://nb.com" target="_blank" rel="noopener">B</a>' in html
    # Two sources is not a mess; folding it would cost a tap for nothing.
    assert "<details" not in html


def _cluster_digest(n_sources: int) -> DigestContent:
    return DigestContent(
        date="2026-04-15",
        period="evening",
        sources_processed=1,
        articles_received=1,
        articles_included=1,
        usage=UsageStats(),
        news_clusters=[
            DigestSection(
                heading="Tech",
                items=[
                    DigestItem(
                        title="Cluster",
                        summary="s",
                        sources=[f"S{i}" for i in range(n_sources)],
                        urls=[f"https://n{i}.com" for i in range(n_sources)],
                    )
                ],
            )
        ],
    )


def test_a_crowded_cluster_folds_its_sources(tmp_path):
    """Five source links wrap into a mess on a phone, so they collapse behind a tap."""
    html = HtmlDigestWriter(tmp_path).render(_cluster_digest(5))

    assert '<details class="src-fold">' in html
    assert "<summary>5 sources</summary>" in html
    # Folded, not dropped — every link is still in the markup and still reachable.
    for i in range(5):
        assert f'<a href="https://n{i}.com" target="_blank" rel="noopener">S{i}</a>' in html


def test_vote_buttons_use_arrows_not_emoji(tmp_path):
    """Bare emoji ignore `color`, so .done could never tint them to the accent."""
    writer = HtmlDigestWriter(tmp_path, promote_worker_url="https://w.dev", promote_token="t")
    html = writer.render(_cluster_digest(2))

    assert '<button class="promote-btn" data-vote="up" title="想看">↑</button>' in html
    assert '<button class="promote-btn" data-vote="down" title="不想看">↓</button>' in html
    assert "👍" not in html
    assert "👎" not in html


def test_fan_item_links_to_newsletter_references_but_votes_on_store_url(tmp_path):
    """Newsletter references are reader links; the store URL remains the vote key."""
    item = DigestItem(
        title="Newsletter item",
        summary="s",
        sources=["Newsletter"],
        urls=["newsletter:abc"],
        ref_urls=["https://r1.com/a", "https://r2.com/b"],
    )
    content = DigestContent(
        date="2026-04-15",
        period="evening",
        sources_processed=1,
        articles_received=1,
        articles_included=1,
        usage=UsageStats(),
        fan_sections=[DigestSection(heading="Fan", items=[item])],
    )

    html = HtmlDigestWriter(tmp_path, promote_worker_url="https://w.dev", promote_token="t").render(
        content
    )

    assert '<a href="https://r1.com/a" target="_blank" rel="noopener">Newsletter item</a>' in html
    assert '<a href="https://r2.com/b" target="_blank" rel="noopener">r2.com</a>' in html
    assert "data-urls='[\"newsletter:abc\"]'" in html
    assert "data-urls='[&#34;https://r1.com/a&#34;" not in html


def test_mixed_cluster_references_render_while_votes_use_store_urls(tmp_path):
    item = DigestItem(
        title="Mixed cluster",
        summary="s",
        sources=["Source A", "Newsletter"],
        urls=["https://store.example/a", "newsletter:202"],
        ref_urls=[
            "https://store.example/a",
            "https://ref.example/one",
            "https://ref.example/two",
        ],
    )
    content = DigestContent(
        date="2026-04-15",
        period="evening",
        sources_processed=2,
        articles_received=2,
        articles_included=2,
        usage=UsageStats(),
        news_clusters=[DigestSection(heading="Mixed", items=[item])],
    )

    html = HtmlDigestWriter(tmp_path, promote_worker_url="https://w.dev", promote_token="t").render(
        content
    )

    for url in item.ref_urls:
        assert f'<a href="{url}" target="_blank" rel="noopener">' in html
    assert 'data-urls=\'["https://store.example/a", "newsletter:202"]\'' in html


def test_newsletter_references_render_for_every_digest_section(tmp_path):
    """Every newsletter section exposes each original article link."""
    item = DigestItem(
        title="Newsletter item",
        summary="s",
        sources=["Newsletter"],
        urls=["newsletter:abc"],
        ref_urls=["https://r1.com/a", "https://r2.com/b"],
    )
    content = DigestContent(
        date="2026-04-15",
        period="evening",
        sources_processed=1,
        articles_received=1,
        articles_included=1,
        usage=UsageStats(),
        featured_articles=[DigestSection(heading="Features", items=[item, item])],
        fan_sections=[DigestSection(heading="Fan", items=[item])],
        attention_sections=[DigestSection(heading="Attention", items=[item])],
        filtered_headlines=[item],
    )

    html = HtmlDigestWriter(tmp_path, promote_worker_url="https://w.dev", promote_token="t").render(
        content
    )

    assert html.count('<a href="https://r2.com/b" target="_blank" rel="noopener">r2.com</a>') == 5
    assert html.count('<span class="source-tag">Newsletter</span>') == 3
    assert html.count('<span class="source-tag">Source: Newsletter</span>') == 1
    assert html.count("data-urls='[\"newsletter:abc\"]'") == 5
    assert "data-urls='[&#34;https://r1.com/a&#34;" not in html


def test_fan_item_without_references_keeps_its_store_url(tmp_path):
    """Non-newsletter fan items retain their existing reader link."""
    item = DigestItem(
        title="Mailchimp item",
        summary="s",
        sources=["Newsletter"],
        urls=["https://mailchi.mp/x"],
        ref_urls=[],
    )
    content = DigestContent(
        date="2026-04-15",
        period="evening",
        sources_processed=1,
        articles_received=1,
        articles_included=1,
        usage=UsageStats(),
        fan_sections=[DigestSection(heading="Fan", items=[item])],
    )

    html = HtmlDigestWriter(tmp_path).render(content)

    assert (
        '<a href="https://mailchi.mp/x" target="_blank" rel="noopener">Mailchimp item</a>' in html
    )


def test_news_cluster_without_references_keeps_existing_source_markup(tmp_path):
    """RSS cluster source links keep their existing source labels and order."""
    html = HtmlDigestWriter(tmp_path).render(_cluster_digest(2))

    assert (
        '<a href="https://n0.com" target="_blank" rel="noopener">S0</a> · '
        '<a href="https://n1.com" target="_blank" rel="noopener">S1</a>'
    ) in html


def test_fan_item_with_one_reference_keeps_existing_source_markup(tmp_path):
    """A single original link changes the destination, not the source label."""
    item = DigestItem(
        title="Newsletter item",
        summary="s",
        sources=["Newsletter"],
        urls=["newsletter:abc"],
        ref_urls=["https://r1.com/a"],
    )
    content = DigestContent(
        date="2026-04-15",
        period="evening",
        sources_processed=1,
        articles_received=1,
        articles_included=1,
        usage=UsageStats(),
        fan_sections=[DigestSection(heading="Fan", items=[item])],
    )

    html = HtmlDigestWriter(tmp_path).render(content)

    assert '<span class="source-tag">Newsletter</span>' in html
    assert '<a href="https://r1.com/a" target="_blank" rel="noopener">r1.com</a>' not in html


def test_synthetic_store_url_never_becomes_an_href(sample_digest_content, tmp_path):
    """A newsletter:<id> store URL is a dead link: render the title and source unlinked."""
    sample_digest_content.featured_articles = [
        DigestSection(
            heading="Featured",
            items=[
                DigestItem(
                    title="曼報本期",
                    summary="內文",
                    sources=["曼報"],
                    urls=["newsletter:deadbeef"],
                )
            ],
        )
    ]

    html = HtmlDigestWriter(tmp_path).render(sample_digest_content)

    assert "曼報本期" in html
    assert "newsletter:deadbeef" not in html.replace("data-urls='[\"newsletter:deadbeef\"]'", "")


def _stored(title, source, state=ArticleState.PENDING, score=None):
    now = datetime.now(UTC)
    return StoredArticle(
        url=f"https://example.com/{abs(hash(title))}",
        original_id=abs(hash(title)),
        title=title,
        content="",
        published_at=now,
        source_name=source,
        source_tier=Tier.FILTER,
        state=state,
        first_seen_at=now,
        score=score,
    )


def test_write_raw_groups_by_source_and_escapes(tmp_path):
    """Raw page groups articles per source and escapes feed-controlled titles."""
    writer = HtmlDigestWriter(tmp_path)
    articles = [
        _stored("Kept", "Src A", state=ArticleState.ACCEPTED, score=0.9),
        _stored("Unscored", "Src A"),
        _stored("<script>alert(1)</script> & co", "Src B", state=ArticleState.REJECTED),
    ]

    path = writer.write_raw("2026-08-20", "morning", articles)
    html = path.read_text(encoding="utf-8")

    assert path == tmp_path / "2026-08-20-morning-raw.html"
    assert "Src A" in html and "Src B" in html
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; co" in html
    # Scored article precedes the unscored one within its source group
    assert html.index("Kept") < html.index("Unscored")
    assert "3 articles" in html
    assert 'href="2026-08-20-morning.html"' in html


def test_index_skips_raw_pages(tmp_path):
    """Archive index lists digests only — raw companions are not issues."""
    writer = HtmlDigestWriter(tmp_path)
    (tmp_path / "2026-08-20-morning.html").write_text("x")
    (tmp_path / "2026-08-20-morning-raw.html").write_text("x")

    index = writer.render_index(tmp_path)

    assert "2026-08-20-morning.html" in index
    assert "morning-raw" not in index


def test_write_raw_renders_vote_buttons_when_promote_configured(tmp_path):
    """Rejected articles get up/down buttons so the raw page can pull them back."""
    writer = HtmlDigestWriter(tmp_path, "https://promote.example/", "tok")
    articles = [_stored("Dropped", "Src A", state=ArticleState.REJECTED)]

    html = writer.write_raw("2026-08-20", "morning", articles).read_text(encoding="utf-8")

    assert html.count('class="vote-group"') == 1
    assert 'data-vote="up"' in html and 'data-vote="down"' in html
    assert '"https://promote.example"' in html

    plain = HtmlDigestWriter(tmp_path).write_raw("2026-08-20", "evening", articles)
    assert 'class="vote-group"' not in plain.read_text(encoding="utf-8")
