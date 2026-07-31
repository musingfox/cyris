"""Tests for email webhook server."""

from unittest.mock import AsyncMock, patch

import pytest

from cyris.domain.models import SourceConfig, Tier
from cyris.entrypoints.webhook_server import EmailWebhookServer


@pytest.fixture
def sources():
    return {
        "Benedict Evans": SourceConfig(
            name="Benedict Evans Newsletter",
            tier=Tier.SUMMARIZE,
            tags=["tech"],
            email_match="from:list@benedictevans.com",
        )
    }


@pytest.fixture
def server_and_callback(sources):
    callback = AsyncMock()
    server = EmailWebhookServer(
        host="127.0.0.1",
        port=0,
        path="/webhook/email",
        webhook_secret="test-secret",
        sources=sources,
        on_received=callback,
    )
    return server, callback


@pytest.mark.asyncio
async def test_valid_request(server_and_callback):
    server, callback = server_and_callback

    from aiohttp import test_utils

    client = test_utils.TestClient(test_utils.TestServer(server._app))
    await client.start_server()

    payload = {
        "from": "list@benedictevans.com",
        "subject": "Newsletter #1",
        "html": "<p>Hello</p>",
        "text": "Hello",
        "headers": {"Date": "Tue, 18 Mar 2026 08:00:00 +0800"},
    }

    with patch(
        "cyris.entrypoints.webhook_server.newsletter_article",
        return_value=None,
    ):
        resp = await client.post(
            "/webhook/email", json=payload, headers={"X-Webhook-Secret": "test-secret"}
        )

    assert resp.status == 200
    await client.close()


@pytest.mark.asyncio
async def test_invalid_secret(server_and_callback):
    server, _ = server_and_callback

    from aiohttp import test_utils

    client = test_utils.TestClient(test_utils.TestServer(server._app))
    await client.start_server()

    resp = await client.post("/webhook/email", json={}, headers={"X-Webhook-Secret": "wrong"})
    assert resp.status == 401
    await client.close()


@pytest.mark.asyncio
async def test_unknown_sender(server_and_callback):
    server, _ = server_and_callback

    from aiohttp import test_utils

    client = test_utils.TestClient(test_utils.TestServer(server._app))
    await client.start_server()

    resp = await client.post(
        "/webhook/email",
        json={
            "from": "unknown@example.com",
            "subject": "Test",
            "html": "",
            "text": "",
            "headers": {},
        },
        headers={"X-Webhook-Secret": "test-secret"},
    )
    assert resp.status == 404
    await client.close()
