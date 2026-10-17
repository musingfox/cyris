"""The docs describe the suppression the code runs, and the grade-A rows match their constants."""

from pathlib import Path

import pytest

from cyris.domain.similarity import MIN_OUTLIER_WINDOW, OUTLIER_MARGIN
from cyris.domain.suppression import SUPPRESSION_CLOCK_DAYS

pytestmark = [pytest.mark.unit, pytest.mark.guard]

ROOT = Path(__file__).parent.parent
ARCHITECTURE = (ROOT / "docs" / "architecture.md").read_text()
CLAUDE = (ROOT / "CLAUDE.md").read_text()
ADR_0006 = (
    ROOT / "docs" / "decisions" / "0006-embedding-thresholds-are-per-model-calibrations.md"
).read_text()
DECISIONS = ROOT / "docs" / "decisions"

TRACKED_TOPIC_SENTENCES = (
    "A tracked topic's threshold keeps ADR-0006's per-model rule: it is stored with the "
    "topic,\nbeside the model it was set for, and a run embedding with any other model skips "
    "the topic."
)


def _grade_row(name: str) -> list[str]:
    """The §5 table row naming `name`, split into cells."""
    section = ARCHITECTURE.split("## 5. Configuration", 1)[1].split("\n## ", 1)[0]
    [row] = [line for line in section.splitlines() if f"`{name}`" in line and line.startswith("|")]
    return [cell.strip() for cell in row.strip("|").split("|")]


def test_the_section_5_rows_are_grade_a_and_name_the_constants() -> None:
    margin = _grade_row("OUTLIER_MARGIN")
    window = _grade_row("MIN_OUTLIER_WINDOW")
    clock = _grade_row("SUPPRESSION_CLOCK_DAYS")

    assert margin[1] == "A" and f"{OUTLIER_MARGIN:g}" in " ".join(margin)
    assert window[1] == "A" and str(MIN_OUTLIER_WINDOW) in window[0]
    assert clock[1] == "A" and str(SUPPRESSION_CLOCK_DAYS) in " ".join(clock)


def test_section_3_and_adr_0017_state_the_minimum_window() -> None:
    section_3 = ARCHITECTURE.split("## 3. How a digest is made", 1)[1].split("\n## ", 1)[0]
    [adr] = list(DECISIONS.glob("0017-*.md"))
    outcome = adr.read_text().split("## Decision Outcome", 1)[1].split("### Consequences", 1)[0]

    assert f"`MIN_OUTLIER_WINDOW` ({MIN_OUTLIER_WINDOW})" in section_3
    assert f"`MIN_OUTLIER_WINDOW` ({MIN_OUTLIER_WINDOW})" in outcome


def test_the_cap_tripwire_counts_what_a_hold_removed() -> None:
    tripwire = "`received + suppressed + suppression_honoured == max_articles_per_digest`"

    assert tripwire in " ".join(ARCHITECTURE.split())


def test_no_document_still_states_the_absolute_cutoffs() -> None:
    body = ARCHITECTURE.split("## 3. How a digest is made", 1)[1].split("## 6", 1)[0]

    assert "0.53" not in body
    assert "0.68" not in body
    assert "Embedding threshold" not in body


def test_an_adr_0017_exists_and_no_neighbour_number_was_taken() -> None:
    [adr] = list(DECISIONS.glob("0017-*.md"))
    front = adr.read_text().split("---", 2)[1]

    assert "status: accepted" in front
    assert list(DECISIONS.glob("0015-*.md")) == []
    assert list(DECISIONS.glob("0016-*.md")) == []
    assert "0017" in ADR_0006
    assert "status: accepted" in ADR_0006.split("---", 2)[1]


@pytest.mark.parametrize(
    "stale",
    ["fails until", "is skipped and reported", "is not part of this decision"],
)
def test_adr_0006_no_longer_states_its_retired_behaviour_as_current(stale: str) -> None:
    assert stale not in ADR_0006


def test_claude_md_describes_the_relative_cutoff_and_keeps_the_tracked_topic_rule() -> None:
    assert "carries its **own** vote-similarity threshold" not in CLAUDE
    assert "ADR-0017" in CLAUDE
    assert "relative" in CLAUDE
    assert TRACKED_TOPIC_SENTENCES in CLAUDE
