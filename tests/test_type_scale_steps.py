"""The type size's steps live in three places, and they must be the same three."""

import re
import sys
from pathlib import Path
from typing import get_args

import pytest

from cyris.config import DigestConfig
from cyris.entrypoints.triage_server import render_settings_page

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from cdp_probe import LARGEST_TYPE_SCALE, LARGEST_TYPE_SCALE_STYLE  # noqa: E402

pytestmark = [pytest.mark.unit, pytest.mark.guard]

WORKER_SOURCE = Path(__file__).parents[1] / "workers" / "app" / "src" / "type_scale.js"


def _setting_steps() -> list[float]:
    return [float(step) for step in get_args(DigestConfig.model_fields["type_scale"].annotation)]


def _worker_steps(source: str) -> list[float]:
    match = re.search(r"^export const TYPE_SCALES = \[([^\]]*)\];$", source, re.M)
    assert match, "type_scale.js declares no TYPE_SCALES"
    return [float(step) for step in match.group(1).split(",")]


def _page_option_values(page: str) -> list[str]:
    select = re.search(r'<select class="select" id="type-scale">(.*?)</select>', page)
    assert select, "the settings page has no type size select"
    return re.findall(r'<option value="([^"]*)"', select.group(1))


def _page_steps(page: str) -> list[float]:
    return [float(value) for value in _page_option_values(page)]


def test_the_page_spells_each_option_as_the_stored_value() -> None:
    # A select stores text. "1.0" is the same float as the stored 1, so the
    # saved value would no longer preselect.
    assert _page_option_values(render_settings_page()) == ["0.875", "1", "1.125"]


def _assert_steps_agree(worker_source: str, page: str) -> None:
    setting = _setting_steps()
    for where, steps in (
        ("the Worker", _worker_steps(worker_source)),
        ("the page", _page_steps(page)),
    ):
        assert steps == setting, f"{where} offers {steps}, the setting takes {setting}"


def test_the_setting_the_worker_and_the_page_have_the_same_steps() -> None:
    assert _setting_steps() == [0.875, 1, 1.125]
    _assert_steps_agree(WORKER_SOURCE.read_text(), render_settings_page())


def test_a_worker_with_a_step_of_its_own_is_caught() -> None:
    diverged = WORKER_SOURCE.read_text().replace(
        "TYPE_SCALES = [0.875, 1, 1.125];", "TYPE_SCALES = [0.875, 1, 1.25];"
    )

    with pytest.raises(AssertionError, match=r"1\.25"):
        _assert_steps_agree(diverged, render_settings_page())


def _worker_style(source: str, scale: float) -> str:
    match = re.search(r"^export const typeScaleStyle = \(scale\) => `([^`]*)`;$", source, re.M)
    assert match, "type_scale.js declares no typeScaleStyle"
    return match.group(1).replace("${scale}", str(scale))


def test_the_probes_inject_what_the_worker_serves() -> None:
    worker = _worker_style(WORKER_SOURCE.read_text(), LARGEST_TYPE_SCALE)
    assert worker == LARGEST_TYPE_SCALE_STYLE


def test_a_worker_with_a_style_of_its_own_is_caught() -> None:
    diverged = WORKER_SOURCE.read_text().replace("html:root{", ":root{")
    assert _worker_style(diverged, LARGEST_TYPE_SCALE) != LARGEST_TYPE_SCALE_STYLE
