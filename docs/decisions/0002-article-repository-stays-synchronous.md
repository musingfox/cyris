---
status: accepted
date: 2026-10-08
decision-makers: ""
---

# `ArticleRepository` stays synchronous, so the D1 store uses a blocking client

## Context and Problem Statement

`ports.ArticleRepository` is a synchronous Protocol: it declares `def save(...)`, and
`run_digest` calls it without `await`. From Python inside a Container, D1 is not a binding but an
HTTP API. The cloud move needed a D1-backed store, and it had promised that `service_layer/` and
`domain/` would not change.

Before any code moved, the cloud migration plan (`docs/cloud-migration.md` at commit `c353d3f`)
settled this as its first design constraint and said that if the constraint failed, the plan
would need rewriting.

## Considered Options

* Keep the Protocol synchronous and give the D1 store a blocking HTTP client
* Make the Protocol async and give the D1 store an async client

## Decision Outcome

Chosen option: "keep the Protocol synchronous and give the D1 store a blocking HTTP client",
because an async adapter would push `async` up through every call site and into
`service_layer/`, which the cloud move promised not to touch. `newsletter_worker_source.py`
already used a blocking client, so the D1 store followed it.

### Consequences

* Good, because `D1ArticleStore` landed without a line changing in the pipeline, which is the
  payoff `docs/architecture.md` §8 describes.
* Good, because `ArticleStore` (JSON) and `D1ArticleStore` satisfy one Protocol and stay
  interchangeable.
* Bad, because a cancellation lands only at an `await`, so a synchronous D1 call in flight,
  `D1Client`'s own retries included, finishes before a SIGTERM can cancel the run.

### Confirmation

`tests/test_protocol_conformance.py` checks every implementation against its Protocol.

## More Information

Extracted on 2026-10-08 from these sources at commit `c353d3f`:

* `docs/architecture.md:210-212` (*`ArticleRepository` is synchronous*).
* `docs/cloud-migration.md:71-81` (*Design constraints*, constraint 1).
* The docstring at `src/cyris/adapters/store/d1.py:1-7`, which points here.
