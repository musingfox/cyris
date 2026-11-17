"""The onboarding wizard's "Add your own feed": `POST /api/onboarding/feed-check`.

The handler is called directly with the body the page sends, over the real
`FeedChecker` and its `HttpClient`; respx answers every address, so no test
reaches the network. What a browser does with the answer is
`scripts/onboarding_probe.py`'s.
"""

import asyncio
import gzip
import json
import threading
from pathlib import Path

import httpcore
import httpx
import pytest
import respx
from fakes import SqliteD1

import cyris
from cyris.adapters.fetch import feed_check
from cyris.adapters.fetch.feed_check import MAX_BYTES, MAX_REDIRECTS, FeedChecker, GuardedHttp
from cyris.adapters.store.source_store import D1SourceStore
from cyris.domain.models import SourceConfig
from cyris.entrypoints.triage_server import TriageServer

pytestmark = pytest.mark.integration

RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Slow Reads</title><link>https://slow.test/</link>
<item><title>One</title><link>https://slow.test/1</link>
<pubDate>Mon, 19 Oct 2026 08:00:00 GMT</pubDate></item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Atom Notes</title>
<id>urn:atom-notes</id><updated>2026-10-19T08:00:00Z</updated>
<entry><title>First</title><id>urn:atom-notes:1</id><link href="https://atom.test/1"/>
<updated>2026-10-19T08:00:00Z</updated></entry>
</feed>"""

EMPTY_RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Nothing Yet</title><link>https://empty.test/</link>
</channel></rss>"""

HOME = """<!doctype html><html><head><title>Slow Reads</title>
<link rel="stylesheet" href="/style.css">
<link rel="alternate" type="application/json" href="/feed.json">
<link rel="alternate" type="application/rss+xml" title="RSS" href="/feed.xml">
<link rel="alternate" type="application/atom+xml" href="/atom.xml">
</head><body>Home</body></html>"""

BARE_HOME = "<!doctype html><html><head><title>No feed</title></head><body>Hi</body></html>"

XML = {"Content-Type": "application/rss+xml; charset=utf-8"}
HTML = {"Content-Type": "text/html; charset=utf-8"}


class Request:
    """What the handler reads of a request: its JSON body, and the languages it asks for."""

    def __init__(self, body, headers: dict[str, str] | None = None) -> None:
        self._body = body
        self.headers = headers or {}

    async def json(self):
        return self._body


# Every test host resolves here, as a public address would; no test asks real DNS.
PUBLIC = "93.184.216.34"


async def _public(host: str) -> list[str]:
    return [PUBLIC]


def _server(
    sources: list[SourceConfig] = (), resolve=_public, values: dict | None = None
) -> TriageServer:
    store = D1SourceStore(SqliteD1())
    for source in sources:
        store.upsert(source)
    return TriageServer(
        source_store=store,
        feed_checker=FeedChecker(GuardedHttp(resolve=resolve)),
        values=values,
    )


async def _check(server: TriageServer, url: str, taken=(), headers=None) -> tuple[int, dict]:
    request = Request({"url": url, "taken": list(taken)}, headers)
    response = await server._handle_post_feed_check(request)
    return response.status, json.loads(response.text)


