"""A local `cyris triage-ui` takes the pages' votes into its SQLite file, as the cloud edition does.

The server is built as `cyris triage-ui` builds it, over a real SQLite file (`LocalD1Client`,
the schema applied as a first boot applies it), and each request goes through the
application's own dispatch, the Host and Origin check included, with no socket, as
`tests/test_local_pages.py` serves the pages. Every row is read back through a connection of
its own to the file, as the next `cyris run` opens it. The request and answer cases are the
ones `workers/app/test/vote_contract.test.js` holds the app Worker to. ADR-0044.
"""

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.streams import StreamReader
from aiohttp.test_utils import make_mocked_request
from multidict import CIMultiDict

from cyris.adapters.promotions import sync_promotions
from cyris.adapters.store.d1 import apply_schema
from cyris.adapters.store.d1_store import D1ArticleStore
from cyris.adapters.store.local_d1 import LocalD1Client
from cyris.adapters.store.topics import D1TopicStore
from cyris.domain.models import Article, Lane, TopicRecord
from cyris.domain.similarity import normalize
from cyris.entrypoints.triage_server import TriageServer
from cyris.service_layer.vote_similarity import judge_by_votes

pytestmark = pytest.mark.integration

ROOT = Path(__file__).parents[1]
CONTRACT = json.loads((ROOT / "workers/app/test/vote_cases.json").read_text(encoding="utf-8"))
STORED = CONTRACT["stored"]
OWN = "127.0.0.1:8766"
DATE = "2026-11-24"
PERIOD = "morning"


def _database(path: Path) -> LocalD1Client:
    client = LocalD1Client(path)
    apply_schema(client)
    return client


@pytest.fixture
def db(tmp_path: Path) -> tuple[LocalD1Client, Path]:
    path = tmp_path / "cyris.sqlite3"
    return _database(path), path


def _save(client: LocalD1Client, titles: dict[str, str]) -> None:
    D1ArticleStore(client).save(
        [
            Article(
                id=url,
                title=title,
                url=url,
                content="Some text.",
                published_at=datetime.now(UTC) - timedelta(hours=1),
                source_name="A feed",
                source_lane=Lane.SKIM,
            )
            for url, title in titles.items()
        ]
    )


def _server(client: LocalD1Client, **kwargs) -> TriageServer:
    """The stores `cyris triage-ui` hands its server, on one file."""
    kwargs.setdefault("store_is_local", True)
    kwargs.setdefault("issue_topics", D1TopicStore(client))
    return TriageServer(article_store=D1ArticleStore(client), **kwargs)


async def _send(
    server: TriageServer, method: str, body: object = None, **headers: str
) -> web.StreamResponse:
    """One request, as a page sends it: a probe with no body, a vote with its body as JSON."""
    app = server._app
    if not app.frozen:
        app.freeze()
    payload = StreamReader(mock.Mock(_reading_paused=False), 2**16, loop=asyncio.get_running_loop())
    if method not in ("GET", "HEAD"):
        payload.feed_data(json.dumps(body).encode())
    payload.feed_eof()
    named = CIMultiDict({"Host": OWN, "Content-Type": "application/json"})
    named.update((name.replace("_", "-"), value) for name, value in headers.items())
    request = make_mocked_request(method, "/api/vote", headers=named, payload=payload, app=app)
    try:
        return await app._handle(request)
    except web.HTTPException as refusal:
        return refusal


async def _vote(server: TriageServer, url: str, vote: str, **headers: str) -> web.StreamResponse:
    return await _send(server, "POST", {"url": url, "vote": vote, "digest_date": DATE}, **headers)


def _read(path: Path, sql: str, params: tuple = ()) -> list[dict]:
    """Rows of the file, read through a connection of their own."""
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute(sql, params)]
    finally:
        connection.close()


def _row(path: Path, url: str) -> dict:
    [row] = _read(
        path,
        "SELECT state, rejection_reason, triaged_at FROM stored_articles WHERE url = ?",
        (url,),
    )
    return row


class TestTheSharedContract:
    @pytest.mark.parametrize("case", CONTRACT["cases"], ids=lambda c: c["case"])
    async def test_each_case_is_answered_and_applied_as_the_cloud_edition_does(self, db, case):
        client, path = db
        _save(client, {STORED: "A stored article"})

        response = await _send(_server(client), case["method"], case["body"])

        assert response.status == case["status"]
        if case["answer"] is not None:
            assert response.content_type == "application/json"
            assert json.loads(response.text) == case["answer"]
        stored = _row(path, STORED)
        if case["verdict"] is not None and case["body"]["url"] == STORED:
            assert stored["state"] == case["verdict"]
            assert stored["triaged_at"] is not None
        else:
            assert (stored["state"], stored["triaged_at"]) == ("pending", None)
        # The cloud's sync stamps an article the store lacks with nothing, and so does this.
        assert [r["url"] for r in _read(path, "SELECT url FROM stored_articles")] == [STORED]


