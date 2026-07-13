"""Tests for embedding generation and storage."""

from datetime import UTC, datetime

import pytest
from httpx import ConnectError, Response

from cyris.domain.models import ArticleState, StoredArticle, Tier, TriageFeedbackData
from cyris.learn.embeddings import (
    EmbeddingStore,
    cosine_similarity,
    generate_embedding,
    generate_embeddings_batch,
    generate_embeddings_from_triage,
    load_embedding_store,
    save_embedding_store,
    update_centroid,
)


class TestEmbeddingGeneration:
    @pytest.mark.asyncio
    async def test_generate_single_embedding(self, respx_mock):
        """Generate 768-dim embedding from Ollama."""
        mock_embedding = [0.1] * 768

        respx_mock.post("http://localhost:11434/api/embeddings").mock(
            return_value=Response(
                200,
                json={"embedding": mock_embedding},
            )
        )

        result = await generate_embedding("test text")

        assert len(result) == 768
        assert result == mock_embedding

    @pytest.mark.asyncio
    async def test_generate_embeddings_batch(self, respx_mock):
        """Generate multiple embeddings."""
        mock_emb_1 = [0.1] * 768
        mock_emb_2 = [0.2] * 768

        respx_mock.post("http://localhost:11434/api/embeddings").mock(
            side_effect=[
                Response(200, json={"embedding": mock_emb_1}),
                Response(200, json={"embedding": mock_emb_2}),
            ]
        )

        results = await generate_embeddings_batch(["text1", "text2"])

        assert len(results) == 2
        assert len(results[0]) == 768
        assert len(results[1]) == 768

    @pytest.mark.asyncio
    async def test_ollama_offline_raises_error(self, respx_mock):
        """Raise ConnectError when Ollama is offline."""
        respx_mock.post("http://localhost:11434/api/embeddings").mock(
            side_effect=ConnectError("Connection refused")
        )

        with pytest.raises(ConnectError):
            await generate_embedding("test")


class TestEmbeddingStore:
    def test_save_and_load_roundtrip(self, tmp_path):
        """Save and load embedding store."""
        store = EmbeddingStore(
            centroid=[0.5] * 768,
            sample_count=10,
            last_updated="2026-03-19T10:00:00Z",
        )

        save_embedding_store(store, tmp_path)

        loaded = load_embedding_store(tmp_path)
        assert loaded is not None
        assert len(loaded.centroid) == 768
        assert loaded.sample_count == 10
        assert loaded.last_updated == "2026-03-19T10:00:00Z"

    def test_load_from_empty_dir(self, tmp_path):
        """Return None when no store exists."""
        loaded = load_embedding_store(tmp_path)
        assert loaded is None

    def test_update_centroid_weighted_average(self):
        """Update centroid with weighted average."""
        store = EmbeddingStore(
            centroid=[0.5] * 768,
            sample_count=5,
            last_updated="2026-03-18T00:00:00Z",
        )

        new_embeddings = [
            [0.3] * 768,
            [0.3] * 768,
            [0.3] * 768,
            [0.3] * 768,
            [0.3] * 768,
        ]

        updated = update_centroid(store, new_embeddings)

        # (0.5 * 5 + 0.3 * 5) / 10 = 0.4
        assert updated.sample_count == 10
        assert abs(updated.centroid[0] - 0.4) < 0.001

    def test_dimension_mismatch_raises_error(self):
        """Raise ValueError on dimension mismatch."""
        store = EmbeddingStore(
            centroid=[0.5] * 768,
            sample_count=5,
            last_updated="2026-03-18T00:00:00Z",
        )

        bad_embedding = [0.3] * 512  # Wrong dimension

        with pytest.raises(ValueError, match="Dimension mismatch"):
            update_centroid(store, [bad_embedding])


class TestCosineSimilarity:
    def test_identical_vectors(self):
        """Cosine similarity of identical vectors is 1.0."""
        vec = [1.0, 2.0, 3.0]
        similarity = cosine_similarity(vec, vec)
        assert abs(similarity - 1.0) < 0.001

    def test_orthogonal_vectors(self):
        """Cosine similarity of orthogonal vectors is ~0.0."""
        vec_a = [1.0, 0.0, 0.0]
        vec_b = [0.0, 1.0, 0.0]
        similarity = cosine_similarity(vec_a, vec_b)
        assert abs(similarity) < 0.001

    def test_opposite_vectors(self):
        """Cosine similarity of opposite vectors is -1.0."""
        vec_a = [1.0, 0.0, 0.0]
        vec_b = [-1.0, 0.0, 0.0]
        similarity = cosine_similarity(vec_a, vec_b)
        assert abs(similarity - (-1.0)) < 0.001


class TestGenerateEmbeddingsFromTriage:
    @pytest.mark.asyncio
    async def test_generate_from_triage_feedback(self, respx_mock):
        """Generate embeddings for 3 accepted articles."""
        now = datetime.now(UTC)

        accepted = [
            StoredArticle(
                url=f"https://example.com/accepted/{i}",
                original_id=i,
                title=f"Article Title {i}",
                content="test content",
                published_at=now,
                source_name="TestSource",
                source_tier=Tier.SUMMARIZE,
                first_seen_at=now,
                state=ArticleState.ACCEPTED,
            )
            for i in range(3)
        ]

        feedback = TriageFeedbackData(
            accepted_articles=accepted,
            rejected_articles=[],
            date_range_start=now.isoformat(),
            date_range_end=now.isoformat(),
        )

        # Mock Ollama API responses
        mock_emb_1 = [0.1] * 768
        mock_emb_2 = [0.2] * 768
        mock_emb_3 = [0.3] * 768

        respx_mock.post("http://localhost:11434/api/embeddings").mock(
            side_effect=[
                Response(200, json={"embedding": mock_emb_1}),
                Response(200, json={"embedding": mock_emb_2}),
                Response(200, json={"embedding": mock_emb_3}),
            ]
        )

        embeddings = await generate_embeddings_from_triage(feedback)

        # Should return 3 embeddings (one per accepted article)
        assert len(embeddings) == 3
        assert all(len(emb) == 768 for emb in embeddings)
