"""Shared async HTTP client."""

import httpx


class HttpClient:
    """Async HTTP client with the project's shared timeout and User-Agent."""

    def __init__(self, timeout: int = 30):
        self._client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={
                "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36")
            },
        )

    async def get(self, url: str) -> httpx.Response:
        """Fetch URL."""
        return await self._client.get(url)

    async def close(self) -> None:
        """Close the client."""
        await self._client.aclose()

    async def __aenter__(self) -> "HttpClient":
        return self

    async def __aexit__(self, *args) -> None:
        await self.close()
