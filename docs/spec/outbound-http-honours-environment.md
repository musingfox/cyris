---
id: outbound-http-honours-environment
status: accepted
scope:
  - "src/cyris/**/*.py"
verify: check:! grep -rnE "trust_env|verify=|proxy=|proxies=|mounts=|HTTPTransport|aiohttp\.ClientSession|import requests|urllib\.request|http\.client" src/cyris
related: [e2e-egress-fails-closed, e2e-asserts-what-the-fakes-received]
source: e2e-fake-edges
adr: null
---

Every request cyris sends to another host must go through an httpx client, or an SDK
built on one, that takes its proxy and its trusted CAs from the process environment.

httpx does this by default: with `trust_env` left on, it reads `HTTPS_PROXY`,
`HTTP_PROXY`, `NO_PROXY` and `SSL_CERT_FILE` (httpx 0.28.1, `httpx/_config.py:34`), and
the Anthropic SDK reads the same proxy variables (`anthropic/_base_client.py:863`). So an
adapter builds its client with a timeout, headers and redirect policy only, and never
passes `trust_env`, `verify`, a proxy or a custom transport.

Out of scope: servers. `aiohttp` serves `/settings` and is not a client. Test code under
`tests/` may build any client it likes. An adapter that needs a client this rule forbids
needs a decision first, because the end-to-end suite stops seeing that edge.

A violation is silent. An adapter that turns `trust_env` off, or a new edge written with
`requests` or an `aiohttp` client session, goes straight to the real host and never
passes the test proxy, so the proxy's list of unclaimed requests stays empty. The
end-to-end run carries fake credentials, the real service refuses them, and adapters
such as Discord and mail log the failure and carry on. The run ends ok and the suite
stays green while that edge was never tested.
