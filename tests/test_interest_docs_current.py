"""The architecture doc says where the interest page is made, who may read it, and its limits."""

import re
from pathlib import Path

import pytest

import cyris
from cyris.domain.interest_graph import MAX_INTEREST_NODES

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


def test_section_four_names_the_interest_page_as_derived_with_no_pages_copy() -> None:
    text = ARCHITECTURE.read_text()
    section = _section(text, "## 4. Data residency", "## 5. Configuration")

    rows = [row for row in _rows(section) if "interest" in row[0].lower()]

    assert len(rows) == 1
    row = " ".join(rows[0])
    assert "derived" in row
    assert "container" in row
    assert "no copy in the Pages project" in row


def test_the_doc_says_the_interest_page_needs_sign_in_on_the_worker_path() -> None:
    sentences = re.split(r"(?<=\.)\s+", ARCHITECTURE.read_text())

    assert any("`/interest`" in s and "sign-in" in s for s in sentences)


def test_section_five_states_the_interest_maps_node_limit_as_grade_a() -> None:
    text = ARCHITECTURE.read_text()
    section = _section(text, "## 5. Configuration", "## 6.")

    rows = [row for row in _rows(section) if "`MAX_INTEREST_NODES`" in " ".join(row[2:3])]

    assert len(rows) == 1
    assert rows[0][1] == "A"
    (stated,) = re.findall(r"Interest map limits: (\d+) nodes", rows[0][0])
    assert int(stated) == MAX_INTEREST_NODES
