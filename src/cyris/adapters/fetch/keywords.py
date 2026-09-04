"""Fetch-side vocabulary loaded from keywords.json.

The words and the hosts are data; the structure around them is not. A subject
line's forward and reply prefixes repeat, nest, and are followed by either ASCII
or fullwidth colons — that shape is the same in every language. A host matches
itself or any subdomain of it. Those rules live here; what they are applied to
lives in the JSON, so adding an ESP or a locale never means editing code.
"""

from __future__ import annotations

import json
import re
from functools import cache
from importlib.resources import files

_SEPARATOR = "[:：]"


@cache
def _vocabulary() -> dict:
    raw = (files(__package__) / "keywords.json").read_text(encoding="utf-8")
    return {k: v for k, v in json.loads(raw).items() if not k.startswith("_")}


def _alternation(key: str) -> str:
    return "|".join(re.escape(token) for token in _vocabulary()[key])


@cache
def subject_prefix_re() -> re.Pattern[str]:
    """Any run of forward/reply prefixes at the head of a subject."""
    both = f"{_alternation('forward_prefixes')}|{_alternation('reply_prefixes')}"
    return re.compile(rf"^\s*(?:(?:{both}){_SEPARATOR}\s*)+", re.IGNORECASE)


@cache
def private_reply_re() -> re.Pattern[str]:
    """A reply, or a forward of one — forwards may stack, the reply is the last prefix."""
    forwards = _alternation("forward_prefixes")
    replies = _alternation("reply_prefixes")
    return re.compile(
        rf"^(?:(?:{forwards}){_SEPARATOR}\s*)*(?:{replies}){_SEPARATOR}\s*",
        re.IGNORECASE,
    )


@cache
def view_in_browser_re() -> re.Pattern[str]:
    """The "read this on the web" wording newsletters put above their content."""
    return re.compile(_alternation("view_in_browser_markers"), re.IGNORECASE)


def host_matches(hostname: str, host: str) -> bool:
    """A host entry covers the host itself and every subdomain of it."""
    return hostname == host or hostname.endswith(f".{host}")


@cache
def _reject_host_res() -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(p) for p in _vocabulary()["reject_host_patterns"])


def is_rejected_host(hostname: str) -> bool:
    """An ESP or campaign-archive host: never the article, whatever the path says."""
    return any(host_matches(hostname, h) for h in _vocabulary()["reject_hosts"]) or any(
        pattern.search(hostname) for pattern in _reject_host_res()
    )


@cache
def _view_url_host_res() -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(p) for p in _vocabulary()["view_url_host_patterns"])


def is_view_url_host(hostname: str) -> bool:
    """An ESP host whose page IS the issue, unlike the click wrapper next to it."""
    return any(host_matches(hostname, h) for h in _vocabulary()["view_url_hosts"]) or any(
        pattern.search(hostname) for pattern in _view_url_host_res()
    )


def _path_matches(rule: dict, path: str) -> bool:
    prefix = rule.get("path_segment_prefix")
    if prefix is not None:
        return path == prefix or path.startswith(f"{prefix}/")
    return rule["path_contains"] in path


def is_share_link(hostname: str, path: str) -> bool:
    """A "share this" endpoint on a social host, not the thing being shared."""
    return any(
        host_matches(hostname, rule["host"]) and _path_matches(rule, path)
        for rule in _vocabulary()["share_links"]
    )


def is_rejected_path(path: str) -> bool:
    """Paths that disqualify a link wherever it is hosted."""
    vocab = _vocabulary()
    segments = path.split("/")
    return (
        any(word in path for word in vocab["reject_path_contains"])
        or any(segment in segments for segment in vocab["reject_path_segments"])
        or path.endswith(tuple(vocab["reject_path_suffixes"]))
    )


def tracking_redirect_param(hostname: str, path: str) -> str | None:
    """The query parameter holding the real destination, when this is a click wrapper."""
    for rule in _vocabulary()["tracking_redirects"]:
        if host_matches(hostname, rule["host"]) and _path_matches(rule, path):
            return rule["target_param"]
    return None


@cache
def tracking_params() -> frozenset[str]:
    """Per-recipient parameters stripped from a newsletter link."""
    return frozenset(_vocabulary()["tracking_params"])


@cache
def base_tracking_params() -> frozenset[str]:
    """Parameters stripped from every link, newsletter or feed."""
    return frozenset(_vocabulary()["base_tracking_params"])
