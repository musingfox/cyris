---
id: e2e-asserts-what-the-fakes-received
status: accepted
scope:
  - "tests/e2e/**"
  - "tests/test_e2e_*.py"
verify: check:uv run pytest tests/test_e2e_digest.py::TestSelfChecks -q
related: [outbound-http-honours-environment, e2e-egress-fails-closed, tests-pin-every-outgoing-request]
source: e2e-fake-edges
adr: null
---

An end-to-end test must decide pass or fail from the complete record of what each fake
received: the exact number of requests to every route, and the content of each one.

Every fake keeps each request it answered, in order. For each route a test asserts an
exact count, never "at least one", and zero for every route the scenario must not touch.
For each request it asserts the method, the path, the query, the credential header the
real service requires, and every body field cyris is responsible for: the D1 SQL and its
parameters, the Pages file list and the hashes it uploads, the Discord embeds, the mail's
recipients, subject and both bodies, and each LLM call's model, prompt kind and the
article positions it names. Each scripted LLM case is paired with the receipt it must
change, such as two high scorers reaching the uploaded page as one grouped Top story, or
an article the per-issue cap cut staying `pending` in the fake D1.

Out of scope: wording the code does not own. A test compares prompt and page prose only
where cyris put a value into it, so editing a prompt sentence does not break the suite.
The exit code and stdout may be a first gate, never the only assertion.

A violation is silent. `cyris run` exits 0 when it published the wrong page, retried a
call three times, sent one Discord post twice, or never called the mail service, because
each step logs its failure and carries on. An "at least once" assertion passes every one
of those runs.

`verify` runs the suite's self-tests. One doubles a request, the others each break one
scripted case's receipt, and every one of them must fail the check the suite relies on.
