"""/settings adds, edits and removes tracked topics, and an empty list asks for nothing."""

import pytest
from aiohttp.test_utils import TestClient, TestServer
from fakes import SqliteD1

from cyris.adapters.store.tracked_topics import D1TrackedTopicStore
from cyris.domain.models import TrackedTopic
from cyris.entrypoints.triage_server import TriageServer

pytestmark = pytest.mark.integration

TOPIC = {
    "name": "Anthropic",
    "description": "The company that makes Claude",
    "threshold": 0.72,
    "model": "gemini-embedding-001",
}


async def _client(server: TriageServer) -> TestClient:
    client = TestClient(TestServer(server._app))
    await client.start_server()
    return client


@pytest.fixture
async def bare() -> TestClient:
    client = await _client(TriageServer())
    yield client
    await client.close()


@pytest.fixture
def store() -> D1TrackedTopicStore:
    return D1TrackedTopicStore(SqliteD1())


@pytest.fixture
async def client(store: D1TrackedTopicStore) -> TestClient:
    values = {"vote_similarity.provider": "gemini", "vote_similarity.model": ""}
    client = await _client(TriageServer(values=values, topic_store=store))
    yield client
    await client.close()


async def test_no_topics_wired_is_an_empty_list_not_an_error(bare: TestClient) -> None:
    data = await (await bare.get("/api/topics")).json()

    assert data == {"writable": False, "topics": [], "embedding_model": ""}


async def test_no_topic_is_never_a_missing_setting(bare: TestClient) -> None:
    data = await (await bare.get("/api/settings")).json()

    assert not [key for key in data["missing"] if "topic" in key]


async def test_a_json_deployment_shows_its_file_topics_and_refuses_writes() -> None:
    server = TriageServer(tracked_topics=[TrackedTopic(**TOPIC)])
    client = await _client(server)
    try:
        data = await (await client.get("/api/topics")).json()
        post = await client.post("/api/topics", json=TOPIC)
        delete = await client.delete("/api/topics/Anthropic")
    finally:
        await client.close()

    assert data["writable"] is False
    assert data["topics"] == [TOPIC]
    assert (post.status, delete.status) == (409, 409)


async def test_the_model_a_new_topic_starts_from_is_the_resolved_one(client: TestClient) -> None:
    data = await (await client.get("/api/topics")).json()

    assert data == {"writable": True, "topics": [], "embedding_model": "gemini-embedding-001"}


async def test_a_saved_topic_is_listed_and_stored(client, store) -> None:
    resp = await client.post("/api/topics", json=TOPIC)

    assert resp.status == 200
    assert (await resp.json())["name"] == "Anthropic"
    assert store.list_topics() == [TrackedTopic(**TOPIC)]
    assert (await (await client.get("/api/topics")).json())["topics"] == [TOPIC]


async def test_saving_a_name_again_edits_that_topic(client, store) -> None:
    await client.post("/api/topics", json=TOPIC)
    await client.post("/api/topics", json={**TOPIC, "threshold": 0.8})

    assert [t.threshold for t in store.list_topics()] == [0.8]


async def test_removing_a_topic_deletes_its_row(client, store) -> None:
    await client.post("/api/topics", json=TOPIC)
    await client.post("/api/topics", json={**TOPIC, "name": "Other"})

    resp = await client.delete("/api/topics/Anthropic")

    assert resp.status == 200
    assert [t.name for t in store.list_topics()] == ["Other"]


async def test_removing_a_topic_that_is_not_there_says_so(client) -> None:
    resp = await client.delete("/api/topics/Nothing")

    assert resp.status == 404
    assert (await resp.json())["ok"] is False


@pytest.mark.parametrize(
    "field,value",
    [("name", " "), ("description", ""), ("threshold", 1.2), ("threshold", 0), ("model", "")],
)
async def test_a_field_out_of_its_rule_is_a_400_naming_it(client, store, field, value) -> None:
    resp = await client.post("/api/topics", json={**TOPIC, field: value})

    body = await resp.json()
    assert resp.status == 400
    assert body["field"] == field
    assert body["ok"] is False and body["error"]
    assert store.list_topics() == []


async def test_a_missing_threshold_is_refused_not_defaulted(client, store) -> None:
    resp = await client.post(
        "/api/topics", json={k: v for k, v in TOPIC.items() if k != "threshold"}
    )

    assert resp.status == 400
    assert (await resp.json())["field"] == "threshold"
    assert store.list_topics() == []
