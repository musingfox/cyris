"""Tests for notification senders."""

import json

import httpx
import pytest

from cyris.adapters.notify import (
    build_discord_embeds,
    build_discord_payload,
    is_masked_webhook_url,
    mask_discord_webhook_url,
    parse_discord_webhook_url,
    send_discord,
)
from cyris.domain.models import DigestContent, DigestItem, DigestSection, UsageStats

pytestmark = pytest.mark.unit


class TestDiscordEmbeds:
    def test_embeds_order_mirrors_digest(self):
        """Embeds follow Obsidian digest section order."""
        content = DigestContent(
            date="2026-04-10",
            period="morning",
            sources_processed=5,
            articles_received=20,
            articles_included=8,
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
            "⭐ Featured",
            "📰 News",
            "📋 Themes",
            "👀 Worth a look",
            "📌 Other headlines (1)",
            "Morning 2026-04-10",
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
        featured = next(e for e in embeds if e["title"] == "⭐ Featured")

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
        attention = next(e for e in embeds if e["title"] == "👀 Worth a look")

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

        news_embed = next(e for e in embeds if e["title"] == "📰 News")
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
            assert embed["title"] != "📰 News"

    def test_digest_url_adds_online_link_to_stats(self):
        """A digest_url renders a 'Read online' link in the stats embed."""
        content = DigestContent(
            date="2026-07-15",
            period="morning",
            sources_processed=1,
            articles_received=3,
            articles_included=1,
        )
        url = "https://cyris-digest.pages.dev/2026-07-15-morning"

        without = build_discord_embeds(content)
        assert not any(url in e["description"] for e in without)

        with_link = build_discord_embeds(content, digest_url=url)
        stats = with_link[-1]  # stats embed is always last
        assert f"[Read online]({url})" in stats["description"]

    def test_failed_publish_is_called_out_not_silently_omitted(self):
        """A missing link used to look like the digest simply had no online version."""
        content = DigestContent(
            date="2026-08-20",
            period="morning",
            sources_processed=1,
            articles_received=3,
            articles_included=1,
        )

        stats = build_discord_embeds(content, publish_failed=True)[-1]
        assert "Publishing the online edition failed" in stats["description"]

        quiet = build_discord_embeds(content)[-1]
        assert "Publishing the online edition failed" not in quiet["description"]

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
        headlines = next(e for e in embeds if "Other headlines" in e["title"])

        assert headlines["color"] == 0x95A5A6
        assert "(2)" in headlines["title"]
        assert "[Short Title](https://example.com/1)" in headlines["description"]
        assert "Brief summary" in headlines["description"]
        # Long summary should be truncated
        assert "…" in headlines["description"]

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
        assert embeds[-1]["title"] == "Evening 2026-04-10"
        assert embeds[-1]["color"] == 0x57F287
        assert "Sources **3**" in embeds[-1]["description"]

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
        assert embeds[0]["title"] == "Morning 2026-04-10"

    def test_stats_embed_link_health_line_when_counts_present(self):
        content = DigestContent(
            date="2026-04-10",
            period="morning",
            sources_processed=1,
            articles_received=1,
            articles_included=1,
            synthetic_url_count=2,
            dead_link_count=1,
        )
        desc = build_discord_embeds(content)[-1]["description"]
        assert "⚠️" in desc
        assert "2" in desc
        assert "1" in desc

    def test_stats_embed_omits_link_health_when_counts_zero(self):
        content = DigestContent(
            date="2026-04-10",
            period="morning",
            sources_processed=1,
            articles_received=1,
            articles_included=1,
            synthetic_url_count=0,
            dead_link_count=0,
        )
        desc = build_discord_embeds(content)[-1]["description"]
        assert "no canonical link" not in desc


class TestDiscordPayload:
    def test_degraded_usage_adds_factual_content_line(self):
        content = DigestContent(
            date="2026-04-10",
            period="morning",
            sources_processed=1,
            articles_received=1,
            articles_included=1,
            usage=UsageStats(model="gemini-3-flash", input_tokens=0),
        )

        payload = build_discord_payload(content)

        assert payload["content"] == (
            "⚠️ Degraded digest: LLM gemini-3-flash was configured but this run used 0 input "
            "tokens, so scores and summaries are excerpts."
        )

    def test_healthy_usage_omits_content_line(self):
        content = DigestContent(
            date="2026-04-10",
            period="morning",
            sources_processed=1,
            articles_received=1,
            articles_included=1,
            usage=UsageStats(model="gemini-3-flash", input_tokens=12000, api_calls=3),
        )

        assert "content" not in build_discord_payload(content)

    def test_degraded_payload_preserves_embeds_and_stats_order(self):
        content = DigestContent(
            date="2026-04-10",
            period="morning",
            sources_processed=1,
            articles_received=1,
            articles_included=1,
            usage=UsageStats(model="gemini-3-flash", input_tokens=0),
        )

        payload = build_discord_payload(content)

        assert payload["embeds"] == build_discord_embeds(content)
        assert payload["embeds"][-1]["title"] == "Morning 2026-04-10"

    def test_default_usage_has_only_embeds(self):
        content = DigestContent(
            date="2026-04-10",
            period="morning",
            sources_processed=1,
            articles_received=1,
            articles_included=1,
        )

        assert build_discord_payload(content) == {"embeds": build_discord_embeds(content)}


class TestSendDiscord:
    async def test_posts_degraded_payload(self, monkeypatch):
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(204)

        client = httpx.AsyncClient
        monkeypatch.setattr(
            "cyris.adapters.notify.httpx.AsyncClient",
            lambda: client(transport=httpx.MockTransport(handler)),
        )
        content = DigestContent(
            date="2026-04-10",
            period="morning",
            sources_processed=1,
            articles_received=1,
            articles_included=1,
            usage=UsageStats(model="gemini-3-flash", input_tokens=0),
        )

        await send_discord("https://discord.com/api/webhooks/123/token", content)

        assert json.loads(requests[0].content)["content"].startswith("⚠️ Degraded digest")

    async def test_posts_healthy_payload_without_content(self, monkeypatch):
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(204)

        client = httpx.AsyncClient
        monkeypatch.setattr(
            "cyris.adapters.notify.httpx.AsyncClient",
            lambda: client(transport=httpx.MockTransport(handler)),
        )
        content = DigestContent(
            date="2026-04-10",
            period="morning",
            sources_processed=1,
            articles_received=1,
            articles_included=1,
            usage=UsageStats(model="gemini-3-flash", input_tokens=12000, api_calls=3),
        )

        await send_discord("https://discord.com/api/webhooks/123/token", content)

        assert "content" not in json.loads(requests[0].content)

    async def test_empty_webhook_posts_nothing(self, monkeypatch):
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(204)

        client = httpx.AsyncClient
        monkeypatch.setattr(
            "cyris.adapters.notify.httpx.AsyncClient",
            lambda: client(transport=httpx.MockTransport(handler)),
        )
        content = DigestContent(
            date="2026-04-10",
            period="morning",
            sources_processed=1,
            articles_received=1,
            articles_included=1,
        )

        await send_discord("", content)

        assert requests == []


class TestMaskDiscordWebhookUrl:
    def test_replaces_token_keeps_id(self):
        assert (
            mask_discord_webhook_url("https://discord.com/api/webhooks/123/abcTOKEN")
            == "https://discord.com/api/webhooks/123/••••"
        )

    def test_empty_stays_empty(self):
        assert mask_discord_webhook_url("") == ""

    def test_unrecognised_is_never_echoed(self):
        assert mask_discord_webhook_url("garbage") == "••••"

    def test_token_is_not_in_result(self):
        masked = mask_discord_webhook_url("https://discord.com/api/webhooks/123/abcTOKEN")
        assert "abcTOKEN" not in masked


class TestIsMaskedWebhookUrl:
    def test_a_masked_url_is_recognised(self):
        masked = mask_discord_webhook_url("https://discord.com/api/webhooks/123/abcTOKEN")
        assert is_masked_webhook_url(masked)

    def test_a_real_url_is_not(self):
        assert not is_masked_webhook_url("https://discord.com/api/webhooks/123/abcTOKEN")


class TestParseDiscordWebhookUrl:
    def test_standard_webhook(self):
        assert parse_discord_webhook_url("https://discord.com/api/webhooks/123/abcTOKEN") == (
            "123",
            "abcTOKEN",
        )

    def test_api_versioned_webhook(self):
        assert parse_discord_webhook_url("https://discord.com/api/v10/webhooks/123/abcTOKEN") == (
            "123",
            "abcTOKEN",
        )

    def test_discordapp_host(self):
        assert parse_discord_webhook_url("https://discordapp.com/api/webhooks/123/abcTOKEN") == (
            "123",
            "abcTOKEN",
        )

    def test_foreign_host_is_rejected(self):
        assert parse_discord_webhook_url("https://example.com/api/webhooks/123/abc") is None

    def test_missing_token_is_rejected(self):
        assert parse_discord_webhook_url("https://discord.com/api/webhooks/123") is None

    def test_empty_is_rejected(self):
        assert parse_discord_webhook_url("") is None
