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
def system_senders() -> frozenset[str]:
    """Addresses whose mail never makes a source: a platform's own notices, not a newsletter."""
    return frozenset(address.lower() for address in _vocabulary()["system_senders"])


def _block_words(part: str) -> str:
    """One part of every language's forwarded block, as an alternation, longest word first."""
    words = {word for block in _vocabulary()["forwarded_blocks"].values() for word in block[part]}
    return "|".join(re.escape(word) for word in sorted(words, key=len, reverse=True))


# A quoted line's lead: the `>` of a plain-text quote, and the `*` a client's bold label leaves.
_LEAD = r"^[\s>*]*"


@cache
def forwarded_marker_re() -> re.Pattern[str]:
    """The line some mail clients open a forwarded message's quoted headers with."""
    # On a line of its own, framed by rule characters or a colon, so a sentence that only
    # mentions a forwarded message is not taken for one.
    return re.compile(
        rf"{_LEAD}[\-_]*\s*(?:{_block_words('markers')})[\s\-_*]*{_SEPARATOR}?\s*$", re.IGNORECASE
    )


@cache
def forwarded_from_line_re() -> re.Pattern[str]:
    """A quoted header block's From line; group 1 is what follows its label."""
    labels = _block_words("from")
    return re.compile(rf"{_LEAD}(?:{labels})\s*\**\s*{_SEPARATOR}(.*)$", re.IGNORECASE)


@cache
def forwarded_header_line_re() -> re.Pattern[str]:
    """Any line of a quoted header block, its From line included."""
    labels = f"{_block_words('from')}|{_block_words('headers')}"
    return re.compile(rf"{_LEAD}(?:{labels})\s*\**\s*{_SEPARATOR}", re.IGNORECASE)


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


def is_media_link(hostname: str, path: str) -> bool:
    """A video's own page, whose text is the site's navigation rather than the video."""
    return any(
        host_matches(hostname, rule["host"]) and _path_matches(rule, path)
        for rule in _vocabulary()["media_links"]
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


def canonical_host(hostname: str) -> str:
    """The host a known alias stands for (`host_aliases`), else the host itself."""
    return _vocabulary()["host_aliases"].get(hostname, hostname)


def _shape_matches(shape: str, path: str) -> bool:
    want = [part for part in shape.split("/") if part]
    have = [part for part in path.split("/") if part]
    return len(want) == len(have) and all(w in ("*", h) for w, h in zip(want, have, strict=True))


def fits_platform_post_shape(hostname: str, path: str) -> bool:
    """False only for a known platform's host whose path is not one of its post shapes."""
    for rule in _vocabulary()["platform_post_paths"]:
        if host_matches(hostname, rule["host"]):
            return any(_shape_matches(shape, path) for shape in rule["shapes"])
    return True
