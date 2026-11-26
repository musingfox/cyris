"""Tests for CloudflareNewsletterSource pull/match/ack."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from cyris.adapters.fetch.newsletter_worker_source import (
    MAX_NEW_SOURCES_PER_RUN,
    UNMATCHED_HOLD_DAYS,
    CloudflareNewsletterSource,
    _match_address,
    own_address_keys,
    read_forward,
)
from cyris.domain.models import Lane, SourceConfig

pytestmark = pytest.mark.unit

WORKER = "https://cyris-newsletter.example.workers.dev"


def _source(**kw) -> dict[str, SourceConfig]:
    return {
        "Benedict Evans": SourceConfig(
            name="Benedict Evans",
            lane=Lane.READ,
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
    pull = respx.get(f"{WORKER}/newsletters").mock(return_value=httpx.Response(200, json=[item]))
    ack = respx.post(f"{WORKER}/ack").mock(return_value=httpx.Response(200, json={"ok": True}))

    src = CloudflareNewsletterSource(WORKER, "tok")
    after, before = _now()
    articles = await src.fetch_articles(after, before, _source())

    assert len(articles) == 1  # body becomes the one article
    assert articles[0].url.startswith("newsletter:")
    assert pull.call_count == 1
    assert pull.calls.last.request.headers["Authorization"] == "Bearer tok"
    assert ack.call_count == 1
    assert ack.calls.last.request.headers["Authorization"] == "Bearer tok"
    assert json.loads(ack.calls.last.request.content) == {"ids": ["nl:abc"]}


@pytest.mark.asyncio
@respx.mock
async def test_a_system_senders_mail_is_skipped_but_acked():
    """A system sender gets no source, so its mail is skipped yet still ACKed (no pileup)."""
    item = {
        "id": "nl:xyz",
        "from": "Gmail Team <Forwarding-Noreply@Google.com>",
        "subject": "(#123) Gmail Forwarding Confirmation",
        "html": "<a href='http://x.com'>x</a>",
        "text": "",
        "date": "2026-07-13T00:00:00Z",
    }
    pull = respx.get(f"{WORKER}/newsletters").mock(return_value=httpx.Response(200, json=[item]))
    ack = respx.post(f"{WORKER}/ack").mock(return_value=httpx.Response(200, json={"ok": True}))

    src = CloudflareNewsletterSource(WORKER, "tok")
    after, before = _now()
    articles = await src.fetch_articles(after, before, _source())

    assert articles == []  # unknown sender still skipped
    assert pull.call_count == 1
    assert ack.call_count == 1
    assert json.loads(ack.calls.last.request.content) == {"ids": ["nl:xyz"]}


def test_a_senders_address_is_read_in_any_case():
    m = _match_address("list@benedictevans.com", _source())
    assert m is not None and m.name == "Benedict Evans"


@pytest.mark.parametrize("sender", ["", "  ", None])
async def test_an_empty_or_missing_from_files_nothing_and_is_acked(sender):
    create = _Creator()

    articles, acked = await _run([_mail("nl:1", sender)], _source(), create)

    assert (articles, create.made, acked) == ([], [], ["nl:1"])


def test_each_address_of_a_several_address_email_match_matches():
    src = {"X": SourceConfig(name="X", lane=Lane.READ, email_match="from:(a@x.test OR B@Y.test)")}
    for sender in ("a@x.test", "b@y.test", "a+list@x.test"):
        m = _match_address(sender, src)
        assert m is not None and m.name == "X"
    assert _match_address("c@z.test", src) is None


def test_an_email_match_without_the_from_prefix_matches_too():
    src = {"X": SourceConfig(name="X", lane=Lane.READ, email_match="A@B.com")}
    assert _match_address("a@b.com", src) is not None


@pytest.mark.asyncio
@respx.mock
async def test_empty_queue_no_ack():
    """Nothing queued -> no ACK call."""
    pull = respx.get(f"{WORKER}/newsletters").mock(return_value=httpx.Response(200, json=[]))
    ack = respx.post(f"{WORKER}/ack").mock(return_value=httpx.Response(200, json={"ok": True}))

    src = CloudflareNewsletterSource(WORKER, "tok")
    after, before = _now()
    assert await src.fetch_articles(after, before, _source()) == []
    assert pull.call_count == 1
    assert not ack.called


@pytest.mark.asyncio
@respx.mock
async def test_a_failed_pull_raises_and_acks_nothing():
    """A dead queue raises, so the run lists this source under `failed_sources`."""
    pull = respx.get(f"{WORKER}/newsletters").mock(return_value=httpx.Response(500))
    ack = respx.post(f"{WORKER}/ack").mock(return_value=httpx.Response(200, json={"ok": True}))

    src = CloudflareNewsletterSource(WORKER, "tok")
    after, before = _now()
    with pytest.raises(httpx.HTTPStatusError):
        await src.fetch_articles(after, before, _source())
    assert pull.call_count == 1
    assert not ack.called


@pytest.mark.asyncio
@respx.mock
async def test_a_known_sources_reply_subject_is_read_as_an_issue():
    """A subject is a guess: a Re: from a source's sender is its issue, not a private reply."""
    item = {
        "id": "nl:reply",
        "from": "list@benedictevans.com",
        "subject": "Re: 你的問題",
        "html": "<p>好的</p>",
        "text": "好的",
        "date": "2026-07-30T02:00:00Z",
    }
    pull = respx.get(f"{WORKER}/newsletters").mock(return_value=httpx.Response(200, json=[item]))
    ack = respx.post(f"{WORKER}/ack").mock(return_value=httpx.Response(200, json={"ok": True}))

    src = CloudflareNewsletterSource(WORKER, "tok")
    after, before = _now()
    articles = await src.fetch_articles(after, before, _source())
    assert [a.source_name for a in articles] == ["Benedict Evans"]
    assert pull.call_count == 1
    assert ack.call_count == 1
    assert json.loads(ack.calls.last.request.content) == {"ids": ["nl:reply"]}


