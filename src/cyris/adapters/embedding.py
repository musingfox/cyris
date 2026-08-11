"""Embedding adapters behind `ports.Embedder`.

Two providers, kept side by side deliberately. The 2026-08-10 evaluation found them
behaviourally identical on the whole store — same 69 suppressions, zero disagreement —
but that was one downvote class with a very wide margin, and the corpus is small. They
run in parallel so the comparison keeps accumulating on real traffic, including the two
things a one-off measurement cannot show: what each actually costs and how long it takes.

See docs/vote-signal-measurement.md. Swapping is a config choice, not a code change.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from pathlib import Path

import httpx

from cyris.domain.similarity import normalize

logger = logging.getLogger(__name__)

GEMINI_MODEL = "gemini-embedding-001"
GEMINI_API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"
WORKERS_AI_MODEL = "@cf/baai/bge-m3"
WORKERS_AI_ROOT = "https://api.cloudflare.com/client/v4/accounts"

MAX_RETRIES = 5
TIMEOUT_SECONDS = 180


class _VectorCache:
    """Every vector ever produced, keyed by a hash of the text.

    Keyed by text and not by URL: the same headline re-fetched under a
    tracking-stripped URL must not pay twice, and a voted article's vector has to
    survive the article leaving the store.

    ponytail: whole-file JSON, rewritten on every miss. 3072 float64 per row as text
    is ~14KB, so the Gemini corpus reached 81MB — fine for one machine and a few
    hundred candidates a run, wrong past that. The upgrade is Vectorize (or any vector
    store) once this moves to Cloudflare, which is also where the read pattern stops
    being "load everything".
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._data: dict[str, list[float]] = {}
        if path.exists():
            try:
                self._data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                logger.warning("Embedding cache unreadable, starting empty: %s", path)

    @staticmethod
    def key(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]

    def __len__(self) -> int:
        return len(self._data)

    def get(self, text: str) -> list[float]:
        return self._data.get(self.key(text), [])

    def put(self, text: str, vector: list[float]) -> None:
        self._data[self.key(text)] = vector

    def missing(self, texts: list[str]) -> list[str]:
        return [t for t in dict.fromkeys(texts) if self.key(t) not in self._data]

    def save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(self._data), encoding="utf-8")
        except OSError as e:
            # A lost cache costs money and time, never correctness.
            logger.warning("Could not persist embedding cache: %s", e)


class _Usage:
    """What one provider spent, for the side-by-side log.

    `input_tokens` and `neurons` are None where the API does not report them —
    Gemini's `batchEmbedContents` returns bare vectors — rather than filled with a
    guess that would read like a measurement.
    """

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
        cache_path: Path,
        model: str = GEMINI_MODEL,
        output_dimensions: int | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._dims = output_dimensions
        self._cache = _VectorCache(cache_path)
        self.usage = _Usage()

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Return a unit-length vector per input, hitting the API only for misses."""
        missing = self._cache.missing(texts)
        if missing:
            logger.info("Embedding %d new text(s) (%d cached)", len(missing), len(self._cache))
            async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as http:
                for start in range(0, len(missing), self.BATCH_SIZE):
                    batch = missing[start : start + self.BATCH_SIZE]
                    for text, vector in zip(
                        batch, await self._post_batch(http, batch), strict=True
                    ):
                        self._cache.put(text, normalize(vector))
                    self.usage.embedded += len(batch)
                    if start + self.BATCH_SIZE < len(missing):
                        await asyncio.sleep(self.PAUSE_SECONDS)
            self._cache.save()
        return [self._cache.get(t) for t in texts]

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

    Multilingual, which the 62%-中央社 corpus requires — the English-only trap is the
    `bge-*-en-v1.5` family, not this model. Its cosines run lower than Gemini's across
    the board, so its threshold is its own (~0.53 against ~0.68); the scale differs,
    the discrimination does not.

    Needs a token with Workers AI → Read; the wrangler token carries account:read only.
    """

    # No pause: the text-embedding limit is 3000 req/min and a full-corpus pass at 100
    # per batch never came near it.
    BATCH_SIZE = 100

    def __init__(
        self,
        api_token: str,
        account_id: str,
        cache_path: Path,
        model: str = WORKERS_AI_MODEL,
    ) -> None:
        self._token = api_token
        self._account = account_id
        self._model = model
        self._cache = _VectorCache(cache_path)
        self.usage = _Usage()
        self.usage.input_tokens = 0
        self.usage.neurons = 0.0

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Return a unit-length vector per input, hitting the API only for misses."""
        missing = self._cache.missing(texts)
        if missing:
            logger.info("Embedding %d new text(s) (%d cached)", len(missing), len(self._cache))
            async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as http:
                for start in range(0, len(missing), self.BATCH_SIZE):
                    batch = missing[start : start + self.BATCH_SIZE]
                    for text, vector in zip(
                        batch, await self._post_batch(http, batch), strict=True
                    ):
                        self._cache.put(text, normalize(vector))
                    self.usage.embedded += len(batch)
            self._cache.save()
        return [self._cache.get(t) for t in texts]

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