class TestAFeedThatPasses:
    async def test_an_rss_feed_is_named_by_its_own_title(self) -> None:
        async with respx.mock:
            feed = respx.get("https://slow.test/feed.xml").mock(
                return_value=httpx.Response(200, content=RSS, headers=XML)
            )

            status, body = await _check(_server(), "https://slow.test/feed.xml")

        assert (status, body) == (
            200,
            {"ok": True, "name": "Slow Reads", "url": "https://slow.test/feed.xml"},
        )
        assert feed.call_count == 1
        assert feed.calls.last.request.headers["Accept-Encoding"] == "identity"

    async def test_an_atom_feed_is_named_by_its_own_title(self) -> None:
        async with respx.mock:
            feed = respx.get("https://atom.test/atom").mock(
                return_value=httpx.Response(
                    200, content=ATOM, headers={"Content-Type": "application/atom+xml"}
                )
            )

            status, body = await _check(_server(), "  https://atom.test/atom  ")

        assert (status, body["name"], body["url"]) == (200, "Atom Notes", "https://atom.test/atom")
        assert feed.call_count == 1

    async def test_a_home_page_is_followed_to_the_first_rss_or_atom_feed_it_links(self) -> None:
        async with respx.mock:
            home = respx.get("https://slow.test/").mock(
                return_value=httpx.Response(200, text=HOME, headers=HTML)
            )
            feed = respx.get("https://slow.test/feed.xml").mock(
                return_value=httpx.Response(200, content=RSS, headers=XML)
            )

            status, body = await _check(_server(), "https://slow.test/")

        # The JSON feed comes first on the page, and is not a type either parser reads.
        assert (status, body["name"], body["url"]) == (
            200,
            "Slow Reads",
            "https://slow.test/feed.xml",
        )
        assert home.call_count == 1
        assert feed.call_count == 1