@pytest.mark.asyncio
@respx.mock
async def test_non_string_subject_does_not_raise_and_still_acks():
    # pinning: a non-str subject (e.g. int) makes the parse raise inside the try (caught),
    # so nothing escapes and the ACK happens
    item = {
        "id": "nl:badsubj",
        "from": "list@benedictevans.com",
        "subject": 12345,  # non-str
        "html": "<p>foo</p>",
        "text": "foo",
        "date": "2026-07-30T02:00:00Z",
    }
    pull = respx.get(f"{WORKER}/newsletters").mock(return_value=httpx.Response(200, json=[item]))
    ack = respx.post(f"{WORKER}/ack").mock(return_value=httpx.Response(200, json={"ok": True}))

    src = CloudflareNewsletterSource(WORKER, "tok")
    after, before = _now()
    # call must not raise to outer (would skip ack)
    articles = await src.fetch_articles(after, before, _source())
    assert articles == []
    assert pull.call_count == 1
    assert ack.call_count == 1
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
    pull = respx.get(f"{WORKER}/newsletters").mock(return_value=httpx.Response(200, json=queued))
    ack = respx.post(f"{WORKER}/ack").mock(return_value=httpx.Response(200))
    source = CloudflareNewsletterSource(WORKER, "tok")
    articles = await source.fetch_articles(*_now(), sources=_source())
    assert len(articles) == 2
    assert articles[0].title == "Weekly #1"
    assert articles[1].title == "Weekly #2"
    assert pull.call_count == 1
    assert ack.call_count == 1
    assert json.loads(ack.calls.last.request.content) == {"ids": ["nl:good1", "nl:good2"]}


