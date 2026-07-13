"""Tests for notification senders."""

from cyris.adapters.notify import build_discord_embeds
from cyris.domain.models import DigestContent, DigestItem, DigestSection


class TestDiscordEmbeds:
    def test_embeds_order_mirrors_digest(self):
        """Embeds follow Obsidian digest section order."""
        content = DigestContent(
            date="2026-04-10",
            period="morning",
            sources_processed=5,
            articles_received=20,
            articles_included=8,
            tracked_updates=DigestSection(
                heading="AI 監管",
                items=[
                    DigestItem(
                        title="AI 監管",
                        summary="歐盟推進 AI 法案",
                        sources=["Reuters"],
                        urls=["https://reuters.com/ai"],
                    )
                ],
            ),
            featured_articles=[
                DigestSection(
                    heading="Cross-Cutting Concerns",
                    items=[
                        DigestItem(
                            title="Cross-Cutting Concerns",
                            summary="API development deep dive",
                            sources=["ByteByteGo"],
                            urls=["https://bytebytego.com/1"],
                        )
                    ],
                )
            ],
            news_clusters=[
                DigestSection(
                    heading="科技裁員潮",
                    items=[
                        DigestItem(
                            title="科技裁員潮",
                            summary="多家科技公司宣布裁員",
                            sources=["Reuters", "Bloomberg"],
                            urls=["https://reuters.com/1"],
                        )
                    ],
                )
            ],
            thematic_summaries=[
                DigestSection(
                    heading="開發工具",
                    items=[
                        DigestItem(
                            title="開發工具",
                            summary="新一代 IDE 趨勢",
                            sources=["InfoQ"],
                            urls=["https://infoq.com/1"],
                        )
                    ],
                )
            ],
            attention_sections=[
                DigestSection(
                    heading="tech",
                    items=[
                        DigestItem(
                            title="Stratechery",
                            summary="Interview analysis",
                            sources=["Stratechery"],
                            urls=["https://stratechery.com/1"],
                        )
                    ],
                )
            ],
            filtered_headlines=[
                DigestItem(
                    title="Google Intel Partnership",
                    summary="Google and Intel deepen AI partnership",
                    sources=["TechCrunch"],
                    urls=["https://techcrunch.com/1"],
                )
            ],
        )

        embeds = build_discord_embeds(content)
        titles = [e["title"] for e in embeds]

        assert titles == [
            "🔔 追蹤主題更新",
            "⭐ 精選文章",
            "📰 新聞聚合",
            "📋 主題摘要",
            "👀 值得關注",
            "📌 其他標題（1篇）",
            "早報 2026-04-10",
        ]

    def test_featured_articles_embed(self):
        """Featured articles embed has heading with link and summary."""
        content = DigestContent(
            date="2026-04-10",
            period="morning",
            sources_processed=1,
            articles_received=5,
            articles_included=1,
            featured_articles=[
                DigestSection(
                    heading="API 橫切關注點",
                    items=[
                        DigestItem(
                            title="API 橫切關注點",
                            summary="深入探討 cross-cutting concerns",
                            sources=["ByteByteGo"],
                            urls=["https://bytebytego.com/article"],
                        )
                    ],
                )
            ],
        )

        embeds = build_discord_embeds(content)
        featured = next(e for e in embeds if e["title"] == "⭐ 精選文章")

        assert featured["color"] == 0xF1C40F
        assert "[API 橫切關注點](https://bytebytego.com/article)" in featured["description"]
        assert "深入探討 cross-cutting concerns" in featured["description"]
        assert "*ByteByteGo*" in featured["description"]

    def test_attention_sections_embed(self):
        """Attention sections embed renders correctly."""
        content = DigestContent(
            date="2026-04-10",
            period="morning",
            sources_processed=1,
            articles_received=3,
            articles_included=1,
            attention_sections=[
                DigestSection(
                    heading="tech",
                    items=[
                        DigestItem(
                            title="Interview Analysis",
                            summary="NYT CEO interview",
                            sources=["Stratechery"],
                            urls=["https://stratechery.com/interview"],
                        )
                    ],
                )
            ],
        )

        embeds = build_discord_embeds(content)
        attention = next(e for e in embeds if e["title"] == "👀 值得關注")

        assert attention["color"] == 0x9B59B6
        assert "[tech](https://stratechery.com/interview)" in attention["description"]
        assert "*Stratechery*" in attention["description"]

    def test_discord_embeds_with_news_clusters(self):
        content = DigestContent(
            date="2026-03-31",
            period="morning",
            sources_processed=2,
            articles_received=5,
            articles_included=2,
            news_clusters=[
                DigestSection(
                    heading="科技裁員潮",
                    items=[
                        DigestItem(
                            title="科技裁員潮",
                            summary="多家科技公司宣布裁員計畫",
                            sources=["Reuters", "Bloomberg"],
                            urls=["https://reuters.com/1", "https://bloomberg.com/2"],
                        )
                    ],
                )
            ],
        )

        embeds = build_discord_embeds(content)

        news_embed = next(e for e in embeds if e["title"] == "📰 新聞聚合")
        assert news_embed["color"] == 0xFEE75C
        assert "科技裁員潮" in news_embed["description"]
        assert "多家科技公司宣布裁員計畫" in news_embed["description"]

    def test_discord_embeds_without_news_clusters(self):
        content = DigestContent(
            date="2026-03-31",
            period="morning",
            sources_processed=1,
            articles_received=3,
            articles_included=1,
            news_clusters=[],
            filtered_headlines=[
                DigestItem(
                    title="Tech News",
                    summary="Some tech news",
                    sources=["TechCrunch"],
                    urls=["https://techcrunch.com/1"],
                )
            ],
        )

        embeds = build_discord_embeds(content)

        for embed in embeds:
            assert embed["title"] != "📰 新聞聚合"

    def test_filtered_headlines_with_links(self):
        """Filtered headlines include clickable links and truncated summaries."""
        content = DigestContent(
            date="2026-04-10",
            period="morning",
            sources_processed=1,
            articles_received=3,
            articles_included=2,
            filtered_headlines=[
                DigestItem(
                    title="Short Title",
                    summary="Brief summary",
                    sources=["Source1"],
                    urls=["https://example.com/1"],
                ),
                DigestItem(
                    title="Long Summary Article",
                    summary="A" * 100,
                    sources=["Source2"],
                    urls=["https://example.com/2"],
                ),
            ],
        )

        embeds = build_discord_embeds(content)
        headlines = next(e for e in embeds if "其他標題" in e["title"])

        assert headlines["color"] == 0x95A5A6
        assert "（2篇）" in headlines["title"]
        assert "[Short Title](https://example.com/1)" in headlines["description"]
        assert "Brief summary" in headlines["description"]
        # Long summary should be truncated
        assert "…" in headlines["description"]

    def test_tracked_updates_embed(self):
        """Tracked updates appear first when present."""
        content = DigestContent(
            date="2026-04-10",
            period="morning",
            sources_processed=1,
            articles_received=2,
            articles_included=1,
            tracked_updates=DigestSection(
                heading="AI 監管動態",
                items=[
                    DigestItem(
                        title="AI 監管",
                        summary="歐盟新法案進展",
                        sources=["Reuters"],
                        urls=["https://reuters.com/ai"],
                    )
                ],
            ),
        )

        embeds = build_discord_embeds(content)
        assert embeds[0]["title"] == "🔔 追蹤主題更新"
        assert embeds[0]["color"] == 0xED4245
        assert "AI 監管動態" in embeds[0]["description"]

    def test_tracked_updates_embed_multi_item(self):
        """Tracked updates embed lists all items as **links** (T1)."""
        content = DigestContent(
            date="2026-04-10",
            period="morning",
            sources_processed=2,
            articles_received=2,
            articles_included=2,
            tracked_updates=DigestSection(
                heading="追蹤",
                items=[
                    DigestItem(title="t1", summary="s1", sources=["S1"], urls=["u1"]),
                    DigestItem(title="t2", summary="s2", sources=["S2"], urls=["u2"]),
                ],
            ),
        )

        embeds = build_discord_embeds(content)
        assert embeds[0]["title"] == "🔔 追蹤主題更新"
        assert embeds[0]["color"] == 0xED4245
        desc = embeds[0]["description"]
        assert "- **[t1](u1)** — s1 (S1)" in desc
        assert "- **[t2](u2)** — s2 (S2)" in desc

    def test_tracked_updates_embed_no_wiki_link(self):
        """Description has no [[ wiki syntax for tracked (T2)."""
        content = DigestContent(
            date="2026-04-10",
            period="morning",
            sources_processed=1,
            articles_received=1,
            articles_included=1,
            tracked_updates=DigestSection(
                heading="主題",
                items=[DigestItem(title="t1", summary="", sources=[], urls=["u1"])],
            ),
        )

        embeds = build_discord_embeds(content)
        assert "[[" not in embeds[0]["description"]

    def test_stats_embed_always_last(self):
        """Stats embed is always the last embed."""
        content = DigestContent(
            date="2026-04-10",
            period="evening",
            sources_processed=3,
            articles_received=10,
            articles_included=4,
        )

        embeds = build_discord_embeds(content)
        assert embeds[-1]["title"] == "晚報 2026-04-10"
        assert embeds[-1]["color"] == 0x57F287
        assert "來源 **3**" in embeds[-1]["description"]

    def test_empty_sections_skipped(self):
        """Empty sections produce no embeds (only stats)."""
        content = DigestContent(
            date="2026-04-10",
            period="morning",
            sources_processed=0,
            articles_received=0,
            articles_included=0,
        )

        embeds = build_discord_embeds(content)
        assert len(embeds) == 1
        assert embeds[0]["title"] == "早報 2026-04-10"