class TestARefusal:
    async def test_a_home_page_without_a_feed_link_is_refused_by_name(self) -> None:
        async with respx.mock:
            home = respx.get("https://bare.test/").mock(
                return_value=httpx.Response(200, text=BARE_HOME, headers=HTML)
            )

            status, body = await _check(_server(), "https://bare.test/")

        assert status == 422 and not body["ok"]
        assert body["error"] == (
            "The page at https://bare.test/ links no RSS or Atom feed. "
            "Find the feed's own address on the site and paste that."
        )
        assert home.call_count == 1

    async def test_an_http_error_names_the_status_quotes_nothing_and_is_asked_once(self) -> None:
        async with respx.mock:
            gone = respx.get("https://slow.test/feed.xml").mock(
                return_value=httpx.Response(
                    404, text="no such feed", headers={"Content-Type": "text/plain"}
                )
            )

            status, body = await _check(_server(), "https://slow.test/feed.xml")

        assert status == 422
        assert body["error"] == (
            # The body is never read back: the server's answer is not the reader's to see.
            "https://slow.test/feed.xml answered HTTP 404. "
            "Check the address, or paste the feed's own address."
        )
        # A 4xx is not tried again.
        assert gone.call_count == 1

    async def test_an_html_error_page_is_not_quoted(self) -> None:
        async with respx.mock:
            down = respx.get("https://slow.test/feed.xml").mock(
                return_value=httpx.Response(503, text="<html>busy</html>", headers=HTML)
            )

            status, body = await _check(_server(), "https://slow.test/feed.xml")

        assert status == 422
        assert body["error"].startswith("https://slow.test/feed.xml answered HTTP 503. ")
        assert down.call_count == 1

    async def test_an_address_that_cannot_be_reached_says_so(self) -> None:
        async with respx.mock:
            refused = respx.get("https://down.test/feed").mock(
                side_effect=httpx.ConnectError("connection refused")
            )

            status, body = await _check(_server(), "https://down.test/feed")

        assert status == 422
        assert body["error"] == (
            "Could not reach https://down.test/feed (connection refused). "
            "Check the address and try again."
        )
        assert refused.call_count == 1

    async def test_a_feed_with_no_article_is_refused(self) -> None:
        async with respx.mock:
            feed = respx.get("https://empty.test/rss").mock(
                return_value=httpx.Response(200, content=EMPTY_RSS, headers=XML)
            )

            status, body = await _check(_server(), "https://empty.test/rss")

        assert status == 422
        assert body["error"] == (
            "The feed at https://empty.test/rss has no articles, "
            "so there is nothing to read from it yet."
        )
        assert feed.call_count == 1

    async def test_something_that_is_neither_page_nor_feed_is_not_a_feed(self) -> None:
        async with respx.mock:
            image = respx.get("https://slow.test/logo.png").mock(
                return_value=httpx.Response(
                    200, content=b"\x89PNG", headers={"Content-Type": "image/png"}
                )
            )

            status, body = await _check(_server(), "https://slow.test/logo.png")

        assert status == 422
        assert body["error"].startswith("https://slow.test/logo.png is not an RSS or Atom feed.")
        assert image.call_count == 1

    @pytest.mark.parametrize("url", ["", "slow.test/feed.xml", "ftp://slow.test/feed"])
    async def test_an_address_that_is_not_a_web_address_is_never_fetched(self, url: str) -> None:
        async with respx.mock:
            status, body = await _check(_server(), url)

        assert status == 422
        assert body["error"] == "Paste a web address that starts with http:// or https://."

    @pytest.mark.parametrize("url", ["https://exa\x00mple.test/feed", "https://xn--a.test/feed"])
    async def test_a_malformed_address_is_unreachable_not_a_crash(self, url: str) -> None:
        async with respx.mock:
            status, body = await _check(_server(), url)

        assert status == 422 and body["error"].startswith(f"Could not reach {url} (")

    async def test_a_malformed_redirect_target_is_unreachable_not_a_crash(self) -> None:
        async with respx.mock:
            hop = respx.get("https://slow.test/feed.xml").mock(
                return_value=httpx.Response(302, headers={"Location": "https://xn--a.test/"})
            )

            status, body = await _check(_server(), "https://slow.test/feed.xml")

        assert status == 422
        assert body["error"].startswith("Could not reach https://slow.test/feed.xml (")
        assert hop.call_count == 1

    async def test_an_unclosed_bracket_in_the_pasted_address_is_not_a_web_address(self) -> None:
        async with respx.mock:
            status, body = await _check(_server(), "https://[::1/feed")

        assert status == 422
        assert body["error"] == "Paste a web address that starts with http:// or https://."

    async def test_a_redirect_to_an_unclosed_bracket_is_unreachable(self) -> None:
        async with respx.mock:
            hop = respx.get("https://slow.test/feed.xml").mock(
                return_value=httpx.Response(302, headers={"Location": "http://[x/"})
            )

            status, body = await _check(_server(), "https://slow.test/feed.xml")

        assert status == 422
        assert body["error"].startswith("Could not reach https://slow.test/feed.xml (")
        assert hop.call_count == 1

    async def test_a_home_page_is_scanned_off_the_event_loop(self, monkeypatch) -> None:
        threads = []
        scan = feed_check.feed_link

        def recording(page: str, base: str):
            threads.append(threading.current_thread())
            return scan(page, base)

        monkeypatch.setattr(feed_check, "feed_link", recording)
        async with respx.mock:
            home = respx.get("https://slow.test/").mock(
                return_value=httpx.Response(200, text=HOME, headers=HTML)
            )
            feed = respx.get("https://slow.test/feed.xml").mock(
                return_value=httpx.Response(200, content=RSS, headers=XML)
            )

            status, _ = await _check(_server(), "https://slow.test/")

        assert status == 200
        assert threads and threads[0] is not threading.main_thread()
        assert (home.call_count, feed.call_count) == (1, 1)

    async def test_a_malformed_feed_link_is_passed_over_for_the_next(self) -> None:
        page = (
            '<html><head><link rel="alternate" type="application/rss+xml" href="http://[x/feed">'
            '<link rel="alternate" type="application/rss+xml" href="/feed.xml"></head></html>'
        )
        async with respx.mock:
            home = respx.get("https://slow.test/").mock(
                return_value=httpx.Response(200, text=page, headers=HTML)
            )
            feed = respx.get("https://slow.test/feed.xml").mock(
                return_value=httpx.Response(200, content=RSS, headers=XML)
            )

            status, body = await _check(_server(), "https://slow.test/")

        assert (status, body["url"]) == (200, "https://slow.test/feed.xml")
        assert (home.call_count, feed.call_count) == (1, 1)

    async def test_a_malformed_taken_address_matches_nothing(self) -> None:
        async with respx.mock:
            feed = respx.get("https://slow.test/feed.xml").mock(
                return_value=httpx.Response(200, content=RSS, headers=XML)
            )

            status, body = await _check(
                _server(),
                "https://slow.test/feed.xml",
                taken=[{"name": "Odd", "url": "http://[x/"}],
            )

        assert (status, body["name"]) == (200, "Slow Reads")
        assert feed.call_count == 1

    async def test_a_body_naming_a_local_file_is_parsed_as_text_not_opened(self, tmp_path) -> None:
        local = tmp_path / "local.xml"
        local.write_bytes(RSS.replace(b"Slow Reads", b"Read From Disk"))
        async with respx.mock:
            answer = respx.get("https://slow.test/feed.xml").mock(
                return_value=httpx.Response(200, content=str(local).encode(), headers=XML)
            )

            status, body = await _check(_server(), "https://slow.test/feed.xml")

        assert status == 422
        assert body["error"].startswith("https://slow.test/feed.xml is not an RSS or Atom feed.")
        assert answer.call_count == 1

    async def test_an_answer_compressed_anyway_is_refused_before_it_is_read(self) -> None:
        async with respx.mock:
            packed = respx.get("https://slow.test/feed.xml").mock(
                return_value=httpx.Response(
                    200,
                    content=gzip.compress(RSS),
                    headers={**XML, "Content-Encoding": "gzip"},
                )
            )

            status, body = await _check(_server(), "https://slow.test/feed.xml")

        assert status == 422 and "compressed" in body["error"]
        assert packed.call_count == 1

    async def test_a_check_past_its_deadline_is_unreachable(self, monkeypatch) -> None:
        monkeypatch.setattr(feed_check, "CHECK_DEADLINE_SECONDS", 0.05)

        asked = []

        async def stall(request: httpx.Request) -> httpx.Response:
            asked.append(str(request.url))
            await asyncio.sleep(1)
            return httpx.Response(200, content=RSS, headers=XML)

        async with respx.mock:
            slow = respx.get("https://slow.test/feed.xml").mock(side_effect=stall)

            status, body = await _check(_server(), "https://slow.test/feed.xml")

        assert status == 422
        assert body["error"].startswith(
            "Could not reach https://slow.test/feed.xml (no answer within"
        )
        # Cancelled mid-answer, so respx never records the call it was asked.
        assert (asked, slow.call_count) == (["https://slow.test/feed.xml"], 0)

    @pytest.mark.parametrize(
        "body", [None, [], {"url": 3}, {"url": "https://a.test/", "taken": [{"url": "x"}]}]
    )
    async def test_a_body_of_another_shape_is_a_400(self, body) -> None:
        response = await _server()._handle_post_feed_check(Request(body))

        assert response.status == 400

    async def test_a_deployment_without_the_wizard_has_no_check(self) -> None:
        response = await TriageServer()._handle_post_feed_check(Request({"url": "https://a/"}))

        assert response.status == 409


