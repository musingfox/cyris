"""The architecture doc says where the interest page is made and who may read it."""

import re
from pathlib import Path

import pytest

import cyris

pytestmark = [pytest.mark.unit, pytest.mark.guard]

ARCHITECTURE = Path(cyris.__file__).parents[2] / "docs" / "architecture.md"


def _section(text: str, start: str, end: str) -> str:
    return text[text.index(start) : text.index(end)]


def _rows(section: str) -> list[list[str]]:
    return [
        [cell.strip() for cell in line.strip("|").split("|")]
        for line in section.splitlines()
        if line.startswith("|") and not line.startswith("|---")
    ]


def test_section_four_names_the_interest_page_as_a_derived_artifact_in_pages() -> None:
    text = ARCHITECTURE.read_text()
    section = _section(text, "## 4. Data residency", "## 5. Configuration")

    rows = [row for row in _rows(section) if "interest" in row[0].lower()]

    assert len(rows) == 1
    row = " ".join(rows[0])
    assert "Pages" in row
    assert "derived" in row


def test_the_doc_says_the_interest_page_needs_sign_in_on_the_worker_path() -> None:
    sentences = re.split(r"(?<=\.)\s+", ARCHITECTURE.read_text())

    assert any("/interest.html" in s and "sign-in" in s for s in sentences)
