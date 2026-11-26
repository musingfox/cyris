"""A hosted tenant's AI spend: metered as each call is made, charged to its own month.

The hosted Worker's `SpendGateway` answers a run container's `GET /t/<tenant>/budget` and
`POST /t/<tenant>/spend` from the operator's registry (ADR-0038). Here `FakeSpendGateway` answers
them as it does, so a run is checked against its tenant's month as it starts and charges what it
spent as it ends: every LLM response and every embedded text, priced in the container, failed run
or not. A run that starts with the month spent calls no model and says so on its page. The month
itself, and its reset on the first of the next, is the registry's (`workers/app/test/
hosted_spend.test.js`).
"""

import asyncio
import json
import os
import signal
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
import typer
from fakes import TEST_SETTINGS, FakeLLM, load_files
from test_run_digest import FakeSource
from test_tenant_gateway import FakeGateway

from cyris import bootstrap
from cyris.adapters import hosted_spend
from cyris.adapters.ai_gateway_client import AIGatewayClient
from cyris.adapters.clef import CLEF_MODEL, ClefError
from cyris.adapters.embedding import _Usage
from cyris.adapters.gemini_client import GeminiClient, OutputBudgetSpentError
from cyris.adapters.hosted import OPERATOR_ORIGIN
from cyris.adapters.hosted_spend import (
    CLEF_FLASH_USD_PER_MTOK,
    EMBEDDING_BYTES_PER_TOKEN,
    GEMINI_EMBEDDING_USD_PER_MTOK,
    SPEND_ORIGIN,
    UNPRICED_USD_PER_MTOK,
    WORKERS_AI_USD_PER_KNEURON,
    MeteredClef,
    MeteredEmbedder,
    MeteredLLM,
    SpendLedger,
    SpendMeter,
    estimated_clef_tokens,
    estimated_embedding_tokens,
)
from cyris.adapters.store.tenant_gateway import apply_hosted_schema
from cyris.domain.models import _PRICES_PER_MTOK, Article, DigestContent, Lane, UsageStats
from cyris.service_layer.ports import NoulAnswer
from cyris.service_layer.run_digest import RunOptions, run_digest

pytestmark = pytest.mark.integration

HAIKU = "claude-haiku-5-5"


class FakeSpendGateway:
    """Answers `GET /t/<tenant>/budget` and `POST /t/<tenant>/spend` as `SpendGateway` does.

    Only `tenant` is this run's; another tenant, path or method is refused with 403. `spent`
    is what the month's answer says; every report posted is kept in `reports`, once per
    `report_id` as the registry adds it, and a status
    other than 200 or 202 answers every ask or every report. The ledger's alerts to the
    operator's gateway, `ops.cyris`, are kept in `alerts`.
    """

    def __init__(self, tenant: str, *, spent: bool = False, budget_status: int = 200) -> None:
        self.tenant = tenant
        self.spent = spent
        self.budget_usd = 2.25
        self.budget_status = budget_status
        self.spend_status = 202
        self.requests: list[httpx.Request] = []
        self.reports: list[dict] = []
        self.alerts: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == httpx.URL(OPERATOR_ORIGIN).host:
            self.alerts.append({"path": request.url.path, **json.loads(request.content)})
            return httpx.Response(202, json={"sent": True})
        self.requests.append(request)
        parts = request.url.path.strip("/").split("/")
        if len(parts) != 3 or parts[0] != "t" or parts[1] != self.tenant:
            return httpx.Response(403, json={"error": "not this run's"})
        if parts[2] == "budget" and request.method == "GET":
            if self.budget_status != 200:
                return httpx.Response(self.budget_status, json={"error": "down"})
            month = {"tenant": self.tenant, "month": "2026-11", "budget_usd": self.budget_usd}
            return httpx.Response(200, json={**month, "cost_usd": 0.0, "spent": self.spent})
        if parts[2] == "spend" and request.method == "POST":
            if self.spend_status != 202:
                return httpx.Response(self.spend_status, json={"error": "down"})
            report = json.loads(request.content)
            # As the registry adds it: once per report id (`addSpend` in ai_budget.js).
            if any(r["report_id"] == report["report_id"] for r in self.reports):
                return httpx.Response(202, json={"tenant": self.tenant, "repeated": True})
            self.reports.append(report)
            return httpx.Response(202, json={"tenant": self.tenant, "runs": len(self.reports)})
        return httpx.Response(403, json={"error": "not a route of this gateway"})