class TestOnlyPublicAddresses:
    """The check fetches from inside the deployment, so it reaches only the public web.

    An address it must not ask has no respx route: were it asked, respx would
    refuse the unmocked request and the answer would not be the refusal.
    """

    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1:8766/settings",
            "http://169.254.169.254/latest/meta-data/",
            "http://10.0.0.5/feed",
            "http://[::1]/feed",
        ],
    )
    async def test_an_address_on_a_private_or_local_network_is_never_fetched(self, url) -> None:
        async with respx.mock:
            status, body = await _check(_server(), url)

        assert status == 422
        assert "private or local network" in body["error"]

    async def test_a_name_that_resolves_inside_is_never_fetched(self) -> None:
        async def inside(host: str) -> list[str]:
            return [PUBLIC, "192.168.1.10"]

        async with respx.mock:
            status, body = await _check(_server(resolve=inside), "https://slow.test/feed.xml")

        assert status == 422 and "private or local network" in body["error"]

    async def test_a_redirect_into_a_private_network_is_not_followed(self) -> None:
        async with respx.mock:
            hop = respx.get("https://slow.test/feed.xml").mock(
                return_value=httpx.Response(
                    302, headers={"Location": "http://169.254.169.254/latest/meta-data/"}
                )
            )

            status, body = await _check(_server(), "https://slow.test/feed.xml")

        assert status == 422 and "private or local network" in body["error"]
        assert hop.call_count == 1

    async def test_a_public_redirect_is_followed_and_its_end_is_the_address(self) -> None:
        async with respx.mock:
            hop = respx.get("http://slow.test/feed.xml").mock(
                return_value=httpx.Response(301, headers={"Location": "https://slow.test/feed.xml"})
            )
            feed = respx.get("https://slow.test/feed.xml").mock(
                return_value=httpx.Response(200, content=RSS, headers=XML)
            )

            status, body = await _check(_server(), "http://slow.test/feed.xml")

        assert (status, body["url"]) == (200, "https://slow.test/feed.xml")
        assert (hop.call_count, feed.call_count) == (1, 1)

    async def test_endless_redirects_are_refused(self) -> None:
        async with respx.mock:
            loop = respx.get("https://slow.test/loop").mock(
                return_value=httpx.Response(302, headers={"Location": "/loop"})
            )

            status, body = await _check(_server(), "https://slow.test/loop")

        assert status == 422 and "redirect" in body["error"]
        assert loop.call_count == MAX_REDIRECTS + 1

    async def test_an_answer_past_the_size_cap_is_refused_unparsed(self) -> None:
        async with respx.mock:
            huge = respx.get("https://slow.test/huge.xml").mock(
                return_value=httpx.Response(200, content=b"x" * (MAX_BYTES + 1), headers=XML)
            )

            status, body = await _check(_server(), "https://slow.test/huge.xml")

        assert status == 422 and "too large" in body["error"]
        assert huge.call_count == 1

    async def test_a_name_that_turns_private_after_the_check_is_never_connected(self) -> None:
        """No respx: the connect itself must refuse, before any socket is opened."""
        answers = iter([[PUBLIC], ["127.0.0.1"]])

        async def rebinding(host: str) -> list[str]:
            return next(answers)

        status, body = await _check(_server(resolve=rebinding), "https://slow.test/feed.xml")

        assert status == 422 and "private or local network" in body["error"]

    @pytest.mark.parametrize(
        "failure", [httpcore.ConnectError("no IPv6 route"), httpcore.ConnectTimeout("dropped")]
    )
    async def test_a_connect_tries_each_checked_address_in_turn(self, failure) -> None:
        asked = []

        class Inner:
            async def connect_tcp(self, host, port, **kwargs):
                asked.append(host)
                if host == "2606:2800:220:1:248:1893:25c8:1946":
                    raise failure
                return "stream"

        async def dual_stack(host: str) -> list[str]:
            return ["2606:2800:220:1:248:1893:25c8:1946", PUBLIC, PUBLIC]

        backend = feed_check._PublicOnlyBackend(dual_stack)
        backend._inner = Inner()

        assert await backend.connect_tcp("slow.test", 443) == "stream"
        assert asked == ["2606:2800:220:1:248:1893:25c8:1946", PUBLIC]