@pytest.mark.asyncio
@respx.mock
async def test_a_preview_pulls_without_acking():
    """A dry run must leave the queue where it found it.

    `run_digest` skips `store.save` when previewing, so an acked issue would be
    deleted from KV having been stored nowhere — the one loss no later run can
    undo.
    """
    item = {
        "id": "nl:abc",
        "from": "list@benedictevans.com",
        "subject": "Weekly",
        "html": "<p>no links here</p>",
        "text": "no links",
        "date": "2026-07-13T00:00:00Z",
    }
    pull = respx.get(f"{WORKER}/newsletters").mock(return_value=httpx.Response(200, json=[item]))
    ack = respx.post(f"{WORKER}/ack").mock(return_value=httpx.Response(200, json={"ok": True}))

    src = CloudflareNewsletterSource(WORKER, "tok", ack=False)
    after, before = _now()
    articles = await src.fetch_articles(after, before, _source())

    # The preview still sees what the run would fetch — only the delete is off.
    assert len(articles) == 1
    assert pull.call_count == 1
    assert not ack.called


def _mail(id_: str, sender: str, **extra) -> dict:
    return {
        "id": id_,
        "from": sender,
        "subject": f"Issue {id_}",
        "html": "",
        "text": f"This week's letter, {id_}.",
        "date": "2026-07-13T00:00:00Z",
        **extra,
    }


class _Creator:
    """Stands in for `D1SourceStore.insert`: records each source and answers `result`."""

    def __init__(self, result: bool = True) -> None:
        self.result = result
        self.made: list[SourceConfig] = []

    def __call__(self, source: SourceConfig) -> bool:
        self.made.append(source)
        return self.result


# The reader of these tests: mail from this address is a forward.
READER = own_address_keys("Me@Gmail.example")


async def _run(
    items: list[dict],
    sources: dict[str, SourceConfig],
    create,
    own: frozenset[str] = READER,
    now: datetime | None = None,
) -> tuple[list, list]:
    """Pull `items` at `now`, one pull and at most one ack; the articles and the ids acked."""
    at = now or _now()[0]
    with respx.mock:
        pull = respx.get(f"{WORKER}/newsletters").mock(return_value=httpx.Response(200, json=items))
        ack = respx.post(f"{WORKER}/ack").mock(return_value=httpx.Response(200, json={}))
        src = CloudflareNewsletterSource(
            WORKER, "tok", create_source=create, own_addresses=own, now=lambda: at
        )
        articles = await src.fetch_articles(*_now(), sources)
        assert pull.call_count == 1 and ack.call_count <= 1
        acked = json.loads(ack.calls.last.request.content)["ids"] if ack.called else []
    return articles, acked


async def test_an_unknown_sender_becomes_a_source_and_its_issue_an_article():
    create = _Creator()
    given = _source()

    articles, acked = await _run([_mail("nl:1", "Writer@Letters.example")], given, create)

    assert [(s.name, s.type, s.lane, s.email_match, s.review) for s in create.made] == [
        ("writer@letters.example", "newsletter", Lane.READ, "from:writer@letters.example", "new")
    ]
    assert [(a.title, a.source_name, a.source_lane) for a in articles] == [
        ("Issue nl:1", "writer@letters.example", Lane.READ)
    ]
    assert acked == ["nl:1"]
    assert list(given) == ["Benedict Evans"]


async def test_a_plus_tag_of_an_existing_sources_address_matches_it_and_creates_none():
    create = _Creator()

    articles, _ = await _run([_mail("nl:1", "list+ai-weekly@benedictevans.com")], _source(), create)

    assert create.made == []
    assert [a.source_name for a in articles] == ["Benedict Evans"]


async def test_two_issues_from_one_new_sender_make_one_source():
    create = _Creator()
    items = [
        _mail("nl:1", "lenny+how-i-ai@substack.example"),
        _mail("nl:2", "lenny@substack.example"),
    ]

    articles, acked = await _run(items, _source(), create)

    assert [s.name for s in create.made] == ["lenny@substack.example"]
    assert [a.source_name for a in articles] == ["lenny@substack.example"] * 2
    assert acked == ["nl:1", "nl:2"]


