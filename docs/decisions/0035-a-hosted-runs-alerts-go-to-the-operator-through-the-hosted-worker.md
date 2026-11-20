---
status: accepted
date: 2026-11-20
decision-makers: ""
---

# A hosted run's alerts go to the operator through the hosted Worker, and its reader never sees the error

## Context and Problem Statement

A self-hosted run sends its failure alert to the reader's own channels: Discord and mail, as
`notify` configures them. The alert's body is the raw error (`run_failed`, `could_not_start`) or
the embedding API's status and message (`vote_similarity_rejected`). That reader is also the
operator, so the raw text is theirs to read and to act on.

A hosted tenant's reader is not the operator. The operator holds the model keys, the account and
the code, so only the operator can fix what a raw error describes. A raw error can also name what
the reader should never see: a provider's message about the operator's key, an account id, or a
URL. Until this decision a hosted run sent no alert at all (`adapters/hosted.py` was a stub,
ADR-0028), so the operator heard of a failed tenant only by reading Workers Logs. Those logs
did not say which tenant a container line belonged to, because every tenant's container writes
to one stream.

Four questions were open. Where does the operator's alert go? What does the reader still get?
How does the operator hear of a run that failed before it could say so itself? How does each line
of a run's log name its tenant?

## Decision Drivers

* The operator must hear of every tenant's failed run, and must know which tenant it was.
* The reader must never be shown the raw error.
* A container holds no credential beyond the model keys (ADR-0028). It must not be able to send
  as another tenant.
* `service_layer/` changes only by calling an optional `Deps` callable (`docs/architecture.md` §8).
* No new external service. Discord is already a channel this project sends to.
* A lost alert must never fail or delay a run.

## Considered Options

* The operator's Discord webhook held by the hosted Worker, reached through an intercepted gateway
  bound to the tenant
* The operator's webhook handed to every tenant's container
* Workers Logs only, with the operator searching or alerting on it
* Mail to the operator through the Email Sending API

## Decision Outcome

Chosen option: "the operator's Discord webhook held by the hosted Worker, reached through an
intercepted gateway bound to the tenant". It is the only option where the tenant id the operator
reads comes from the Worker rather than from the container. It is also the only one where no
container holds the operator's credential. It reuses the pattern `db.cyris` already proved
(ADR-0028).

How it is built:

* **The operator's channel.** Before every `start()`, a tenant's run object registers
  `interceptOutboundHttp("ops.cyris", ...)` to `OperatorAlertGateway`
  (`workers/app/src/hosted_alerts.js`), beside the database gateway. `props.tenant` is set to the
  object's own name. The gateway answers only `POST /t/<props.tenant>/alert` and refuses every
  other path or tenant with 403. It hands the alert to `alertOperator`, which posts it to
  `CYRIS_HOSTED_OPERATOR_WEBHOOK`, a secret of the hosted Worker that `scripts/hosted.sh secrets`
  sets. Each alert leaves one `operator alert sent` or `operator alert not sent` line, with the
  tenant and the subject, in the Worker's logs. A webhook that is unset is answered 503, and one
  that refuses or cannot be reached is answered 502.
* **The message.** `operatorMessage` writes `[<tenant>] <subject>` on the first line and the body,
  the raw error, in a code block below it. The subject has its line breaks replaced by spaces, so
  it cannot forge a second `[<tenant>]` line. It also has Markdown's characters escaped, so it
  cannot draw a masked link, and it is cut to 200 characters. Nothing inside the code block
  renders. The body's backticks are broken up with a zero-width space, so the body cannot close
  the block. The whole message is cut to Discord's 2,000 characters, never inside a character,
  and the block stays closed. Mentions are off. A `429` is not paced or retried. A shared cause
  that fails every tenant in one tick, such as a revoked model key, can drop some alerts. Each
  dropped alert keeps its `operator alert not sent HTTP 429` line, and the alerts that land
  already name the cause.
