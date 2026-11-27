"""Tests for HTTP client."""

import httpx
import pytest
import respx

from cyris.adapters.http_client import HttpClient

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_get_follows_a_redirect_to_the_page():
    async with respx.mock:
        moved = respx.get("https://example.com/old").mock(
            return_value=httpx.Response(301, headers={"Location": "https://example.com/new"})
        )
        page = respx.get("https://example.com/new").mock(
            return_value=httpx.Response(200, text="OK")
        )
        async with HttpClient() as client:
            resp = await client.get("https://example.com/old")
        assert (moved.call_count, page.call_count) == (1, 1)
        assert (resp.status_code, resp.text) == (200, "OK")
