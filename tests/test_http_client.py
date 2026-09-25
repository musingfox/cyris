"""Tests for HTTP client."""

import httpx
import pytest
import respx

from cyris.adapters.http_client import HttpClient


@pytest.mark.asyncio
async def test_get_success():
    async with respx.mock:
        respx.get("https://example.com").mock(return_value=httpx.Response(200, text="OK"))
        async with HttpClient() as client:
            resp = await client.get("https://example.com")
        assert resp.status_code == 200
