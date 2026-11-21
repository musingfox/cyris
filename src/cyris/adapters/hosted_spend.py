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

from cyris.adapters.store.d1 import D1Error, post_with_retries
from cyris.domain.models import UsageStats

logger = logging.getLogger(__name__)

# The host the hosted Worker intercepts for the tenant's allowance and spend. Plain http: the
# request is answered inside the Workers runtime. `SPEND_HOST` in workers/app/src/hosted_spend.js
# is the same name; tests/test_hosted_config.py holds the two.
SPEND_ORIGIN = "http://spend.cyris"

# One ask as the run starts. Short, since the answer only decides whether this run may spend.
BUDGET_TIMEOUT_SECONDS = 10.0

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
            # A model with no rate card here: its tokens are counted, and named apart, so the
            # operator sees spend the total cannot price.
            cost, unpriced = 0.0, self.llm.total_tokens
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
    """An `LLMClient` that counts each response on `meter` before handing it back."""

    def __init__(self, inner: Any, meter: SpendMeter) -> None:
        self._inner = inner
        self._meter = meter
        self.model = inner.model

    async def complete(self, prompt: str, **kwargs: Any) -> Any:
        response = await self._inner.complete(prompt, **kwargs)
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

    def __init__(self, tenant_id: str, origin: str = SPEND_ORIGIN) -> None:
        self.tenant_id = tenant_id
        self._base = f"{origin}/t/{tenant_id}"
        self._http = httpx.Client(timeout=BUDGET_TIMEOUT_SECONDS)

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
        except (httpx.HTTPError, ValueError) as e:
            logger.warning("AI allowance unreadable, this run may spend: %s", e)
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
        except D1Error as e:
            logger.error("AI spend not recorded: %s", e)
            return {**spend, **said, "recorded": False}
        return {**spend, **said, "recorded": True}
