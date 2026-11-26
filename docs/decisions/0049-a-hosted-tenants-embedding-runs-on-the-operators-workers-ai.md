---
status: accepted
date: 2026-11-26
decision-makers: "the owner"
---

# A hosted tenant's vote similarity embeds on the operator's Workers AI

## Context and Problem Statement

Vote similarity holds back candidates that sit close to an article the reader voted down
(ADR-0017). A self-hosted reader turns it on in `/settings`, and the save checks the embedder with
one real call first. A hosted tenant could not turn it on. The policy kept
`vote_similarity.enabled` out of `writable_settings`, so the shared settings page locked the switch
for the operator, the seed stored it off (`src/cyris/settings_starting_values.json`), and since
milestone M8 the vote form's route has saved the seeds alone and refused a change to the switch.

Milestone M7 of the edition-parity plan (row 14, the owner's decision Q3 on 2026-11-24) gives the
tenant the switch, so that its next run embeds on the operator's Workers AI account and the cost
lands in its month as `embedding_usd`
([ADR-0038](0038-a-hosted-tenants-ai-spend-is-metered-in-the-registry.md)). Some of this was
already in place. A hosted container is already handed `CLOUDFLARE_EMBEDDING_API_TOKEN` and
`CLOUDFLARE_ACCOUNT_ID` when the Worker holds them (`HOSTED_SECRETS` in
`workers/app/src/hosted_container.js`). `MeteredEmbedder` already prices a Workers AI batch by the
neurons its answer reports (`src/cyris/adapters/hosted_spend.py`). But `scripts/hosted.sh secrets`
set neither variable, so the page showed the embedder's key as missing.

Four questions were open. Whose account and key the embedding runs on. Which provider and model it
uses. Whether a save that turns the switch on checks anything. And what a tenant row naming another
embedder does at the run.

## Decision Drivers

* A tenant must not be able to spend beyond its monthly allowance, or spend in a way the ledger
  cannot see (ADR-0038).
* A settings save must not start a container (ADR-0040). The Worker must make no model call that
  the ledger does not meter: ADR-0038 records spend outside a run as unmetered.
* A tenant manages no API key. Where a key the reader types would live is still undecided (§5,
  *API keys on the settings page*), and the hosted policy pins every credential-bearing setting.
* One embedder for every tenant. A suppression row and a tracked topic's threshold each name the
  model that set them (ADR-0006, ADR-0017), so a fixed model keeps every tenant's rows comparable
  and the operator's prices known.
* The page and its routes answer one contract on both editions (ADR-0043, ADR-0047).

## Considered Options

* The operator's Workers AI. The embedder is pinned to `workers_ai` and its default model, and the
  switch is the tenant's.
* The tenant's own embedding key, typed on its settings page.
* Another provider on the operator's key, such as Gemini's `gemini-embedding-001`.
* The operator's Workers AI, with the Worker checking the embedder by one real call when the switch
  is saved on.

## Decision Outcome

Chosen option: "the operator's Workers AI, embedder pinned, switch the tenant's". It is the only
option that adds no key a tenant holds, no unmetered call and no new price, and it reuses what the
container, the meter and the ledger already do.

How it is built:

* **The policy.** `workers/app/src/hosted_settings_policy.json` adds `vote_similarity.enabled` to
  `writable_settings`, which now lists fifteen keys, held to on or off (`onOff` in
  `hosted_settings_rules.js`). It also names the embedder, `embedding_provider` `workers_ai` and
  `embedding_model` `""`, which is the provider's default (`@cf/baai/bge-m3` in
  `src/cyris/provider_defaults.json`). The empty model is the seed's own starting value, so no
  stored row changes.
* **The pin.** As a run's settings load, `pin_tenant_settings` (`src/cyris/adapters/hosted.py`)
  puts back `vote_similarity.provider` and `.model` from the policy, whatever the tenant's rows say,
  as it already did for the LLM provider and model. It also puts back `digest.preference_source`
  as the policy's `preference_source`, `cosine`: the page already locked the vote order, but the
  token Clef falls back to now reaches every hosted container, so a row the container wrote must
  not select Clef. Clef stays out of hosted. The Worker's `hostedValues` shows the page the same
  pinned values. The vote form's route compares a sent embedder with those pinned values, not
  with the rows, so a container-written row cannot block the form. The page shows both keys locked
  for `operator-set`, as it shows the LLM provider and model. A change to either is refused as
  locked (ADR-0047), and neither is ever stored.