async def test_a_manual_forwards_source_is_keyed_by_the_forwarded_blocks_from():
    create = _Creator()
    text = (
        "---------- Forwarded message ---------\n"
        "From: Some Writer <Writer+tag@Letters.example>\n"
        "Subject: Issue 9\n\nBody"
    )
    item = _mail("nl:1", "me@gmail.example", subject="Fwd: Issue 9", text=text)

    articles, _ = await _run([item], _source(), create)

    assert [s.name for s in create.made] == ["writer@letters.example"]
    assert [a.source_name for a in articles] == ["writer@letters.example"]


async def test_a_forward_with_no_readable_block_makes_no_source_and_stays_queued():
    create = _Creator()
    item = _mail("nl:1", "me@gmail.example", subject="Fwd: Issue 9", text="no headers here")

    articles, acked = await _run([item, _mail("nl:2", "list@benedictevans.com")], _source(), create)

    assert create.made == []
    assert [a.source_name for a in articles] == ["Benedict Evans"]
    assert acked == ["nl:2"]


async def test_an_unknown_senders_reply_subject_still_makes_a_new_source_and_its_article():
    create = _Creator()
    item = _mail("nl:1", "writer@letters.example", subject="Re: your question about AI")

    articles, acked = await _run([item], _source(), create)

    assert [(s.name, s.review) for s in create.made] == [("writer@letters.example", "new")]
    assert [a.source_name for a in articles] == ["writer@letters.example"]
    assert acked == ["nl:1"]


async def test_a_known_sources_forwarded_reply_subject_is_read_too():
    create = _Creator()
    item = _mail("nl:1", "list@benedictevans.com", subject="Fwd: Re: your question")

    articles, acked = await _run([item], _source(), create)

    assert ([a.source_name for a in articles], create.made, acked) == (
        ["Benedict Evans"],
        [],
        ["nl:1"],
    )


async def test_no_more_sources_than_the_run_cap_are_made_and_the_rest_stay_queued():
    create = _Creator()
    count = MAX_NEW_SOURCES_PER_RUN + 2
    items = [_mail(f"nl:{i}", f"writer{i}@letters.example") for i in range(count)]

    articles, acked = await _run(items, _source(), create)

    assert len(create.made) == MAX_NEW_SOURCES_PER_RUN
    assert len(articles) == MAX_NEW_SOURCES_PER_RUN
    assert acked == [f"nl:{i}" for i in range(MAX_NEW_SOURCES_PER_RUN)]


async def _preview(items: list[dict], sources: dict[str, SourceConfig]) -> list:
    with respx.mock:
        pull = respx.get(f"{WORKER}/newsletters").mock(return_value=httpx.Response(200, json=items))
        ack = respx.post(f"{WORKER}/ack").mock(return_value=httpx.Response(200, json={}))
        source = CloudflareNewsletterSource(WORKER, "tok", ack=False, own_addresses=READER)
        articles = await source.fetch_articles(*_now(), sources)
        assert (pull.call_count, ack.call_count) == (1, 0)
    return articles


def _tagged_and_main() -> dict[str, SourceConfig]:
    return {
        "Lenny": SourceConfig(
            name="Lenny", lane=Lane.READ, email_match="from:lenny@substack.example"
        ),
        "How I AI": SourceConfig(
            name="How I AI", lane=Lane.READ, email_match="from:lenny+how-i-ai@substack.example"
        ),
    }


_ONE_OF_EACH = [
    _mail("nl:1", "lenny+how-i-ai@substack.example"),
    _mail("nl:2", "Lenny+Other@substack.example"),
    _mail("nl:3", "writer@letters.example"),
    _mail(
        "nl:4",
        "me@gmail.example",
        subject="Fwd: Issue 9",
        text="---------- Forwarded message ---------\nFrom: <someone@else.example>\n\nBody",
    ),
    _mail("nl:5", "forwarding-noreply@google.com", subject="(#1) Gmail Forwarding Confirmation"),
]


