---
status: accepted
date: 2026-10-17
decision-makers: ""
---

# Changes to an existing D1 database are versioned steps recorded in a version table

## Context and Problem Statement

`schema.sql` creates a table only when it is missing: every statement is `CREATE ... IF NOT
EXISTS`, so an existing table keeps its old shape. A column added there reaches a fresh database
and never an existing one, and the rename of `source_tier` to `source_lane` could not be shipped
that way. Existing deployments hold articles and source definitions in the old shape, so the
change has to rewrite their data as well as their columns. How a database that already exists is
changed was undecided, and each option moves a different amount of state.

## Decision Drivers

* A boot must reach the same shape on an old database and on an empty one.
* A rewrite of stored data must run once, even when a boot is interrupted part way.
* Nothing may run against a database whose shape a step does not expect.
* The Worker `workers/rss` also creates tables in the same D1, so a mechanism must not assume
  cyris alone touches the schema.

## Considered Options

* A. `schema.sql` holds the final shape, and ordered steps in the code change an existing
  database. Each step has a precondition and is recorded in a version table in D1.
* B. A frozen baseline: `schema.sql` keeps the old shape, and a step builds the new one on top.
* C. Detect a fresh database and stamp it: an empty database gets the final shape, and an
  existing one runs the steps.
* Wrangler migrations, applied by a deployer with `wrangler d1 migrations apply`.
* Keep `CREATE IF NOT EXISTS` only and change existing databases by hand.

## Decision Outcome

Chosen option: A, "`schema.sql` holds the final shape, and ordered steps change an existing
database", because it runs on the same boot path that already creates the tables, so a one-click
deployment and a `cyris sources push` on a fresh account reach the same shape without a deployer
running anything. Wrangler migrations would make the schema depend on a step the deploy button and
CI do not run. Hand changes would leave an old database different from a fresh one, which is the
failure this decision exists to prevent.

B was rejected because `schema.sql` and `SqliteD1()` would keep showing the old shape, so the
schema the tests and a reader see would not be the one deployed, and dozens of tests would change.
C was rejected because it is the fresh-versus-existing fork that the first driver forbids, and a
first boot that stopped half-created would be ambiguous: the detection could not tell it from an
existing database.

The mechanism, in `src/cyris/adapters/store/migrations.py`:

* `schema.sql` runs first on every boot, then each step in `MIGRATIONS` whose id is not in
  `schema_migrations` runs, in list order, and its id is recorded.
* A step may carry a `Requires(table, column, present)` precondition. It holds when the table
  exists and the column is present, or absent, as stated. When it does not hold the step's SQL is
  skipped and its id is still recorded, so a database that already has the new shape is not
  rewritten.
* A step's statements are ordered so an interrupted run can be finished by the next boot: data
  rewrites come first, and a rename of the column the precondition reads comes last. A rename
  first would leave the precondition false before the data was rewritten.
* A step whose SQL fails stops the run, is not recorded, and names its id in the error. Ids in the
  table that the code does not know are left alone.

The hazard this decision creates: a statement in `schema.sql` that names a column a later step
adds, such as an index on that column, fails on an existing database. `schema.sql` runs before
the steps, so the column is not there yet, the script fails, and the boot stops before any step
runs. An index on a step-added column belongs in that step. The step's precondition is what makes
its `ADD COLUMN` safe to ship: it runs when the column is absent and records itself when the column
is already there. Declaring the column in `schema.sql` is harmless, since `CREATE TABLE IF NOT
EXISTS` does nothing on a table that exists.

### Consequences

* Good, because an old and an empty database end with the same column set, which a test pins for
  both boot paths.
* Good, because an `ADD COLUMN` is now a step, not a schema change, and reaches an existing
  deployment on its next boot, not only a fresh one.
* Bad, because every boot reads `schema_migrations` and each new step adds a permanent entry in
  the code that must stay until no database predates it.
* Neutral, because the version table lives in the same D1 as the Worker's tables, so the Worker is
  not versioned by it.

### Confirmation

`tests/test_schema_migrations.py` runs each rule above against a local sqlite database: each
step runs once, a failed step stops the run, a precondition skips the SQL, an interrupted lane
migration finishes on the next boot, and an `ADD COLUMN` step runs on both starting shapes.
`tests/test_first_boot_schema.py` checks that both D1 entry paths end in the same shape.

## Pros and Cons of the Options

### A. A version table in D1, read by cyris on every boot

* Good, because the boot that creates the tables also applies the steps.
* Good, because a step can rewrite data, which wrangler's schema migrations also allow but not
  from the same boot.
* Good, because `schema.sql` stays the readable final shape.
* Bad, because the mechanism is cyris code that a future change must keep ordered.
* Bad, because a statement in `schema.sql` must not index a column a step adds.

### B. A frozen baseline

* Good, because `schema.sql` never changes, so a fresh database is built by the same statements
  as before.
* Bad, because `schema.sql` and `SqliteD1()` would show the old shape, and dozens of tests
  would change to match it.

### C. Detect a fresh database and stamp it

* Good, because a fresh database skips the steps entirely.
* Bad, because a boot that stops half-created cannot be told apart from an existing database,
  so the steps may run against a partial shape.

### Wrangler migrations

* Good, because it is the platform's own tool with its own history table.
* Bad, because it needs a deployer to run it, and a one-click deployment never does.
* Bad, because it does not run from the boot path that `cyris sources push` uses on a fresh
  database.

### Keep `CREATE IF NOT EXISTS` only

* Good, because there is nothing to maintain.
* Bad, because an existing database never receives a new column or a rewritten value, which
  silently leaves it on the old shape.

## More Information

`docs/architecture.md` §6 describes the boot order and §4 names the `schema_migrations` row. The
first step the mechanism runs is `0001_tier_to_lane`, the rename of `source_tier` to `source_lane`
and of the tier values to lanes.
