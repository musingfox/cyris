"""No credential reaches a log line, whichever way it got into the record.

A Discord webhook's posting token is part of its URL — Discord's design, not a
choice this code can make differently. So the URL cannot be made safe; what can
be made safe is every path by which it reaches a handler. There are three, and a
fix that covers one is not a fix:

1. httpx logs `HTTP Request: POST <url>` at INFO, which a run emits on every
   notification (observed 2026-09-18 00:01:11 UTC, token in plain text).
2. `HTTPStatusError`'s own message embeds the URL.
3. `logger.warning(..., exc_info=True)` renders a traceback the message never
   passes through — `adapters/notify.py` does exactly this on a failed send.

The container's stdout is Workers Logs for seven days, readable by any token
carrying observability read.
"""

import contextlib
import io
import logging
import re

import pytest

from cyris.adapters.notify import WEBHOOK_MASK
from cyris.entrypoints.cli import _RedactCredentials, _setup_logging

WEBHOOK = "https://discord.com/api/webhooks/123456789/s3cr3t-posting-token"
TOKEN = "s3cr3t-posting-token"


def _emit(caplog, make_record) -> str:
    """Run one record through the configured root handler and return what it wrote."""
    _setup_logging()
    logger = logging.getLogger("test.redaction")
    with caplog.at_level(logging.INFO):
        make_record(logger)
    formatter = logging.Formatter("%(message)s")
    return "\n".join(formatter.format(r) for r in caplog.records)


def test_a_webhook_url_in_the_message_loses_its_token(caplog):
    text = _emit(caplog, lambda log: log.info("HTTP Request: POST %s", WEBHOOK))

    assert TOKEN not in text
    assert WEBHOOK_MASK in text
    assert "discord.com/api/webhooks/123456789" in text  # the id still identifies it


def test_a_webhook_url_inside_a_traceback_loses_its_token(caplog):
    def raise_and_log(log):
        try:
            raise RuntimeError(f"Client error '401 Unauthorized' for url '{WEBHOOK}'")
        except RuntimeError:
            log.warning("Discord webhook failed", exc_info=True)

    text = _emit(caplog, raise_and_log)

    assert TOKEN not in text
    assert WEBHOOK_MASK in text


def test_an_ordinary_message_is_left_exactly_as_it_was(caplog):
    """A redactor that rewrites unrelated lines trades one defect for another."""
    message = "Fetched 61 article(s) from 43 source(s) in 12.4s"
    text = _emit(caplog, lambda log: log.info(message))

    assert text.strip() == message


def test_percent_style_arguments_still_render(caplog):
    text = _emit(caplog, lambda log: log.info("Embedding %d text(s)", 612))

    assert "Embedding 612 text(s)" in text


# A credential reaches the query string two ways: written into the URL, or
# handed to httpx as `params=`, which expands into the URL it then logs.
_CREDENTIAL_NAME = r"(?:key|token|api_key|access_token)"
_CREDENTIAL_IN_URL = re.compile(
    rf"[?&]{_CREDENTIAL_NAME}=\{{"  # f-string: ...?key={self._api_key}
    rf"|params\s*=\s*\{{[^}}]*[\"']{_CREDENTIAL_NAME}[\"']"  # httpx: params={"key": ...}
)


def test_no_source_file_builds_a_url_with_a_credential_in_its_query_string():
    """The structural half: a credential that is never in a URL cannot leak from one.

    `adapters/embedding.py` put `GEMINI_API_KEY` in `?key=` while its sibling LLM
    client used the `x-goog-api-key` header. Both are accepted by Google; only one
    of them is safe to log.
    """
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src"
    offenders = [
        path.relative_to(src).as_posix()
        for path in src.rglob("*.py")
        if _CREDENTIAL_IN_URL.search(path.read_text(encoding="utf-8"))
    ]

    assert offenders == [], f"credential in a URL query string: {offenders}"


async def test_the_real_send_path_logs_no_token(monkeypatch, caplog):
    """The end-to-end form of the same property, through httpx's own logger.

    The three synthetic records above prove the filter; this proves the wiring —
    that `cyris run`'s notification actually emits its `HTTP Request: POST <url>`
    line through a handler carrying the filter. It is the line the 2026-09-18 run
    leaked verbatim.
    """
    import httpx

    from cyris.adapters.notify import send_discord
    from cyris.domain.models import DigestContent, UsageStats

    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **kw: real(
            *a, **{**kw, "transport": httpx.MockTransport(lambda r: httpx.Response(204))}
        ),
    )
    content = DigestContent(
        date="2026-09-21",
        period="evening",
        sources_processed=1,
        articles_received=1,
        articles_included=1,
        usage=UsageStats(model="gemini-3.8-flash", input_tokens=0),
    )

    _setup_logging()
    # `doctor` lowers this logger globally (cli.py), and logger levels outlive the
    # test that set them — so state it here rather than inherit the suite's order.
    logging.getLogger("httpx").setLevel(logging.INFO)
    with caplog.at_level(logging.INFO):
        await send_discord(WEBHOOK, content)

    emitted = "\n".join(logging.Formatter("%(message)s").format(r) for r in caplog.records)
    assert "HTTP Request" in emitted, "httpx stopped logging requests; this test no longer guards"
    assert TOKEN not in emitted