async def test_a_preview_files_each_mail_where_a_run_does_and_writes_nothing():
    create = _Creator()
    sources = _tagged_and_main()

    run, _ = await _run(_ONE_OF_EACH, dict(sources), create)
    preview = await _preview(_ONE_OF_EACH, dict(sources))

    assert [a.source_name for a in preview] == [a.source_name for a in run]
    assert [a.source_name for a in run] == [
        "How I AI",
        "Lenny",
        "writer@letters.example",
        "someone@else.example",
    ]
    assert [s.name for s in create.made] == ["writer@letters.example", "someone@else.example"]


async def test_an_exact_plus_tag_source_takes_its_mail_before_the_main_address():
    articles, _ = await _run(
        [_mail("nl:1", "lenny+how-i-ai@substack.example"), _mail("nl:2", "lenny@substack.example")],
        _tagged_and_main(),
        _Creator(),
    )

    assert [a.source_name for a in articles] == ["How I AI", "Lenny"]


async def test_a_source_that_names_the_readers_own_address_never_takes_a_forward():
    """A row made for the reader's address before it was known, even stopped, loses no forward."""
    create = _Creator()
    reader = {
        "me@gmail.example": SourceConfig(
            name="me@gmail.example",
            type="newsletter",
            lane=Lane.READ,
            email_match="from:me@gmail.example",
            review="declined",
        )
    }

    articles, acked = await _run([_ONE_OF_EACH[3]], reader, create)

    assert [s.name for s in create.made] == ["someone@else.example"]
    assert [a.source_name for a in articles] == ["someone@else.example"]
    assert acked == ["nl:4"]


async def test_mail_from_the_readers_address_is_a_forward_without_a_forward_subject():
    create = _Creator()
    text = "Look.\n---------- Forwarded message ---------\nFrom: <writer@letters.example>\n\nBody"

    articles, _ = await _run([_mail("nl:1", "me@gmail.example", text=text)], _source(), create)

    assert [s.name for s in create.made] == ["writer@letters.example"]
    assert [a.source_name for a in articles] == ["writer@letters.example"]


@pytest.mark.parametrize(
    "sender", ["ME@gmail.example", "me+cyris@gmail.example", "Me <me+news@Gmail.example>"]
)
async def test_the_readers_address_is_known_in_any_case_and_any_plus_tag(sender):
    create = _Creator()
    text = "---------- Forwarded message ---------\nFrom: <writer@letters.example>\n\nBody"

    await _run([_mail("nl:1", sender, text=text)], _source(), create)

    assert [s.name for s in create.made] == ["writer@letters.example"]


async def test_a_forward_whose_only_sender_is_the_reader_makes_no_source_and_stays_queued():
    create = _Creator()
    text = "Forwarded message\nFrom: Me <me+x@gmail.example>\nDate: today\n\nA note to self"

    articles, acked = await _run([_mail("nl:1", "me@gmail.example", text=text)], _source(), create)

    assert (articles, create.made, acked) == ([], [], [])


async def test_without_the_readers_address_a_forward_is_filed_under_its_from():
    create = _Creator()

    articles, _ = await _run([_ONE_OF_EACH[3]], _source(), create, own=frozenset())

    assert [s.name for s in create.made] == ["me@gmail.example"]
    assert [a.source_name for a in articles] == ["me@gmail.example"]


async def test_an_unknown_senders_mail_that_quotes_a_forward_is_filed_under_its_from():
    create = _Creator()
    text = "---------- Forwarded message ---------\nFrom: <friend@else.example>\nDate: x\n\nHi"

    item = _mail("nl:1", "writer@letters.example", text=text)

    articles, _ = await _run([item], _source(), create)

    assert [s.name for s in create.made] == ["writer@letters.example"]
    assert [a.source_name for a in articles] == ["writer@letters.example"]


