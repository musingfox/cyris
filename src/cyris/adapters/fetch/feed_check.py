"""The onboarding wizard's check of a feed the reader pastes: a feed, or a home page that links one.

One fetch of the address, through `GuardedHttp`, parsed with feedparser as
`RssSource` parses a feed. A page is not a feed, so a page that names one with
`<link rel="alternate">` is followed once to that feed and parsed in turn. The
feed passes when it has at least one entry, and is named by its own title.

The words matched are `feed_check.json`'s, and each refusal is a code the UI
catalogs word as `feed_check.<code>` (ADR-0027); the order of the fetches and
the attributes read are here.
"""

from __future__ import annotations

import asyncio
import io
import ipaddress
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from functools import cache
from html.parser import HTMLParser
from importlib.resources import files
from typing import Literal
from urllib.parse import urljoin, urlsplit

import feedparser
import httpcore
import httpx

from cyris.adapters.http_client import USER_AGENT, HttpClient

logger = logging.getLogger(__name__)

# The most one answer may hold, counted as it streams in. A busy site's feed is
# a few hundred kilobytes; past this the answer is refused before it is parsed.
MAX_BYTES = 5 * 1024 * 1024
# Redirects followed by hand, so each hop's host is checked before it is asked.
MAX_REDIRECTS = 5
TIMEOUT_SECONDS = 30
# Per address, so a silently dropped IPv6 connect leaves time to try IPv4.
CONNECT_TIMEOUT_SECONDS = 10
# One press, every fetch and redirect included: the reader is waiting on it.
CHECK_DEADLINE_SECONDS = 60

Resolver = Callable[[str], Awaitable[list[str]]]

# Every refusal the check makes. `url` is the address that failed, which after a home
# page is the feed it linked; the catalogs' `feed_check.` messages read the fields each
# refusal is raised with.
FeedRefusal = Literal[
    "not_a_url",
    "unreachable",
    "http_status",
    "not_a_feed",
    "no_feed_link",
    "no_articles",
    "already_listed",
    "not_public",
    "too_many_redirects",
    "too_large",
    "encoded",
]


@cache
def _vocabulary() -> dict:
    raw = (files(__package__) / "feed_check.json").read_text(encoding="utf-8")
    return {k: v for k, v in json.loads(raw).items() if not k.startswith("_")}


def _parse(content: bytes) -> feedparser.FeedParserDict:
    # Wrapped: feedparser opens bytes that name a file as that file.
    return feedparser.parse(io.BytesIO(content))


def _media_type(value: str | None) -> str:
    return (value or "").split(";", 1)[0].strip().lower()


class FeedCheckError(Exception):
    """A refusal by its code and the fields its message reads; `name` is set when the feed
    is already listed."""

    def __init__(self, code: FeedRefusal, **fields: str) -> None:
        super().__init__(code, fields)
        self.code = code
        self.fields = fields
        self.name = fields.get("name") if code == "already_listed" else None


@dataclass(frozen=True)
class CheckedFeed:
    """A feed that passed: its own title and the address it was finally read from."""

    name: str
    url: str


class _FeedLinks(HTMLParser):
    """Every `<link rel="alternate">` href whose type is a feed type, in page order."""

    def __init__(self, types: set[str]) -> None:
        super().__init__(convert_charrefs=True)
        self._types = types
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "link":
            return
        found = {key: value or "" for key, value in attrs}
        rel = found.get("rel", "").lower().split()
        href = found.get("href", "").strip()
        if "alternate" in rel and _media_type(found.get("type")) in self._types and href:
            self.hrefs.append(href)


def feed_link(page: str, base: str) -> str | None:
    """The first RSS or Atom feed a page links with `<link rel="alternate">`, made absolute."""
    parser = _FeedLinks({t.lower() for t in _vocabulary()["feed_link_types"]})
    parser.feed(page)
    parser.close()
    for href in parser.hrefs:
        try:
            return urljoin(base, href)
        except ValueError:
            continue
    return None


def _same_address(a: str, b: str) -> bool:
    """One address written two ways: a trailing slash or the host's case differs."""

    def key(url: str) -> tuple[str, str, str, str]:
        parts = urlsplit(url.strip())
        return (parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), parts.query)

    try:
        return key(a) == key(b)
    except ValueError:
        return False


