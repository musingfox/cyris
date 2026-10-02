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


# ---- each fake answers as its service --------------------------------------------

CF = "https://api.cloudflare.com/client/v4"
D1_URL = f"{CF}/accounts/{harness.ACCOUNT_ID}/d1/database/{harness.DATABASE_ID}/query"
CF_AUTH = {"Authorization": f"Bearer {harness.CF_TOKEN}"}
WORKER_AUTH = {"Authorization": f"Bearer {harness.WORKER_TOKEN}"}
PROMOTE_AUTH = {"Authorization": f"Bearer {harness.PROMOTE_TOKEN}"}
RSS_URL = f"https://{harness.HOSTS['rss_host']}/articles"
RSS_ROWS = [
    {"url": f"https://e2e.test/{day}", "published_at": f"2026-10-0{day}T08:00:00.000Z"}
    for day in (1, 2, 3)
]
PAGES = f"{CF}/accounts/{harness.ACCOUNT_ID}/pages/projects/{harness.PAGES_PROJECT}"
JWT_AUTH = {"Authorization": f"Bearer {harness.PAGES_UPLOAD_JWT}"}
SITE = f"https://{harness.HOSTS['pages_host']}"


@pytest.fixture(scope="module")
def service(tmp_path_factory):
    """One running proxy with a client that goes through it."""
    run_dir = tmp_path_factory.mktemp("e2e-fakes")
    scenario = harness.Scenario(
        settings={}, sources=[], rss_rows=RSS_ROWS, newsletters=[], llm={}, pages_pending_reads=1
    )
    with harness.fakes(run_dir, scenario) as (proxy_url, ca_file), _client(proxy_url, ca_file) as c:
        yield c


def _d1(client, sql, params=None):
    return client.post(D1_URL, json={"sql": sql, "params": params or []}, headers=CF_AUTH)


def test_d1_reports_the_rows_a_cte_update_changed(service) -> None:
    _d1(service, "CREATE TABLE cte_probe (url TEXT PRIMARY KEY, score REAL)")
    _d1(service, "INSERT INTO cte_probe VALUES ('a', 0), ('b', 0), ('c', 0)")
    answer = _d1(
        service,
        "WITH v(url, score) AS (VALUES (?, ?), (?, ?)) "
        "UPDATE cte_probe SET score = v.score FROM v WHERE cte_probe.url = v.url",
        ["a", 1, "b", 2],
    ).json()
    assert answer["result"][0]["meta"]["changes"] == 2


def test_d1_refuses_a_sixth_compound_select_term_however_it_is_written(service) -> None:
    five = "\nUNION ALL\n".join(["SELECT 1"] * 5)
    six = "\nunion  all\n".join(["SELECT 1"] * 6)
    assert _d1(service, five).status_code == 200
    assert _d1(service, six).status_code == 400


def test_d1_refuses_a_wrong_token(service) -> None:
    answer = service.post(D1_URL, json={"sql": "SELECT 1"}, headers={"Authorization": "Bearer x"})
    assert answer.status_code == 401


def test_the_rss_buffer_needs_both_bounds(service) -> None:
    answer = service.get(RSS_URL, params={"after": "2026-10-01"}, headers=WORKER_AUTH)
    assert answer.status_code == 400


def test_the_rss_buffer_reads_its_window_as_the_worker_does(service) -> None:
    """String bounds against the stored ISO strings, newest first, capped at the limit."""
    capped = service.get(
        RSS_URL,
        params={"after": "2026-10-01T09:00:00", "before": "2026-10-04", "limit": "1"},
        headers=WORKER_AUTH,
    )
    assert [row["url"] for row in capped.json()] == ["https://e2e.test/3"]
    whole = service.get(
        RSS_URL, params={"after": "2026-10-01", "before": "2026-10-03"}, headers=WORKER_AUTH
    )
    assert [row["url"] for row in whole.json()] == ["https://e2e.test/2", "https://e2e.test/1"]


def test_the_workers_refuse_a_wrong_token(service) -> None:
    answer = service.get(
        RSS_URL,
        params={"after": "2026-10-01", "before": "2026-10-04"},
        headers={"Authorization": "Bearer wrong"},
    )
    assert answer.status_code == 401


@pytest.mark.parametrize(
    ("host", "auth", "body"),
    [
        ("newsletter_host", WORKER_AUTH, {"urls": ["x"]}),
        ("promote_host", PROMOTE_AUTH, {"ids": ["x"]}),
    ],
    ids=["newsletter", "promote"],
)
def test_an_ack_without_its_list_is_refused(service, host, auth, body) -> None:
    answer = service.post(f"https://{harness.HOSTS[host]}/ack", json=body, headers=auth)
    assert answer.status_code == 400


def _embeds(total: int) -> list[dict]:
    """Embeds whose titles and descriptions add up to `total` characters."""
    embeds = []
    while total > 0:
        description = min(total - 10, 4096)
        embeds.append({"title": "t" * 10, "description": "d" * description})
        total -= 10 + description
    return embeds


@pytest.mark.parametrize(
    ("embeds", "status"),
    [
        (_embeds(6000), 204),
        (_embeds(6001), 400),
        ([{"title": "t"}] * 11, 400),
        ([{"title": "t" * 257}], 400),
    ],
    ids=["6000-chars", "6001-chars", "11-embeds", "long-title"],
)
def test_discord_enforces_its_embed_limits(service, embeds, status) -> None:
    answer = service.post(harness.DISCORD_WEBHOOK, json={"embeds": embeds})
    assert answer.status_code == status


def test_discord_refuses_a_wrong_webhook_token(service) -> None:
    answer = service.post(harness.DISCORD_WEBHOOK + "-wrong", json={"content": "x"})
    assert answer.status_code == 401


def test_pages_serves_what_was_uploaded_and_the_front_page_for_a_missing_path(service) -> None:
    """The declared type and encoding; a deployment in progress until it is re-read once."""
    service.post(
        f"{CF}/pages/assets/upload",
        headers=JWT_AUTH,
        json=[
            {"key": "h-index", "value": "PHRpdGxlPmZyb250PC90aXRsZT4=", "base64": True,
             "metadata": {"contentType": "text/html"}},
            {"key": "h-icon", "value": "<svg/>", "base64": False,
             "metadata": {"contentType": "image/svg+xml"}},
        ],
    )  # fmt: skip
    manifest = '{"/index.html": "h-index", "/favicon.svg": "h-icon"}'
    created = service.post(
        f"{PAGES}/deployments", headers=CF_AUTH, files={"manifest": (None, manifest)}
    ).json()["result"]
    assert created["latest_stage"] == {"name": "deploy", "status": "active"}
    reread = service.get(f"{PAGES}/deployments/{created['id']}", headers=CF_AUTH).json()["result"]
    assert reread["latest_stage"] == {"name": "deploy", "status": "success"}

    icon = service.get(f"{SITE}/favicon.svg")
    assert (icon.text, icon.headers["content-type"]) == ("<svg/>", "image/svg+xml")
    missing = service.get(f"{SITE}/2026-10-02-morning")
    assert (missing.status_code, missing.text) == (200, "<title>front</title>")