async def test_a_forwards_article_starts_at_the_original_message():
    text = (
        "Have a look at this one.\n\n"
        "---------- Forwarded message ---------\n"
        "From: Paper Moon <editor@paper-moon.example>\n"
        "Date: Wed, Nov 25, 2026 at 9:31 AM\n"
        "Subject: Issue 12\n"
        "To: <me@gmail.example>\n\n"
        "This week the moon is paper."
    )
    item = _mail("nl:1", "me@gmail.example", subject="Fwd: Issue 12", text=text)

    articles, _ = await _run([item], _source(), _Creator())

    assert [(a.source_name, a.title, a.content) for a in articles] == [
        ("editor@paper-moon.example", "Issue 12", "This week the moon is paper.")
    ]


async def test_a_pull_names_each_forwards_article_url_and_no_other():
    text = "---------- Forwarded message ---------\nFrom: <editor@paper-moon.example>\n\nHi"
    items = [
        _mail("nl:1", "me@gmail.example", text=text),
        _mail("nl:2", "writer@letters.example"),
    ]
    with respx.mock:
        pull = respx.get(f"{WORKER}/newsletters").mock(return_value=httpx.Response(200, json=items))
        ack = respx.post(f"{WORKER}/ack").mock(return_value=httpx.Response(200, json={}))
        src = CloudflareNewsletterSource(
            WORKER, "tok", create_source=_Creator(), own_addresses=READER
        )
        articles = await src.fetch_articles(*_now(), _source())

    assert pull.call_count == 1 and ack.call_count == 1
    by_source = {a.source_name: a.url for a in articles}
    assert src.forwarded == {by_source["editor@paper-moon.example"]}


# A forward's quoted headers as each client writes them, by language.
_BLOCKS = {
    "gmail en": "---------- Forwarded message ---------\nFrom: W <w@x.example>\nDate: Wed\n",
    "gmail de": "---------- Forwarded message ---------\nVon: W <w@x.example>\nDate: Mi.\n",
    "gmail fr": "---------- Forwarded message ---------\nDe : W <w@x.example>\nDate: mer.\n",
    "gmail zh-Hant": "---------- 已轉寄郵件 ----------\n寄件者： W <w@x.example>\n日期：週三\n",
    "gmail zh-Hans": "---------- 转发的邮件 ---------\n发件人： W <w@x.example>\n",
    "apple en": "> Begin forwarded message:\n>\n> From: W <w@x.example>\n> Subject: I\n",
    "apple de": "> Anfang der weitergeleiteten Nachricht:\n>\n> Von: W <w@x.example>\n",
    "apple fr": "> Début du message réexpédié :\n>\n> De: W <w@x.example>\n> Objet: I\n",
    "apple es": "> Inicio del mensaje reenviado:\n>\n> De: W <w@x.example>\n",
    "apple zh-Hant": (
        '> 開始轉寄郵件：\n>\n> 寄件人: "W" <w@x.example>\n> 標題: I\n'
        "> 日期: 2026年11月25日 GMT+8 13:31:20\n"
        "> 收件人: <me@gmail.example>\n> 回覆給: W <w@x.example>\n"
    ),
    "apple ja": "転送されたメッセージ:\n\n差出人: W <w@x.example>\n件名: I\n",
    "outlook en": "________________________________\nFrom: W <w@x.example>\nSent: Wednesday\n",
    "outlook de": "Von: W <w@x.example>\nDatum: Donnerstag\nAn: me@gmail.example\n",
    "outlook fr": "De : W <w@x.example>\nDate : jeudi\nÀ : me@gmail.example\n",
    "outlook es": "De: W <w@x.example>\nFecha: jueves\nPara: me@gmail.example\n",
    "outlook ja": "差出人: W <w@x.example>\n送信日時: 2026年11月25日 9:31\n宛先: me\n",
    "outlook zh-Hans": "发件人: W <w@x.example>\n发送时间: 2026年11月25日\n收件人: me\n",
    "outlook zh-Hant": "寄件者: W <w@x.example>\n寄件日期: 2026年11月25日\n收件者: me\n",
}