def ledger_through(fake: FakeSpendGateway, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every `SpendLedger` `build_deps` makes talks to `fake`, as the Worker's gateway would."""
    real = SpendLedger

    def ledger(tenant_id: str) -> SpendLedger:
        made = real(tenant_id)
        made._http = httpx.Client(transport=httpx.MockTransport(fake))
        return made

    monkeypatch.setattr(hosted_spend, "SpendLedger", ledger)


class UsageEmbedder:
    """An embedder that counts its batches as the adapters do, one vector per text, and fails
    after `fail_after` texts when set, as a batch that raised would."""

    BATCH_SIZE = 2

    def __init__(self, *, neurons: float | None = None, fail_after: int | None = None) -> None:
        self.usage = _Usage()
        self.usage.neurons = 0.0 if neurons is not None else None
        self._neurons = neurons
        self._fail_after = fail_after
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        unique = list(dict.fromkeys(texts))
        for start in range(0, len(unique), self.BATCH_SIZE):
            batch = unique[start : start + self.BATCH_SIZE]
            if self._fail_after is not None and self.usage.embedded + len(batch) > self._fail_after:
                raise RuntimeError("the embedding API went away")
            self.usage.embedded += len(batch)
            if self._neurons is not None:
                self.usage.neurons += self._neurons * len(batch)
        return [[1.0, 0.0] for _ in texts]


async def test_the_meter_prices_each_llm_response_at_its_models_rate_as_it_is_made() -> None:
    meter = SpendMeter(HAIKU)
    llm = MeteredLLM(FakeLLM(model=HAIKU, input_tokens=68_393, output_tokens=20_052), meter)

    await llm.complete("p")

    report = meter.report()
    # The run measured on production on 2026-11-13: about 0.0168 USD.
    assert report["llm_usd"] == pytest.approx(0.0168, abs=1e-4)
    assert report["llm_usd"] == pytest.approx(
        UsageStats(model=HAIKU, input_tokens=68_393, output_tokens=20_052).estimated_cost
    )
    assert (report["input_tokens"], report["output_tokens"], report["llm_calls"]) == (
        68_393,
        20_052,
        1,
    )
    assert report["unpriced_tokens"] == 0
    assert llm.model == HAIKU


async def test_a_model_with_no_rate_card_is_charged_at_the_dearest_rates_and_named_apart() -> None:
    unpriced = SpendMeter("someones-model")
    await MeteredLLM(
        FakeLLM(model="someones-model", input_tokens=1_000_000, output_tokens=100_000), unpriced
    ).complete("p")
    neurons = SpendMeter("@cf/meta/llama")
    neurons.count_llm(type("R", (), {"input_tokens": 1, "output_tokens": 1, "neurons": 2000.0})())

    # Never zero, which would let a tenant on an unlisted model spend without limit: no rate
    # card the deployment knows is dearer, input or output.
    input_rate, output_rate = UNPRICED_USD_PER_MTOK
    assert all(i <= input_rate and o <= output_rate for i, o in _PRICES_PER_MTOK.values())
    assert unpriced.report()["llm_usd"] == pytest.approx(input_rate + output_rate / 10)
    assert unpriced.report()["llm_usd"] > 0
    assert unpriced.report()["unpriced_tokens"] == 1_100_000
    assert neurons.report()["llm_usd"] == pytest.approx(2 * WORKERS_AI_USD_PER_KNEURON)
    assert neurons.report()["unpriced_tokens"] == 0


# A thinking model that spent its whole output budget: billed, then raised on (gemini_client.py).
BUDGET_SPENT = {
    "candidates": [{"content": {"role": "model"}, "finishReason": "MAX_TOKENS"}],
    "usageMetadata": {"promptTokenCount": 40_000, "thoughtsTokenCount": 8_000},
}
# A blocked prompt: no candidate, its input billed.
BLOCKED = {"promptFeedback": {"blockReason": "SAFETY"}, "usageMetadata": {"promptTokenCount": 900}}


@pytest.mark.parametrize(
    ("client", "url"),
    [
        (
            lambda: GeminiClient(api_key="k", model="gemini-2.5-flash", max_retries=0),
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
        ),
        (
            lambda: AIGatewayClient(
                api_token="t", account_id="acct", model="google/gemini-2.5-flash", max_retries=0
            ),
            None,
        ),
    ],
    ids=["gemini", "ai_gateway"],
)
async def test_a_response_billed_then_raised_on_is_still_charged(client, url) -> None:
    meter = SpendMeter("gemini-2.5-flash")
    llm = MeteredLLM(client(), meter)

    async with respx.mock:
        route = respx.post(url) if url else respx.post(host__regex=r".+")
        route.mock(
            side_effect=[httpx.Response(200, json=BUDGET_SPENT), httpx.Response(200, json=BLOCKED)]
        )
        with pytest.raises(OutputBudgetSpentError):
            await llm.complete("p")
        with pytest.raises(RuntimeError, match="no candidates"):
            await llm.complete("p")

    report = meter.report()
    assert (report["input_tokens"], report["output_tokens"]) == (40_900, 8_000)
    assert report["llm_calls"] == 2
    assert report["llm_usd"] == pytest.approx(
        UsageStats(
            model="gemini-2.5-flash", input_tokens=40_900, output_tokens=8_000
        ).estimated_cost
    )


async def test_each_clef_question_is_charged_by_its_tokens_or_an_estimate_of_what_it_sent() -> None:
    meter = SpendMeter(HAIKU)
    answers = [
        NoulAnswer(noul=0.7, model=CLEF_MODEL, input_tokens=1_200),
        NoulAnswer(noul=0.2, model=CLEF_MODEL, input_tokens=None),
    ]

    async def ask(_state: dict, _instructions: str) -> NoulAnswer:
        if not answers:
            raise ClefError("Clef answered without a noul probability")
        return answers.pop(0)

    clef = MeteredClef(ask, meter)
    state = {"item": "台灣半導體", "nearest": "A launch"}
    await clef(state, "Would the reader keep this?")
    await clef(state, "Would the reader keep this?")
    # Answered 200 and unusable: it may have been billed, so it is charged, erring high.
    with pytest.raises(ClefError):
        await clef(state, "Would the reader keep this?")

    guess = estimated_clef_tokens(state, "Would the reader keep this?")
    report = meter.report()
    assert (report["clef_calls"], report["clef_tokens"]) == (3, 1_200 + 2 * guess)
    assert report["clef_usd"] == pytest.approx(
        (1_200 + 2 * guess) * CLEF_FLASH_USD_PER_MTOK / 1_000_000, abs=1e-8
    )


async def test_a_hosted_runs_clef_is_metered_and_charged_to_its_tenant(tenant) -> None:
    cfg, _, ledger, *_ = tenant
    deps = bootstrap.build_deps(cfg)

    await deps.ask_clef({"item": "x"}, "keep?")
    deps.record_spend()

    [charged] = ledger.reports
    assert (charged["clef_calls"], charged["clef_tokens"]) == (1, 300)
    assert charged["clef_usd"] == pytest.approx(300 * CLEF_FLASH_USD_PER_MTOK / 1_000_000)


async def test_gemini_embeddings_are_estimated_from_the_texts_bytes_and_workers_ai_by_neurons() -> (
    None
):
    texts = ["台灣半導體", "A launch today", "台灣半導體"]
    meter = SpendMeter(HAIKU)

    await MeteredEmbedder(UsageEmbedder(), meter, "gemini").embed(texts)
    # Each unique text once, as the adapters send it.
    tokens = estimated_embedding_tokens(["台灣半導體", "A launch today"])
    assert tokens == 5 + -(-len(b"A launch today") // EMBEDDING_BYTES_PER_TOKEN)
    assert meter.report()["embedding_usd"] == pytest.approx(
        tokens * GEMINI_EMBEDDING_USD_PER_MTOK / 1_000_000
    )
    assert meter.report()["embedded_texts"] == 2

    by_neurons = SpendMeter(HAIKU)
    await MeteredEmbedder(UsageEmbedder(neurons=1000.0), by_neurons, "workers_ai").embed(texts[:2])
    assert by_neurons.report()["embedding_usd"] == pytest.approx(2 * WORKERS_AI_USD_PER_KNEURON)


async def test_an_embed_call_that_raised_part_way_still_counts_the_batches_it_got_embedded() -> (
    None
):
    meter = SpendMeter(HAIKU)
    embedder = MeteredEmbedder(UsageEmbedder(fail_after=2), meter, "gemini")

    with pytest.raises(RuntimeError, match="went away"):
        await embedder.embed(["a" * 30, "b" * 30, "c" * 30])

    assert meter.report()["embedded_texts"] == 2
    assert meter.report()["embedding_tokens_estimated"] == 20


def test_a_ledger_that_cannot_answer_lets_the_run_spend_and_a_lost_report_is_said() -> None:
    down = FakeSpendGateway("alpha", budget_status=503)
    ledger = SpendLedger("alpha")
    ledger._http = httpx.Client(transport=httpx.MockTransport(down))

    assert ledger.spent() is None

    refused = SpendLedger("alpha", origin="http://spend.cyris")
    refused._http = httpx.Client(transport=httpx.MockTransport(FakeSpendGateway("bravo")))
    assert refused.report(SpendMeter(HAIKU))["recorded"] is False


def test_a_report_retried_after_the_registry_took_it_names_the_same_run_so_it_is_added_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("cyris.adapters.store.d1.time.sleep", lambda _s: None)
    gateway = FakeSpendGateway("alpha")
    posts: list[dict] = []

    def slow(request: httpx.Request) -> httpx.Response:
        # The registry adds the first post, and its answer comes after the run gave up on it.
        posts.append(json.loads(request.content))
        answered = gateway(request)
        if len(posts) == 1:
            raise httpx.ReadTimeout("no answer within 10 s", request=request)
        return answered

    ledger = SpendLedger("alpha")
    ledger._http = httpx.Client(transport=httpx.MockTransport(slow))
    meter = SpendMeter(HAIKU)
    meter.count_llm(type("R", (), {"input_tokens": 100, "output_tokens": 10, "neurons": None})())

    assert ledger.report(meter)["recorded"] is True

    assert len(posts) == 2
    assert posts[0]["report_id"] == posts[1]["report_id"] == ledger.report_id
    [charged] = gateway.reports
    assert charged["input_tokens"] == 100
    # Each run draws its own, so two runs' reports are both added.
    assert SpendLedger("alpha").report_id != ledger.report_id


def test_a_report_the_ledger_answers_with_no_json_is_said_lost_and_never_raises() -> None:
    ledger = SpendLedger("alpha")
    ledger._http = httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(202, text="accepted"))
    )

    assert ledger.report(SpendMeter(HAIKU))["recorded"] is False
    # And a month that is JSON but no object lets the run spend, as an unreadable one does.
    ledger._http = httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=["spent"]))
    )
    assert ledger.spent() is None


