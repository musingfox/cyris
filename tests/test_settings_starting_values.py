"""The starting values the wizard shows a new reader agree with `cyris.toml.example`.

`src/cyris/settings_starting_values.json` is what the onboarding wizard's Model step
pre-fills for a required key the store lacks (ADR-0025), and `cyris.toml.example` is
what a `json` deployer copies. Two lists of the same values drift unless something
holds them together; this file does, for every key both hold.
"""

import tomllib
from pathlib import Path

import pytest

from cyris.config import REQUIRED_KEYS, STARTING_VALUES, validate_setting

pytestmark = [pytest.mark.unit, pytest.mark.guard]

EXAMPLE = Path(__file__).parent.parent / "cyris.toml.example"

# The required keys a wizard step asks the reader for by name, so none starts at a
# value the reader did not pick: the language and the clock are theirs, the hours
# are typed, the model is one this machine has a key for, and a notification
# address only the reader knows.
ASKED = {
    "digest.output_language",
    "general.timezone",
    "general.digest_schedule",
    "llm_provider.provider",
    "llm_provider.model",
    "notify.discord_webhook_url",
    "notify.email_to",
    "notify.email_from",
}


def _example_values() -> dict:
    """Every `table.field` in the example's grade-D tables, with its value."""
    tables = tomllib.loads(EXAMPLE.read_text(encoding="utf-8"))
    return {
        f"{table}.{field}": value
        for table, fields in tables.items()
        if isinstance(fields, dict)
        for field, value in fields.items()
    }


def test_every_required_key_is_asked_for_or_starts_at_a_value() -> None:
    """A key in neither would stay missing after the wizard, and the run would stop."""
    assert set(STARTING_VALUES) == set(REQUIRED_KEYS) - ASKED


def test_an_asked_key_never_starts_at_a_value() -> None:
    assert set(STARTING_VALUES) & ASKED == set()


@pytest.mark.parametrize("key", sorted(STARTING_VALUES))
def test_each_starting_value_is_one_the_store_takes(key: str) -> None:
    """The route that writes it validates it the same way, so a bad entry fails the save."""
    assert validate_setting(key, STARTING_VALUES[key]) == STARTING_VALUES[key]


@pytest.mark.parametrize("key", sorted(STARTING_VALUES))
def test_each_starting_value_is_the_examples(key: str) -> None:
    example = _example_values()

    assert key in example, f"{key} is not in cyris.toml.example"
    assert STARTING_VALUES[key] == example[key]
    assert type(STARTING_VALUES[key]) is type(example[key])
