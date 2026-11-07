"""`TriageServer` refuses a foreign Host and a cross-site write, on every route.

Each request goes through the application's own dispatch with no socket, so routing and
the middleware run as they do on a live server. One test at the end binds a port, to
see the same refusal through aiohttp's real parser.
"""

import json
import logging

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from multidict import CIMultiDict

from cyris.entrypoints.triage_server import TriageServer

pytestmark = pytest.mark.unit

OWN = "127.0.0.1:8766"
FOREIGN = "evil.example"
# A probe that reaches its handler with an empty body answers this; nothing is spent.
PROBE = "/api/diagnostics/llm"
REACHED_THE_PROBE = {"ok": False, "error": "invalid JSON"}


async def _answer(
    method: str, path: str, *, server: TriageServer | None = None, **headers: str
) -> web.StreamResponse:
    """Dispatch one request. A header is named as a keyword: `sec_fetch_site="cross-site"`."""
    app = (server or TriageServer())._app
    app.freeze()
    named = CIMultiDict((name.replace("_", "-"), value) for name, value in headers.items())
    return await app._handle(make_mocked_request(method, path, headers=named, app=app))


def _body(response: web.StreamResponse) -> dict:
    return json.loads(response.text)


def _assert_refused(response: web.StreamResponse, names: str) -> None:
    assert response.status == 403
    assert response.content_type == "application/json"
    body = _body(response)
    assert body["ok"] is False
    assert names in body["error"]


def _assert_reached_the_probe(response: web.StreamResponse) -> None:
    assert response.status == 400
    assert _body(response) == REACHED_THE_PROBE


class TestHost:
    @pytest.mark.parametrize(
        ("method", "path"),
        [("GET", "/api/settings"), ("POST", PROBE), ("HEAD", "/settings"), ("OPTIONS", PROBE)],
    )
    async def test_a_foreign_host_is_refused_on_any_method(self, method: str, path: str) -> None:
        _assert_refused(await _answer(method, path, host=FOREIGN), FOREIGN)

    @pytest.mark.parametrize(
        "host",
        [
            "127.0.0.1",
            "127.0.0.1:8766",
            "127.0.0.1:1",
            "localhost",
            "localhost:8766",
            "LOCALHOST:8766",
            "[::1]",
            "[::1]:8766",
        ],
    )
    async def test_a_loopback_name_passes_with_any_port(self, host: str) -> None:
        assert (await _answer("GET", "/api/settings", host=host)).status == 200

    @pytest.mark.parametrize(
        "host",
        [
            "",
            "localhost.evil.example",
            "127.0.0.1.evil.example",
            "evil.example:8766",
            "127.0.0.1@evil.example",
            "evil.example@127.0.0.1",
            "evil.example:80@127.0.0.1:8766",
            "127.0.0.1/evil.example",
            "[::1",
            "::1",
            "[::2]:8766",
            "127.0.0.2:8766",
            "2130706433",
            "127.0.0.1:port",
        ],
    )
    async def test_a_host_that_only_resembles_loopback_is_refused(self, host: str) -> None:
        assert (await _answer("GET", "/api/settings", host=host)).status == 403

    async def test_a_request_with_no_host_is_refused(self) -> None:
        # `request.host` would fall back to this machine's own name.
        assert (await _answer("GET", "/api/settings")).status == 403

    @pytest.mark.parametrize(
        ("bound", "host"),
        [
            ("192.168.1.5", "192.168.1.5:8766"),
            ("192.168.1.5", "192.168.1.5"),
            ("cyris.lan", "cyris.lan:8766"),
            ("cyris.lan", "CYRIS.lan:8766"),
            ("fd00::1", "[fd00::1]:8766"),
            ("fd00:0::1", "[fd00::1]:8766"),
        ],
    )
    async def test_the_address_given_to_host_passes(self, bound: str, host: str) -> None:
        server = TriageServer(host=bound)
        assert (await _answer("GET", "/api/settings", server=server, host=host)).status == 200

    @pytest.mark.parametrize("host", ["192.168.1.6:8766", "cyris.lan:8766", FOREIGN])
    async def test_a_host_option_widens_to_that_address_alone(self, host: str) -> None:
        server = TriageServer(host="192.168.1.5")
        assert (await _answer("GET", "/api/settings", server=server, host=host)).status == 403

    async def test_loopback_still_passes_beside_a_host_option(self) -> None:
        server = TriageServer(host="192.168.1.5")
        assert (await _answer("GET", "/api/settings", server=server, host=OWN)).status == 200