class TestAFeedAlreadyListed:
    async def test_a_feed_the_page_lists_is_refused_with_its_listed_name(self) -> None:
        async with respx.mock:
            home = respx.get("https://slow.test/").mock(
                return_value=httpx.Response(200, text=HOME, headers=HTML)
            )
            feed = respx.get("https://slow.test/feed.xml").mock(
                return_value=httpx.Response(200, content=RSS, headers=XML)
            )

            status, body = await _check(
                _server(),
                "https://slow.test/",
                [{"name": "Slow", "url": "https://SLOW.test/feed.xml/"}],
            )

        assert status == 409
        assert body["name"] == "Slow"
        assert body["error"] == "This feed is already in the list as Slow."
        assert home.call_count == 1 and feed.call_count == 1

    async def test_an_address_already_listed_is_not_fetched(self) -> None:
        async with respx.mock:
            status, body = await _check(
                _server([SourceConfig(name="Stored", url="https://slow.test/feed.xml")]),
                "https://slow.test/feed.xml",
            )

        assert (status, body["name"]) == (409, "Stored")

    async def test_a_feed_listed_under_the_address_it_redirects_from_is_found(self) -> None:
        async with respx.mock:
            home = respx.get("https://slow.test/").mock(
                return_value=httpx.Response(200, text=HOME, headers=HTML)
            )
            hop = respx.get("https://slow.test/feed.xml").mock(
                return_value=httpx.Response(301, headers={"Location": "https://feeds.slow.test/"})
            )
            feed = respx.get("https://feeds.slow.test/").mock(
                return_value=httpx.Response(200, content=RSS, headers=XML)
            )

            status, body = await _check(
                _server(),
                "https://slow.test/",
                [{"name": "Slow", "url": "https://slow.test/feed.xml"}],
            )

        assert (status, body["name"]) == (409, "Slow")
        assert (home.call_count, hop.call_count, feed.call_count) == (1, 1, 1)

    async def test_a_title_another_source_has_takes_the_host_after_it(self) -> None:
        async with respx.mock:
            feed = respx.get("https://slow.test/feed.xml").mock(
                return_value=httpx.Response(200, content=RSS, headers=XML)
            )

            status, body = await _check(
                _server([SourceConfig(name="slow reads", url="https://other.test/rss")]),
                "https://slow.test/feed.xml",
                [{"name": "Slow Reads (slow.test)", "url": "https://third.test/rss"}],
            )

        # Its title, then the title with the host, are both taken.
        assert (status, body["name"]) == (200, "Slow Reads (slow.test) 2")
        assert feed.call_count == 1


