"""What a run costs, and what happens when we cannot say.

The defect this pins: `estimated_cost` used to apply Sonnet's $3/$15 to every
model, so a Gemini digest printed roughly four times its real cost with nothing
in the output naming the vendor the rate came from.
"""

from cyris.domain.models import DigestContent, UsageStats


def _usage(model: str, input_tokens: int = 1_000_000, output_tokens: int = 1_000_000):
    return UsageStats(
        model=model, input_tokens=input_tokens, output_tokens=output_tokens, api_calls=3
    )


def test_a_priced_model_uses_its_own_rate_card():
    """One million of each token is just the per-MTok pair, added up."""
    assert _usage("gemini-3.6-flash").estimated_cost == 0.75 + 3.75
    assert _usage("gpt-5.6-luna").estimated_cost == 0.20 + 1.20
    assert _usage("claude-haiku-4-5").estimated_cost == 1.00 + 5.00


def test_providers_are_no_longer_priced_as_sonnet():
    """The regression itself: Gemini billed at Sonnet's rate was ~4x too high."""
    sonnet_rate = (1_000_000 * 3 + 1_000_000 * 15) / 1_000_000
    assert _usage("gemini-3.6-flash").estimated_cost != sonnet_rate


def test_an_unknown_model_has_no_cost_rather_than_someone_elses_price():
    assert _usage("@cf/openai/gpt-oss-120b").estimated_cost is None
    assert _usage("mistral-large-3").estimated_cost is None


def test_no_model_at_all_is_also_unknown():
    """Degraded mode writes "none"; an empty string reaches here from bare UsageStats()."""
    assert _usage("none").estimated_cost is None
    assert _usage("").estimated_cost is None


def test_an_unpriced_run_still_renders_a_digest(tmp_path):
    """The failure that would actually page someone: a format string on None."""
    from cyris.adapters.output.html_digest import HtmlDigestWriter

    content = DigestContent(
        date="2026-08-25",
        period="morning",
        sources_processed=1,
        articles_received=1,
        articles_included=1,
        usage=_usage("@cf/openai/gpt-oss-120b"),
    )
    rendered = HtmlDigestWriter(tmp_path).render(content)

    assert "2000000 tokens" in rendered  # tokens are still real and still reported


def test_an_unpriced_run_still_builds_a_discord_payload():
    from cyris.adapters.notify import build_discord_embeds

    content = DigestContent(
        date="2026-08-25",
        period="morning",
        sources_processed=1,
        articles_received=1,
        articles_included=1,
        usage=_usage("@cf/openai/gpt-oss-120b"),
    )
    embeds = build_discord_embeds(content)

    assert embeds  # the stats embed always goes out
    assert "Cost" not in " ".join(e.get("description", "") for e in embeds)


def test_an_unpriced_run_logs_a_null_cost_not_a_zero(tmp_path):
    import json

    from cyris.adapters.output.usage_log import append_usage

    content = DigestContent(
        date="2026-08-25",
        period="morning",
        sources_processed=1,
        articles_received=1,
        articles_included=1,
        usage=_usage("@cf/openai/gpt-oss-120b"),
    )
    log_path = tmp_path / "usage.jsonl"
    append_usage(content, log_path)

    assert json.loads(log_path.read_text())["estimated_cost_usd"] is None
