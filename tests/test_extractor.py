"""Tests for full-text extraction."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cyris.adapters.fetch.extractor import extract_full_text
from cyris.adapters.http_client import HttpClient


@pytest.mark.asyncio
async def test_successful_extraction():
    mock_client = AsyncMock(spec=HttpClient)
    mock_response = MagicMock()
    mock_response.text = (
        "<html><body><article><p>Full article text here.</p></article></body></html>"
    )
    mock_response.status_code = 200
    mock_client.get.return_value = mock_response

    with (
        patch("trafilatura.extract") as mock_extract,
        patch("trafilatura.extract_metadata") as mock_extract_metadata,
    ):
        mock_extract.return_value = "Full article text here."
        mock_metadata = MagicMock()
        mock_metadata.title = "Article Title"
        mock_metadata.author = "Author"
        mock_metadata.date = None
        mock_extract_metadata.return_value = mock_metadata

        result = await extract_full_text("https://example.com/article", mock_client)

    assert result.content == "Full article text here."
    assert result.title == "Article Title"


@pytest.mark.asyncio
async def test_404_returns_empty():
    mock_client = AsyncMock(spec=HttpClient)
    mock_response = MagicMock()
    mock_response.text = "Not Found"
    mock_response.status_code = 404
    mock_client.get.return_value = mock_response

    result = await extract_full_text("https://example.com/missing", mock_client)
    assert result.content == ""


@pytest.mark.asyncio
async def test_paywall_retry_with_cookies():
    mock_client = AsyncMock(spec=HttpClient)
    paywall_resp = MagicMock()
    paywall_resp.text = "Paywall"
    paywall_resp.status_code = 403

    success_resp = MagicMock()
    success_resp.text = "<html><body><article><p>Premium content</p></article></body></html>"
    success_resp.status_code = 200

    mock_client.get.side_effect = [paywall_resp, success_resp]

    with (
        patch("trafilatura.extract") as mock_extract,
        patch("trafilatura.extract_metadata") as mock_extract_metadata,
    ):
        mock_extract.return_value = "Premium content"
        mock_metadata = MagicMock()
        mock_metadata.title = "Premium"
        mock_metadata.author = None
        mock_metadata.date = None
        mock_extract_metadata.return_value = mock_metadata

        result = await extract_full_text(
            "https://stratechery.com/premium", mock_client, cookies={"session": "abc"}
        )

    assert result.content == "Premium content"
    assert mock_client.get.call_count == 2


@pytest.mark.asyncio
async def test_short_content_triggers_cookie_retry():
    """Test that short content (< 300 chars) triggers retry with cookies."""
    mock_client = AsyncMock(spec=HttpClient)

    # First response: 200 OK but very short content
    short_html = "<html><body><p>Subscribe to read more...</p></body></html>"
    first_resp = MagicMock()
    first_resp.text = short_html
    first_resp.status_code = 200

    # Second response: full content with cookies
    full_html = (
        "<html><body><article><p>"
        + ("Full article content. " * 50)
        + "</p></article></body></html>"
    )
    second_resp = MagicMock()
    second_resp.text = full_html
    second_resp.status_code = 200

    mock_client.get.side_effect = [first_resp, second_resp]

    with (
        patch("trafilatura.extract") as mock_extract,
        patch("trafilatura.extract_metadata") as mock_extract_metadata,
    ):
        # First extraction yields short content (150 chars)
        # Second extraction yields long content (900 chars)
        mock_extract.side_effect = [
            "Subscribe to read more and get access to premium content on our platform.",
            "Full article content. " * 50,
        ]
        mock_metadata = MagicMock()
        mock_metadata.title = "Article"
        mock_metadata.author = "Author"
        mock_metadata.date = None
        mock_extract_metadata.return_value = mock_metadata

        result = await extract_full_text(
            "https://example.com/article", mock_client, cookies={"auth": "token"}
        )

    # Should have made 2 requests
    assert mock_client.get.call_count == 2
    # Should use the longer content from retry
    assert len(result.content) > 900
    assert "Full article content." in result.content


@pytest.mark.asyncio
async def test_no_retry_if_content_sufficient():
    """Test that sufficient content length prevents retry even with cookies."""
    mock_client = AsyncMock(spec=HttpClient)

    # Response with sufficient content
    html = (
        "<html><body><article><p>" + ("Good content here. " * 50) + "</p></article></body></html>"
    )
    resp = MagicMock()
    resp.text = html
    resp.status_code = 200

    mock_client.get.return_value = resp

    with (
        patch("trafilatura.extract") as mock_extract,
        patch("trafilatura.extract_metadata") as mock_extract_metadata,
    ):
        mock_extract.return_value = "Good content here. " * 50
        mock_metadata = MagicMock()
        mock_metadata.title = "Article"
        mock_metadata.author = None
        mock_metadata.date = None
        mock_extract_metadata.return_value = mock_metadata

        result = await extract_full_text(
            "https://example.com/article", mock_client, cookies={"auth": "token"}
        )

    # Should only make 1 request (no retry needed)
    assert mock_client.get.call_count == 1
    assert len(result.content) > 800


@pytest.mark.asyncio
async def test_no_retry_without_cookies():
    """Test that short content does NOT trigger retry if no cookies provided."""
    mock_client = AsyncMock(spec=HttpClient)

    # Response with short content
    short_html = "<html><body><p>Subscribe to read more...</p></body></html>"
    resp = MagicMock()
    resp.text = short_html
    resp.status_code = 200

    mock_client.get.return_value = resp

    with (
        patch("trafilatura.extract") as mock_extract,
        patch("trafilatura.extract_metadata") as mock_extract_metadata,
    ):
        mock_extract.return_value = "Subscribe to read more and get access."
        mock_metadata = MagicMock()
        mock_metadata.title = "Article"
        mock_metadata.author = None
        mock_metadata.date = None
        mock_extract_metadata.return_value = mock_metadata

        result = await extract_full_text("https://example.com/article", mock_client, cookies=None)

    # Should only make 1 request (no retry without cookies)
    assert mock_client.get.call_count == 1
    assert len(result.content) < 300
