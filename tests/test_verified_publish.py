"""A publish counts as done only when the live page serves the bytes just rendered."""

from pathlib import Path

import httpx
import pytest
import respx
from fakes import SqliteD1, make_config

from cyris import bootstrap
from cyris.adapters.output import publish
from cyris.adapters.output.publish import VERIFY_POLLS
from cyris.bootstrap import Deps
from cyris.config import AgentVaultConfig
from cyris.service_layer.run_digest import _publish_verified

pytestmark = pytest.mark.integration

PROJECT = "proj"
SLUG = "2026-10-14-evening"
PATH = f"/{SLUG}.html"
URL = f"https://{PROJECT}.pages.dev/{SLUG}"
EXPECTED = b"<html><title>2026-10-14 evening</title>rendered</html>"


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(publish.time, "sleep", lambda _seconds: None)


@respx.mock
def test_the_live_page_serving_the_rendered_bytes_is_verified(no_sleep) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(200, content=EXPECTED))

    assert publish.page_serves(PROJECT, PATH, EXPECTED) is True
    assert route.call_count == 1


@respx.mock
def test_an_older_render_of_the_same_issue_is_not_verified(no_sleep) -> None:
    older = b"<html><title>2026-10-14 evening</title>an older render</html>"
    route = respx.get(URL).mock(return_value=httpx.Response(200, content=older))

    assert publish.page_serves(PROJECT, PATH, EXPECTED) is False
    assert route.call_count == VERIFY_POLLS


@respx.mock
def test_a_404_then_a_connect_error_is_false_and_raises_nothing(no_sleep) -> None:
    route = respx.get(URL).mock(
        side_effect=[
            httpx.Response(404),
            httpx.ConnectError("connection refused"),
            *([httpx.Response(404)] * (VERIFY_POLLS - 2)),
        ]
    )

    assert publish.page_serves(PROJECT, PATH, EXPECTED) is False
    assert route.call_count == VERIFY_POLLS


@respx.mock
def test_the_poll_loop_stops_at_the_verify_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [0.0]
    monkeypatch.setattr(publish, "_clock", lambda: now[0])
    monkeypatch.setattr(publish.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    older = b"<html><title>2026-10-14 evening</title>an older render</html>"
    route = respx.get(URL).mock(return_value=httpx.Response(200, content=older))

    assert publish.page_serves(PROJECT, PATH, EXPECTED) is False
    # A poll starts only if its request, 5s of sleep plus a 15s timeout, ends by the 120s
    # budget: the last poll starts at 105s and ends at 120s. That is 22 requests, not VERIFY_POLLS.
    assert route.call_count == 22


def _deps(tmp_path: Path, publish_site, page_serves) -> Deps:
    return Deps(
        cfg=make_config(agent_vault=AgentVaultConfig(path=tmp_path / "vault")),
        store=None,
        llm=None,
        fetch_sources=[],
        html_writer=None,
        publish=None,
        sync_promotions=None,
        log_usage=lambda content: None,
        publish_site=publish_site,
        page_serves=page_serves,
    )


def test_a_publish_is_verified_only_when_the_site_and_the_page_both_pass(tmp_path: Path) -> None:
    calls: list[str] = []

    def site(pages, slug):
        calls.append("publish")
        return True

    def serves(path, expected):
        calls.append("serves")
        return True

    assert _publish_verified(_deps(tmp_path, site, serves), {PATH: EXPECTED}, SLUG) is True
    assert calls == ["publish", "serves"]


def test_a_failed_page_check_is_false(tmp_path: Path) -> None:
    deps = _deps(tmp_path, lambda pages, slug: True, lambda path, expected: False)

    assert _publish_verified(deps, {PATH: EXPECTED}, SLUG) is False


def test_a_failed_site_publish_is_false_and_skips_the_page_check(tmp_path: Path) -> None:
    checked: list = []
    deps = _deps(
        tmp_path,
        lambda pages, slug: False,
        lambda path, expected: checked.append(path) or True,
    )

    assert _publish_verified(deps, {PATH: EXPECTED}, SLUG) is False
    assert checked == []


def test_a_missing_page_check_is_false(tmp_path: Path) -> None:
    deps = _deps(tmp_path, lambda pages, slug: True, None)

    assert _publish_verified(deps, {PATH: EXPECTED}, SLUG) is False


def test_a_raising_site_publish_is_false_and_raises_nothing(tmp_path: Path) -> None:
    def broken(pages, slug):
        raise RuntimeError("Pages unreachable")

    deps = _deps(tmp_path, broken, lambda path, expected: True)

    assert _publish_verified(deps, {PATH: EXPECTED}, SLUG) is False


def test_the_page_check_is_bound_only_where_publishing_is_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = SqliteD1()
    monkeypatch.setattr(bootstrap, "build_d1_client", lambda _cfg: db)
    cfg = make_config(agent_vault=AgentVaultConfig(path=tmp_path / "vault"))
    cfg.app.store.backend = "d1"
    cfg.app.store.database_id = "db"
    cfg.app.store.account_id = "acct"
    cfg.app.store.api_token = "tok"
    cfg.app.html_output.enabled = True
    cfg.app.promote.publish_enabled = True
    cfg.app.promote.pages_project = PROJECT

    assert bootstrap.build_deps(cfg).page_serves is not None

    cfg.app.promote.publish_enabled = False

    assert bootstrap.build_deps(cfg).page_serves is None


@respx.mock
def test_the_bound_page_check_takes_the_path_and_bytes_the_run_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_sleep
) -> None:
    monkeypatch.setattr(bootstrap, "build_d1_client", lambda _cfg: SqliteD1())
    cfg = make_config(agent_vault=AgentVaultConfig(path=tmp_path / "vault"))
    cfg.app.store.backend = "d1"
    cfg.app.store.database_id = "db"
    cfg.app.store.account_id = "acct"
    cfg.app.store.api_token = "tok"
    cfg.app.html_output.enabled = True
    cfg.app.promote.publish_enabled = True
    cfg.app.promote.pages_project = PROJECT
    route = respx.get(URL).mock(return_value=httpx.Response(200, content=EXPECTED))

    assert bootstrap.build_deps(cfg).page_serves(PATH, EXPECTED) is True
    assert route.call_count == 1
