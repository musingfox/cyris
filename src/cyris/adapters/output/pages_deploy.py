"""Deploy a directory to Cloudflare Pages over the REST API.

Replaces `wrangler pages deploy`. Two reasons, in order of weight:

1. **The shell-out is the failure this repo has already been burned by.** wrangler
   exits 0 without deploying and truncates its own output mid-upload, which is how
   the 2026-08-18 evening and 2026-08-20 morning digests lost their Discord links
   while both attempts "succeeded". Over REST every step has a checkable answer.
2. A Worker-fronted Container has no node and no wrangler to shell out to.

The protocol is Pages Direct Upload, read off wrangler's own implementation
(`wrangler-dist/cli.js`, `hashFile` / `upload` / `deploy`) rather than reconstructed:

  1. `GET  /accounts/{a}/pages/projects/{p}/upload-token`  → a short-lived JWT
  2. `POST /pages/assets/check-missing` {hashes}           → which the account lacks
  3. `POST /pages/assets/upload` [{key,value,metadata}]    → base64 payloads
  4. `POST /pages/assets/upsert-hashes` {hashes}           → best-effort cache touch
  5. `POST /accounts/{a}/pages/projects/{p}/deployments`   → multipart, `manifest`

Assets are content-addressed **per account**, so step 2 usually answers "I already
have all but the three you just rendered" — which is what makes re-uploading the
whole archive on every deploy cheap. A deployment is a full snapshot: a path absent
from the manifest is gone from the site, so the manifest always covers every file.
"""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
from pathlib import Path

import httpx
from blake3 import blake3

logger = logging.getLogger(__name__)

API_ROOT = "https://api.cloudflare.com/client/v4"
TIMEOUT_SECONDS = 120
# wrangler's own ceilings (MAX_BUCKET_SIZE / MAX_BUCKET_FILE_COUNT).
MAX_BUCKET_BYTES = 40 * 1024 * 1024
MAX_BUCKET_FILES = 2000


def asset_hash(contents: bytes, extension: str) -> str:
    """Cloudflare's Pages asset key: blake3(base64(bytes) + ext), hex, first 32.

    Hashing the *base64* rather than the bytes is not a mistake to tidy up — it is
    what the account-wide asset store is keyed by, so any other formulation makes
    `check-missing` claim every file is new and the deploy silently re-uploads the
    world. Taken verbatim from wrangler's `hashFile`.
    """
    payload = base64.b64encode(contents).decode("ascii") + extension
    return blake3(payload.encode("utf-8")).hexdigest()[:32]


class PagesDeployError(RuntimeError):
    """A step of the direct-upload protocol answered something other than success."""


class PagesClient:
    """Direct-upload client for one Pages project."""

    def __init__(self, account_id: str, api_token: str, project: str) -> None:
        self._account = account_id
        self._token = api_token
        self._project = project

    # ---- plumbing -------------------------------------------------------

    def _call(self, client: httpx.Client, method: str, path: str, **kwargs) -> dict:
        response = client.request(method, f"{API_ROOT}{path}", **kwargs)
        # Read the body before raising: Cloudflare puts the reason in it, and a
        # bare status code has sent us chasing the wrong thing twice.
        try:
            body = response.json()
        except ValueError:
            body = {}
        if not response.is_success or not body.get("success", False):
            detail = body.get("errors") or response.text[:300]
            raise PagesDeployError(f"{method} {path} → {response.status_code} {detail}")
        return body

    def _upload_token(self, client: httpx.Client) -> str:
        # GET, not POST: wrangler calls `fetchResult(path)` with no options, and
        # its default method is GET. POST answers 405 method_not_allowed.
        body = self._call(
            client,
            "GET",
            f"/accounts/{self._account}/pages/projects/{self._project}/upload-token",
            headers={"Authorization": f"Bearer {self._token}"},
        )
        return body["result"]["jwt"]

    # ---- the protocol ---------------------------------------------------

    def deploy(self, directory: Path, branch: str = "main") -> str:
        """Upload what the account is missing, then deploy the whole directory.

        Returns the deployment id. Raises `PagesDeployError` on any failed step —
        deciding whether a failed publish should stop the digest is the caller's
        call, not this adapter's.
        """
        files = _collect(directory)
        if not files:
            raise PagesDeployError(f"{directory} holds no files to deploy")

        with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
            jwt = self._upload_token(client)
            jwt_headers = {"Authorization": f"Bearer {jwt}", "Content-Type": "application/json"}

            hashes = [f["hash"] for f in files]
            missing = set(
                self._call(
                    client,
                    "POST",
                    "/pages/assets/check-missing",
                    headers=jwt_headers,
                    json={"hashes": hashes},
                )["result"]
            )
            logger.info(
                "Pages: %d file(s), %d already in the account's asset store",
                len(files),
                len(files) - len(missing),
            )

            for bucket in _buckets([f for f in files if f["hash"] in missing]):
                self._call(
                    client,
                    "POST",
                    "/pages/assets/upload",
                    headers=jwt_headers,
                    json=[
                        {
                            "key": f["hash"],
                            "value": base64.b64encode(f["contents"]).decode("ascii"),
                            "metadata": {"contentType": f["content_type"]},
                            "base64": True,
                        }
                        for f in bucket
                    ],
                )

            try:
                self._call(
                    client,
                    "POST",
                    "/pages/assets/upsert-hashes",
                    headers=jwt_headers,
                    json={"hashes": hashes},
                )
            except PagesDeployError as e:
                # wrangler treats this the same way: it only warms the account's
                # asset cache for the *next* deploy. This one is already uploaded.
                logger.warning("Pages: could not refresh the asset cache: %s", e)

            manifest = {f["path"]: f["hash"] for f in files}
            body = self._call(
                client,
                "POST",
                f"/accounts/{self._account}/pages/projects/{self._project}/deployments",
                headers={"Authorization": f"Bearer {self._token}"},
                files={
                    "manifest": (None, json.dumps(manifest)),
                    # Without the production branch this lands as a preview
                    # deployment on a URL nobody reads.
                    "branch": (None, branch),
                },
            )
        return body["result"]["id"]


def _collect(directory: Path) -> list[dict]:
    """Every file under `directory`, with the hash Pages keys it by."""
    files = []
    for path in sorted(directory.rglob("*")):
        if not path.is_file() or path.name.startswith("."):
            continue
        contents = path.read_bytes()
        files.append(
            {
                # Manifest paths are absolute-from-root and slash-separated.
                "path": "/" + path.relative_to(directory).as_posix(),
                "hash": asset_hash(contents, path.suffix.lstrip(".")),
                "contents": contents,
                "content_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            }
        )
    return files


def _buckets(files: list[dict]) -> list[list[dict]]:
    """Split uploads the way wrangler does: 40 MB or 2,000 files per request."""
    buckets: list[list[dict]] = []
    current: list[dict] = []
    size = 0
    for f in files:
        too_big = size + len(f["contents"]) > MAX_BUCKET_BYTES
        if current and (too_big or len(current) >= MAX_BUCKET_FILES):
            buckets.append(current)
            current, size = [], 0
        current.append(f)
        size += len(f["contents"])
    if current:
        buckets.append(current)
    return buckets
