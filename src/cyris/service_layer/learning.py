"""Use case: learn user preferences from triage or digest feedback."""

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from cyris.domain.models import PreferenceProfile
from cyris.learn.embeddings import (
    build_or_update_centroid,
    generate_embeddings_from_triage,
    load_embedding_store,
    save_embedding_store,
)
from cyris.learn.profile import (
    generate_profile_from_triage,
    save_profile,
)
from cyris.learn.triage_feedback import collect_triage_feedback

if TYPE_CHECKING:
    from cyris.bootstrap import Deps

logger = logging.getLogger(__name__)


@dataclass
class LearnReport:
    profile: PreferenceProfile
    embeddings_updated: bool
    embedding_count: int = 0


async def _save_embeddings(deps: "Deps", embeddings: list[list[float]]) -> None:
    existing = load_embedding_store(deps.cfg.app.agent_vault.path)
    deps.on_progress(
        "Updating existing embedding centroid..."
        if existing
        else "Creating new embedding centroid..."
    )
    save_embedding_store(
        build_or_update_centroid(existing, embeddings), deps.cfg.app.agent_vault.path
    )


async def learn_from_triage(deps: "Deps", days: int = 14) -> LearnReport:
    """Learn preferences from triage decisions (accepted vs rejected articles).

    Raises:
        ValueError: If there is insufficient triage feedback.
    """
    progress = deps.on_progress
    vault_path = deps.cfg.app.agent_vault.path

    progress(f"Collecting triage feedback from past {days} days...")
    feedback = collect_triage_feedback(deps.store, days=days)

    progress(
        f"Found {feedback.accepted_count} accepted and {feedback.rejected_count} rejected articles"
    )

    progress("Generating preference profile via Claude API...")
    profile = await generate_profile_from_triage(feedback, deps.llm)
    save_profile(profile, vault_path)

    progress("\nGenerating embeddings via Ollama...")
    try:
        embeddings = await generate_embeddings_from_triage(feedback)
        progress(f"Generated {len(embeddings)} embeddings")
        await _save_embeddings(deps, embeddings)
        progress(f"\nLearning complete. Saved to {vault_path / 'learning'}")
        return LearnReport(
            profile=profile, embeddings_updated=True, embedding_count=len(embeddings)
        )
    except Exception as e:
        logger.error("Failed to generate embeddings: %s", e)
        progress(
            "\nWarning: Embedding generation failed. Saved profile only. "
            "Ensure Ollama is running with nomic-embed-text model."
        )
        return LearnReport(profile=profile, embeddings_updated=False)
