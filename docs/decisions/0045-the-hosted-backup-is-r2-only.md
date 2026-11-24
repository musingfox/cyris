---
status: accepted
date: 2026-11-24
decision-makers: "the owner"
---

# The hosted backup is R2 only, and its second copy is left to an evaluation

## Context and Problem Statement

[ADR-0041](0041-the-hosted-databases-are-exported-daily-to-r2-and-b2.md) exports every hosted
tenant's database and the operator's registry once a day, and decided on two copies: R2, and a
second one outside Cloudflare in Backblaze B2, so that a suspended or lost Cloudflare account
would not take the backups with it.

The operator never had a Backblaze account and will not use B2. With the four
`CYRIS_HOSTED_B2_*` settings unset, which they have always been, the Worker sent the operator one
`backup_b2_unset` alert at every backup hour: "Daily backup has no off-Cloudflare copy: B2 is not
configured". It said the same thing every day and asked for something the operator will not do,
so it was noise that hid the alerts that mean something. The B2 code, its SigV4 signer and its
secrets were also maintained for a copy that does not exist.

## Decision Drivers

* The operator will not open a Backblaze account.
* An alert that fires every day and cannot be acted on teaches the operator to skip alerts.
* The risk ADR-0041 named, the loss of the Cloudflare account, has not gone away, and this record
  does not claim it has.
* Which provider holds a second copy is a question of its own, with its own cost, credential and
  key handling, and it should not be answered by default inside a removal.

## Considered Options

* Remove the B2 copy and its alert, and leave the second copy open.
* Keep the B2 code and make `backup_b2_unset` quiet when no B2 setting is set.
* Replace B2 with another S3-compatible provider now.

## Decision Outcome

Chosen: "Remove the B2 copy and its alert, and leave the second copy open", because the owner
decided it on 2026-11-24, and it is the only option that removes the dead code, the secrets and
the noise together without choosing a provider that nobody has evaluated.

The daily export goes to the R2 bucket `cyris-hosted-backup` only. `backupAll` makes no request
outside Cloudflare, and a run whose R2 writes succeed sends no alert. The R2 alerts stay as
[ADR-0041](0041-the-hosted-databases-are-exported-daily-to-r2-and-b2.md) fixed them: an export
that failed, an R2 write that failed, no R2 binding, a tenant list that could not be read, and
the check's missing or unreadable registry export.

Removed: the B2 upload and `B2_PUT_TIMEOUT_MS`; `B2_SETTINGS` and the four secrets
`CYRIS_HOSTED_B2_ENDPOINT`, `CYRIS_HOSTED_B2_BUCKET`, `CYRIS_HOSTED_B2_KEY_ID` and
`CYRIS_HOSTED_B2_APPLICATION_KEY`, with their prompts in `scripts/hosted.sh secrets`; the alerts
`backup_b2_failed` and `backup_b2_unset`; and `workers/app/src/s3_sigv4.js` with its test, which
nothing else imported. The measure that `backupAll` logs and answers no longer has a `b2` count.

A deployment that already set the four secrets leaves four unused secrets on the Worker. The
operator can delete them with `wrangler secret delete`; nothing reads them.

Where a second copy goes, if one does, is not decided here. A separate evaluation of cost and mechanism
decides it, and a later ADR will record that decision. Until then the only copy of a hosted
tenant's data outside its Durable Object is in R2, in the same Cloudflare account, and
`scripts/backup_to_sqlite.py` still reads an export on any machine.

### Consequences

* Good, because the operator's alert channel carries no daily alert that cannot be acted on.
* Good, because the Worker holds no credential for another provider, and the hosted secrets list
  is four entries shorter.
* Good, because about 100 lines of signing code, one timeout and a second upload per export are
  gone, and the backup holds each export in fewer places while it is written.
* Bad, because a suspended or lost Cloudflare account takes the exports with the tenants, which
  is the risk ADR-0041 was written against. Point-in-time recovery lives in the same account, so
  it does not help. This stays true until the evaluation lands a second copy.
* Neutral, because the export format, schedule, check, restore and R2 retention are unchanged.

### Confirmation

* `workers/app/test/hosted_backup.test.js`: a backup run whose R2 writes succeed sends no alert
  and logs no `b2` count, and each R2 failure still alerts with its tenant and copy.
* `tests/test_hosted_config.py` holds that `hosted_backup.js`, `hosted_alerts.json`,
  `wrangler.hosted.toml` and `scripts/hosted.sh` name no B2, and that `s3_sigv4.js` is gone.
  `tests/test_hosted_script.py` holds that `secrets` sets no backup secret.
* `workers/app/test/hosted_legal.test.js` holds that the legal pages say nothing about B2.

## Pros and Cons of the Options

### Remove the B2 copy and its alert, and leave the second copy open

* Good, because it ends the daily alert and removes the unused code and secrets.
* Good, because it keeps the provider choice for the evaluation.
* Bad, because the account-loss risk is open until then.

### Keep the B2 code and silence `backup_b2_unset`

* Good, because it is a small change, and a later operator with a B2 account would need no code.
* Bad, because it keeps a signer, four secrets and a timeout for a copy the operator has said
  they will not make, and a silent missing copy is the worse default for a backup.

### Replace B2 with another S3-compatible provider now

* Good, because the account-loss risk would be closed at once.
* Bad, because no provider has been evaluated, and the choice carries a new account, a new
  credential in the Worker's secrets and a new bill for the operator to accept.

## More Information

* `docs/architecture.md` §4 and §5 hold the backup's rows, `docs/hosted-tenants.md` §11 the
  runbook, and `docs/hosting-and-cost.md` the cost of the R2 bucket.
* The owner's decision is dated 2026-11-24. A second copy is evaluated separately.
