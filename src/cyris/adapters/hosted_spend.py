"""A hosted tenant's AI spend: measured as each call is made, charged to the tenant's month.

The beta charges readers nothing and the operator pays for every model call, so each tenant has
a monthly AI allowance, held in the operator's registry where no run container can write it
(`workers/app/src/ai_budget.js`). This module is the run's side of it:

* `SpendMeter` counts every LLM response and every embedded text as it happens, through
  `MeteredLLM` and `MeteredEmbedder`, so a run that raises halfway has still counted what it
  spent. The run's own `UsageStats` cannot do that: it is summed from the stages that finished.
* `SpendLedger` asks the hosted Worker, as the run is wired, whether the tenant's month is spent,
  and reports the meter, as the run ends, failed or not. Both go to `SPEND_ORIGIN`, which the
  hosted Worker intercepts and answers with a gateway bound to this tenant (`SpendGateway`).

Why the allowance lives in the registry, why a run is checked only as it starts, and why an
unreachable ledger lets the run spend: docs/decisions/0038-a-hosted-tenants-ai-spend-is-metered-
in-the-registry.md.
"""

from __future__ import annotations

import logging
import math
from typing import Any

import httpx

from cyris.adapters.hosted import OPERATOR_ORIGIN
from cyris.adapters.store.d1 import post_with_retries
from cyris.domain.models import _PRICES_PER_MTOK, UsageStats
from cyris.i18n import ENGLISH, translator

logger = logging.getLogger(__name__)

# The host the hosted Worker intercepts for the tenant's allowance and spend. Plain http: the
# request is answered inside the Workers runtime. `SPEND_HOST` in workers/app/src/hosted_spend.js
# is the same name; tests/test_hosted_config.py holds the two.
SPEND_ORIGIN = "http://spend.cyris"

# One ask as the run starts. Short, since the answer only decides whether this run may spend.
BUDGET_TIMEOUT_SECONDS = 10.0

# The `kind` of the operator alert a ledger the run cannot reach sends (`tell_operator`). It ends
# no run, so it is not among `ENDING_KINDS` in workers/app/src/hosted_alerts.js.
LEDGER_ALERT_KIND = "ai_ledger_unreachable"

# Why a run may call no model (`SpendLedger.spent`): its month's allowance is spent, or the
# operator set it to 0. Each has its own notice on the page, since only the first comes back
# when the month turns.
SPENT = "spent"
OFF = "off"

# Grade A, docs/architecture.md §5. gemini-embedding-001's list price per million input tokens,
# Google's paid tier as published when this was written (not re-read on the account): the API
# reports no token count, so the tokens are estimated below.
GEMINI_EMBEDDING_USD_PER_MTOK = 0.15
# One token per three UTF-8 bytes of an embedded text, rounded up per text. A CJK character is
# three bytes and about one token, and English runs about four characters a token, so this
# reads a Chinese text about right and an English one about a third high: a loss ceiling
# should err high, never low.
EMBEDDING_BYTES_PER_TOKEN = 3
# What a model with no rate card in `domain/models.py` is charged per million tokens: the dearest
# input and the dearest output rate that table knows. Not zero, which would give a tenant on an
# unlisted model AI without limit; and not a refusal to run its AI, which would cost the reader
# their issue for a gap in this table, when the operator can close it with one rate card. A
# ceiling should err high, and this errs as high as any model the deployment has priced.
UNPRICED_USD_PER_MTOK = (
    max(rates[0] for rates in _PRICES_PER_MTOK.values()),
    max(rates[1] for rates in _PRICES_PER_MTOK.values()),
)
# Workers AI's price per thousand neurons past the account's free daily allowance. Every neuron
# is charged here, since the free allowance is the account's, not a tenant's.
WORKERS_AI_USD_PER_KNEURON = 0.011


def estimated_embedding_tokens(texts: list[str]) -> int:
    """The tokens `texts` are charged as when the provider reports none."""
    return sum(math.ceil(len(t.encode("utf-8")) / EMBEDDING_BYTES_PER_TOKEN) for t in texts)


class SpendMeter:
    """Every AI call this run made, priced as it was made."""

    def __init__(self, llm_model: str) -> None:
        # Per response, so a stage that raised has still counted the calls it made.
        self.llm = UsageStats(model=llm_model)
        self.embedded_texts = 0
        self.embedding_tokens = 0
        self.embedding_usd = 0.0

    def count_llm(self, response: Any) -> None:
        self.llm.add(response.input_tokens, response.output_tokens, response.neurons)

    def count_embedding(self, texts: list[str], provider: str, neurons: float | None) -> None:
        """`texts` were embedded; `neurons` is what Workers AI reported for them, if it did."""
        self.embedded_texts += len(texts)
        if neurons is not None:
            self.embedding_usd += neurons * WORKERS_AI_USD_PER_KNEURON / 1000
            return
        tokens = estimated_embedding_tokens(texts)
        self.embedding_tokens += tokens
        if provider == "gemini":
            self.embedding_usd += tokens * GEMINI_EMBEDDING_USD_PER_MTOK / 1_000_000

    def report(self) -> dict[str, float | int]:
        """What the ledger adds to the tenant's month (`checkSpend` in ai_budget.js)."""
        cost = self.llm.estimated_cost
        unpriced = 0
        if cost is None and self.llm.neurons is not None:
            cost = self.llm.neurons * WORKERS_AI_USD_PER_KNEURON / 1000
        elif cost is None:
            # A model with no rate card here: charged at the dearest rates, and its tokens
            # named apart, so the operator sees which spend is a guess.
            input_rate, output_rate = UNPRICED_USD_PER_MTOK
            cost = (
                self.llm.input_tokens * input_rate + self.llm.output_tokens * output_rate
            ) / 1_000_000
            unpriced = self.llm.total_tokens
        return {
            "llm_usd": round(cost, 8),
            "embedding_usd": round(self.embedding_usd, 8),
            "input_tokens": self.llm.input_tokens,
            "output_tokens": self.llm.output_tokens,
            "unpriced_tokens": unpriced,
            "llm_calls": self.llm.api_calls,
            "embedded_texts": self.embedded_texts,
            "embedding_tokens_estimated": self.embedding_tokens,
        }


