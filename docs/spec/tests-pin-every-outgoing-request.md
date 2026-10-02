---
id: tests-pin-every-outgoing-request
status: accepted
scope:
  - "tests/test_*.py"
verify: check:! grep -rnE "assert_all_called=False|assert_all_mocked=False" tests
related: [e2e-asserts-what-the-fakes-received]
source: e2e-fake-edges
adr: null
---

A test that fakes an HTTP edge must pin how many requests cyris sent there and what each
one carried.

With respx, the router keeps its defaults, `assert_all_called` and `assert_all_mocked`, so
an unused route or an unmocked request fails the test. With `httpx.MockTransport`, the
handler appends every request to a list. Either way the test asserts the exact count per
route, using `call_count` or the list's length, and the method, path, credential header
and body fields of each request that bear on the behaviour under test.

Out of scope: a test whose subject is not the request, for example a parser fed a
canned response, needs no count assertion. A test that fakes the edge only to make the
code reach another assertion still pins the count, since an extra or missing request is
a behaviour change.

A violation is silent. An adapter that starts retrying, double-posting or skipping a call
still passes a test that checks only `route.called` or the return value, because the
canned response looks the same each time.

The `check:` binds only the defaults: no test switches respx's two assertions off. The
exact-count half has no grep that would not raise false positives, so it is held by
review until an audit of the 18 files that fake an edge has brought each one up to it.
