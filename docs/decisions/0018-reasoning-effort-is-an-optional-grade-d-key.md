---
status: accepted
date: 2026-10-18
decision-makers: ""
---

# Reasoning effort is the one optional grade-D key

## Context and Problem Statement

The reader wants to choose how hard the model reasons, as a `/settings` choice. That is a
preference, so it is grade D. Two rules collide:

* Production D1 `settings` has no row for it, and the change must not alter what production sends
  until the reader saves one.
* [ADR-0003](0003-one-home-per-backend.md) says "No grade-D value lives in code": a missing key is
  an error where it is read, and the loader names every missing key.

A required key would stop every production run at the first deploy; a default in code would break
the rule the ADR exists to hold.

## Considered Options

* A grade-A constant in code, not a setting
* A required grade-D key, pushed to D1 before the deploy
* An optional grade-D key, `llm_provider.reasoning_effort`, with `default` as a storable value

## Decision Outcome

Chosen option: "an optional grade-D key", because it is the only one that keeps production
unchanged without a deploy-ordering step and still leaves the choice with the reader.

* The values are `default`, `low`, `medium` and `high`. `default` means what each provider ran
  with before the setting existed, listed per provider in `reasoning_effort_defaults.json`.
* An absent row and a stored `"default"` behave identically, so the exception is not a value
  hidden in code: `default` is a value a reader can store and read back.
* It is the one key `GRADE_D_KEYS` does not require. The guard tests hold the exception to this
  one key, and to a key whose default is storable.

### Consequences

* Good, because production keeps its behaviour on deploy and `doctor` has no new missing key.
* Good, because the reader can move off the default and back without a code edit.
* Bad, because the rule "a missing key is an error" now has one named exception to remember.

## More Information

[architecture.md §5](../architecture.md) grades the key and gives the per-provider defaults.
