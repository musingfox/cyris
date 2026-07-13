"""Generate and manage text embeddings via Ollama for similarity filtering."""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import httpx
import numpy as np

from cyris.domain.models import EmbeddingStore, TriageFeedbackData

logger = logging.getLogger(__name__)


async def generate_embedding(
    text: str,
    ollama_url: str = "http://localhost:11434",
) -> list[float]:
    """Generate a text embedding via Ollama API.

    Args:
        text: Input text to embed.
        ollama_url: Ollama API base URL.

    Returns:
        768-dimensional embedding vector.

    Raises:
        httpx.HTTPError: If API request fails.
    """
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{ollama_url}/api/embeddings",
            json={"model": "nomic-embed-text", "prompt": text},
            timeout=30.0,
        )
        response.raise_for_status()
        data = response.json()
        return data["embedding"]


async def generate_embeddings_batch(
    texts: list[str],
    ollama_url: str = "http://localhost:11434",
) -> list[list[float]]:
    """Generate embeddings for multiple texts.

    Args:
        texts: List of input texts.
        ollama_url: Ollama API base URL.

    Returns:
        List of 768-dimensional embedding vectors.
    """
    embeddings = []
    for text in texts:
        emb = await generate_embedding(text, ollama_url)
        embeddings.append(emb)
    return embeddings


def save_embedding_store(store: EmbeddingStore, path: Path) -> None:
    """Save embedding store to agent vault.

    Args:
        store: EmbeddingStore to save.
        path: Agent vault root path.
    """
    learning_dir = path / "learning"
    learning_dir.mkdir(parents=True, exist_ok=True)

    embeddings_path = learning_dir / "embeddings.json"
    embeddings_path.write_text(store.model_dump_json(indent=2))
    logger.info("Saved embedding store to %s", embeddings_path)


def load_embedding_store(path: Path) -> EmbeddingStore | None:
    """Load embedding store from agent vault.

    Args:
        path: Agent vault root path.

    Returns:
        EmbeddingStore if found, None otherwise.
    """
    embeddings_path = path / "learning" / "embeddings.json"
    if not embeddings_path.exists():
        return None

    data = json.loads(embeddings_path.read_text())
    return EmbeddingStore(**data)


def update_centroid(
    store: EmbeddingStore,
    new_embeddings: list[list[float]],
) -> EmbeddingStore:
    """Update centroid with new embeddings using weighted average.

    Args:
        store: Existing EmbeddingStore.
        new_embeddings: List of new embedding vectors to incorporate.

    Returns:
        Updated EmbeddingStore with new centroid.

    Raises:
        ValueError: If embedding dimensions don't match.
    """
    if not new_embeddings:
        return store

    old_centroid = np.array(store.centroid)
    old_count = store.sample_count

    # Validate dimensions
    for emb in new_embeddings:
        if len(emb) != len(old_centroid):
            raise ValueError(
                f"Dimension mismatch: centroid has {len(old_centroid)} dims, "
                f"new embedding has {len(emb)} dims"
            )

    new_embs_array = np.array(new_embeddings)
    new_count = len(new_embeddings)

    # Weighted average
    new_centroid = (old_centroid * old_count + np.sum(new_embs_array, axis=0)) / (
        old_count + new_count
    )

    return EmbeddingStore(
        centroid=new_centroid.tolist(),
        sample_count=old_count + new_count,
        last_updated=datetime.now(UTC).isoformat(),
    )


def build_or_update_centroid(
    existing: EmbeddingStore | None,
    embeddings: list[list[float]],
) -> EmbeddingStore:
    """Update an existing centroid or create a new store from embeddings."""
    if existing is not None:
        return update_centroid(existing, embeddings)
    return EmbeddingStore(
        centroid=np.mean(embeddings, axis=0).tolist(),
        sample_count=len(embeddings),
        last_updated=datetime.now(UTC).isoformat(),
    )


def cosine_similarity(vec_a: list[float], vec_b: list[float]) -> float:
    """Compute cosine similarity between two vectors.

    Args:
        vec_a: First vector.
        vec_b: Second vector.

    Returns:
        Cosine similarity in [-1, 1].
    """
    a = np.array(vec_a)
    b = np.array(vec_b)

    dot_product = np.dot(a, b)
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)

    if norm_a == 0 or norm_b == 0:
        return 0.0

    return float(dot_product / (norm_a * norm_b))


def filter_by_similarity(
    articles: list,
    article_embeddings: list[list[float]],
    centroid: list[float],
    threshold: float = 0.6,
) -> tuple[list, list]:
    """Filter articles by cosine similarity to centroid.

    Args:
        articles: List of Article objects.
        article_embeddings: Corresponding embedding vectors.
        centroid: Centroid embedding vector.
        threshold: Minimum similarity to pass.

    Returns:
        Tuple of (passed_articles, rejected_articles).

    Raises:
        ValueError: If len(articles) != len(article_embeddings).
    """
    if len(articles) != len(article_embeddings):
        raise ValueError(
            f"Article count ({len(articles)}) does not match embedding count "
            f"({len(article_embeddings)})"
        )

    passed = []
    rejected = []

    for article, embedding in zip(articles, article_embeddings, strict=False):
        similarity = cosine_similarity(embedding, centroid)
        if similarity >= threshold:
            passed.append(article)
        else:
            rejected.append(article)

    return passed, rejected


async def generate_embeddings_from_triage(
    triage_feedback: TriageFeedbackData,
    ollama_url: str = "http://localhost:11434",
) -> list[list[float]]:
    """Generate embeddings for accepted articles from triage feedback.

    Args:
        triage_feedback: TriageFeedbackData with accepted articles.
        ollama_url: Ollama API base URL.

    Returns:
        List of embedding vectors for accepted articles only.
    """
    # Build text list from accepted articles (match existing pattern)
    texts = [
        f"{article.title} {article.source_name}" for article in triage_feedback.accepted_articles
    ]

    # Call existing batch embedding function
    embeddings = await generate_embeddings_batch(texts, ollama_url)
    return embeddings
