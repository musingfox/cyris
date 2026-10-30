"""Tests for notification senders."""

import json
import logging

import httpx
import pytest
from layered_issue import UNNAMED_SOURCES, four_layer_issue

from cyris.adapters.notify import (
    build_discord_embeds,
    build_discord_payload,
    is_masked_webhook_url,
    mask_discord_webhook_url,
    parse_discord_webhook_url,
    send_discord,
    send_discord_alert,
)
from cyris.domain.models import (
    NO_LLM_MODEL,
    DigestContent,
    DigestItem,
    DigestSection,
    UsageStats,
)

pytestmark = pytest.mark.unit


class TestDiscordEmbeds:
    def test_embeds_order_mirrors_digest(self):
        """Embeds follow the page's section order (ui-language.md §6), then the stats."""
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
            fan_sections=[
                DigestSection(
                    heading="IEO",
                    items=[
                        DigestItem(
                            title="IEO weekly",
                            summary="This week's issue",
                            sources=["IEO"],
                            urls=["https://ieo.example/1"],
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

        assert titles == ["Stories", "Headlines", "Morning 2026-04-10"]

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
        featured = next(e for e in embeds if e["title"] == "Stories")

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
        digest = next(e for e in embeds if e["title"] == "Stories")

        assert "[Interview Analysis](https://stratechery.com/interview)" in digest["description"]
        assert "*Stratechery*" in digest["description"]
        assert "NYT CEO interview" in digest["description"]

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

        digest = next(e for e in embeds if e["title"] == "Topics")
        assert "科技裁員潮" in digest["description"]
        assert "多家科技公司宣布裁員計畫" in digest["description"]

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
        digest = next(e for e in embeds if e["title"] == "Headlines")

        assert "[Short Title](https://example.com/1)" in digest["description"]
        assert "Brief summary" in digest["description"]
        # Long summary should be truncated
        assert "…" in digest["description"]

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
    def test_a_degraded_run_adds_factual_content_line(self):
        content = DigestContent(
            date="2026-04-10",
            period="morning",
            sources_processed=1,
            articles_received=1,
            articles_included=1,
            usage=UsageStats(model="gemini-3-flash", input_tokens=0),
        )

        payload = build_discord_payload(content, degraded=True)

        assert payload["content"] == (
            "⚠️ Degraded digest: LLM gemini-3-flash could not be used for every step this run, "
            "so some or all of it is unscored or plain excerpts."
        )

    def test_a_degraded_run_with_no_client_names_no_model(self):
        content = DigestContent(
            date="2026-04-10",
            period="morning",
            sources_processed=1,
            articles_received=1,
            articles_included=1,
            usage=UsageStats(model=NO_LLM_MODEL, input_tokens=0),
        )

        payload = build_discord_payload(content, degraded=True)

        assert payload["content"] == (
            "⚠️ Degraded digest: the configured LLM could not be used this run, "
            "so some or all of it is unscored or plain excerpts."
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

        payload = build_discord_payload(content, degraded=True)

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


ONLINE = "https://digest.example/2026-04-10-morning"


def _counted(embed: dict) -> int:
    """The characters Discord counts toward a message's 6000 (docs: message.mdx, Embed Limits)."""
    fields = embed.get("fields", [])
    texts = [
        embed.get("title"),
        embed.get("description"),
        embed.get("footer", {}).get("text"),
        embed.get("author", {}).get("name"),
        *(f["name"] for f in fields),
        *(f["value"] for f in fields),
    ]
    return sum(len(t or "") for t in texts)


def _feature(n: int, summary_chars: int) -> DigestSection:
    summary = (f"Feature {n} goes on. " * summary_chars)[:summary_chars]
    item = DigestItem(
        title=f"Feature {n}", summary=summary, sources=["S"], urls=[f"https://ex.com/{n}"]
    )
    return DigestSection(heading=f"Feature {n}", items=[item])


def _issue(features: list[DigestSection], headlines: int) -> DigestContent:
    return DigestContent(
        date="2026-04-10",
        period="morning",
        sources_processed=3,
        articles_received=100,
        articles_included=len(features) + headlines,
        featured_articles=features,
        filtered_headlines=[
            DigestItem(
                title=f"Headline {n}",
                summary="One line about it",
                sources=["Wire"],
                urls=[f"https://wire.example/{n}"],
            )
            for n in range(headlines)
        ],
    )


def _overfull_issue() -> DigestContent:
    """Features alone over 4096, and the 6000 total running out inside it."""
    return _issue([_feature(0, 3000), *(_feature(n, 1500) for n in range(1, 4))], headlines=40)


class TestDiscordPayloadLimits:
    """Discord refuses a message over its embed limits whole, so the payload is held to them."""

    def test_a_full_issue_fits_and_keeps_its_stats_whole(self):
        content = _overfull_issue()
        assert sum(_counted(e) for e in build_discord_embeds(content, ONLINE)) > 6000

        embeds = build_discord_payload(content, digest_url=ONLINE)["embeds"]

        assert sum(_counted(e) for e in embeds) <= 6000
        assert len(embeds) <= 10
        assert all(len(e["title"]) <= 256 and len(e["description"]) <= 4096 for e in embeds)
        assert embeds[-1] == build_discord_embeds(content, ONLINE)[-1]
        assert f"[Read online]({ONLINE})" in embeds[-1]["description"]

    def test_a_layer_over_4096_continues_in_an_embed_of_its_own_title(self):
        content = _issue([_feature(0, 100), *(_feature(n, 1200) for n in range(1, 5))], 2)
        full = build_discord_embeds(content, ONLINE)

        embeds = build_discord_payload(content, ONLINE)["embeds"]

        assert [e["title"] for e in embeds] == [
            "Stories",
            "Stories",
            "Headlines",
            full[-1]["title"],
        ]
        assert all(len(e["description"]) <= 4096 for e in embeds)
        assert embeds == full
        stories = "\n".join(e["description"] for e in embeds[:2])
        for n in range(5):
            assert f"[Feature {n}](https://ex.com/{n})" in stories

    def test_pebbles_outlive_the_rows_when_stories_alone_overflow_the_total(self):
        # The review's case: one long Stories layer, then a few short rows.
        content = _issue([_feature(n, 450) for n in range(14)], headlines=10)

        embeds = build_discord_payload(content, ONLINE)["embeds"]

        assert sum(_counted(e) for e in embeds) <= 6000
        assert {e["title"] for e in embeds[:-1]} == {"Stories"}
        assert "Headline 0" not in json.dumps(embeds)

    def test_pebbles_and_sand_stay_whole_when_the_rows_overflow_the_total(self):
        content = four_layer_issue()
        pebble = content.featured_articles[0].items[0]
        content.featured_articles[0].items = [
            pebble.model_copy(update={"title": f"Read {n}", "summary": "Why it matters. " * 40})
            for n in range(5)
        ]
        cluster = content.news_clusters[0]
        cluster.members = [
            m.model_copy(update={"summary": "Excerpt " * 40}) for m in cluster.members
        ] * 4
        content.filtered_headlines = [
            DigestItem(
                title=f"Headline {n} " + "x" * 60,
                summary="One line about it " * 5,
                sources=["Wire"],
                urls=[f"https://wire.example/{n}"],
            )
            for n in range(120)
        ]
        full = build_discord_embeds(content, ONLINE)
        rows_at = [e["title"] for e in full].index("Headlines")
        # Above one embed's 4096 before the rows, so only the 6000 total can cut them.
        assert sum(len(e["description"]) for e in full[:rows_at]) > 4096
        assert sum(_counted(e) for e in full) > 6000

        embeds = build_discord_payload(content, digest_url=ONLINE)["embeds"]

        assert sum(_counted(e) for e in embeds) <= 6000
        assert embeds[:rows_at] == full[:rows_at]
        assert [e["title"] for e in embeds[rows_at:]] == ["Headlines", full[-1]["title"]]
        *kept, marker = embeds[rows_at]["description"].split("\n")
        assert "more in the online edition" in marker
        assert kept == full[rows_at]["description"].split("\n")[: len(kept)]


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

        await send_discord("https://discord.com/api/webhooks/123/token", content, degraded=True)

        assert len(requests) == 1
        assert requests[0].method == "POST"
        assert str(requests[0].url) == "https://discord.com/api/webhooks/123/token"
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

        assert len(requests) == 1
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


def _discord_transport(monkeypatch, handler) -> None:
    client = httpx.AsyncClient
    monkeypatch.setattr(
        "cyris.adapters.notify.httpx.AsyncClient",
        lambda: client(transport=httpx.MockTransport(handler)),
    )


def _warning_records(caplog):
    return [
        record
        for record in caplog.records
        if record.name == "cyris.adapters.notify" and record.levelno == logging.WARNING
    ]


class TestSendDiscordAlert:
    async def test_posts_plain_content(self, monkeypatch):
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(204)

        _discord_transport(monkeypatch, handler)
        webhook = "https://discord.com/api/webhooks/123/s3cr3t-token"

        await send_discord_alert(webhook, "S", "B")

        assert len(requests) == 1
        assert str(requests[0].url) == webhook
        assert json.loads(requests[0].content) == {"content": "S\nB"}

    async def test_truncates_content_to_discord_limit(self, monkeypatch):
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(204)

        _discord_transport(monkeypatch, handler)
        text = "x" * 2500

        await send_discord_alert("https://discord.com/api/webhooks/123/s3cr3t-token", "S", text)

        assert len(requests) == 1
        content = json.loads(requests[0].content)["content"]
        assert content == ("S\n" + text)[:2000]
        assert len(content) == 2000

    async def test_empty_webhook_posts_nothing(self, monkeypatch):
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(204)

        _discord_transport(monkeypatch, handler)

        await send_discord_alert("", "S", "B")

        assert requests == []

    async def test_http_error_is_logged_not_raised(self, monkeypatch, caplog):
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(404)

        _discord_transport(monkeypatch, handler)

        with caplog.at_level(logging.WARNING, logger="cyris.adapters.notify"):
            result = await send_discord_alert(
                "https://discord.com/api/webhooks/123/s3cr3t-token", "S", "B"
            )

        assert result is None
        assert len(requests) == 1
        assert len(_warning_records(caplog)) == 1

    async def test_connect_error_is_logged_not_raised(self, monkeypatch, caplog):
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            raise httpx.ConnectError("no route")

        _discord_transport(monkeypatch, handler)

        with caplog.at_level(logging.WARNING, logger="cyris.adapters.notify"):
            result = await send_discord_alert(
                "https://discord.com/api/webhooks/123/s3cr3t-token", "S", "B"
            )

        assert result is None
        assert len(requests) == 1
        assert len(_warning_records(caplog)) == 1


_LEAK = (
    "HTTPStatusError: Client error '404 Not Found' for url "
    "'https://discord.com/api/webhooks/999/s3cr3t-token'"
)
_MASKED = "https://discord.com/api/webhooks/999/\u2022\u2022\u2022\u2022"


class TestDiscordAlertRedaction:
    async def test_body_redacts_a_token_in_the_text(self, monkeypatch):
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(204)

        _discord_transport(monkeypatch, handler)

        await send_discord_alert("https://discord.com/api/webhooks/123/s3cr3t-token", "S", _LEAK)

        assert len(requests) == 1
        body = requests[0].content.decode()
        assert "s3cr3t-token" not in body
        assert _MASKED in body

    async def test_body_redacts_a_token_in_the_subject(self, monkeypatch):
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(204)

        _discord_transport(monkeypatch, handler)

        await send_discord_alert("https://discord.com/api/webhooks/123/s3cr3t-token", _LEAK, "B")

        assert len(requests) == 1
        body = requests[0].content.decode()
        assert "s3cr3t-token" not in body
        assert _MASKED in body

    async def test_failure_log_redacts_the_webhook_token(self, monkeypatch, caplog):
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(404)

        _discord_transport(monkeypatch, handler)

        with caplog.at_level(logging.WARNING, logger="cyris.adapters.notify"):
            await send_discord_alert("https://discord.com/api/webhooks/999/s3cr3t-token", "S", "B")

        assert len(requests) == 1
        assert "404" in caplog.text
        assert _MASKED in caplog.text
        assert "s3cr3t-token" not in caplog.text


# Every block of `four_layer_issue`, one title each, in the page's order.
PAGE_ORDER = [
    "A tracked hit",
    "A long read",
    "A followed post",
    "Two reports of one event",
    "One report",
    "A headline",
]


class TestEverySurfaceShowsThePagesOrder:
    """Discord and the mail (HTML and text) draw Tracking, pebble, sand, water as the page does."""

    @staticmethod
    def _in_order(text: str) -> bool:
        positions = [text.index(title) for title in PAGE_ORDER]
        return positions == sorted(positions)

    def test_discord(self):
        assert self._in_order(
            "\n".join(e["description"] for e in build_discord_embeds(four_layer_issue()))
        )

    def test_mail_html(self):
        from cyris.adapters.output.email_digest import render_digest_email

        html = render_digest_email(four_layer_issue(), "https://digest.example/x")
        # After the preheader, which repeats the first pebble's title.
        assert self._in_order(html.split('<div class="wrap">', 1)[1])

    def test_mail_text(self):
        from cyris.adapters.mail import build_digest_mail

        assert self._in_order(build_digest_mail(four_layer_issue())[1])


def _outside_member_lists(text: str) -> str:
    """An embed's text without its member lines, the one place sand names a source."""
    return "\n".join(line for line in text.split("\n") if not line.startswith("- [Report"))


class TestDiscordThreeLayers:
    """The embeds follow the page: Tracking, Stories, Topics and Headlines, then the stats."""

    def test_the_embeds_are_tracking_then_the_three_layers_then_the_stats(self):
        titles = [e["title"] for e in build_discord_embeds(four_layer_issue())]

        assert titles == ["Tracking", "Stories", "Topics", "Headlines", "Morning 2026-10-29"]

    def test_an_empty_layer_has_no_embed(self):
        content = four_layer_issue().model_copy(update={"news_clusters": [], "tracked_topics": []})

        titles = [e["title"] for e in build_discord_embeds(content)]

        assert titles == ["Stories", "Headlines", "Morning 2026-10-29"]

    def test_tracking_lists_each_hit_as_a_link_alone(self):
        (tracking, *_) = build_discord_embeds(four_layer_issue())

        assert tracking["description"] == (
            "### Tracked topic\n- [A tracked hit](https://tracked.example.test/hit)"
        )

    def test_a_pebble_is_its_title_link_summary_and_source(self):
        stories = build_discord_embeds(four_layer_issue())[1]

        assert stories["description"] == (
            "### [A long read](https://pebble.example.test/read)\n"
            "Why it matters.\n"
            "*SRC-PEBBLE*\n"
            "\n"
            "### [A followed post](https://fan.example.test/post)\n"
            "*SRC-FAN*"
        )

    def test_sand_is_its_heading_its_line_and_each_member_with_its_source(self):
        topics = build_discord_embeds(four_layer_issue())[2]

        assert topics["description"] == (
            "### Two reports of one event\n"
            "Excerpt a\n"
            "- [Report a](https://wire-a.example.test/a) · *SRC-WIRE-a*\n"
            "- [Report b](https://wire-b.example.test/b) · *SRC-WIRE-b*"
        )

    def test_water_rows_are_a_title_link_and_a_sentence_with_no_source(self):
        headlines = build_discord_embeds(four_layer_issue())[3]

        assert headlines["description"] == (
            "- **[One report](https://lone.example.test/1)**\n"
            "  Its summary.\n"
            "- **[A headline](https://headline.example.test/1)**\n"
            "  The filter's sentence."
        )

    def test_no_source_is_named_outside_a_pebble_or_a_sand_member(self):
        embeds = build_discord_embeds(four_layer_issue())
        text = _outside_member_lists("\n".join(e["description"] for e in embeds[:4]))

        for source in UNNAMED_SOURCES:
            assert source not in text, source
        assert "The model's cluster summary." not in text

    def test_the_payload_carries_no_webhook_url_or_token(self, monkeypatch):
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/123/s3cr3t")
        content = DigestContent(
            date="2026-04-10",
            period="morning",
            sources_processed=1,
            articles_received=1,
            articles_included=1,
            filtered_headlines=[
                DigestItem(title="Row", summary="", sources=["S"], urls=["https://ex.com/row"])
            ],
        )

        body = json.dumps(build_discord_payload(content, digest_url="https://digest.example/x"))

        assert "/api/webhooks/" not in body
        assert "s3cr3t" not in body
        assert "https://digest.example/x" in body


def _paragraph_issue():
    from cyris.domain.models import DigestContent, DigestItem, NewsParagraph

    pair = DigestItem(
        title="Quake",
        summary="",
        sources=["Wire A", "Wire B"],
        urls=["https://a.test/quake", "https://b.test/quake"],
        area="World",
    )
    letter = DigestItem(
        title="Letter",
        summary="",
        sources=["Letters"],
        urls=["newsletter:abc"],
        area="World",
    )
    return DigestContent(
        date="2026-10-27",
        period="morning",
        sources_processed=2,
        articles_received=3,
        articles_included=3,
        area_order=["World"],
        news_paragraphs=[
            NewsParagraph(text="A quake struck; a letter followed.", units=[pair, letter])
        ],
    )


def test_a_legacy_news_paragraph_in_discord_takes_the_layers_of_its_units() -> None:
    topics, headlines, _ = build_discord_embeds(_paragraph_issue())
    assert topics["description"] == (
        "### Quake\n"
        "A quake struck; a letter followed.\n"
        "- [Wire A](https://a.test/quake)\n"
        "- [Wire B](https://b.test/quake)"
    )
    assert headlines["description"] == "- **Letter**"
