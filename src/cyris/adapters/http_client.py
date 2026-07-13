"""Cookie-aware HTTP client."""

import httpx


class HttpClient:
    """Async HTTP client with cookie support."""

    def __init__(self, timeout: int = 30):
        self._client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={
                "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36")
            },
        )

    async def get(self, url: str, cookies: dict[str, str] | None = None) -> httpx.Response:
        """Fetch URL with optional cookies."""
        if cookies:
            self._client.cookies = httpx.Cookies(cookies)
        try:
            return await self._client.get(url)
        finally:
            if cookies:
                self._client.cookies.clear()

    async def close(self) -> None:
        """Close the client."""
        await self._client.aclose()

    async def __aenter__(self) -> "HttpClient":
        return self

    async def __aexit__(self, *args) -> None:
        await self.close()
