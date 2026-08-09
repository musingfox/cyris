"""Gemini embedding adapter, with an on-disk cache keyed by text.

Workers AI (`@cf/baai/bge-m3`) is the natural home for this once the pipeline
moves to Cloudflare, but the account token in use carries only `account (read)`
and is refused by the AI endpoint. Gemini needs no new credential — the digest
already runs on GEMINI_API_KEY — so it is what the measurement used and what this
implements. Swapping later is a new class behind ports.Embedder, nothing else.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path

import httpx

from cyris.domain.similarity import normalize

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-embedding-001"
API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"
# The free tier 429s well before 100. 50 with a pause between batches held for a
# full-corpus pass; the retry below covers the rest.
BATCH_SIZE = 50
PAUSE_SECONDS = 1.5
MAX_RETRIES = 5
TIMEOUT_SECONDS = 180


class GeminiEmbedder:
    """Embeds text via the Gemini API, caching every vector it has ever produced.

    The cache is keyed by a hash of the text, not by URL: the same headline
    re-fetched under a tracking-stripped URL must not pay twice, and a voted
    article's vector has to survive the article leaving the store.

    ponytail: whole-file JSON, rewritten on every miss. 3072 float64 per row as
    text is ~14KB, so the corpus reached 81MB — fine for one machine and a few
    hundred candidates a run, wrong past that. The upgrade is Vectorize (or any
    vector store) once this moves to Cloudflare, which is also where the read
    pattern stops being "load everything".
    """

    def __init__(self, api_key: str, cache_path: Path, model: str = DEFAULT_MODEL) -> None:
        self._api_key = api_key
        self._model = model
        self._cache_path = cache_path
        self._cache: dict[str, list[float]] = {}
        if cache_path.exists():
            try:
                self._cache = json.loads(cache_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                logger.warning("Embedding cache unreadable, starting empty: %s", cache_path)

    @staticmethod
    def _key(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Return a unit-length vector per input, hitting the API only for misses."""
        missing = [t for t in dict.fromkeys(texts) if self._key(t) not in self._cache]
        if missing:
            logger.info("Embedding %d new text(s) (%d cached)", len(missing), len(self._cache))
            await self._fetch_into_cache(missing)
            self._save()
        return [self._cache.get(self._key(t), []) for t in texts]

    async def _fetch_into_cache(self, texts: list[str]) -> None:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as http:
            for start in range(0, len(texts), BATCH_SIZE):
                batch = texts[start : start + BATCH_SIZE]
                vectors = await self._post_batch(http, batch)
                for text, vector in zip(batch, vectors, strict=True):
                    self._cache[self._key(text)] = normalize(vector)
                if start + BATCH_SIZE < len(texts):
                    await asyncio.sleep(PAUSE_SECONDS)

    async def _post_batch(self, http: httpx.AsyncClient, batch: list[str]) -> list[list[float]]:
        url = f"{API_ROOT}/{self._model}:batchEmbedContents?key={self._api_key}"
        payload = {
            "requests": [
                {"model": f"models/{self._model}", "content": {"parts": [{"text": t}]}}
                for t in batch
            ]
        }
        for attempt in range(MAX_RETRIES):
            response = await http.post(url, json=payload)
            if response.status_code == 429:
                # Exponential backoff: the free tier rate-limits a full-corpus pass
                # partway through, and the whole batch is lost if we give up here.
                wait = 3 * 2**attempt
                logger.warning("Embedding rate-limited, retrying in %ds", wait)
                await asyncio.sleep(wait)
                continue
            response.raise_for_status()
            return [e["values"] for e in response.json()["embeddings"]]
        raise RuntimeError(f"Embedding API rate-limited after {MAX_RETRIES} retries")

    def _save(self) -> None:
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_path.write_text(json.dumps(self._cache), encoding="utf-8")
        except OSError as e:
            # A lost cache costs money and time, never correctness.
            logger.warning("Could not persist embedding cache: %s", e)
