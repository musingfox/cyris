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

    assert len(articles) == 1  # body becomes the one article
    assert articles[0].url.startswith("newsletter:")
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

    assert articles == []  # unknown sender still skipped
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


def test_is_private_reply_detects_direct_re():
    # T1
    assert CloudflareNewsletterSource._is_private_reply({"subject": "Re: 關於上一期的問題"}) is True


def test_is_private_reply_detects_fwd_of_re():
    # T2
    assert CloudflareNewsletterSource._is_private_reply({"subject": "Fwd: Re: 曼報 #67"}) is True


def test_is_private_reply_ignores_fwd_of_original():
    # T3
    assert (
        CloudflareNewsletterSource._is_private_reply({"subject": "Fwd: 粉虱通訊 No. 28"}) is False
    )


def test_is_private_reply_normal_subject_false():
    # T4
    assert (
        CloudflareNewsletterSource._is_private_reply({"subject": "曼報 #67｜IMAX：稀缺的代價"})
        is False
    )


def test_is_private_reply_detects_fw_re():
    # pinning: Fw: Re: judged as private reply
    assert CloudflareNewsletterSource._is_private_reply({"subject": "Fw: Re: 關於問題"}) is True


def test_is_private_reply_detects_repeated_fwd_re():
    # pinning: repeated Fwd: Fwd: Re: judged private
    assert (
        CloudflareNewsletterSource._is_private_reply({"subject": "Fwd: Fwd: Re: 曼報 #67"}) is True
    )


def test_is_private_reply_detects_chinese_reply():
    # pinning: 回覆: judged as private reply
    assert CloudflareNewsletterSource._is_private_reply({"subject": "回覆: 你的問題"}) is True


def test_is_private_reply_returns_false_for_non_string_subject():
    # pinning: non-str/ missing subject -> False (no AttributeError)
    assert CloudflareNewsletterSource._is_private_reply({"subject": 123}) is False
    assert CloudflareNewsletterSource._is_private_reply({"subject": None}) is False
    assert CloudflareNewsletterSource._is_private_reply({}) is False
    assert CloudflareNewsletterSource._is_private_reply({"subject": ["bad"]}) is False


@pytest.mark.asyncio
@respx.mock
async def test_private_reply_not_ingested_but_acked():
    # T5
    item = {
        "id": "nl:reply",
        "from": "list@benedictevans.com",
        "subject": "Re: 你的問題",
        "html": "<p>好的</p>",
        "text": "好的",
        "date": "2026-07-30T02:00:00Z",
    }
    respx.get(f"{WORKER}/newsletters").mock(return_value=httpx.Response(200, json=[item]))
    ack = respx.post(f"{WORKER}/ack").mock(return_value=httpx.Response(200, json={"ok": True}))

    src = CloudflareNewsletterSource(WORKER, "tok")
    after, before = _now()
    articles = await src.fetch_articles(after, before, _source())
    assert articles == []
    assert json.loads(ack.calls.last.request.content) == {"ids": ["nl:reply"]}


@pytest.mark.asyncio
@respx.mock
async def test_non_string_subject_does_not_raise_and_still_acks():
    # pinning: non-str subject (e.g. int) -> _is_private safe False,
    # parse raises inside try (caught), no escape, ACK happens
    item = {
        "id": "nl:badsubj",
        "from": "list@benedictevans.com",
        "subject": 12345,  # non-str
        "html": "<p>foo</p>",
        "text": "foo",
        "date": "2026-07-30T02:00:00Z",
    }
    respx.get(f"{WORKER}/newsletters").mock(return_value=httpx.Response(200, json=[item]))
    ack = respx.post(f"{WORKER}/ack").mock(return_value=httpx.Response(200, json={"ok": True}))

    src = CloudflareNewsletterSource(WORKER, "tok")
    after, before = _now()
    # call must not raise to outer (would skip ack)
    articles = await src.fetch_articles(after, before, _source())
    assert articles == []
    assert ack.called
    assert json.loads(ack.calls.last.request.content) == {"ids": ["nl:badsubj"]}


@pytest.mark.asyncio
@respx.mock
async def test_malformed_item_missing_id_does_not_crash_batch_acks_goods():
    """Malformed missing 'id' in batch: goods still produce articles + get ACKed."""
    good = {
        "id": "nl:good1",
        "from": "list@benedictevans.com",
        "subject": "Weekly #1",
        "html": "<p>body1</p>",
        "text": "body1",
        "date": "2026-07-13T00:00:00Z",
    }
    bad = {  # missing id -> would have crashed on item["id"]
        "from": "list@benedictevans.com",
        "subject": "Bad no id",
        "html": "<p/>",
        "text": "",
        "date": "2026-07-13T00:00:00Z",
    }
    good2 = {
        "id": "nl:good2",
        "from": "list@benedictevans.com",
        "subject": "Weekly #2",
        "html": "<p>body2</p>",
        "text": "body2",
        "date": "2026-07-13T00:00:00Z",
    }
    queued = [good, bad, good2]
    respx.get(f"{WORKER}/newsletters").mock(return_value=httpx.Response(200, json=queued))
    ack = respx.post(f"{WORKER}/ack").mock(return_value=httpx.Response(200))
    source = CloudflareNewsletterSource(WORKER, "tok")
    articles = await source.fetch_articles(*_now(), sources=_source())
    assert len(articles) == 2
    assert articles[0].title == "Weekly #1"
    assert articles[1].title == "Weekly #2"
    assert json.loads(ack.calls.last.request.content) == {"ids": ["nl:good1", "nl:good2"]}
