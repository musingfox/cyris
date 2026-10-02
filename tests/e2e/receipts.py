"""Read a run's record into receipts, and the checks every e2e test decides by.

The scenario tests and the self-tests call the same functions, so a self-test that
breaks a receipt proves the check a scenario relies on can fail
(docs/spec/e2e-asserts-what-the-fakes-received.md).
"""

from __future__ import annotations

import base64
import json
import re
from collections import Counter
from dataclasses import dataclass
from html.parser import HTMLParser

from blake3 import blake3


def unclaimed(records: list[dict]) -> list[dict]:
    """Requests no fake claimed, and those a fake failed on: neither was answered."""
    return [r for r in records if r["route"] is None or "error" in r]


def assert_no_unclaimed(records: list[dict]) -> None:
    stray = [
        f"{r.get('method')} {r.get('host')}{r.get('path')} {r.get('error', '')}".strip()
        for r in unclaimed(records)
    ]
    assert not stray, f"requests no fake claims: {stray}"


def assert_route_counts(records: list[dict], routes: list[str], expected: dict[str, int]) -> None:
    """Exactly `expected` requests per route, and every route named, zero included."""
    assert set(expected) == set(routes), (
        f"name every route the fakes serve: missing {sorted(set(routes) - set(expected))}, "
        f"unknown {sorted(set(expected) - set(routes))}"
    )
    counts = Counter(r["route"] for r in records if r["route"] is not None)
    assert {route: counts.get(route, 0) for route in routes} == expected


def by_route(records: list[dict], route: str) -> list[dict]:
    return [r for r in records if r["route"] == route]


def json_body(record: dict):
    return json.loads(record["body"])


_WRITE = re.compile(r"\b(INSERT(?: OR \w+)? INTO|UPDATE|DELETE FROM)\s+(\w+)(?:\s+SET\s+(\w+))?")
_READ = re.compile(r"^\s*SELECT\b.*?\bFROM\s+(\w+)", re.DOTALL)


def statement_kind(sql: str) -> str:
    """`INSERT OR IGNORE INTO stored_articles`, `UPDATE stored_articles SET state`, ...

    The schema script cyris sends on every boot is `schema`.
    """
    if "CREATE TABLE" in sql:
        return "schema"
    if match := _WRITE.search(sql):
        verb, table, column = match.groups()
        return f"{verb} {table}" + (f" SET {column}" if column else "")
    if match := _READ.match(sql):
        return f"SELECT {match.group(1)}"
    raise AssertionError(f"unrecognised D1 statement: {sql[:120]}")


def d1_statements(records: list[dict]) -> list[dict]:
    """Each D1 request's {"sql", "params"}, in order."""
    return [json_body(r) for r in by_route(records, "d1_query")]


def pages_site(records: list[dict]) -> dict[str, bytes]:
    """The site the deployment named, path → the bytes uploaded under its hash.

    Asserts what the deploy protocol promises: each hash is Cloudflare's key for
    those bytes, blake3(base64(bytes) + extension)[:32], and every hash the
    manifest names was uploaded.
    """
    deployment = only(records, "pages_deployment_create")
    manifest = json.loads(deployment["form"]["manifest"])
    uploaded = {
        asset["key"]: (
            base64.b64decode(asset["value"]) if asset.get("base64") else asset["value"].encode()
        )
        for record in by_route(records, "pages_upload")
        for asset in json_body(record)
    }
    site = {}
    for path, digest in manifest.items():
        assert digest in uploaded, f"{path}: hash {digest} was never uploaded"
        contents = uploaded[digest]
        extension = path.rsplit("/", 1)[-1].rpartition(".")[2]
        payload = base64.b64encode(contents).decode("ascii") + extension
        assert blake3(payload.encode()).hexdigest()[:32] == digest, f"{path}: wrong hash"
        site[path] = contents
    return site


@dataclass
class Node:
    tag: str
    attrs: dict[str, str]
    children: list[Node | str]

    def classes(self) -> set[str]:
        return set(self.attrs.get("class", "").split())

    def find_all(self, tag: str, cls: str | None = None) -> list[Node]:
        found = []
        for child in self.children:
            if isinstance(child, str):
                continue
            if child.tag == tag and (cls is None or cls in child.classes()):
                found.append(child)
            found.extend(child.find_all(tag, cls))
        return found

    def one(self, tag: str, cls: str | None = None) -> Node:
        found = self.find_all(tag, cls)
        assert len(found) == 1, f"expected one <{tag} class={cls}>, found {len(found)}"
        return found[0]

    def text(self) -> str:
        parts = [c if isinstance(c, str) else c.text() for c in self.children]
        return " ".join(" ".join(parts).split())

    def hrefs(self) -> list[str]:
        return [a.attrs["href"] for a in self.find_all("a") if "href" in a.attrs]


_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source"}


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#root", {}, [])
        self._stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Node(tag, {k: v or "" for k, v in attrs}, [])
        self._stack[-1].children.append(node)
        if tag not in _VOID:
            self._stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self._stack[-1].children.append(Node(tag, {k: v or "" for k, v in attrs}, []))

    def handle_endtag(self, tag):
        for i in range(len(self._stack) - 1, 0, -1):
            if self._stack[i].tag == tag:
                del self._stack[i:]
                return

    def handle_data(self, data):
        self._stack[-1].children.append(data)


def parse_html(html: str | bytes) -> Node:
    builder = _TreeBuilder()
    builder.feed(html.decode("utf-8") if isinstance(html, bytes) else html)
    builder.close()
    return builder.root


def only(records: list[dict], route: str) -> dict:
    """The one request a route received; any other count fails the check."""
    found = by_route(records, route)
    assert len(found) == 1, f"{route} received {len(found)} requests, not one"
    return found[0]