# --- the hosted run --------------------------------------------------------------------------

CONFIG = """
[agent_vault]
path = "{vault}"
"""

SOURCES = """
sources:
  - name: "A"
    url: "https://a.test/feed"
"""

DOMAIN = "cyris-hosted.trial.workers.dev"

# A hosted tenant on Haiku 5.5, with vote similarity on Gemini, as a beta tenant's settings are.
AI_SETTINGS = {
    **TEST_SETTINGS,
    "llm_provider.provider": "anthropic",
    "llm_provider.model": HAIKU,
    "vote_similarity.enabled": True,
    "vote_similarity.provider": "gemini",
}

SCORES = json.dumps({"scores": [{"id": 0, "score": 80, "language": "en", "tags": ["ai"]}]})


def _article(url: str = "https://a.test/launch", title: str = "A launch") -> Article:
    return Article(
        id=1,
        title=title,
        url=url,
        content="Something launched today.",
        published_at=datetime.now(UTC) - timedelta(hours=1),
        source_name="A",
        source_lane=Lane.READ,
        source_tags=["tech"],
    )


@pytest.fixture
def tenant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A hosted tenant `alpha`'s run: its database and its ledger behind fakes, a Haiku client
    and a Gemini embedder that record their calls, and one downvote for similarity to judge by."""
    monkeypatch.setenv("CYRIS_STORE_BACKEND", "d1")
    monkeypatch.setenv("CYRIS_STORE_TENANT_ID", "alpha")
    monkeypatch.setenv("CYRIS_STORE_HOSTED_DOMAIN", DOMAIN)
    gateway = FakeGateway("alpha")
    apply_hosted_schema(gateway.db)
    from test_hosted_run_wiring import _through

    _through(gateway, monkeypatch)
    ledger = FakeSpendGateway("alpha")
    ledger_through(ledger, monkeypatch)
    llm = FakeLLM(SCORES, model=HAIKU, input_tokens=1_000, output_tokens=200)
    embedder = UsageEmbedder()
    monkeypatch.setattr(bootstrap, "build_llm", lambda _cfg: llm)
    monkeypatch.setattr(bootstrap, "make_embedder", lambda _provider, _model: embedder)

    async def clef(_state: dict, _instructions: str) -> NoulAnswer:
        return NoulAnswer(noul=0.5, model=CLEF_MODEL, input_tokens=300)

    monkeypatch.setattr(bootstrap, "build_ask_clef", lambda: clef)
    config_path = tmp_path / "cyris.toml"
    config_path.write_text(CONFIG.format(vault=tmp_path / "vault"), encoding="utf-8")
    sources_path = tmp_path / "sources.yaml"
    sources_path.write_text(SOURCES, encoding="utf-8")
    cfg = load_files(config_path, sources_path, AI_SETTINGS)
    # One human downvote, so vote similarity has a seed and embeds the window.
    gateway.db.query(
        "INSERT INTO stored_articles (url, original_id, title, published_at, source_name, "
        "source_lane, state, first_seen_at, triaged_at) VALUES ('https://a.test/old', 0, "
        "'An old rumour', '2026-11-01T00:00:00.000000+00:00', 'A', 'read', 'rejected', "
        "'2026-11-01T00:00:00.000000+00:00', '2026-11-01T01:00:00+00:00')"
    )
    return cfg, gateway, ledger, llm, embedder


def _summary(gateway: FakeGateway) -> dict:
    [run] = gateway.db.query("SELECT summary FROM digest_runs").rows
    return json.loads(run["summary"])


def _page(gateway: FakeGateway, path: str) -> str:
    [page] = gateway.db.query("SELECT body FROM hosted_pages WHERE path = ?", [path]).rows
    return page["body"]


async def test_a_hosted_run_charges_its_llm_and_embedding_spend_to_its_own_tenant(tenant) -> None:
    cfg, gateway, ledger, llm, embedder = tenant
    deps = replace(bootstrap.build_deps(cfg), fetch_sources=[FakeSource([_article()])])

    with respx.mock:
        anywhere = respx.route().mock(return_value=httpx.Response(500))
        report = await run_digest(deps, RunOptions(period="morning"))

    assert report.status == "ok"
    assert anywhere.call_count == 0
    assert llm.calls and embedder.calls
    # Asked once as it started, charged once as it ended, on its own tenant's paths.
    assert [(r.method, r.url.path) for r in ledger.requests] == [
        ("GET", "/t/alpha/budget"),
        ("POST", "/t/alpha/spend"),
    ]
    [charged] = ledger.reports
    calls = len(llm.calls)
    assert (charged["input_tokens"], charged["output_tokens"]) == (1_000 * calls, 200 * calls)
    assert charged["llm_usd"] == pytest.approx(
        UsageStats(
            model=HAIKU, input_tokens=1_000 * calls, output_tokens=200 * calls
        ).estimated_cost
    )
    assert charged["embedded_texts"] == 2
    assert charged["embedding_usd"] > 0
    # The run's own record carries what it charged.
    assert _summary(gateway)["ai_spend"] == {**charged, "recorded": True}
    assert ledger.alerts == []


async def test_a_run_whose_month_is_spent_calls_no_model_and_its_page_says_why(tenant) -> None:
    cfg, gateway, ledger, llm, embedder = tenant
    ledger.spent = True
    article = _article()
    deps = replace(bootstrap.build_deps(cfg), fetch_sources=[FakeSource([article])])

    report = await run_digest(deps, RunOptions(period="morning"))

    assert report.status == "ok"
    assert llm.calls == []
    assert embedder.calls == []
    assert deps.ask_clef is None
    # Still an issue: the plain list, every article in it.
    [issue] = gateway.db.query("SELECT date, content FROM digests").rows
    assert article.url in issue["content"]
    page = _page(gateway, f"/{issue['date']}-morning.html")
    assert "This month&#39;s AI allowance is used up" in page
    # Not the degraded notice, whose advice to check the provider would be wrong.
    assert "Check the provider and its key" not in page
    # The run's record says why it called no model.
    assert _summary(gateway)["ai_spend"]["ai_budget"] == "spent"
    # What it spent, nothing, is still reported.
    [charged] = ledger.reports
    assert (charged["llm_usd"], charged["embedding_usd"], charged["llm_calls"]) == (0.0, 0.0, 0)


async def test_an_allowance_of_0_says_ai_is_off_and_never_that_the_month_brings_it_back(
    tenant,
) -> None:
    cfg, gateway, ledger, llm, _ = tenant
    ledger.spent, ledger.budget_usd = True, 0
    deps = replace(bootstrap.build_deps(cfg), fetch_sources=[FakeSource([_article()])])

    await run_digest(deps, RunOptions(period="morning"))

    assert llm.calls == []
    [issue] = gateway.db.query("SELECT date FROM digests").rows
    page = _page(gateway, f"/{issue['date']}-morning.html")
    assert "AI is turned off for this site" in page
    assert "month turns" not in page
    assert _summary(gateway)["ai_spend"]["ai_budget"] == "off"


def test_a_spent_months_notice_is_on_the_issues_its_run_starts_and_no_older_one(tenant) -> None:
    cfg, _, ledger, *_ = tenant
    ledger.spent = True
    writer = bootstrap.build_deps(cfg).html_writer
    since = writer.ai_pause.since
    older = DigestContent(
        date="2026-11-20",
        period="morning",
        started_at=since - timedelta(hours=12),
        sources_processed=1,
        articles_received=0,
        articles_included=0,
    )
    this_runs = older.model_copy(update={"started_at": since + timedelta(seconds=1)})

    # An older issue the run republishes, one whose publish failed, keeps its own notice.
    assert "AI allowance" not in writer.render(older, degraded=True)
    assert "Check the provider and its key" in writer.render(older, degraded=True)
    assert "AI allowance is used up" in writer.render(this_runs, degraded=True)
    assert "Check the provider and its key" not in writer.render(this_runs, degraded=True)


async def test_a_run_whose_month_is_not_spent_calls_its_model(tenant) -> None:
    cfg, gateway, ledger, llm, _ = tenant
    deps = replace(bootstrap.build_deps(cfg), fetch_sources=[FakeSource([_article()])])

    await run_digest(deps, RunOptions(period="evening"))

    assert llm.calls
    assert "ai_budget" not in _summary(gateway)["ai_spend"]
    [issue] = gateway.db.query("SELECT date FROM digests").rows
    assert "AI allowance" not in _page(gateway, f"/{issue['date']}-evening.html")


async def test_a_run_that_fails_after_its_model_calls_still_charges_what_it_spent(tenant) -> None:
    cfg, gateway, ledger, llm, embedder = tenant
    built = bootstrap.build_deps(cfg)

    class Breaks:
        """The store, whose state update after the pipeline raises: no step guards it."""

        def __init__(self, inner) -> None:
            self._inner = inner

        def update_states(self, *_args, **_kwargs):
            raise RuntimeError("the tenant's database went away")

        def __getattr__(self, name):
            return getattr(self._inner, name)

    deps = replace(built, store=Breaks(built.store), fetch_sources=[FakeSource([_article()])])

    with respx.mock:
        # The failure's alert to the operator, which a hosted run sends as it ends.
        told = respx.post(f"{OPERATOR_ORIGIN}/t/alpha/alert").mock(return_value=httpx.Response(202))
        with pytest.raises(RuntimeError, match="went away"):
            await run_digest(deps, RunOptions(period="morning"))

    assert told.call_count == 1
    assert llm.calls and embedder.calls
    [charged] = ledger.reports
    calls = len(llm.calls)
    assert (charged["input_tokens"], charged["output_tokens"]) == (1_000 * calls, 200 * calls)
    assert charged["llm_usd"] > 0
    assert charged["embedded_texts"] == 2
    summary = _summary(gateway)
    assert summary["status"] == "error"
    assert summary["ai_spend"]["recorded"] is True


def test_a_run_the_container_stops_with_sigterm_still_charges_what_it_had_spent(
    tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The container stops a run with SIGTERM: `cyris run` cancels the run's task, so
    `run_digest`'s `finally` still reports, and the CLI exits 143 (`_run_pipeline`)."""
    from cyris.entrypoints import cli

    cfg, gateway, ledger, llm, _ = tenant
    inner = llm.complete

    async def stopped_on_the_second_call(prompt: str, **kwargs):
        if len(llm.calls) == 1:
            # As the container's stop arrives: mid-call, at an await.
            os.kill(os.getpid(), signal.SIGTERM)
            await asyncio.sleep(60)
        return await inner(prompt, **kwargs)

    monkeypatch.setattr(llm, "complete", stopped_on_the_second_call)
    build = bootstrap.build_deps
    monkeypatch.setattr(
        bootstrap,
        "build_deps",
        lambda *a, **k: replace(
            build(*a, **k), fetch_sources=[FakeSource([_article()])], lengthen_articles=None
        ),
    )

    with pytest.raises(typer.Exit) as stopped:
        cli._run_pipeline(cfg, "morning", False, False, None, None, False)

    assert stopped.value.exit_code == 128 + signal.SIGTERM
    # The first call was answered and billed; the second was cut off before any answer.
    [charged] = ledger.reports
    assert (charged["llm_calls"], charged["input_tokens"]) == (1, 1_000)
    assert _summary(gateway)["ai_spend"]["recorded"] is True