def listed_as(urls: list[str], taken: Sequence[tuple[str, str]]) -> str | None:
    """The name a listed feed has when any of `urls` is its address, else None."""
    for name, url in taken:
        if url and any(_same_address(url, candidate) for candidate in urls):
            return name
    return None


def unique_name(title: str, url: str, taken: list[str]) -> str:
    """The feed's title, or failing that its host, made distinct from every taken name.

    A name is a source's key, so a second source under one name would replace
    the first. A clash takes the host after it, then a number.
    """
    host = urlsplit(url).hostname or url
    base = " ".join(title.split()) or host
    used = {name.casefold() for name in taken}
    if base.casefold() not in used:
        return base
    name = f"{base} ({host})" if base != host else base
    n = 2
    candidate = name
    while candidate.casefold() in used:
        candidate = f"{name} {n}"
        n += 1
    return candidate


async def resolve_host(host: str) -> list[str]:
    """Every address `host` resolves to."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, None)
    return [info[4][0] for info in infos]


def _is_public(address: str) -> bool:
    return ipaddress.ip_address(address.split("%", 1)[0]).is_global


async def _public_addresses(host: str, resolve: Resolver, url: str) -> list[str]:
    """What `host` resolves to, refused unless every address is on the public web."""
    try:
        addresses = [str(ipaddress.ip_address(host))]
    except ValueError:
        try:
            addresses = await resolve(host)
        except OSError as e:
            raise httpx.ConnectError(f"cannot resolve {host}") from e
    if not addresses or not all(_is_public(a) for a in addresses):
        raise FeedCheckError("not_public", url=url)
    return addresses


class _PublicOnlyBackend(httpcore.AsyncNetworkBackend):
    """Connects to an address it resolved and checked itself, never one resolved again later.

    A check made before the request is not enough: a name can answer it with a
    public address and the connect that follows with a private one. TLS still
    names the host, since httpcore passes it to `start_tls` apart from this.
    """

    def __init__(self, resolve: Resolver) -> None:
        self._resolve = resolve
        self._inner = httpcore.AnyIOBackend()

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        addresses = await _public_addresses(host, self._resolve, f"{host}:{port}")
        # In resolver order, which often puts IPv6 first on a host that has no IPv6 route.
        for address in dict.fromkeys(addresses):
            try:
                return await self._inner.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as e:
                error = e
        raise error

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise httpcore.ConnectError("unix sockets are never asked")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class _PublicOnlyTransport(httpx.AsyncHTTPTransport):
    # httpx 0.28 takes no network backend, so the pool it built is replaced by one that has it.
    def __init__(self, resolve: Resolver) -> None:
        super().__init__(trust_env=False)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=httpx.create_ssl_context(trust_env=False),
            network_backend=_PublicOnlyBackend(resolve),
        )


class GuardedHttp:
    """GETs that reach only the public web, for an address a reader typed.

    The check runs inside the deployment, so an address on a private, loopback
    or link-local network would read what only the deployment can see. Every
    hop's host must resolve to public addresses alone, checked before the
    request for a refusal that names the address and again at the connect,
    which uses the address it checked; redirects are followed by hand so each
    one is checked, no proxy from the environment is used, and an answer is
    read only up to `MAX_BYTES`.
    """

    def __init__(self, resolve: Resolver = resolve_host) -> None:
        self._resolve = resolve
        self._client = httpx.AsyncClient(
            transport=_PublicOnlyTransport(resolve),
            trust_env=False,
            timeout=httpx.Timeout(TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS),
            follow_redirects=False,
            # Uncompressed, so `MAX_BYTES` counts what arrives, not what one chunk inflates to.
            headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"},
        )

    async def get(self, url: str) -> httpx.Response:
        for _ in range(MAX_REDIRECTS + 1):
            await self._admit(url)
            response = await self._client.send(self._client.build_request("GET", url), stream=True)
            try:
                if response.is_redirect:
                    url = urljoin(str(response.url), response.headers["location"])
                    continue
                # Asked for identity; an answer encoded anyway could inflate past any cap.
                encoding = response.headers.get("content-encoding", "").strip().lower()
                if encoding not in ("", "identity"):
                    raise FeedCheckError("encoded", url=url)
                body = bytearray()
                async for chunk in response.aiter_raw():
                    body += chunk
                    if len(body) > MAX_BYTES:
                        raise FeedCheckError("too_large", url=url)
            finally:
                await response.aclose()
            # Read whole already, so the copy drops the headers that describe the wire form.
            headers = [
                (k, v)
                for k, v in response.headers.multi_items()
                if k.lower() not in ("content-encoding", "content-length", "transfer-encoding")
            ]
            return httpx.Response(
                response.status_code, headers=headers, content=bytes(body), request=response.request
            )
        raise FeedCheckError("too_many_redirects", url=url)

    async def _admit(self, url: str) -> None:
        await _public_addresses(urlsplit(url).hostname or "", self._resolve, url)

    async def close(self) -> None:
        await self._client.aclose()


class FeedChecker:
    """Fetches and parses one pasted address, following a home page to its feed once."""

    def __init__(self, http_client: HttpClient | GuardedHttp | None = None) -> None:
        self._http = http_client or GuardedHttp()

    async def check(self, url: str, taken: Sequence[tuple[str, str]] = ()) -> CheckedFeed:
        """The feed at `url`, named apart from every `(name, url)` in `taken`.

        Refused, with the listed name on the error, when the address pasted,
        the feed a page linked or the address a redirect ended on is already one
        of `taken`: a second source for one feed would only fetch it twice.
        """
        pasted = url.strip()
        if (name := listed_as([pasted], taken)) is not None:
            raise FeedCheckError("already_listed", name=name)
        try:
            async with asyncio.timeout(CHECK_DEADLINE_SECONDS):
                title, final, linked = await self._read(pasted)
        except TimeoutError as e:
            reason = f"no answer within {CHECK_DEADLINE_SECONDS} seconds"
            raise FeedCheckError("unreachable", url=pasted, reason=reason) from e
        if (name := listed_as([a for a in (linked, final) if a], taken)) is not None:
            raise FeedCheckError("already_listed", name=name)
        return CheckedFeed(name=unique_name(title, final, [n for n, _ in taken]), url=final)

    async def _read(self, url: str) -> tuple[str, str, str | None]:
        """The feed's own title, the address it was read from and the link a page gave, if any."""
        try:
            parts = urlsplit(url)
            web = parts.scheme.lower() in ("http", "https") and bool(parts.hostname)
        except ValueError:
            web = False
        if not web:
            raise FeedCheckError("not_a_url")

        linked = None
        response = await self._get(url)
        parsed = await asyncio.to_thread(_parse, response.content)
        if not parsed.version:
            if _media_type(response.headers.get("content-type")) not in _vocabulary()["html_types"]:
                raise FeedCheckError("not_a_feed", url=url)
            # Off the event loop, as the feed parse is: the page may be MAX_BYTES of tags.
            page = response
            linked = await asyncio.to_thread(lambda: feed_link(page.text, str(page.url)))
            if linked is None:
                raise FeedCheckError("no_feed_link", url=url)
            url = linked
            response = await self._get(url)
            parsed = await asyncio.to_thread(_parse, response.content)
            if not parsed.version:
                raise FeedCheckError("not_a_feed", url=url)

        if not parsed.entries:
            raise FeedCheckError("no_articles", url=url)
        # The address the feed was read from, after any redirect, is the one a run polls.
        return str(parsed.feed.get("title", "")), str(response.url), linked

    async def _get(self, url: str) -> httpx.Response:
        """One GET, no retry: the reader is waiting and can press again.

        An error answer is named by its status only: its body is what a server
        said to the deployment, not something to read back to the page.
        """
        try:
            response = await self._http.get(url)
        # A malformed address or redirect target is not an HTTPError, but is as unreachable;
        # ValueError covers urllib's (an unclosed `[`) and the IDNA codec's UnicodeError.
        except (httpx.HTTPError, httpx.InvalidURL, ValueError) as e:
            reason = str(e) or type(e).__name__
            raise FeedCheckError("unreachable", url=url, reason=reason) from e
        if response.status_code >= 400:
            logger.info("Feed check: %s answered HTTP %d", url, response.status_code)
            raise FeedCheckError("http_status", url=url, status=str(response.status_code))
        return response

    async def close(self) -> None:
        await self._http.close()
