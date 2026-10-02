"""The proxy the end-to-end suite stands on: it fails closed and its fakes answer as their services.

Each test starts the fakes behind mitmdump with no cyris run, and talks to them with
an httpx client of its own.
"""

import socket
import ssl
import threading
from contextlib import suppress

import httpx
import pytest
from e2e import harness
from e2e.receipts import assert_no_unclaimed

pytestmark = pytest.mark.e2e

EMPTY = harness.Scenario(settings={}, sources=[], rss_rows=[], newsletters=[], llm={})
# A multipart part that is not UTF-8: decoding it raises inside the addon.
BAD_MULTIPART = b'--b\r\nContent-Disposition: form-data; name="f"\r\n\r\n\xff\xfe\r\n--b--\r\n'


class _Listener:
    """A local TCP server standing in for a real host; it records every connection."""

    def __init__(self) -> None:
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen()
        self.port = self._sock.getsockname()[1]
        self.connections = 0
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            self.connections += 1
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 13\r\n\r\nREAL UPSTREAM")
            conn.close()

    def close(self) -> None:
        self._sock.close()


@pytest.fixture
def listener():
    server = _Listener()
    yield server
    server.close()


def _client(proxy_url: str, ca_file) -> httpx.Client:
    return httpx.Client(
        proxy=proxy_url, verify=ssl.create_default_context(cafile=str(ca_file)), timeout=10
    )


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_a_request_the_addon_cannot_parse_never_leaves_the_proxy(tmp_path, listener, scheme):
    url = f"{scheme}://127.0.0.1:{listener.port}/upload"
    with harness.fakes(tmp_path, EMPTY) as (proxy_url, ca_file), _client(proxy_url, ca_file) as c:
        response = c.post(
            url,
            content=BAD_MULTIPART,
            headers={"Content-Type": "multipart/form-data; boundary=b"},
        )
    assert listener.connections == 0
    assert response.text != "REAL UPSTREAM"
    records = harness.read_records(tmp_path)
    assert [(r["method"], r["host"], r["path"]) for r in records] == [
        ("POST", "127.0.0.1", "/upload")
    ]
    with pytest.raises(AssertionError):
        assert_no_unclaimed(records)


def test_no_upstream_connection_opens_even_when_the_strategy_would_open_one(tmp_path, listener):
    """`server_connect` refuses every connection, whatever the request hook did."""
    url = f"https://127.0.0.1:{listener.port}/"
    # Refusing the upstream connection may fail the client's TLS handshake.
    with (
        harness.fakes(tmp_path, EMPTY, connection_strategy="eager") as (proxy_url, ca_file),
        _client(proxy_url, ca_file) as c,
        suppress(httpx.HTTPError),
    ):
        c.get(url)
    assert listener.connections == 0