class TestAVoteOnTheFile:
    async def test_an_upvote_stamps_its_row_as_a_human_decision(self, db):
        client, path = db
        _save(client, {STORED: "A stored article", "https://example.com/other": "Another"})
        before = datetime.now(UTC)

        response = await _vote(_server(client), STORED, "up")

        assert (response.status, json.loads(response.text)) == (200, {"ok": True})
        row = _row(path, STORED)
        assert (row["state"], row["rejection_reason"]) == ("accepted", None)
        assert before <= datetime.fromisoformat(row["triaged_at"]) <= datetime.now(UTC)
        other = _row(path, "https://example.com/other")
        assert (other["state"], other["triaged_at"]) == ("pending", None)

    async def test_a_downvote_rejects_it_as_not_interested(self, db):
        client, path = db
        _save(client, {STORED: "A stored article"})

        await _vote(_server(client), STORED, "down")

        row = _row(path, STORED)
        assert (row["state"], row["rejection_reason"]) == ("rejected", "not_interested")
        assert row["triaged_at"] is not None

    async def test_a_change_of_mind_ends_on_the_later_vote(self, db):
        client, path = db
        _save(client, {STORED: "A stored article"})
        server = _server(client)

        await _vote(server, STORED, "up")
        await _vote(server, STORED, "down")

        assert _row(path, STORED)["state"] == "rejected"

    async def test_a_sand_blocks_click_stamps_each_member_and_votes_its_topic_once(self, db):
        """The page posts once per member URL; the topic takes one vote (`ONE_CLICK_WINDOW`)."""
        client, path = db
        members = {f"https://example.com/m{i}": f"Member {i}" for i in range(3)}
        _save(client, members)
        D1TopicStore(client).save_topics(
            DATE,
            PERIOD,
            [TopicRecord(id="t-sand", name="Sand", urls=list(members))],
            datetime.now(UTC),
        )
        server = _server(client)

        answers = [await _vote(server, url, "up") for url in members]

        assert [a.status for a in answers] == [200, 200, 200]
        assert {_row(path, url)["state"] for url in members} == {"accepted"}
        votes = _read(path, "SELECT topic_id, direction FROM topic_votes")
        assert votes == [{"topic_id": "t-sand", "direction": 1}]

    async def test_a_url_no_topic_holds_is_stamped_and_writes_no_topic_vote(self, db):
        client, path = db
        _save(client, {STORED: "A raw-page article"})

        await _vote(_server(client), STORED, "up")

        assert _row(path, STORED)["state"] == "accepted"
        assert _read(path, "SELECT * FROM topic_votes") == []


class TestWhoMayVote:
    @pytest.mark.parametrize(
        "headers",
        [
            {"origin": "https://evil.example"},
            {"sec_fetch_site": "cross-site"},
            {"host": "evil.example"},
        ],
    )
    async def test_a_write_another_page_sends_is_refused_and_stamps_nothing(self, db, headers):
        client, path = db
        _save(client, {STORED: "A stored article"})

        response = await _vote(_server(client), STORED, "up", **headers)

        assert response.status == 403
        assert _row(path, STORED)["triaged_at"] is None

    async def test_a_page_of_this_server_may_vote(self, db):
        client, path = db
        _save(client, {STORED: "A stored article"})

        response = await _vote(
            _server(client), STORED, "up", origin=f"http://{OWN}", sec_fetch_site="same-origin"
        )

        assert response.status == 200
        assert _row(path, STORED)["state"] == "accepted"

    async def test_behind_the_app_worker_the_worker_takes_the_vote_not_this_server(self, db):
        client, path = db
        _save(client, {STORED: "A stored article"})
        server = _server(client, behind_app_worker=True)

        response = await _vote(server, STORED, "up")
        probe = await _send(server, "GET")

        assert response.status == 405
        assert json.loads(probe.text) == {"authorized": True, "votes": False}
        assert _row(path, STORED)["triaged_at"] is None

    async def test_a_server_on_d1_answers_that_it_takes_no_vote(self, db):
        # The cloud's queue takes a D1 store's votes; one written here would block the loop
        # on D1's round trips and lose to an older queued vote the next sync stamps later.
        client, path = db
        _save(client, {STORED: "A stored article"})
        server = _server(client, store_is_local=False)

        response = await _vote(server, STORED, "up")
        probe = await _send(server, "GET")

        assert response.status == 405
        assert json.loads(probe.text) == {"authorized": True, "votes": False}
        assert _row(path, STORED)["triaged_at"] is None

    async def test_a_server_with_no_store_answers_that_it_takes_no_vote(self):
        server = TriageServer(store_is_local=True)

        assert (await _vote(server, STORED, "up")).status == 405
        assert json.loads((await _send(server, "GET")).text) == {"authorized": True, "votes": False}

    async def test_a_failed_write_answers_500_naming_it(self, db):
        client, path = db
        _save(client, {STORED: "A stored article"})
        store = D1ArticleStore(client)
        store.record_verdicts = MagicMock(side_effect=RuntimeError("database is locked"))

        response = await _vote(TriageServer(article_store=store, store_is_local=True), STORED, "up")

        assert response.status == 500
        assert json.loads(response.text) == {"error": "database is locked"}
        assert _row(path, STORED)["triaged_at"] is None

    async def test_a_failed_topic_vote_write_answers_500_naming_it(self, db):
        client, _ = db
        _save(client, {STORED: "A stored article"})
        topics = D1TopicStore(client)
        topics.save_topics(
            DATE, PERIOD, [TopicRecord(id="t-a", name="A", urls=[STORED])], datetime.now(UTC)
        )
        topics.record_topic_votes = MagicMock(side_effect=RuntimeError("database is locked"))

        response = await _vote(_server(client, issue_topics=topics), STORED, "up")

        assert response.status == 500
        assert json.loads(response.text) == {
            "error": "topic store failed: RuntimeError: database is locked"
        }


