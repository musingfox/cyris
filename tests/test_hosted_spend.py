"""A hosted tenant's AI spend: metered as each call is made, charged to its own month.

The hosted Worker's `SpendGateway` answers a run container's `GET /t/<tenant>/budget` and
`POST /t/<tenant>/spend` from the operator's registry (ADR-0038). Here `FakeSpendGateway` answers
them as it does, so a run is checked against its tenant's month as it starts and charges what it
spent as it ends: every LLM response and every embedded text, priced in the container, failed run
or not. A run that starts with the month spent calls no model and says so on its page. The month
itself, and its reset on the first of the next, is the registry's (`workers/app/test/
hosted_spend.test.js`).
"""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from fakes import TEST_SETTINGS, FakeLLM, load_files
from test_run_digest import FakeSource
from test_tenant_gateway import FakeGateway

from cyris import bootstrap
from cyris.adapters import hosted_spend
from cyris.adapters.embedding import _Usage
from cyris.adapters.hosted import OPERATOR_ORIGIN
from cyris.adapters.hosted_spend import (
    EMBEDDING_BYTES_PER_TOKEN,
    GEMINI_EMBEDDING_USD_PER_MTOK,
    SPEND_ORIGIN,
    WORKERS_AI_USD_PER_KNEURON,
    MeteredEmbedder,
    MeteredLLM,
    SpendLedger,
    SpendMeter,
    estimated_embedding_tokens,
)
from cyris.adapters.store.tenant_gateway import apply_hosted_schema
from cyris.domain.models import Article, DigestContent, Lane, UsageStats
from cyris.service_layer.run_digest import RunOptions, run_digest

pytestmark = pytest.mark.integration

HAIKU = "claude-haiku-5-5"


class FakeSpendGateway:
    """Answers `GET /t/<tenant>/budget` and `POST /t/<tenant>/spend` as `SpendGateway` does.

    Only `tenant` is this run's; another tenant, path or method is refused with 403. `spent`
    is what the month's answer says; every report posted is kept in `reports`.
    """

    def __init__(self, tenant: str, *, spent: bool = False, budget_status: int = 200) -> None:
        self.tenant = tenant
        self.spent = spent
        self.budget_usd = 2.25
        self.budget_status = budget_status
        self.requests: list[httpx.Request] = []
        self.reports: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
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
            self.reports.append(json.loads(request.content))
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


async def test_a_model_with_no_rate_card_is_counted_apart_and_workers_ai_by_its_neurons() -> None:
    unpriced = SpendMeter("someones-model")
    await MeteredLLM(
        FakeLLM(model="someones-model", input_tokens=10, output_tokens=5), unpriced
    ).complete("p")
    neurons = SpendMeter("@cf/meta/llama")
    neurons.count_llm(type("R", (), {"input_tokens": 1, "output_tokens": 1, "neurons": 2000.0})())

    assert (unpriced.report()["llm_usd"], unpriced.report()["unpriced_tokens"]) == (0.0, 15)
    assert neurons.report()["llm_usd"] == pytest.approx(2 * WORKERS_AI_USD_PER_KNEURON)


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


async def test_a_run_whose_month_is_spent_calls_no_model_and_its_page_says_why(tenant) -> None:
    cfg, gateway, ledger, llm, embedder = tenant
    ledger.spent = True
    article = _article()
    deps = replace(bootstrap.build_deps(cfg), fetch_sources=[FakeSource([article])])

    report = await run_digest(deps, RunOptions(period="morning"))

    assert report.status == "ok"
    assert llm.calls == []
    assert embedder.calls == []
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


async def test_a_ledger_that_cannot_be_read_lets_the_run_spend_and_still_charges_it(tenant) -> None:
    cfg, _, ledger, llm, _ = tenant
    ledger.budget_status = 503
    deps = replace(bootstrap.build_deps(cfg), fetch_sources=[FakeSource([_article()])])

    report = await run_digest(deps, RunOptions(period="morning"))

    assert report.status == "ok"
    assert llm.calls
    assert len(ledger.reports) == 1


def test_only_a_tenants_own_run_is_metered_never_a_self_hosted_one_or_the_operators(
    tenant, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, *_ = tenant
    hosted = bootstrap.build_deps(cfg)
    assert isinstance(hosted.llm, MeteredLLM)
    assert isinstance(hosted.embedder, MeteredEmbedder)
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
    assert self_hosted.record_spend is None
    assert self_hosted.html_writer is None or self_hosted.html_writer.ai_pause is None


def test_the_spend_host_is_one_on_both_sides() -> None:
    js = (Path(__file__).resolve().parents[1] / "workers/app/src/hosted_spend.js").read_text(
        "utf-8"
    )

    assert f'export const SPEND_HOST = "{httpx.URL(SPEND_ORIGIN).host}";' in js