* **What the run sends.** `bootstrap.build_operator_alert` binds `adapters/hosted.send_operator_alert`
  to the tenant, and only when the store carries a tenant id. Two paths call it with the
  `RunAlert` they hand the reader's channels: `run_digest`, through the optional
  `Deps.send_operator_alert`, and the CLI's could-not-start alert. Every alert kind reaches the
  operator. The CLI tells the operator whenever a hosted run stops before `run_digest`: on its
  settings, on its wiring, or on a store that answered an error before any setting was read. This
  holds whatever started it, the tick's `--if-due`, a catch-up or a manual `--period`. Only a
  dry run is exempt. The reader's could-not-start alert keeps its rule: a scheduled run's digest
  hour only, since a self-hosted tick fails every hour. The operator's needs no such rule,
  because the hosted Worker starts a container only when a run is due. With no setting read, the
  tenant comes from the environment and the alert is in UTC. The payload carries the alert's
  `kind` beside its subject and body. The body is the raw error, worded in English whatever the tenant's
  `digest.output_language`, since one operator reads every tenant's alerts. Each send has its own
  guard and is never retried, so a lost alert is logged and the run's own result stands.
* **What the reader gets.** The reader is still sent each alert on their own channels, as `notify`
  configures them. The error is withheld: `run_alert.reader_alert_message` words `run_failed` and
  `could_not_start` with `alert.error_withheld_body`. It words `vote_similarity_rejected` with
  `alert.vote_similarity_withheld_body`, which names neither the embedding API's status nor its
  message. `fetched_nothing` keeps its body, because the sources it names are the reader's own.
  `bootstrap.build_alert_senders` hands the senders that wording for a hosted tenant, so no path
  can send the raw body to the reader. A hosted container has no Cloudflare API token, so its
  reader's mail alert is not sent and a warning is logged. The digest notification stays a stub
  that sends nothing.
* **The Worker's own alerts.** Some failed runs never reach the code that would report them, so
  the tenant's run object (`TenantContainer`) sends those alerts through `alertOperator`, worded
  by `workers/app/src/hosted_alerts.json`:
  * `startRun` throws. That covers the 90-second give-up while no instance is free, an unset
    hosted domain, and a registry that cannot be read. The alert is "Digest run could not start",
    with the period, the date and the error, and then the error is rethrown. A tenant the
    registry does not hold as active is refused without an alert, since no run of it was due.
  * The container exits non-zero or is killed. That covers an OOM kill, a SIGKILL, and the
    inactivity cap's SIGTERM, after which the run sends nothing itself. `Lifecycle` calls
    `onAbnormalStop` with `monitor()`'s error, and the alert is "Digest run stopped before it
    reported", with the exit code.

  `alertOperator(env, tenant, alert)` is one exported function that never throws. A future
  caller, such as a run queue's "run failed" or "run lost", sends with it as these two do.
* **One alert per failure.** A run that fails usually reports it and then exits non-zero, and the
  Worker would otherwise repeat that alert. So when the gateway receives an alert whose `kind`
  ends a run (`run_failed`, `could_not_start`, `fetched_nothing`), it records the kind in the
  tenant's run object first (`TenantContainer.alertReported`, storage key `reported`). It does
  this before it posts. The record is cleared before every start. On an abnormal stop the run
  object alerts only when no record exists. A vote-similarity warning ends nothing, so it is not
  recorded, and a crash after one still alerts. A start that throws always alerts, because no
  container ran to report anything. The record is made when the alert is received, whether or
  not the webhook takes it, because the Worker's own alert would go to the same webhook.
* **Every line names its tenant.** When `CYRIS_STORE_TENANT_ID` is set, the CLI's callback starts
  every line written to stdout and stderr with `tenant=<id> `. It does this at the stream, before
  any command runs, so log records, progress lines, typer messages and tracebacks are all tagged.
  The entrypoint's own lines carry the tenant too. The egress probe adds a `tenant` field and stays
  JSON, and the SIGTERM line is prefixed. The Worker's lifecycle lines for a tenant's container
  (`container stopped`, the start retry) carry `tenant` through `Lifecycle`'s `logFields`.

### Consequences

* Good, because the operator hears of each failed run, by tenant, on one channel for all tenants.
  That covers a run that raised, fetched nothing, or stopped before `run_digest` in any mode,
  each with its raw error, and a start that threw or a container that stopped abnormally
  without reporting, each with the Worker's detail. Each failure is told once.
* Good, because the tenant in the operator's message comes from the Worker's `props`. A run cannot
  report as another tenant, and no container holds the webhook.
* Good, because the reader still learns that an issue failed, in their own language, and never
  what the error said.
* Good, because one Workers Logs search for `tenant=<id>` finds a tenant's whole run.
* Bad, because some failures still reach no channel:
  * A tenant whose schedule the tick cannot read gets no start and no alert. Its warning line
    names the tenant when the read failed (`tenantRows`), and not when the stored time zone is
    unknown. An alert here would repeat every hour.
  * An alert the webhook refuses (a 429 included) or cannot take is dropped, with its log line.
  * A run that reported why it ended and is then killed has its kill unreported.
