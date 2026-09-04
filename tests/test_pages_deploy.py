"""The Pages direct-upload protocol, read off wrangler and pinned here."""

import base64
import json

import httpx
import pytest

from cyris.adapters.output.pages_deploy import (
    PagesClient,
    PagesDeployError,
    _buckets,
    asset_hash,
)


def test_the_asset_hash_is_blake3_of_base64_plus_extension():
    """Not blake3 of the bytes. Cloudflare's account-wide asset store is keyed by
    this exact formulation, and any other one makes `check-missing` answer "all of
    them are new" — a silent full re-upload on every deploy, forever."""
    from blake3 import blake3

    contents = b"<html>hi</html>"
    expected = blake3((base64.b64encode(contents).decode() + "html").encode("utf-8")).hexdigest()[
        :32
    ]

    assert asset_hash(contents, "html") == expected
    assert len(asset_hash(contents, "html")) == 32
    assert asset_hash(contents, "html") != asset_hash(contents, "txt")


def test_buckets_split_on_size_and_never_lose_a_file():
    files = [{"contents": b"x" * (15 * 1024 * 1024)} for _ in range(5)]

    buckets = _buckets(files)

    assert sum(len(b) for b in buckets) == 5
    assert all(sum(len(f["contents"]) for f in b) <= 40 * 1024 * 1024 for b in buckets)


def test_an_empty_input_makes_no_buckets():
    assert _buckets([]) == []


def _routed(handler, tmp_path):
    (tmp_path / "index.html").write_text("<html>index</html>")
    (tmp_path / "2026-08-27-morning.html").write_text("<html>digest</html>")
    client = PagesClient("acct", "tok", "proj")
    original = httpx.Client

    class Patched(original):
        def __init__(self, *a, **kw):
            super().__init__(*a, **{**kw, "transport": httpx.MockTransport(handler)})

    return client, Patched


def test_only_the_files_the_account_lacks_are_uploaded(tmp_path, monkeypatch):
    """The whole archive goes in the manifest every deploy — a Pages deployment is
    a full snapshot — but the bytes only go up once per account."""
    seen = {"uploaded": [], "manifest": None}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/upload-token"):
            return httpx.Response(200, json={"success": True, "result": {"jwt": "j"}})
        if request.url.path.endswith("/check-missing"):
            hashes = json.loads(request.content)["hashes"]
            # The account already holds the first one.
            return httpx.Response(200, json={"success": True, "result": hashes[1:]})
        if request.url.path.endswith("/assets/upload"):
            seen["uploaded"].extend(item["key"] for item in json.loads(request.content))
            return httpx.Response(200, json={"success": True, "result": None})
        if request.url.path.endswith("/upsert-hashes"):
            return httpx.Response(200, json={"success": True, "result": None})
        if request.url.path.endswith("/deployments"):
            body = request.content.decode("utf-8", "replace")
            seen["manifest"] = json.loads(
                body.split('name="manifest"')[1].split("\r\n\r\n")[1].split("\r\n")[0]
            )
            seen["branch"] = "main" in body
            return httpx.Response(200, json={"success": True, "result": {"id": "dep-1"}})
        raise AssertionError(f"unexpected call: {request.url.path}")

    client, patched = _routed(handler, tmp_path)
    monkeypatch.setattr(httpx, "Client", patched)

    assert client.deploy(tmp_path) == "dep-1"
    assert len(seen["uploaded"]) == 1, "an asset the account already holds was re-uploaded"
    assert sorted(seen["manifest"]) == ["/2026-08-27-morning.html", "/index.html"]
    assert seen["branch"], "without the production branch this lands as a preview"


def test_a_step_that_answers_success_false_is_an_error(tmp_path, monkeypatch):
    """Cloudflare returns 200 with success:false. Raising on status alone would let
    a deploy that uploaded nothing report as a success — the 08-18 failure mode."""

    def handler(request):
        return httpx.Response(200, json={"success": False, "errors": [{"code": 8000013}]})

    client, patched = _routed(handler, tmp_path)
    monkeypatch.setattr(httpx, "Client", patched)

    with pytest.raises(PagesDeployError, match="8000013"):
        client.deploy(tmp_path)


def test_an_empty_directory_is_refused_rather_than_wiping_the_site(tmp_path):
    """A deployment is a full snapshot: an empty manifest deletes every page."""
    with pytest.raises(PagesDeployError, match="no files"):
        PagesClient("a", "t", "p").deploy(tmp_path)


def _probe(handler, monkeypatch):
    original = httpx.Client

    class Patched(original):
        def __init__(self, *a, **kw):
            super().__init__(*a, **{**kw, "transport": httpx.MockTransport(handler)})

    monkeypatch.setattr(httpx, "Client", Patched)
    return PagesClient("acct", "tok", "proj")


def test_has_deployments_is_true_when_the_list_holds_one(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "result": [{"id": "dep-1"}]})

    assert _probe(handler, monkeypatch).has_deployments() is True


def test_has_deployments_is_false_when_the_list_is_empty(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "result": []})

    assert _probe(handler, monkeypatch).has_deployments() is False


def test_has_deployments_raises_when_the_project_is_missing(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={"success": False, "errors": [{"message": "Project not found"}]},
        )

    with pytest.raises(PagesDeployError, match="404"):
        _probe(handler, monkeypatch).has_deployments()


def test_has_deployments_raises_when_result_is_missing(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True})

    with pytest.raises(PagesDeployError):
        _probe(handler, monkeypatch).has_deployments()


def test_has_deployments_asks_for_one_page_of_the_project_list(monkeypatch):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"success": True, "result": []})

    _probe(handler, monkeypatch).has_deployments()

    assert len(seen) == 1
    assert seen[0].url.path.endswith("/pages/projects/proj/deployments")
    assert seen[0].url.params.get("per_page") == "1"
    assert "env" not in seen[0].url.params
