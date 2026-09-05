"""Embedding adapters behind `ports.Embedder`.

Two providers, kept side by side deliberately. The 2026-08-10 evaluation found them
behaviourally identical on the whole store — same 69 suppressions, zero disagreement —
but that was one downvote class with a very wide margin, and the corpus is small. They
run in parallel so the comparison keeps accumulating on real traffic, including the two
things a one-off measurement cannot show: what each actually costs and how long it takes.

See docs/vote-signal-measurement.md. Swapping is a config choice, not a code change.

**Neither keeps a vector cache**, and that is a deliberate removal (2026-08-27). One
existed — whole-file JSON, 338 MB for Gemini — and it optimised a cost that stopped
existing when bge-m3 became the provider: 222 texts measured at 7.59 neurons, so a full
run of ~600 (400 seeds + 200 candidates) is ~20 against a 10,000/day free allowance.
Persisting 22 MB to skip five seconds of arithmetic is not a trade worth a storage tier.
The cache never extended a seed's life either — `vote_similarity._voted` reads seeds from
store rows, so a deleted row takes its seed with it, cached vector or not.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from cyris.domain.similarity import normalize

logger = logging.getLogger(__name__)

GEMINI_MODEL = "gemini-embedding-001"
GEMINI_API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"
WORKERS_AI_MODEL = "@cf/baai/bge-m3"
WORKERS_AI_ROOT = "https://api.cloudflare.com/client/v4/accounts"

MAX_RETRIES = 5
TIMEOUT_SECONDS = 180


class _Usage:
    """`ports.EmbeddingUsage` — see there for what the fields mean and why None."""

    def __init__(self) -> None:
        self.requests = 0
        self.embedded = 0
        self.api_seconds = 0.0
        self.input_tokens: int | None = None
        self.neurons: float | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "requests": self.requests,
            "embedded": self.embedded,
            "api_seconds": round(self.api_seconds, 2),
            "input_tokens": self.input_tokens,
            "neurons": round(self.neurons, 4) if self.neurons is not None else None,
        }


class GeminiEmbedder:
    """Embeds text via the Gemini API. 3072 dimensions, or fewer on request.

    `output_dimensions` truncates via Matryoshka: the API's 1024d vector is the first
    1024 dims of the 3072d one renormalised, at cosine 1.000000. Below 3072 the API
    returns non-unit vectors, which `normalize` fixes.
    """

    # 429s appear well before 100 per batch — on the paid tier, so this is the client
    # being polite rather than a tier to upgrade out of.
    BATCH_SIZE = 50
    PAUSE_SECONDS = 1.5

    def __init__(
        self,
        api_key: str,
        model: str = GEMINI_MODEL,
        output_dimensions: int | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._dims = output_dimensions
        self.usage = _Usage()

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Return a unit-length vector per input."""
        unique = list(dict.fromkeys(texts))
        if not unique:
            return []
        logger.info("Embedding %d text(s)", len(unique))
        vectors: dict[str, list[float]] = {}
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as http:
            for start in range(0, len(unique), self.BATCH_SIZE):
                batch = unique[start : start + self.BATCH_SIZE]
                for text, vector in zip(batch, await self._post_batch(http, batch), strict=True):
                    vectors[text] = normalize(vector)
                self.usage.embedded += len(batch)
                if start + self.BATCH_SIZE < len(unique):
                    await asyncio.sleep(self.PAUSE_SECONDS)
        return [vectors[t] for t in texts]

    async def _post_batch(self, http: httpx.AsyncClient, batch: list[str]) -> list[list[float]]:
        url = f"{GEMINI_API_ROOT}/{self._model}:batchEmbedContents?key={self._api_key}"
        request: dict[str, object] = {"model": f"models/{self._model}"}
        if self._dims:
            request["outputDimensionality"] = self._dims
        payload = {"requests": [{**request, "content": {"parts": [{"text": t}]}} for t in batch]}
        for attempt in range(MAX_RETRIES):
            started = time.monotonic()
            response = await http.post(url, json=payload)
            self.usage.api_seconds += time.monotonic() - started
            self.usage.requests += 1
            if response.status_code == 429:
                # Exponential backoff: a full-corpus pass gets rate-limited partway
                # through, and the whole batch is lost if we give up here.
                wait = 3 * 2**attempt
                logger.warning("Embedding rate-limited, retrying in %ds", wait)
                await asyncio.sleep(wait)
                continue
            response.raise_for_status()
            return [e["values"] for e in response.json()["embeddings"]]
        raise RuntimeError(f"Embedding API rate-limited after {MAX_RETRIES} retries")


class WorkersAIEmbedder:
    """Embeds text via Workers AI `@cf/baai/bge-m3`. 1024 dimensions, already unit-length.

    Multilingual, which a corpus 62% of which is Chinese-language newswire requires — the
    English-only trap is the
    `bge-*-en-v1.5` family, not this model. Its cosines run lower than Gemini's across
    the board, so its threshold is its own (~0.53 against ~0.68); the scale differs,
    the discrimination does not.

    Needs a token with Workers AI → Read (`CLOUDFLARE_EMBEDDING_API_TOKEN`); the wrangler
    token carries account:read only.
    """

    # No pause: the text-embedding limit is 3000 req/min and a full-corpus pass at 100
    # per batch never came near it.
    BATCH_SIZE = 100

    def __init__(
        self,
        api_token: str,
        account_id: str,
        model: str = WORKERS_AI_MODEL,
    ) -> None:
        self._token = api_token
        self._account = account_id
        self._model = model
        self.usage = _Usage()
        self.usage.input_tokens = 0
        self.usage.neurons = 0.0

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Return a unit-length vector per input."""
        unique = list(dict.fromkeys(texts))
        if not unique:
            return []
        logger.info("Embedding %d text(s)", len(unique))
        vectors: dict[str, list[float]] = {}
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as http:
            for start in range(0, len(unique), self.BATCH_SIZE):
                batch = unique[start : start + self.BATCH_SIZE]
                for text, vector in zip(batch, await self._post_batch(http, batch), strict=True):
                    vectors[text] = normalize(vector)
                self.usage.embedded += len(batch)
        return [vectors[t] for t in texts]

    async def _post_batch(self, http: httpx.AsyncClient, batch: list[str]) -> list[list[float]]:
        url = f"{WORKERS_AI_ROOT}/{self._account}/ai/run/{self._model}"
        for attempt in range(MAX_RETRIES):
            started = time.monotonic()
            response = await http.post(
                url,
                headers={"Authorization": f"Bearer {self._token}"},
                json={"text": batch},
            )
            self.usage.api_seconds += time.monotonic() - started
            self.usage.requests += 1
            if response.status_code == 429:
                wait = 3 * 2**attempt
                logger.warning("Workers AI rate-limited, retrying in %ds", wait)
                await asyncio.sleep(wait)
                continue
            response.raise_for_status()
            body = response.json()
            if not body.get("success"):
                raise RuntimeError(f"Workers AI refused the request: {body.get('errors')}")
            result = body["result"]
            # Unlike Gemini, this API reports what it charged. Recorded rather than
            # estimated, which is the whole reason the cost comparison is worth logging.
            meta = result.get("meta") or {}
            if meta.get("cost_metric_name_1") == "input_tokens":
                self.usage.input_tokens = (self.usage.input_tokens or 0) + int(
                    meta.get("cost_metric_value_1") or 0
                )
            self.usage.neurons = (self.usage.neurons or 0.0) + float(meta.get("neurons") or 0.0)
            return result["data"]
        raise RuntimeError(f"Workers AI rate-limited after {MAX_RETRIES} retries")