async def test_a_failed_send_logs_neither_the_error_url_nor_the_traceback_token(
    monkeypatch, caplog
):
    """`notify.send_discord` logs its failure with `exc_info=True` (notify.py).

    That traceback never passes through the record's message, so a redactor that
    only rewrites messages would leave the token in it. This is the case the
    `exc_text` rewrite exists for, and a 204 in the test above never reaches it.
    """
    import httpx

    from cyris.adapters.notify import send_discord
    from cyris.domain.models import DigestContent, UsageStats

    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **kw: real(
            *a, **{**kw, "transport": httpx.MockTransport(lambda r: httpx.Response(401))}
        ),
    )
    content = DigestContent(
        date="2026-09-21",
        period="evening",
        sources_processed=1,
        articles_received=1,
        articles_included=1,
        usage=UsageStats(model="gemini-3.8-flash", input_tokens=0),
    )

    _setup_logging()
    logging.getLogger("httpx").setLevel(logging.INFO)
    with caplog.at_level(logging.INFO):
        await send_discord(WEBHOOK, content)

    formatter = logging.Formatter("%(message)s")
    emitted = "\n".join(formatter.format(r) + (r.exc_text or "") for r in caplog.records)
    assert "Discord webhook failed" in emitted, "the exception branch did not run"
    assert TOKEN not in emitted


def _handler_with_the_filter() -> tuple[logging.Handler, io.StringIO]:
    """A handler carrying the real filter.

    Not `caplog`: pytest's own capture handler is installed separately, so it
    never carries what `_setup_logging` attaches to the root handler — and it
    re-raises logging errors by design, which is the very behaviour under test.
    """
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(_RedactCredentials())
    return handler, stream


@contextlib.contextmanager
def _logging_through_the_filter(name: str):
    handler, stream = _handler_with_the_filter()
    log = logging.getLogger(name)
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False
    try:
        yield log, stream
    finally:
        log.removeHandler(handler)


def test_a_record_whose_percent_args_do_not_match_does_not_abort_the_caller(capsys):
    """A logging bug must stay a logging bug.

    Handler filters run in `Handler.handle`, outside the try/except that wraps
    `emit`/`format` — so an exception raised while inspecting a record propagates
    all the way back to the `logger.info(...)` call site. Before this filter
    existed, a mismatched format string surfaced as a `--- Logging error ---`
    note on stderr and the run carried on. A digest that dies here loses that
    issue permanently, which is a steep price for a wrong `%d`.
    """
    with _logging_through_the_filter("test.redaction.mismatch") as (log, _):
        log.info("two slots %d %d", 1)  # must not raise

    assert "Logging error" in capsys.readouterr().err  # still reported, just not fatal


def test_unrenderable_args_are_redacted_for_the_error_path(capsys):
    """`Handler.handleError` prints `record.args` raw when formatting fails.

    So swallowing the render error is not enough — the record still reaches that
    dump. The arguments themselves have to be masked, or the guard trades a
    crash for the leak it was added to prevent.
    """
    with _logging_through_the_filter("test.redaction.mismatch.args") as (log, _):
        log.info("two slots %d %d", WEBHOOK)

    printed = capsys.readouterr().err
    assert "Logging error" in printed
    assert TOKEN not in printed
    assert WEBHOOK_MASK in printed


@pytest.mark.parametrize(
    ("source", "is_offence"),
    [
        ('url = f"{ROOT}/{model}:embed?key={self._api_key}"', True),
        ('await http.post(url, params={"key": self._api_key})', True),
        ("await http.get(url, params={'token': tok})", True),
        ('headers={"x-goog-api-key": self._api_key}', False),
        ('url = f"{ROOT}/{self._model}:batchEmbedContents"', False),
        ('params={"model": name}', False),
    ],
)
def test_the_guard_tells_a_credential_in_the_query_string_from_one_in_a_header(source, is_offence):
    """The guard above is only worth having if it discriminates.

    It first shipped matching `?key={` alone, which happened to catch the one
    offender in the tree and nothing else — a reintroduction via httpx's
    `params=`, which expands into the same logged URL, would have passed.
    """
    assert bool(_CREDENTIAL_IN_URL.search(source)) is is_offence
