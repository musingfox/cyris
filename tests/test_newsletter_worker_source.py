"""Tests for CloudflareNewsletterSource pull/match/ack."""

import json
from datetime import UTC, datetime

import httpx
import pytest
import respx

from cyris.adapters.fetch.newsletter_worker_source import CloudflareNewsletterSource
from cyris.domain.models import SourceConfig, Tier

WORKER = "https://cyris-newsletter.example.workers.dev"


def _source(**kw) -> dict[str, SourceConfig]:
    return {
        "Benedict Evans": SourceConfig(
            name="Benedict Evans",
            tier=Tier.SUMMARIZE,
            tags=["tech"],
            email_match="from:list@benedictevans.com",
            **kw,
        )
    }


def _now() -> tuple[datetime, datetime]:
    n = datetime(2026, 7, 13, tzinfo=UTC)
    return n, n


@pytest.mark.asyncio
@respx.mock
async def test_matched_sender_acked():
    """A newsletter from an email_match sender is processed and its id ACKed."""
    item = {
        "id": "nl:abc",
        "from": "list@benedictevans.com",
        "subject": "Weekly",
        "html": "<p>no links here</p>",
        "text": "no links",
        "date": "2026-07-13T00:00:00Z",
    }
    respx.get(f"{WORKER}/newsletters").mock(return_value=httpx.Response(200, json=[item]))
    ack = respx.post(f"{WORKER}/ack").mock(return_value=httpx.Response(200, json={"ok": True}))

    src = CloudflareNewsletterSource(WORKER, "tok")
    after, before = _now()
    articles = await src.fetch_articles(after, before, _source())

    assert articles == []  # no links -> no articles, but flow completed
    assert ack.called
    assert json.loads(ack.calls.last.request.content) == {"ids": ["nl:abc"]}


@pytest.mark.asyncio
@respx.mock
async def test_unknown_sender_skipped_but_acked():
    """A sender with no matching source is skipped yet still ACKed (no pileup)."""
    item = {
        "id": "nl:xyz",
        "from": "stranger@nowhere.com",
        "subject": "Spam",
        "html": "<a href='http://x.com'>x</a>",
        "text": "",
        "date": "2026-07-13T00:00:00Z",
    }
    respx.get(f"{WORKER}/newsletters").mock(return_value=httpx.Response(200, json=[item]))
    ack = respx.post(f"{WORKER}/ack").mock(return_value=httpx.Response(200, json={"ok": True}))

    src = CloudflareNewsletterSource(WORKER, "tok")
    after, before = _now()
    articles = await src.fetch_articles(after, before, _source())

    assert articles == []
    assert json.loads(ack.calls.last.request.content) == {"ids": ["nl:xyz"]}


def test_match_forwarded_finds_body_sender():
    """Manual 'Fwd:' — original sender in the body matches the email_match source."""
    item = {
        "from": "me@gmail.com",
        "text": "---------- Forwarded message ---------\nFrom: BE <list@benedictevans.com>\n",
        "html": "",
    }
    src = CloudflareNewsletterSource(WORKER, "tok")
    m = src._match_forwarded(item, _source())
    assert m is not None and m.name == "Benedict Evans"


def test_match_forwarded_no_body_sender_returns_none():
    """No email_match address anywhere in the body -> no match."""
    item = {"from": "me@gmail.com", "text": "just some text, no headers", "html": ""}
    src = CloudflareNewsletterSource(WORKER, "tok")
    assert src._match_forwarded(item, _source()) is None


@pytest.mark.asyncio
@respx.mock
async def test_empty_queue_no_ack():
    """Nothing queued -> no ACK call."""
    respx.get(f"{WORKER}/newsletters").mock(return_value=httpx.Response(200, json=[]))
    ack = respx.post(f"{WORKER}/ack").mock(return_value=httpx.Response(200, json={"ok": True}))

    src = CloudflareNewsletterSource(WORKER, "tok")
    after, before = _now()
    assert await src.fetch_articles(after, before, _source()) == []
    assert not ack.called