# The next run's vote similarity, embedding each title to a fixed direction: the reader liked
# a compiler release and disliked a lottery story, and one of the new candidates is another
# lottery story among ten unlike it.
LIKED = "https://example.com/liked"
DISLIKED = "https://example.com/disliked"
LOTTERY_AGAIN = "https://example.com/lottery-again"
VOTED = {LIKED: "Compiler release", DISLIKED: "Lottery jackpot"}
CANDIDATES = {
    LOTTERY_AGAIN: "Lottery again",
    **{f"https://example.com/c{i}": f"Candidate {i}" for i in range(10)},
}
VECTORS = {
    "Compiler release": [1.0, 0.0, 0.0],
    "Lottery jackpot": [0.0, 1.0, 0.0],
    "Lottery again": [0.0, 1.0, 0.05],
    **{f"Candidate {i}": [1.0, 0.02 * i, 0.3] for i in range(10)},
}
CLICKS = [(LIKED, "up"), (DISLIKED, "down")]


class _TitleEmbedder:
    def __init__(self) -> None:
        self.usage = SimpleNamespace(as_dict=lambda: {})

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [normalize(VECTORS[t]) for t in texts]


def _seeded(path: Path) -> LocalD1Client:
    """A store holding the voted articles under topics of one issue, and the new candidates."""
    client = _database(path)
    _save(client, {**VOTED, **CANDIDATES})
    D1TopicStore(client).save_topics(
        DATE,
        PERIOD,
        [
            TopicRecord(id="t-liked", name="Compilers", urls=[LIKED]),
            TopicRecord(id="t-disliked", name="Lottery", urls=[DISLIKED]),
        ],
        datetime.now(UTC),
    )
    return client


def _promote_worker_answer(json_data) -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = json_data
    resp.raise_for_status.return_value = None
    return resp


async def _next_run(path: Path):
    """What the next run's vote similarity makes of the file, opened afresh."""
    client = LocalD1Client(path)
    store = D1ArticleStore(client)
    return await judge_by_votes(
        store,
        _TitleEmbedder(),
        store.get_by_urls(list(CANDIDATES)),
        max_seeds=20,
        topics=D1TopicStore(client),
    )


async def test_local_votes_seed_the_next_run_as_the_cloud_editions_synced_votes_do(tmp_path):
    """The same clicks, taken by this server and by the cloud's queue and `promote-sync`.

    D1 is SQLite and both stores speak it through `D1Queryable`, so the cloud edition's side is
    the real `sync_promotions` over a file of its own, given what the promote Worker queues for
    each click (`workers/promote/src/index.js`). Both files must then hold the same verdict
    columns and topic votes, and the next run's similarity must read the same seeds from them.
    """
    local_path, cloud_path = tmp_path / "local.sqlite3", tmp_path / "cloud.sqlite3"
    server = _server(_seeded(local_path))
    cloud = _seeded(cloud_path)

    for url, vote in CLICKS:
        assert (await _vote(server, url, vote)).status == 200
    queued = [
        {"url": url, "vote": vote, "digest_date": DATE, "ts": datetime.now(UTC).isoformat()}
        for url, vote in CLICKS
    ]
    with (
        patch("cyris.adapters.promotions.httpx.get", return_value=_promote_worker_answer(queued)),
        patch("cyris.adapters.promotions.httpx.post", return_value=_promote_worker_answer({})),
    ):
        sync_promotions(
            "https://promote.example", "tok", D1ArticleStore(cloud), D1TopicStore(cloud)
        )

    verdicts = (
        "SELECT url, state, rejection_reason, triaged_at IS NOT NULL AS stamped "
        "FROM stored_articles ORDER BY url"
    )
    topic_votes = "SELECT topic_id, direction FROM topic_votes ORDER BY topic_id"
    assert _read(local_path, verdicts) == _read(cloud_path, verdicts)
    assert (
        _read(local_path, topic_votes)
        == _read(cloud_path, topic_votes)
        == [
            {"topic_id": "t-disliked", "direction": -1},
            {"topic_id": "t-liked", "direction": 1},
        ]
    )

    local, synced = await _next_run(local_path), await _next_run(cloud_path)

    assert local.ran and synced.ran
    assert (
        (local.upvote_seeds, local.downvote_seeds)
        == (synced.upvote_seeds, synced.downvote_seeds)
        == (1, 1)
    )
    assert local.seed_titles == synced.seed_titles == VOTED
    assert local.suppressed_urls == synced.suppressed_urls == [LOTTERY_AGAIN]
