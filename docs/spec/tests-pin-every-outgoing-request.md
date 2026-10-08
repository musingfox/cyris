---
id: tests-pin-every-outgoing-request
status: accepted
scope:
  - "tests/test_*.py"
verify: check:uv run pytest tests/test_respx_routes_pin_counts.py -q
related: [e2e-asserts-what-the-fakes-received]
source: e2e-fake-edges
adr: null
---

A test that fakes an HTTP edge must pin how many requests cyris sent there and what each
one carried.

With respx, no test switches `assert_all_called` or `assert_all_mocked` off. The bare
`respx.mock` router every test uses is respx's global one, built with `assert_all_called`
already off (respx 0.23.1, `respx/api.py:8`). An unmocked request fails the test, and an
unused route fails it through its exact count. With `httpx.MockTransport`, the handler
appends every request to a list. Either way the test asserts the exact count per
route, using `call_count` or the list's length, and the method, path, credential header
and body fields of each request that bear on the behaviour under test.

Out of scope: a test whose subject is not the request, for example a parser fed a
canned response, needs no count assertion. A test that fakes the edge only to make the
code reach another assertion still pins the count, since an extra or missing request is
a behaviour change.

A violation is silent. An adapter that starts retrying, double-posting or skipping a call
still passes a test that checks only `route.called` or the return value, because the
canned response looks the same each time.

The `check:` runs `tests/test_respx_routes_pin_counts.py`. It fails when a test file
switches either respx assertion off. It also fails when a test registers a respx route that
is not bound to a name, or whose `call_count`, `len(route.calls)` or `not route.called`
the same test never reads. Its self-test plants each kind of violation. Two gaps stay with
review. The scan sees that the count is read, not that it is asserted exactly. It also
leaves `httpx.MockTransport` fakes out: their handlers record into lists, dicts, fixtures
and helper classes, and no scan of those shapes is free of false positives. The audit that
brought the 19 edge-faking files up to the rule is
`docs/spec/audits/tests-pin-every-outgoing-request-2026-10-08.md`.