@pytest.mark.parametrize("block", _BLOCKS.values(), ids=_BLOCKS)
def test_a_forwards_sender_is_read_in_each_listed_language(block):
    body = f"A note from me.\n\n{block}\nThe original letter."

    assert read_forward(body, READER) == ("w@x.example", "The original letter.")


def test_a_from_line_with_no_block_around_it_is_no_forward():
    assert read_forward("Thanks!\nFrom: a friend, w@x.example\n\nSee you.", READER) is None


async def test_an_html_only_forward_is_read_for_its_sender():
    html = (
        "<div>Look</div><div>---------- Forwarded message ---------<br>"
        "From: <strong>W</strong> <span>&lt;w@x.example&gt;</span><br>Date: Wed<br></div>"
        "<p>Original</p>"
    )
    item = _mail("nl:1", "me@gmail.example", text="", html=html)

    articles, _ = await _run([item], _source(), _Creator())

    assert [a.source_name for a in articles] == ["w@x.example"]


async def test_a_system_sender_makes_no_source():
    create = _Creator()

    articles, acked = await _run([_ONE_OF_EACH[4]], _source(), create)

    assert create.made == []
    assert articles == []
    assert acked == ["nl:5"]


async def test_a_creator_that_writes_nothing_keeps_the_mail_queued():
    create = _Creator(result=False)

    articles, acked = await _run([_mail("nl:1", "writer@letters.example")], _source(), create)

    assert len(create.made) == 1
    assert articles == []
    assert acked == []


async def test_a_creator_that_raises_keeps_the_mail_queued_and_the_batch_whole():
    def broken(_source: SourceConfig) -> bool:
        raise RuntimeError("store down")

    items = [_mail("nl:1", "writer@letters.example"), _mail("nl:2", "list@benedictevans.com")]

    articles, acked = await _run(items, _source(), broken)

    assert [a.source_name for a in articles] == ["Benedict Evans"]
    assert acked == ["nl:2"]


async def test_a_sender_whose_address_ends_another_sources_gets_its_own_source():
    create = _Creator()
    sources = {
        "Tech": SourceConfig(name="Tech", lane=Lane.READ, email_match="from:technews@x.example")
    }

    articles, _ = await _run([_mail("nl:1", "news@x.example")], sources, create)

    assert [s.name for s in create.made] == ["news@x.example"]
    assert [a.source_name for a in articles] == ["news@x.example"]


async def test_a_new_sender_is_not_filed_under_a_source_its_footer_names():
    create = _Creator()
    sources = {"News": SourceConfig(name="News", lane=Lane.READ, email_match="from:news@x.example")}
    item = _mail("nl:1", "technews@x.example", text="Write to news@x.example for tips.")

    articles, _ = await _run([item], sources, create)

    assert [s.name for s in create.made] == ["technews@x.example"]
    assert [a.source_name for a in articles] == ["technews@x.example"]


async def test_an_existing_sources_own_mail_stays_with_it_whatever_its_body_quotes():
    create = _Creator()
    text = "Forwarded message\nFrom: friend@else.example\n\nA reader's letter, quoted."
    item = _mail("nl:1", "list@benedictevans.com", text=text)

    articles, _ = await _run([item], _source(), create)

    assert create.made == []
    assert [a.source_name for a in articles] == ["Benedict Evans"]


async def test_a_sentence_that_mentions_a_forwarded_message_is_no_forward():
    create = _Creator()
    text = "Got this in a forwarded message from a friend?\nFrom: friend@else.example\nSubscribe."
    item = _mail("nl:1", "writer@letters.example", text=text)

    articles, _ = await _run([item], _source(), create)

    assert [s.name for s in create.made] == ["writer@letters.example"]
    assert [a.source_name for a in articles] == ["writer@letters.example"]


