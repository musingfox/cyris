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
real service requires, and every body field cyris is responsible for: the Pages file
list, hashes and per-file metadata it uploads, the Discord embeds, the mail's recipients,
subject and both bodies, and each LLM call's model, prompt kind and the article positions
it names. D1 is held by effect: the ordered list of statement kinds the run sent, and
every column of every row cyris wrote, read back from the fake's sqlite file. Each scripted LLM case is paired with the receipt it must
change, such as two high scorers reaching the uploaded page as one grouped Top story, or
an article the per-issue cap cut staying `pending` in the fake D1.

Out of scope: wording the code does not own. A test compares prompt and page prose only
where cyris put a value into it, and never compares D1 SQL text, so rewording a prompt or
refactoring a query whose effect is unchanged does not break the suite. A guard a default
`cyris run` cannot reach, such as `update_states` refusing a human-stamped row, is held
by its unit test instead. The exit code and stdout may be a first gate, never the only
assertion.

A violation is silent. `cyris run` exits 0 when it published the wrong page, retried a
call three times, sent one Discord post twice, or never called the mail service, because
each step logs its failure and carries on. An "at least once" assertion passes every one
of those runs.

`verify` runs the suite's self-tests. One doubles a request, others each break one
scripted case's receipt, and one is a real run whose model splits the Top story; every
one of them must fail the check the suite relies on.