class TestEveryAddressBound:
    """`--host 0.0.0.0` is what the `ui` role runs with, and names no address to accept."""

    @pytest.mark.parametrize("bound", ["0.0.0.0", "::", "0:0:0:0:0:0:0:0", ""])
    @pytest.mark.parametrize(
        "host", [FOREIGN, "192.168.1.5:8766", "0.0.0.0:8766", "0.0.0.0", "[::]:8766", "[::]"]
    )
    async def test_only_loopback_passes(self, bound: str, host: str) -> None:
        server = TriageServer(host=bound)
        assert (await _answer("GET", "/api/settings", server=server, host=host)).status == 403

    @pytest.mark.parametrize("host", ["127.0.0.1:8766", "localhost:8766", "[::1]:8766"])
    async def test_a_loopback_name_passes(self, host: str) -> None:
        server = TriageServer(host="0.0.0.0")
        assert (await _answer("GET", "/api/settings", server=server, host=host)).status == 200

    async def test_a_cross_site_write_is_refused(self) -> None:
        response = await _answer(
            "POST",
            PROBE,
            server=TriageServer(host="0.0.0.0"),
            host="localhost:8766",
            origin="https://evil.example",
        )
        _assert_refused(response, "https://evil.example")


class TestOrigin:
    @pytest.mark.parametrize(
        "content_type", ["text/plain", "application/json", "application/x-www-form-urlencoded"]
    )
    async def test_a_cross_site_origin_is_refused_whatever_the_content_type(
        self, content_type: str
    ) -> None:
        response = await _answer(
            "POST", PROBE, host=OWN, origin="https://evil.example", content_type=content_type
        )
        _assert_refused(response, "https://evil.example")

    @pytest.mark.parametrize(
        "origin",
        [
            # Another port, and another name for this same machine, are other origins.
            "http://127.0.0.1:9000",
            "http://127.0.0.1",
            "http://localhost:8766",
            "http://[::1]:8766",
            # What a file:// page and a sandboxed frame send.
            "null",
            "",
            "127.0.0.1:8766",
            "http://evil.example@127.0.0.1:8766",
            "chrome-extension://abc",
        ],
    )
    async def test_an_origin_that_differs_from_the_host_is_refused(self, origin: str) -> None:
        assert (await _answer("POST", PROBE, host=OWN, origin=origin)).status == 403

    @pytest.mark.parametrize(
        ("host", "origin"),
        [
            (OWN, f"http://{OWN}"),
            ("localhost:8766", "http://localhost:8766"),
            ("LOCALHOST:8766", "http://localhost:8766"),
            ("[::1]:8766", "http://[::1]:8766"),
            ("localhost", "http://localhost"),
            ("localhost:80", "http://localhost"),
            ("localhost", "http://localhost:80"),
        ],
    )
    async def test_a_same_origin_write_passes(self, host: str, origin: str) -> None:
        _assert_reached_the_probe(await _answer("POST", PROBE, host=host, origin=origin))

    async def test_a_write_with_no_origin_and_no_fetch_metadata_passes(self) -> None:
        _assert_reached_the_probe(await _answer("POST", PROBE, host=OWN))

    @pytest.mark.parametrize("site", ["cross-site", "same-site", "CROSS-SITE"])
    async def test_sec_fetch_site_is_refused_without_an_origin(self, site: str) -> None:
        response = await _answer("POST", PROBE, host=OWN, sec_fetch_site=site)
        _assert_refused(response, site.lower())

    async def test_sec_fetch_site_is_refused_beside_a_same_origin(self) -> None:
        response = await _answer(
            "POST", PROBE, host=OWN, origin=f"http://{OWN}", sec_fetch_site="cross-site"
        )
        assert response.status == 403

    @pytest.mark.parametrize("site", ["same-origin", "none"])
    async def test_a_sec_fetch_site_of_this_origin_passes(self, site: str) -> None:
        _assert_reached_the_probe(await _answer("POST", PROBE, host=OWN, sec_fetch_site=site))

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("PUT", "/api/areas/order"),
            ("DELETE", "/api/sources/one"),
            ("PATCH", "/api/settings"),
            ("post", PROBE),
        ],
    )
    async def test_every_other_method_is_a_write(self, method: str, path: str) -> None:
        response = await _answer(method, path, host=OWN, origin="https://evil.example")
        assert response.status == 403

    @pytest.mark.parametrize(("method", "status"), [("GET", 200), ("HEAD", 200), ("OPTIONS", 405)])
    async def test_a_read_is_not_held_to_the_origin(self, method: str, status: int) -> None:
        try:
            response = await _answer(
                method,
                "/api/settings",
                host=OWN,
                origin="https://evil.example",
                sec_fetch_site="cross-site",
            )
        except web.HTTPException as raised:
            response = raised
        assert response.status == status