async def test_a_sender_whose_name_another_source_holds_is_kept_queued():
    create = _Creator()
    taken = {
        "news@letter.example": SourceConfig(
            name="news@letter.example",
            type="newsletter",
            lane=Lane.READ,
            email_match="from:editor@letter.example",
        )
    }

    articles, acked = await _run([_mail("nl:1", "news@letter.example")], taken, create)

    assert (articles, create.made, acked) == ([], [], [])


async def test_mail_no_run_can_file_is_kept_for_its_hold_from_arrival_then_acked():
    """An own-address mail with no readable block would otherwise be read again forever."""
    arrived = datetime(2026, 7, 13, 6, tzinfo=UTC)
    note = {
        "subject": "look",
        "text": "just a link https://x.example",
        "date": "2026-07-01T00:00:00Z",
    }
    item = _mail("nl:1", "me@gmail.example", received_at=arrived.isoformat(), **note)
    young = arrived + timedelta(days=UNMATCHED_HOLD_DAYS) - timedelta(minutes=1)
    old = arrived + timedelta(days=UNMATCHED_HOLD_DAYS)

    _, kept = await _run([item], _source(), _Creator(), now=young)
    articles, acked = await _run([item], _source(), _Creator(), now=old)

    assert (kept, articles, acked) == ([], [], ["nl:1"])


async def test_mail_queued_before_the_worker_stamped_its_arrival_is_held_from_its_date():
    capped = [
        _mail(f"nl:{i}", f"writer{i}@letters.example") for i in range(MAX_NEW_SOURCES_PER_RUN)
    ]
    late = _mail("nl:late", "late@letters.example")
    dated = datetime.fromisoformat(late["date"])

    _, kept = await _run([*capped, late], _source(), _Creator(), now=dated + timedelta(days=1))
    _, acked = await _run(
        [*capped, late], _source(), _Creator(), now=dated + timedelta(days=UNMATCHED_HOLD_DAYS)
    )

    assert "nl:late" not in kept
    assert "nl:late" in acked


@pytest.mark.parametrize("when", [{}, {"date": ""}, {"date": "not a date"}])
async def test_mail_with_no_time_to_count_its_hold_from_stays_queued(when):
    item = {**_mail("nl:1", "me@gmail.example", text="no block here"), **when}
    if "date" not in when:
        del item["date"]

    articles, acked = await _run(
        [item], _source(), _Creator(), now=datetime(2030, 1, 1, tzinfo=UTC)
    )

    assert (articles, acked) == ([], [])


async def test_a_local_part_with_an_equals_sign_is_kept_whole():
    create = _Creator()

    await _run([_mail("nl:1", "bounce=abc@m.example")], _source(), create)

    assert [s.name for s in create.made] == ["bounce=abc@m.example"]


async def test_mail_from_a_declined_source_is_acked_unread_and_makes_no_source():
    create = _Creator()
    declined = {
        "writer@letters.example": SourceConfig(
            name="writer@letters.example",
            type="newsletter",
            lane=Lane.READ,
            email_match="from:writer@letters.example",
            review="declined",
        )
    }
    items = [_mail("nl:1", "writer+tag@letters.example"), _mail("nl:2", "writer@letters.example")]

    articles, acked = await _run(items, declined, create)

    assert create.made == []
    assert articles == []
    assert acked == ["nl:1", "nl:2"]


async def test_a_new_source_still_takes_its_mail():
    new = {
        "w": SourceConfig(name="w", lane=Lane.READ, email_match="from:w@x.example", review="new")
    }

    articles, _ = await _run([_mail("nl:1", "w@x.example")], new, None)

    assert [a.source_name for a in articles] == ["w"]
