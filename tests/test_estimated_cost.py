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


def test_usage_jsonl_row_matches_bootstrap():
    """§4 called `usage.jsonl` retired while `build_deps` still wrote it.

    Only one of those can be true. The json backend is a supported fallback, so
    the writer is what stands and §4 has to say so.
    """
    from pathlib import Path

    bootstrap = Path("src/cyris/bootstrap.py").read_text()
    architecture = Path("docs/architecture.md").read_text()

    assert 'log_path=cfg.app.agent_vault.path / "usage.jsonl"' in bootstrap
    spend_row = next(line for line in architecture.splitlines() if line.startswith("| LLM spend "))
    assert "usage.jsonl" in spend_row
    assert "retired" not in spend_row
    assert "fallback" in spend_row


async def test_neurons_survive_the_trip_from_a_response_to_the_run_total():
    """`complete_json` is the one place every digest call is accounted for.

    Dropping `neurons` there is invisible: the digest still renders and the token
    counts still add up, and the only symptom is that Workers AI runs report no
    cost at all — the number the provider is chosen on.
    """
    from cyris.domain.models import UsageStats
    from cyris.service_layer.ports import LLMResponse, complete_json

    class NeuronReportingLLM:
        model = "@cf/test"

        async def complete(self, prompt, **kwargs):
            return LLMResponse(text="{}", input_tokens=10, output_tokens=2, neurons=1.25)

    usage = UsageStats(model="@cf/test")
    await complete_json(NeuronReportingLLM(), "prompt", usage=usage)
    await complete_json(NeuronReportingLLM(), "prompt", usage=usage)

    assert usage.neurons == 2.5
    assert usage.api_calls == 2


async def test_neurons_and_calls_survive_every_aggregation_hop():
    """The trip does not end at `complete_json`: a run folds three totals into one.

    Each hop used `add`, which counts one call and takes no accumulated neuron
    figure, so a Workers AI run reported no scoring spend at all and collapsed
    the whole scoring stage into a single `api_calls`. Both are read off the
    digest footer and the `usage_log` row, and neither looked wrong.
    """
    from cyris.domain.models import UsageStats

    stage = UsageStats(
        model="@cf/test", input_tokens=300, output_tokens=60, api_calls=3, neurons=9.0
    )
    run = UsageStats(model="@cf/test")

    run.merge(stage)

    assert (run.api_calls, run.neurons) == (3, 9.0)
    assert (run.input_tokens, run.output_tokens) == (300, 60)


async def test_a_batched_scoring_stage_reports_what_it_spent():
    """`score_in_batches` is the hop that hid it, and the one a digest run reads."""
    import json
    from datetime import UTC, datetime

    from cyris.domain.models import ArticleState, StoredArticle, Tier
    from cyris.service_layer.ports import LLMResponse
    from cyris.service_layer.scoring import score_in_batches

    class NeuronReportingLLM:
        model = "@cf/test"

        async def complete(self, prompt, **kwargs):
            scores = [{"id": str(i), "score": 50} for i in range(60)]
            return LLMResponse(
                text=json.dumps({"scores": scores}), input_tokens=100, output_tokens=20, neurons=4.0
            )

    when = datetime(2026, 9, 1, tzinfo=UTC)
    articles = [
        StoredArticle(
            url=f"https://e.test/{i}",
            original_id=str(i),
            title=f"T{i}",
            content="c" * 50,
            published_at=when,
            first_seen_at=when,
            source_name="S",
            source_tier=Tier.SUMMARIZE,
            state=ArticleState.PENDING,
        )
        for i in range(60)
    ]

    usage = await score_in_batches(articles, NeuronReportingLLM())

    assert usage.api_calls == 3
    assert usage.neurons == 12.0
