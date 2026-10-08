---
status: accepted
date: 2026-10-08
decision-makers: ""
---

# Tools whose subject is the deployment live in their own `diagnostics/` layer

## Context and Problem Statement

The layering rule is that the core (`service_layer/` and `domain/`) imports nothing from
`adapters/` or `bootstrap`. On 2026-09-05 an alignment pass found `cyris doctor` breaking it at
six sites: three built adapters inside the service layer, and three more called
`bootstrap.build_llm` or `build_store`, importing the composition root into the core. It also
found the CLI holding pipeline logic for the comparisons, including a drifted copy of the
scorable filter that scored fan-tier articles `run_digest` skips.

`doctor` cannot simply use the composition root's output. Its `build` check exists because a
config asking for `[store] backend = "d1"` ran for two days on an image with no `[store]` table
at all, and the only check that catches that asks the composition root directly:
`type(build_store(cfg)).__name__`. `embed-compare` and `llm-compare` build *two* wirings on
purpose, which no core module may do and no single `build_deps` can express.

## Considered Options

* Keep `doctor` in `service_layer/` and allow it as an exception to the import rule
* Inject a list of probes into `doctor` from `build_deps`
* Move `doctor` and the comparisons into a `diagnostics/` package above the core

## Decision Outcome

Chosen option: "a `diagnostics/` package above the core", because these tools' subject is the
wiring itself, and with them outside the core the layering rule holds with no exception.

`diagnostics/` may import everything below it, and nothing below it may import it. Deleting the
package would cost three commands and not one digest. Two properties depend on `doctor` building
its own adapters. It never calls `build_deps`, which raises on missing credentials, and reporting
missing credentials is its job. It can also report *not configured* as a verdict of its own,
which an injected list of probes cannot express.

### Consequences

* Good, because `service_layer/` and `domain/` import neither `cyris.adapters` nor
  `cyris.bootstrap` at runtime, with no allow-list.
* Good, because the comparison rules (`margin`, the `api_calls == 0` rule, arm building) became
  testable in `tests/test_compare.py` once they left the typer commands.
* Good, because `compare.py` returns rows and the CLI owns every local write, so the comparisons
  stay read-only by construction and their output needs no row in `docs/architecture.md` §4.
* Bad, because `diagnostics/` duplicates some construction that `build_deps` also does, on
  purpose.

### Confirmation

`tests/test_core_imports.py` fails if `service_layer/` or `domain/` imports `cyris.adapters` or
`cyris.bootstrap` at runtime, and fails again if anything below `diagnostics/` imports it.

## More Information

Extracted on 2026-10-08 from these sources at commit `c353d3f`:

* `docs/architecture.md:186-208` (*`diagnostics/` — the tools that inspect the deployment*).
* `docs/architecture.md:1220-1221` (§7 rows #18 and #19, the two options the ticket named and the
  drifted scorable filter).