async def test_a_ledger_that_cannot_be_read_lets_the_run_spend_and_still_charges_it(tenant) -> None:
    cfg, _, ledger, llm, _ = tenant
    ledger.budget_status = 503
    deps = replace(bootstrap.build_deps(cfg), fetch_sources=[FakeSource([_article()])])

    report = await run_digest(deps, RunOptions(period="morning"))

    assert report.status == "ok"
    assert llm.calls
    assert len(ledger.reports) == 1
    # Failing open is the operator's to know: one alert, through the run's operator gateway.
    [alert] = ledger.alerts
    assert alert["path"] == "/t/alpha/alert"
    assert alert["kind"] == hosted_spend.LEDGER_ALERT_KIND
    assert alert["subject"] == "AI spend ledger unreachable"
    assert "not held to it" in alert["body"] and "503" in alert["body"]


async def test_a_ledger_down_all_run_tells_the_operator_once_and_a_lost_report_too(
    tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, gateway, ledger, llm, _ = tenant
    monkeypatch.setattr("cyris.adapters.store.d1.time.sleep", lambda _s: None)
    ledger.budget_status = ledger.spend_status = 503
    deps = replace(bootstrap.build_deps(cfg), fetch_sources=[FakeSource([_article()])])

    report = await run_digest(deps, RunOptions(period="morning"))

    assert report.status == "ok" and llm.calls
    assert _summary(gateway)["ai_spend"]["recorded"] is False
    # Asked and reported, both failed: still one alert for the run.
    assert len(ledger.alerts) == 1

    # A month that answers and a report that is lost: the alert says what the month lacks.
    ledger.budget_status, ledger.alerts = 200, []
    deps = replace(bootstrap.build_deps(cfg), fetch_sources=[FakeSource([_article()])])
    await run_digest(deps, RunOptions(period="evening"))
    [alert] = ledger.alerts
    assert "was not added to its tenant's month" in alert["body"]


# The operator's Workers AI account and embedding token, which `scripts/hosted.sh secrets` sets on
# the hosted Worker and every tenant's container is handed (ADR-0049).
OPERATOR_ACCOUNT = "0123456789abcdef0123456789abcdef"
OPERATOR_EMBEDDING_TOKEN = "cf-operator-embedding"
# What Workers AI reports it charged for one embedded text, in its answer's `meta`.
NEURONS_PER_TEXT = 0.035


def _workers_ai_answer(request: httpx.Request) -> httpx.Response:
    texts = json.loads(request.content)["text"]
    meta = {"neurons": NEURONS_PER_TEXT * len(texts)}
    return httpx.Response(
        200, json={"success": True, "result": {"data": [[1.0, 0.0] for _ in texts], "meta": meta}}
    )


async def test_a_tenant_that_turns_vote_similarity_on_embeds_on_the_operators_workers_ai(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The switch is the tenant's row; the embedder is the operator's, whatever embedder the rows
    name; and its neurons reach the tenant's month as `embedding_usd`."""
    from fakes import seed_d1_settings
    from test_hosted_run_wiring import _files, _hosted_env, _through

    _hosted_env(monkeypatch)
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", OPERATOR_ACCOUNT)
    monkeypatch.setenv("CLOUDFLARE_EMBEDDING_API_TOKEN", OPERATOR_EMBEDDING_TOKEN)
    monkeypatch.delenv("CLOUDFLARE_AI_TOKEN", raising=False)
    gateway = FakeGateway("alpha")
    apply_hosted_schema(gateway.db)
    seed_d1_settings(
        gateway.db,
        **{
            "llm_provider.provider": "anthropic",
            "llm_provider.model": HAIKU,
            "vote_similarity.enabled": True,
            # Rows the tenant's own container could have written.
            "vote_similarity.provider": "gemini",
            "vote_similarity.model": "gemini-embedding-001",
        },
    )
    gateway.db.query(
        "INSERT INTO stored_articles (url, original_id, title, published_at, source_name, "
        "source_lane, state, first_seen_at, triaged_at) VALUES ('https://a.test/old', 0, "
        "'An old rumour', '2026-11-01T00:00:00.000000+00:00', 'A', 'read', 'rejected', "
        "'2026-11-01T00:00:00.000000+00:00', '2026-11-01T01:00:00+00:00')"
    )
    _through(gateway, monkeypatch)
    ledger = FakeSpendGateway("alpha")
    ledger_through(ledger, monkeypatch)
    llm = FakeLLM(SCORES, model=HAIKU, input_tokens=1_000, output_tokens=200)
    monkeypatch.setattr(bootstrap, "build_llm", lambda _cfg: llm)

    cfg = bootstrap.load_effective_config(*_files(tmp_path))
    deps = replace(bootstrap.build_deps(cfg), fetch_sources=[FakeSource([_article()])])
    with respx.mock:
        model = bootstrap.embedding_defaults("workers_ai")["model"]
        workers_ai = respx.post(
            f"https://api.cloudflare.com/client/v4/accounts/{OPERATOR_ACCOUNT}/ai/run/{model}"
        ).mock(side_effect=_workers_ai_answer)
        anywhere = respx.route().mock(return_value=httpx.Response(500))
        report = await run_digest(deps, RunOptions(period="morning"))

    assert (cfg.app.vote_similarity.provider, cfg.app.vote_similarity.enabled) == (
        "workers_ai",
        True,
    )
    assert isinstance(deps.embedder, MeteredEmbedder)
    assert report.status == "ok"
    assert anywhere.call_count == 0
    # One call for the downvote it is seeded with, one for the window's one candidate.
    assert workers_ai.call_count == 2
    for call in workers_ai.calls:
        assert call.request.headers["Authorization"] == f"Bearer {OPERATOR_EMBEDDING_TOKEN}"
    [charged] = ledger.reports
    assert charged["embedded_texts"] == 2
    assert charged["embedding_usd"] > 0
    assert charged["embedding_usd"] == pytest.approx(
        2 * NEURONS_PER_TEXT * WORKERS_AI_USD_PER_KNEURON / 1000
    )


def test_only_a_tenants_own_run_is_metered_never_a_self_hosted_one_or_the_operators(
    tenant, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, *_ = tenant
    hosted = bootstrap.build_deps(cfg)
    assert isinstance(hosted.llm, MeteredLLM)
    assert isinstance(hosted.embedder, MeteredEmbedder)
    assert isinstance(hosted.ask_clef, MeteredClef)
    assert hosted.record_spend is not None

    monkeypatch.delenv("CYRIS_STORE_TENANT_ID")
    monkeypatch.delenv("CYRIS_STORE_HOSTED_DOMAIN")
    monkeypatch.setenv("CYRIS_STORE_BACKEND", "sqlite")
    own = tmp_path / "own"
    own.mkdir()
    (own / "cyris.toml").write_text(
        f'[agent_vault]\npath = "{own / "vault"}"\n[store]\nsqlite_path = "{own / "c.sqlite3"}"\n',
        encoding="utf-8",
    )
    (own / "sources.yaml").write_text(SOURCES, encoding="utf-8")
    self_hosted = bootstrap.build_deps(
        load_files(own / "cyris.toml", own / "sources.yaml", AI_SETTINGS)
    )
    assert not isinstance(self_hosted.llm, MeteredLLM)
    assert not isinstance(self_hosted.ask_clef, MeteredClef)
    assert self_hosted.record_spend is None
    assert self_hosted.html_writer is None or self_hosted.html_writer.ai_pause is None


def test_the_spend_host_is_one_on_both_sides() -> None:
    js = (Path(__file__).resolve().parents[1] / "workers/app/src/hosted_spend.js").read_text(
        "utf-8"
    )

    assert f'export const SPEND_HOST = "{httpx.URL(SPEND_ORIGIN).host}";' in js