* **The save calls no embedder.** On the hosted Worker, `POST /api/settings/vote-similarity` stores
  the switch and the seeds and calls nothing, whether the switch is on or off. Its answer's detail
  is `settings_api.vote_operator_embedder` ("Not checked here: the service's operator provides the
  embedder."). TriageServer keeps the same rule for a server whose edition locks both the provider
  and the model, so the shared cases get one answer from both. `cyris triage-ui` locks neither, so
  a self-hosted save still probes as before. On the page, with both keys locked, the model's
  "Checked with one real call" line is hidden, and the save's busy notice says Saving rather than
  Checking the embedder.
* **The secret.** `scripts/hosted.sh secrets` asks for `CLOUDFLARE_EMBEDDING_API_TOKEN` without
  echo, after the Anthropic key, and sends it to wrangler on stdin. It also sets
  `CLOUDFLARE_ACCOUNT_ID` to the account the operator typed and confirmed. Those are the two
  variables `make_embedder` reads for `workers_ai` (`EMBEDDING_ENV` in `bootstrap.py`). The
  container allowlist already hands both to every run, and the page's `key ready` for `workers_ai`
  needs both (`providerEntry` over `handedSecrets`).
* **The spend.** A hosted run wraps the embedder in `MeteredEmbedder`, which adds each batch's
  reported neurons at `WORKERS_AI_USD_PER_KNEURON` to the run's `embedding_usd`. A batch whose
  answer reports no neurons is not taken as free. The adapter names its texts
  (`usage.unreported`), and the meter treats them as ADR-0038 treats a model with no rate card. Their
  estimated tokens are charged at the dearest per-token embedding rate the meter holds,
  gemini-embedding-001's (`UNPRICED_EMBEDDING_USD_PER_MTOK`), and counted in the report's
  `unpriced_tokens`, which the operator reads in `scripts/hosted.sh spend`. As for an unpriced LLM,
  no alert is sent. The run reports
  that to the registry's `spend_rollup` as it ends, and the tenant's allowance bounds it, as ADR-0038
  set out. No price or column is new.
* **The cost is an estimate, and the trial measures it.** `build_embedder`'s docstring in
  `bootstrap.py` puts a full run at about 600 texts and about 20 neurons. That figure was
  extrapolated from one 222-text call on 2026-08-27 (`adapters/embedding.py`). No hosted run has
  measured it. M7's trial run measures it: the owner sets the secret, alpha's reader turns vote
  similarity on, and the next run's `scripts/hosted.sh spend` shows alpha's `embedding_usd` above 0
  (`docs/hosted-tenants.md`, *The reader's settings page*, step 8).

### Consequences

* Good, because a tenant's vote similarity costs the operator only what its allowance already
  bounds, and the ledger sees all of it.
* Good, because no tenant holds a key, and the Worker makes no model call for a settings save.
* Good, because every tenant embeds with one model, so its suppressions, shadow rows and tracked
  topics name the model the operator prices.
* Good, because the self-hosted page and routes are unchanged: they lock nothing, so they probe as
  before.
* Bad, because a save cannot tell the reader that the operator's token is missing or wrong. The run
  then skips vote similarity with `embedding failed`. When Workers AI refuses the token with a 4xx,
  the operator also gets the run's `Vote similarity did not run` alert through the operator gateway
  (ADR-0035). The page's `key ready` is the only
  sign before a run, and it shows only that the token is set, not that it works.
* Bad, because an embedding Workers AI answers without `meta.neurons` costs an unknown amount,
  and the meter charges it at an estimate: its tokens at the dearest per-token embedding rate,
  counted in `unpriced_tokens` (above). The trial's `unpriced_tokens` of 0 is what shows the
  answers carry neurons.
* Neutral, because the token also reaches Clef, which falls back to the embedding token when
  `CLOUDFLARE_AI_TOKEN` is blank. The run's vote-order pin (above) keeps a row the container wrote
  from asking for Clef, which stays out of hosted.
* Neutral, because every neuron is charged at the paid rate. The account's free daily allowance is
  the operator's, not a tenant's (ADR-0038).

### Confirmation

* `workers/app/test/shared_settings_cases.json`, run by `workers/app/test/hosted_shared_settings.test.js`
  and `tests/test_shared_settings_cases.py`, holds the following on both servers: the switch turned
  on and off with the operator's embedder as held, with no embedder called; the switch turned on
  with another provider, a changed model, or a value that is neither on nor off, each refused; the
  hosted edition's `locked` without the switch; the pin of a tenant row naming Gemini; and the pin
  of a row naming Clef.
* `workers/app/test/hosted_settings_cases.json` holds `enabled` true and false as accepted and the
  provider and model as operator keys, against the Worker and `validate_setting`.
* `hosted_shared_settings.test.js` checks two more things: turning the switch on makes no `fetch`
  and answers in the catalog's words, and a row naming Gemini is shown, and compared, as the pinned
  `workers_ai`.
* `tests/test_hosted_settings_pin.py` checks two loads. A tenant row naming Gemini and its model
  loads as the policy's embedder, with the switch kept on. A row naming Clef loads as `cosine`. A
  self-hosted load keeps its rows.
* `tests/test_embedding.py` checks that a Workers AI answer with no neurons names its texts.
  `tests/test_hosted_spend.py` checks that such texts are charged as unpriced, never as free.
* `tests/test_hosted_spend.py` drives the hosted wiring from the tenant's rows with the switch on
  and Gemini named. The run embeds only through the operator's account URL with the operator's
  token, and its report carries `embedding_usd` above 0, priced from the neurons reported.
* `tests/test_hosted_script.py` checks that `secrets` sets `CLOUDFLARE_EMBEDDING_API_TOKEN` and
  `CLOUDFLARE_ACCOUNT_ID` on stdin, never in argv or output.
* `scripts/settings_probe.py`'s `locked-embedder-promises-no-call` and
  `locked-embedder-save-checks-nothing` check the page in Chromium, each with its sabotage.
* Unverified here: real Workers AI answers on the trial account and their neurons. That is the
  owner's trial after deploy (above).

## Pros and Cons of the Options

### The operator's Workers AI, embedder pinned, switch the tenant's

* Good, because the container, the meter, the prices and the ledger already handle it, so the
  change is a policy key, a pin and a secret.
* Good, because Workers AI reports what it charged, so the spend is measured, not estimated.
* Bad, because the operator's one token is every tenant's. A revoked or rate-limited token stops
  every tenant's vote similarity at once.

### The tenant's own embedding key

* Good, because the cost would be the tenant's, with no allowance needed.
* Bad, because a key typed on a page would need a grade-D home for a secret, which §5 still lists as
  undecided, and the policy pins every credential the run uses.
* Bad, because each tenant would pick its provider, so models, prices and suppression rows would
  differ by tenant. Its save would need a probe from the Worker or a container.

### Another provider on the operator's key

* Good, because Gemini's embedder is also wired and priced.
* Bad, because Gemini reports no tokens, so the spend would be estimated at one token per three
  bytes (ADR-0038). Workers AI reports neurons.
* Bad, because it adds a second provider account to the hosted deployment, where Workers AI runs on
  the account the deployment already uses.

### The operator's Workers AI, checked by the Worker on save

* Good, because a missing or wrong token would be refused at the save, in the reader's words.
* Bad, because the call would be spend outside any run, which the ledger does not meter (ADR-0038).
  The token's health is the operator's to fix, not the reader's, so refusing the reader's save
  does not help them.
* Bad, because the Worker would need the embedding route and its secrets in its own code, a second
  embedding client beside the container's.

## More Information

* Amends [ADR-0040](0040-a-hosted-tenant-edits-its-settings-on-the-hosted-worker.md): the writable
  keys gain `vote_similarity.enabled`, and the run's pin gains the embedder, beside the LLM
  provider and model. Consistent with
  [ADR-0038](0038-a-hosted-tenants-ai-spend-is-metered-in-the-registry.md): its meter, prices and
  allowance are used unchanged. The locked controls are
  [ADR-0043](0043-one-settings-page-an-edition-locks-a-key.md)'s, refused as
  [ADR-0047](0047-a-route-refuses-a-change-to-a-locked-key.md) refuses a changed lock.
* `docs/architecture.md` §5 has the policy, the embedder and the switch rows, and the container's
  environment; `docs/hosted-tenants.md` step 3 has the secret, and *The reader's settings page* has
  the trial check.