* Bad, because every start now registers two interceptions, `db.cyris` and `ops.cyris`. Only one
  was measured on the trial account (ADR-0028), and the unit tests use a fake container. The
  first hosted run on the trial account (`docs/hosted-tenants.md` step 7) must show both answering
  before this is relied on.
* Bad, because the operator's channel is one Discord webhook. A second channel (mail, paging)
  needs its own sender in the gateway.
* Neutral, because `digest_runs.summary.error` still holds the raw error in the tenant's own
  database. No hosted route shows it to the reader yet. Per-tenant onboarding, whose first-issue
  step reads it back, must withhold it as the alert does.

### Confirmation

* `tests/test_hosted_run_wiring.py` checks three things. A hosted run that raises posts one alert to
  `http://ops.cyris/t/alpha/alert` with the raw error, while the reader's Discord and mail get
  none of it. A run that fetched nothing tells the operator too. A could-not-start tick tells the
  reader without the error and the operator with it. A catch-up or manual run whose settings fail,
  a store that answers 500 while settings load, and a `build_deps` that raises each tell the
  operator, and a dry run tells nobody. The same file checks that every line a hosted
  `cyris run` writes starts with `tenant=alpha `.
* `tests/test_run_alert.py` holds `reader_alert_message` to withholding the error for every kind
  that carries one, in both catalogs.
* `workers/app/test/hosted_alerts.test.js` checks the gateway and the message:
  * The gateway names the tenant it was bound to. It refuses another tenant, and a gateway with no
    tenant. It records only a run-ending kind. It never logs the webhook's URL.
  * A subject cannot forge a line or draw Markdown, and a body cannot leave its code block.
  * The cut keeps characters whole.
  * `alertOperator` never throws.
* `workers/app/test/hosted.test.js` checks the run object:
  * Both interceptions are registered before `start()`, and no container is handed the webhook.
  * The lifecycle lines name the tenant.
  * The operator is told when the start gives up after 90 seconds, when it throws at once, and
    when the registry cannot be read. It is not told about a paused tenant.
  * A container that exits non-zero, is OOM-killed, is stopped by the inactivity cap, or stops
    with no exit code tells the operator.
  * It does not when the run already reported, or exited 0, and a new start forgets the last
    start's report.
* `tests/test_entrypoint.py` checks that the probe's line and the SIGTERM line name the tenant.
* `tests/test_hosted_config.py` holds `ops.cyris` to one name on both sides, and keeps the
  webhook out of `HOSTED_SECRETS`.

## Pros and Cons of the Options

### The operator's webhook held by the hosted Worker, through an intercepted gateway

* Good, because the Worker names the tenant and holds the credential.
* Good, because it is the database gateway's pattern, already measured on the trial account.
* Bad, because it adds a second interception to every start, and a gateway class to the Worker.

The double alert had three ways out. The chosen one is the gateway's record in the run object.
An exit-code convention, where a run that reported exits with a code of its own, would claim a
report whose post never reached the gateway. Alerting from both sides would send every ordinary
failure twice.

### The operator's webhook handed to every tenant's container

* Good, because the container posts straight to Discord, with no gateway.
* Bad, because every container would hold the operator's credential, against ADR-0028's rule.
* Bad, because the tenant id in the message would be whatever the container wrote.

### Workers Logs only

* Good, because nothing is built. The run already logs its error.
* Bad, because the operator must go looking. A failed tenant is found only by a search, or by a
  Workers Observability alert, which this project has not set up.

### Mail to the operator through the Email Sending API

* Good, because mail needs no Discord server.
* Bad, because the container would need a Cloudflare API token that can send mail. The token is
  account-wide, and `HOSTED_SECRETS` keeps it out on purpose. Sending from the Worker would need
  an Email Routing domain the trial account does not have.

## More Information

* `docs/architecture.md` §3.2 says who each alert goes to. §5 grades the gateway host, the webhook
  and the alert wording. §6, *Hosted mode is a separate Worker*, describes the gateway.
* `docs/hosted-tenants.md` step 3 sets the webhook.
* [ADR-0028](0028-one-sqlite-durable-object-per-hosted-tenant.md) ends its "sends no Discord
  message and no mail" consequence here for alerts. The digest notification is still not sent.
* [ADR-0027](0027-the-ui-is-worded-from-messageformat-2-catalogs-in-the-output-language.md) is why
  the reader's withheld body is a catalog key.
