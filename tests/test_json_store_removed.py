"""The json store and every way out of it are gone: what is left of them is a refusal."""

import pytest
from typer.testing import CliRunner

from cyris.entrypoints.cli import app

pytestmark = pytest.mark.integration


def test_the_store_command_group_is_an_unknown_command() -> None:
    result = CliRunner().invoke(app, ["store", "--help"])

    assert result.exit_code != 0
    assert "No such command" in result.output
