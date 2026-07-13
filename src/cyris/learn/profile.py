"""Generate and manage preference profiles from feedback history."""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from cyris.domain.models import PreferenceProfile, TriageFeedbackData
from cyris.service_layer.ports import LLMClient, complete_json

logger = logging.getLogger(__name__)

TRIAGE_PROFILE_PROMPT = """\
You are analyzing user reading preferences from their article triage history.

The user has explicitly accepted some articles and rejected others through a triage process.
Analyze the patterns in BOTH accepted and rejected articles to extract:

1. **Themes**: Common topics/domains the user cares about (3-5 themes) based on ACCEPTED articles
2. **Signals**: Specific attributes that make content interesting (e.g., "M&A deals", \
"open source projects", "enterprise AI adoption") found in ACCEPTED articles
3. **Anti-signals**: Patterns found in REJECTED articles that the user wants to avoid \
(e.g., "overly promotional content", "generic news summaries", "celebrity gossip")

Use the contrast between accepted and rejected articles to identify distinguishing patterns.

Respond in JSON:
{
  "themes": ["theme1", "theme2", ...],
  "signals": ["signal1", "signal2", ...],
  "anti_signals": ["anti1", "anti2", ...],
  "prompt_injection": "<concise text to inject into filter system prompt, \
describing user preferences in 2-3 sentences>"
}

Accepted articles:
"""


def save_profile(profile: PreferenceProfile, path: Path) -> None:
    """Save preference profile to agent vault.

    Args:
        profile: PreferenceProfile to save.
        path: Agent vault root path.
    """
    learning_dir = path / "learning"
    learning_dir.mkdir(parents=True, exist_ok=True)

    profile_path = learning_dir / "profile.json"
    profile_path.write_text(profile.model_dump_json(indent=2))
    logger.info("Saved preference profile to %s", profile_path)


def load_latest_profile(path: Path) -> PreferenceProfile | None:
    """Load the latest preference profile from agent vault.

    Args:
        path: Agent vault root path.

    Returns:
        PreferenceProfile if found, None otherwise.
    """
    profile_path = path / "learning" / "profile.json"
    if not profile_path.exists():
        return None

    data = json.loads(profile_path.read_text())
    return PreferenceProfile(**data)


async def generate_profile_from_triage(
    triage_feedback: TriageFeedbackData,
    llm: LLMClient,
) -> PreferenceProfile:
    """Generate preference profile from triage feedback (accepted vs rejected articles).

    Args:
        triage_feedback: TriageFeedbackData with accepted and rejected articles.
        llm: LLM client.

    Returns:
        PreferenceProfile with themes, signals, anti-signals, and prompt injection.

    Raises:
        ValueError: If accepted_count < 3.
    """
    if triage_feedback.accepted_count < 3:
        raise ValueError(
            f"Insufficient accepted articles: only {triage_feedback.accepted_count} "
            "(minimum 3 required for profile generation)"
        )

    # Build prompt with accepted and rejected article details
    prompt_lines = [TRIAGE_PROFILE_PROMPT]

    for article in triage_feedback.accepted_articles:
        prompt_lines.append(f"- [{article.source_name}] {article.title}")

    prompt_lines.append("\nRejected articles:")
    for article in triage_feedback.rejected_articles:
        prompt_lines.append(f"- [{article.source_name}] {article.title}")

    user_prompt = "\n".join(prompt_lines)

    data = await complete_json(llm, user_prompt, max_tokens=2048)

    return PreferenceProfile(
        generated_at=datetime.now(UTC).isoformat(),
        sample_size=triage_feedback.accepted_count,
        themes=data["themes"],
        signals=data["signals"],
        anti_signals=data["anti_signals"],
        prompt_injection=data["prompt_injection"],
    )
