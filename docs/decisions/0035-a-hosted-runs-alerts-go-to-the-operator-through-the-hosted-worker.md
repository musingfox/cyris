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

Three questions were open. Where does the operator's alert go? What does the reader still get?
How does each line of a run's log name its tenant?

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
  other path or tenant with 403. It writes `[<tenant>] ` in front of the alert's subject and
  posts that to `CYRIS_HOSTED_OPERATOR_WEBHOOK`, a secret of the hosted Worker that
  `scripts/hosted.sh secrets` sets. It sends with no mentions allowed and cuts the message to
  Discord's 2,000 characters. Each alert leaves one `operator alert sent` or `operator alert not
  sent` line, with the tenant and the subject, in the Worker's logs. A webhook that is unset is
  answered 503, and one that refuses or cannot be reached is answered 502.
* **What the run sends.** `bootstrap.build_operator_alert` binds `adapters/hosted.send_operator_alert`
  to the tenant, and only when the store carries a tenant id. Two paths call it with the
  `RunAlert` they hand the reader's channels: `run_digest`, through the optional
  `Deps.send_operator_alert`, and the CLI's could-not-start alert. Every alert kind reaches the
  operator. The body is the raw error, worded in English whatever the tenant's
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
* **Every line names its tenant.** When `CYRIS_STORE_TENANT_ID` is set, the CLI's callback starts
  every line written to stdout and stderr with `tenant=<id> `. It does this at the stream, before
  any command runs, so log records, progress lines, typer messages and tracebacks are all tagged.
  The entrypoint's own lines carry the tenant too. The egress probe adds a `tenant` field and stays
  JSON, and the SIGTERM line is prefixed. The Worker's lifecycle lines for a tenant's container
  (`container stopped`, the start retry) carry `tenant` through `Lifecycle`'s `logFields`.

### Consequences

* Good, because the operator hears of every failure a run can see, by tenant, with the raw error,
  on one channel for all tenants.
* Good, because the tenant in the operator's message comes from the Worker's `props`. A run cannot
  report as another tenant, and no container holds the webhook.
* Good, because the reader still learns that an issue failed, in their own language, and never
  what the error said.
* Good, because one Workers Logs search for `tenant=<id>` finds a tenant's whole run.
* Bad, because a failure the container never reports sends no operator alert. That covers a
  container that never starts, a start the tick gives up on, and a run killed before its alert.
  These are logged with their tenant, in `tick failed` and `container stopped` lines, and
  nothing more. A self-hosted deployment leaves the same failures to Workers Logs.
* Bad, because the operator's channel is one Discord webhook. A second channel (mail, paging)
  needs its own sender in the gateway.
* Neutral, because `digest_runs.summary.error` still holds the raw error in the tenant's own
  database. No hosted route shows it to the reader yet. Per-tenant onboarding, whose first-issue
  step reads it back, must withhold it as the alert does.

### Confirmation

* `tests/test_hosted_run_wiring.py` checks three things. A hosted run that raises posts one alert to
  `http://ops.cyris/t/alpha/alert` with the raw error, while the reader's Discord and mail get
  none of it. A run that fetched nothing tells the operator too. A could-not-start tick tells the
  reader without the error and the operator with it. The same file checks that every line a hosted
  `cyris run` writes starts with `tenant=alpha `.
* `tests/test_run_alert.py` holds `reader_alert_message` to withholding the error for every kind
  that carries one, in both catalogs.
* `workers/app/test/hosted_alerts.test.js` checks the gateway. It names the tenant it was bound to,
  refuses another tenant and a gateway with no tenant, and never logs the webhook's URL.
  `workers/app/test/hosted.test.js` checks that both interceptions are registered before
  `start()`, that no container is handed the webhook, and that the lifecycle lines name the
  tenant.
* `tests/test_entrypoint.py` checks that the probe's line and the SIGTERM line name the tenant.
* `tests/test_hosted_config.py` holds `ops.cyris` to one name on both sides, and keeps the
  webhook out of `HOSTED_SECRETS`.

## Pros and Cons of the Options

### The operator's webhook held by the hosted Worker, through an intercepted gateway

* Good, because the Worker names the tenant and holds the credential.
* Good, because it is the database gateway's pattern, already measured on the trial account.
* Bad, because it adds a second interception to every start, and a gateway class to the Worker.

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
