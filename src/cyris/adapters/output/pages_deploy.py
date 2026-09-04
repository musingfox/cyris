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
from collections.abc import Callable
from pathlib import Path

import httpx
from blake3 import blake3

logger = logging.getLogger(__name__)

API_ROOT = "https://api.cloudflare.com/client/v4"
# ponytail: every deploy names every file, and the upload token caps a deployment
# at 20,000 (`max_file_count_allowed` in its JWT). Four files a day is ~13 years.
# When it matters, prune the archive tail — do not add a storage tier for it.
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

    def has_deployments(self) -> bool:
        """True if this Pages project has at least one deployment."""
        path = f"/accounts/{self._account}/pages/projects/{self._project}/deployments"
        with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
            body = self._call(
                client,
                "GET",
                path,
                headers={"Authorization": f"Bearer {self._token}"},
                params={"per_page": 1},
            )
        result = body.get("result")
        if not isinstance(result, list):
            raise PagesDeployError(f"GET {path} → result is not a list")
        return bool(result)

    # ---- the protocol ---------------------------------------------------

    def deploy(self, directory: Path, branch: str = "main") -> str:
        """Deploy every file under `directory`. The local-archive path."""
        files = _collect(directory)
        if not files:
            raise PagesDeployError(f"{directory} holds no files to deploy")
        return self.deploy_files(files, branch=branch)

    def deploy_manifest(
        self,
        new_files: dict[str, bytes],
        manifest: dict[str, str],
        recover: Callable[[str], bytes | None],
        branch: str = "main",
    ) -> tuple[str, dict[str, str]]:
        """Deploy this run's files plus everything the manifest already names.

        `manifest` is path → hash for the archive; `recover(path)` fetches the
        bytes of an archived file, and is called only for the rare asset
        Cloudflare has evicted. Returns the deployment id and the new manifest.
        """
        files = [
            {
                "path": path,
                "hash": asset_hash(contents, path.rsplit(".", 1)[-1] if "." in path else ""),
                "contents": contents,
                "content_type": mimetypes.guess_type(path)[0] or "application/octet-stream",
            }
            for path, contents in sorted(new_files.items())
        ]
        fresh = {f["path"] for f in files}
        for path, digest in sorted(manifest.items()):
            if path in fresh:
                continue
            files.append(
                {
                    "path": path,
                    "hash": digest,
                    "contents": None,  # fetched from the live site only if evicted
                    "content_type": mimetypes.guess_type(path)[0] or "application/octet-stream",
                }
            )
        if not files:
            raise PagesDeployError("nothing to deploy")
        deployment = self.deploy_files(files, branch=branch, recover=recover)
        return deployment, {f["path"]: f["hash"] for f in files}

    def deploy_files(
        self,
        files: list[dict],
        branch: str = "main",
        recover: Callable[[str], bytes | None] | None = None,
    ) -> str:
        """Upload what the account is missing, then deploy every file given.

        Returns the deployment id. Raises `PagesDeployError` on any failed step —
        deciding whether a failed publish should stop the digest is the caller's
        call, not this adapter's.
        """

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

            to_upload = []
            for f in files:
                if f["hash"] not in missing:
                    continue
                if f["contents"] is None:
                    # Cloudflare aged this asset out. The deployed site still
                    # serves the bytes it was uploaded with, so that is where the
                    # archive is recovered from.
                    contents = recover(f["path"]) if recover else None
                    if contents is None:
                        raise PagesDeployError(
                            f"{f['path']} is missing from the asset store and could not be "
                            "recovered from the live site; deploying without it would delete it"
                        )
                    # Mutated in place, not copied: the manifest sent to the
                    # deployments endpoint is built from this same list, and a
                    # hash there that was never uploaded fails the whole deploy.
                    f["contents"] = contents
                    f["hash"] = asset_hash(contents, _ext(f["path"]))
                to_upload.append(f)

            for bucket in _buckets(to_upload):
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
                    # Recomputed: a recovered asset may have been re-hashed above.
                    json={"hashes": [f["hash"] for f in files]},
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


def _ext(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[-1] if "." in name else ""


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