class MeteredLLM:
    """An `LLMClient` that counts each response on `meter` before handing it back, and the
    usage a raised error says was billed before raising it."""

    def __init__(self, inner: Any, meter: SpendMeter) -> None:
        self._inner = inner
        self._meter = meter
        self.model = inner.model

    async def complete(self, prompt: str, **kwargs: Any) -> Any:
        try:
            response = await self._inner.complete(prompt, **kwargs)
        except Exception as e:
            # Billed, then raised: a thinking model that spent its whole output budget, or a
            # blocked prompt (`BilledError` in adapters/gemini_client.py). Paid for all the same.
            if (billed := getattr(e, "billed", None)) is not None:
                self._meter.count_llm(billed)
            raise
        self._meter.count_llm(response)
        return response

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class MeteredEmbedder:
    """An `Embedder` that counts on `meter` what each `embed` call got embedded, even one that
    raised after some of its batches: the adapters count a batch as it succeeds, unique texts
    in order, so the texts embedded are the first that many."""

    def __init__(self, inner: Any, meter: SpendMeter, provider: str) -> None:
        self._inner = inner
        self._meter = meter
        self._provider = provider

    @property
    def usage(self) -> Any:
        return self._inner.usage

    async def embed(self, texts: list[str]) -> list[list[float]]:
        usage = self._inner.usage
        embedded, neurons = usage.embedded, usage.neurons
        try:
            return await self._inner.embed(texts)
        finally:
            done = list(dict.fromkeys(texts))[: usage.embedded - embedded]
            spent = None if usage.neurons is None else usage.neurons - (neurons or 0.0)
            self._meter.count_embedding(done, self._provider, spent)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class SpendLedger:
    """The tenant's month in the operator's registry, through the hosted Worker's gateway."""

    def __init__(
        self, tenant_id: str, origin: str = SPEND_ORIGIN, operator_origin: str = OPERATOR_ORIGIN
    ) -> None:
        self.tenant_id = tenant_id
        self._base = f"{origin}/t/{tenant_id}"
        self._alert_url = f"{operator_origin}/t/{tenant_id}/alert"
        self._http = httpx.Client(timeout=BUDGET_TIMEOUT_SECONDS)
        # One ledger per run, so one alert per run however many of its calls fail.
        self._told = False

    def tell_operator(self, error: object, *, unread: bool) -> None:
        """Alert the operator, once per run, that the ledger did not answer: a ledger that is
        down holds no tenant to its allowance, and a log line alone would go unread. `unread`
        is the month asked as the run is wired; else it is the run's report that was lost.

        Through the hosted run's operator gateway (ADR-0035), in English as every operator
        alert is. Never raises.
        """
        if self._told:
            return
        self._told = True
        t = translator(ENGLISH)
        if unread:
            body = t("alert.ai_ledger_unread_body", error=str(error))
        else:
            body = t("alert.ai_ledger_unrecorded_body", error=str(error))
        payload = {
            "kind": LEDGER_ALERT_KIND,
            "subject": t("alert.ai_ledger_headline"),
            "body": body,
        }
        try:
            self._http.post(self._alert_url, json=payload).raise_for_status()
        except httpx.HTTPError as e:
            logger.warning("Operator alert failed: %s", e)

    def spent(self) -> str | None:
        """Why this run may call no model: `SPENT`, the tenant's allowance for this month is
        spent, or `OFF`, its allowance is 0, which no new month brings back. None to spend.

        None when the ledger cannot say: one run's spend, about two US cents, is the cost of a
        ledger outage, where the other answer would cost every reader their issue's AI.
        """
        try:
            resp = self._http.get(f"{self._base}/budget")
            resp.raise_for_status()
            month = resp.json()
            if not isinstance(month, dict):
                raise ValueError(f"the month is not a JSON object: {resp.text[:200]}")
        except (httpx.HTTPError, ValueError) as e:
            logger.warning("AI allowance unreadable, this run may spend: %s", e)
            self.tell_operator(e, unread=True)
            return None
        logger.info(
            "AI allowance %s: %s of %s USD spent",
            month.get("month"),
            month.get("cost_usd"),
            month.get("budget_usd"),
        )
        if not month.get("spent"):
            return None
        return OFF if month.get("budget_usd") == 0 else SPENT

    def report(self, meter: SpendMeter, *, paused: str | None = None) -> dict[str, Any]:
        """Charge what `meter` counted to the tenant's month; answers it for `run_summary`,
        with `ai_budget` set to why the run was `paused`, `spent` or `off`, when it was.

        Never raises: a run's own result stands whatever the ledger says.
        """
        spend = meter.report()
        said = {"ai_budget": paused} if paused else {}
        try:
            post_with_retries(self._http, f"{self._base}/spend", spend, what="AI spend ledger")
        except Exception as e:  # noqa: BLE001 - a 2xx that is not JSON raises ValueError, too
            logger.error("AI spend not recorded: %s", e)
            self.tell_operator(e, unread=False)
            return {**spend, **said, "recorded": False}
        return {**spend, **said, "recorded": True}