def _catalog_text(catalog: str, key: str) -> str:
    path = Path(cyris.__file__).parent / "locales" / f"{catalog}.json"
    entry = json.loads(path.read_text(encoding="utf-8"))[key]
    return entry if catalog == "en" else entry["text"]


class TestARefusalIsWordedInThePagesLanguage:
    """The check refuses by a code; the server words it as the wizard's page is worded."""

    @pytest.mark.parametrize(
        ("values", "headers", "catalog"),
        [
            ({"digest.output_language": "zh-Hant"}, {}, "zh-Hant"),
            ({}, {"Accept-Language": "zh-TW,zh;q=0.9"}, "zh-Hant"),
            ({}, {"Accept-Language": "fr"}, "en"),
            ({"digest.output_language": "en"}, {"Accept-Language": "zh-TW"}, "en"),
        ],
    )
    async def test_a_refusal_follows_the_saved_language_else_the_browsers(
        self, values, headers, catalog
    ) -> None:
        async with respx.mock:
            status, body = await _check(_server(values=values), "ftp://slow.test/", (), headers)

        assert status == 422
        assert body["error"] == _catalog_text(catalog, "feed_check.not_a_url")

    async def test_a_refusals_fields_fill_its_translation(self) -> None:
        async with respx.mock:
            home = respx.get("https://slow.test/").mock(
                return_value=httpx.Response(200, text="<html></html>", headers=HTML)
            )

            status, body = await _check(
                _server(values={"digest.output_language": "zh-Hant"}), "https://slow.test/"
            )

        expected = _catalog_text("zh-Hant", "feed_check.no_feed_link")
        assert home.call_count == 1
        assert status == 422
        assert body["error"] == expected.replace("{$url}", "https://slow.test/")

    async def test_an_already_listed_refusal_still_names_the_listed_feed(self) -> None:
        listed = [SourceConfig(name="Stored", url="https://slow.test/feed.xml")]
        async with respx.mock:
            status, body = await _check(
                _server(listed, values={"digest.output_language": "zh-Hant"}),
                "https://slow.test/feed.xml",
            )

        expected = _catalog_text("zh-Hant", "feed_check.already_listed")
        assert (status, body["name"]) == (409, "Stored")
        assert body["error"] == expected.replace("{$name}", "Stored")
