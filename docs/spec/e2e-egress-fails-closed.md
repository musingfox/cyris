---
id: e2e-egress-fails-closed
status: accepted
scope:
  - "tests/e2e/**"
  - "tests/test_e2e_*.py"
verify: null
related: [outbound-http-honours-environment, e2e-asserts-what-the-fakes-received]
source: e2e-fake-edges
adr: null
---

An end-to-end run must be able to reach only the fakes, and any request no fake claims
must fail the test.

The run is a `cyris` subprocess whose environment is built from nothing, never copied
from the developer's shell or CI. It holds the proxy variables pointing at the local
proxy, `SSL_CERT_FILE` naming a bundle that contains only the test CA, and fake
credentials. Its `cyris.toml` and `.env` sit together in a temporary directory, because
cyris reads the `.env` beside its config file, so the repo's real `.env` is never loaded.
The proxy hands each request to the fake that claims its host and path, answers every
other request with an error, and records it. After the run, the test asserts that this
record is empty.

Out of scope: which APIs each fake mirrors. That list lives in the fake module beside
the code that answers, not here.

A violation is silent. A copied environment carries a real token or the system CA
bundle, so a request no fake claims can still reach the real service: it posts to the
real Discord, spends real LLM credit or writes to the production D1. An adapter that
swallows errors lets a missing fake pass the same way. Either way the run ends ok and
the suite is green.

This entry is prose because the suite does not exist yet. Once it lands, `verify` should
run the suite's own self-test: one case drops a fake, and the suite must report the
unclaimed request rather than pass.