class TestEveryRoute:
    @pytest.mark.parametrize(
        "path",
        [
            "/settings",
            "/onboarding",
            "/labels",
            "/interest",
            "/favicon.svg",
            "/api/build",
            "/api/settings",
            "/static/style.css",
            "/static/settings.js",
            # A path no route holds answers the refusal too, not a 404 saying what is served.
            "/no-such-page",
            "/static/no-such-file.css",
        ],
    )
    async def test_a_foreign_host_is_refused(self, path: str) -> None:
        _assert_refused(await _answer("GET", path, host=FOREIGN), FOREIGN)

    @pytest.mark.parametrize("path", ["/settings", "/static/style.css", "/favicon.svg"])
    async def test_a_loopback_host_is_served(self, path: str) -> None:
        assert (await _answer("GET", path, host=OWN)).status == 200

    async def test_a_cross_site_write_to_a_static_path_is_refused(self) -> None:
        response = await _answer(
            "POST", "/static/style.css", host=OWN, origin="https://evil.example"
        )
        assert response.status == 403


class TestTheRefusal:
    async def test_a_refused_host_is_logged_as_a_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="cyris.entrypoints.triage_server"):
            await _answer("GET", "/api/settings", host=FOREIGN)
        [record] = caplog.records
        assert record.levelno == logging.WARNING
        assert FOREIGN in record.getMessage()

    async def test_a_refused_origin_is_logged_as_a_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="cyris.entrypoints.triage_server"):
            await _answer("POST", PROBE, host=OWN, origin="https://evil.example")
        [record] = caplog.records
        assert record.levelno == logging.WARNING
        assert "https://evil.example" in record.getMessage()

    async def test_a_passing_request_logs_nothing(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="cyris.entrypoints.triage_server"):
            await _answer("GET", "/api/settings", host=OWN)
        assert caplog.records == []

    async def test_a_refused_write_runs_no_handler(self, monkeypatch) -> None:
        reached = []

        async def probe(self, request: web.Request) -> web.Response:
            reached.append(request.path)
            return web.json_response({})

        monkeypatch.setattr(TriageServer, "_handle_diagnose_llm", probe)
        await _answer("POST", PROBE, host=OWN, origin="https://evil.example")
        assert reached == []


class TestBehindTheAppWorker:
    """The app Worker forwards with the original URL, so the container is told the public name.

    The Worker has already checked the Access JWT (ADR-0015) and the check stays off
    whatever Host arrives, because what the runtime sends is not something this
    server can pin: see `Lifecycle.forward`.
    """

    @pytest.mark.parametrize("host", ["cyris-app.example.workers.dev", "cyris.example.com", "ping"])
    async def test_the_public_hostname_passes(self, host: str) -> None:
        server = TriageServer(host="0.0.0.0", behind_app_worker=True)
        assert (await _answer("GET", "/api/settings", server=server, host=host)).status == 200

    async def test_a_write_from_the_public_hostname_passes(self) -> None:
        server = TriageServer(host="0.0.0.0", behind_app_worker=True)
        response = await _answer(
            "POST",
            PROBE,
            server=server,
            host="cyris.example.com",
            origin="https://cyris.example.com",
            sec_fetch_site="same-origin",
        )
        _assert_reached_the_probe(response)

    async def test_it_answers_as_it_always_did_whatever_the_origin(self) -> None:
        server = TriageServer(host="0.0.0.0", behind_app_worker=True)
        response = await _answer(
            "POST",
            PROBE,
            server=server,
            host="cyris.example.com",
            origin="https://evil.example",
            sec_fetch_site="cross-site",
        )
        _assert_reached_the_probe(response)

    async def test_a_server_not_told_so_keeps_the_check(self) -> None:
        # The default is what a deployment that forgot everything gets.
        server = TriageServer(host="0.0.0.0")
        response = await _answer("GET", "/api/settings", server=server, host="cyris.example.com")
        assert response.status == 403


async def test_a_live_socket_refuses_what_the_dispatch_refuses() -> None:
    """Binds a port, so it cannot run where the sandbox forbids one."""
    client = TestClient(TestServer(TriageServer()._app))
    await client.start_server()
    try:
        foreign = await client.get("/api/settings", headers={"Host": FOREIGN})
        assert foreign.status == 403
        assert FOREIGN in (await foreign.json())["error"]
        static = await client.get("/static/style.css", headers={"Host": FOREIGN})
        assert static.status == 403
        assert (await client.get("/api/settings")).status == 200
        cross = await client.post(
            PROBE,
            data="{}",
            headers={"Origin": "https://evil.example", "Content-Type": "text/plain"},
        )
        assert cross.status == 403
        own = await client.post(
            PROBE,
            data="not json",
            headers={"Origin": f"http://{client.host}:{client.port}", "Content-Type": "text/plain"},
        )
        assert await own.json() == REACHED_THE_PROBE
        assert (await client.post(PROBE, data="not json")).status == 400
    finally:
        await client.close()
