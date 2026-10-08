"""Which custom domains the app Worker has, asked of the Cloudflare API."""

import httpx
import pytest
import respx

from cyris.adapters.cloudflare import list_worker_domains

pytestmark = pytest.mark.unit

URL = "https://api.cloudflare.com/client/v4/accounts/acct/workers/domains"


def _domains(*hosts: str) -> httpx.Response:
    result = [{"hostname": h, "service": "cyris-app", "environment": "production"} for h in hosts]
    return httpx.Response(200, json={"success": True, "errors": [], "result": result})


def test_the_worker_is_named_and_its_hostnames_come_back_sorted():
    with respx.mock:
        route = respx.get(URL).mock(return_value=_domains("z.example.org", "a.example.org"))
        hosts = list_worker_domains("acct", "tok", "cyris-app")

    assert hosts == ["a.example.org", "z.example.org"]
    assert route.call_count == 1
    assert route.calls.last.request.url.params["service"] == "cyris-app"
    assert route.calls.last.request.headers["Authorization"] == "Bearer tok"


def test_a_worker_with_no_custom_domain_has_none():
    with respx.mock:
        route = respx.get(URL).mock(return_value=_domains())
        assert list_worker_domains("acct", "tok", "cyris-app") == []

    assert route.call_count == 1


def test_a_token_without_the_permission_raises_the_api_message():
    body = {"success": False, "errors": [{"code": 10000, "message": "Authentication error"}]}
    with respx.mock:
        route = respx.get(URL).mock(return_value=httpx.Response(403, json=body))
        with pytest.raises(RuntimeError, match="Authentication error"):
            list_worker_domains("acct", "tok", "cyris-app")

    assert route.call_count == 1


def test_an_unreachable_api_raises_rather_than_answering_none():
    with respx.mock:
        route = respx.get(URL).mock(side_effect=httpx.ConnectError("down"))
        with pytest.raises(RuntimeError, match="could not reach"):
            list_worker_domains("acct", "tok", "cyris-app")

    assert route.call_count == 1
