"""Every implementation, checked against the Protocol it claims to satisfy.

`ports.py` says of `ArticleRepository` that a replacement covering less "will
fail at the CLI or the triage UI rather than at import", and `Embedder.usage`
carries the same warning. Both were true and neither was checked: there is no
type checker in this project, so a Protocol was only ever enforced by whichever
call site happened to run — a method the digest run does not touch could be
missing for as long as nobody opened the triage UI.

Structural conformance is checked here once, for every implementation
`bootstrap` can wire. Signatures are compared by parameter name and kind, not by
annotation: the callers pass these by keyword, and a renamed parameter breaks
them exactly as a missing method would.
"""

import ast
import inspect
import textwrap
from typing import Protocol

from cyris.adapters.anthropic_client import AnthropicClient
from cyris.adapters.embedding import GeminiEmbedder, WorkersAIEmbedder
from cyris.adapters.fetch.newsletter_worker_source import CloudflareNewsletterSource
from cyris.adapters.fetch.rss_source import RssSource
from cyris.adapters.fetch.rss_worker_source import CloudflareRssSource
from cyris.adapters.gemini_client import GeminiClient
from cyris.adapters.openai_client import OpenAIClient
from cyris.adapters.store.article_store import ArticleStore
from cyris.adapters.store.d1_store import D1ArticleStore
from cyris.adapters.workers_ai_client import WorkersAIClient
from cyris.service_layer.ports import ArticleRepository, Embedder, FetchSource, LLMClient

IMPLEMENTATIONS = [
    (ArticleRepository, ArticleStore),
    (ArticleRepository, D1ArticleStore),
    (LLMClient, AnthropicClient),
    (LLMClient, GeminiClient),
    (LLMClient, OpenAIClient),
    (LLMClient, WorkersAIClient),
    (Embedder, GeminiEmbedder),
    (Embedder, WorkersAIEmbedder),
    (FetchSource, RssSource),
    (FetchSource, CloudflareRssSource),
    (FetchSource, CloudflareNewsletterSource),
]


def _params(func) -> list[tuple[str, inspect._ParameterKind]]:
    return [
        (p.name, p.kind)
        for p in inspect.signature(func).parameters.values()
        if p.name not in ("self", "cls")
    ]


def _optional(func, name: str) -> bool:
    return inspect.signature(func).parameters[name].default is not inspect.Parameter.empty


def _self_assigned(cls: type) -> set[str]:
    """Names the class binds to `self`, which is where a data member is set."""
    found: set[str] = set()
    for ancestor in cls.__mro__:
        if ancestor is object:
            continue
        for node in ast.walk(ast.parse(textwrap.dedent(inspect.getsource(ancestor)))):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.ctx, ast.Store)
                and isinstance(node.value, ast.Name)
                and node.value.id == "self"
            ):
                found.add(node.attr)
    return found


def shortfalls(protocol: type[Protocol], impl: type) -> list[str]:
    found: list[str] = []
    for name in sorted(protocol.__protocol_attrs__):
        declared = getattr(protocol, name, None)
        if not callable(declared):
            if not hasattr(impl, name) and name not in _self_assigned(impl):
                found.append(f"{impl.__name__} never sets {name}")
            continue

        method = getattr(impl, name, None)
        if method is None:
            found.append(f"{impl.__name__} is missing {name}()")
            continue

        wanted, got = _params(declared), _params(method)
        for param in wanted:
            if param not in got:
                found.append(f"{impl.__name__}.{name}() does not take {param[0]}")
        for extra_name, _ in got:
            if extra_name not in {n for n, _ in wanted} and not _optional(method, extra_name):
                found.append(f"{impl.__name__}.{name}() demands {extra_name}, unknown to callers")
    return found


def test_every_implementation_satisfies_its_protocol() -> None:
    found = [line for protocol, impl in IMPLEMENTATIONS for line in shortfalls(protocol, impl)]

    assert found == []


def test_a_store_missing_a_triage_only_method_is_named() -> None:
    """The failure mode the `ArticleRepository` docstring describes.

    `reset_to_pending` is called by the triage UI and by nothing in a digest
    run, so a backend without it passes every pipeline test.
    """

    class PartialStore(D1ArticleStore):
        reset_to_pending = None

    assert "PartialStore is missing reset_to_pending()" in shortfalls(
        ArticleRepository, PartialStore
    )


def test_an_embedder_without_usage_is_named() -> None:
    class Anonymous:
        async def embed(self, texts: list[str]) -> list[list[float]]:
            return []

    assert shortfalls(Embedder, Anonymous) == ["Anonymous never sets usage"]


def test_a_renamed_parameter_is_named() -> None:
    class Renamed(RssSource):
        async def fetch_articles(self, since, before, sources, limit=200):
            return []

    assert "Renamed.fetch_articles() does not take after" in shortfalls(FetchSource, Renamed)
