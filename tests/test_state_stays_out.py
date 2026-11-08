"""A SQLite install's file and a settings push's source stay out of git and out of the image.

`state/cyris.sqlite3` holds the Discord webhook URL and the mail addresses, and
`settings.toml` (`docs/install-cloudflare.md`) holds the same values on their way to D1.
"""

from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.guard]

ROOT = Path(__file__).resolve().parent.parent


def _patterns(name: str) -> set[str]:
    lines = (ROOT / name).read_text(encoding="utf-8").splitlines()
    return {line.strip() for line in lines if line.strip() and not line.startswith("#")}


@pytest.mark.parametrize(
    ("ignore_file", "pattern"),
    [
        (".gitignore", "state/"),
        (".gitignore", "settings.toml"),
        (".dockerignore", "state"),
        (".dockerignore", "settings.toml"),
    ],
)
def test_the_ignore_file_keeps_it_out(ignore_file: str, pattern: str) -> None:
    assert pattern in _patterns(ignore_file)
