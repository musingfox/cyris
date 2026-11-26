---
status: accepted
date: 2026-11-26
decision-makers: "the owner"
---

# Mail from an unknown sender becomes a newsletter source the reader keeps or stops

## Context and Problem Statement

A newsletter reached a digest only through a source whose `email_match` named its sender. A
self-hosted reader added that source on `/settings`. A hosted tenant could not add one at all: the
operator added it with `POST /hosted/tenants/<tenant>/newsletter-source`
([ADR-0034](0034-a-hosted-tenants-newsletters-arrive-at-a-random-address.md)), and the tenant's
settings page took RSS sources only
([ADR-0040](0040-a-hosted-tenant-edits-its-settings-on-the-hosted-worker.md)). Mail no source matched
was acked unread on a self-hosted deployment and held 8 days on a hosted tenant, then deleted. So a
reader who forwarded a letter, or subscribed at the deployment's address, had to add its source too,
or lose the issue.

Matching was also loose. A sender was matched when its From was a case-insensitive substring of a
source's `email_match`, so `a@b.com` was filed under `from:xa@b.com`. A forward was matched by
scanning its body for any source's address.

The owner decided on 2026-11-26 that mail from a sender no source names becomes a source of its own,
and that the reader decides on it from the digest card where its issue appears. Four questions were
open: how a sender is told, what a decision leaves behind, which mail never becomes a source, and
where each edition writes a decision made on a digest page.

## Decision Drivers

* A reader should not have to add a source before its mail is read.
* A stopped sender must stay stopped. A newsletter source with no row is made again by its sender's
  next mail.
* The owner's rule: a decision made on the digest must not wait on the self-hosted container. A
  `/settings` visit wakes it, which is slow.
* A page's vote token may write only what is cheap to reverse. ADR-0039 let it carry votes on that
  ground.
* A self-hosted change should ship with the app Worker, which deploys on every push to `main` once
  `scripts/check.sh` passes ([ADR-0024](0024-a-push-to-main-releases-and-deploys.md)). The promote
  Worker deploys outside any workflow.
* Every new source is another stream the LLM reads each run, so their number must be bounded.
* A preview must resolve each message as a run does.

## Considered Options

* Mail from an unknown sender makes a source marked `new`. The reader keeps, stops or restores it
  from the digest card, through the vote door. A stop keeps the row.
* Make the source the same way, but decide on `/settings` only.
* Decide on the digest, with the self-hosted decision written by the promote Worker.
* Delete a stopped source's row and record its sender in a ledger of declined senders.
* Keep the substring sender match.

## Decision Outcome

Chosen option: "mail from an unknown sender makes a source marked `new`, decided on the digest
card through the vote door, and a stop keeps the row". It is the only option that needs no
container start for a decision, no new table and no Worker that ships outside a workflow.

How it is built:

* **Which sender.** `CloudflareNewsletterSource._file`
  (`src/cyris/adapters/fetch/newsletter_worker_source.py`) reads one address per message,
  lower-cased and whole: its header From. A From that is one of the reader's own addresses is a
  forward, read for the sender it quotes (*Forwards*, below), before any source is matched, so no
  source takes it; any other From is matched (below) or makes a source of its own, whatever its body
  quotes, so a newsletter that quotes a forward is filed under its own sender.
* **The reader's own addresses.** `own_address_keys` lower-cases them and drops each `+tag`, so
  `Me+cyris@Gmail.example` is `me@gmail.example`. A self-hosted run takes `notify.email_to` and
  `notify.own_addresses`, a grade-D list the reader edits on `/settings` under Notifications and
  saves through the plain values route, so no test message is sent for it. A hosted run takes the
  same `notify.own_addresses`, which the tenant writes on its own settings page (a key of the
  policy's `writable_settings`, held to the same address-list rule), and its members' addresses:
  the registry's `members` table (`workers/app/src/tenant_registry.js`), the invite list a reader
  signs in with. `HostedRunner.startRun` (`workers/app/src/hosted_container.js`) reads them with
  `members(tenant)` and sets `CYRIS_STORE_MEMBER_ADDRESSES` on that tenant's run container alone.
  A member's list is no full one: a mailbox rule may forward from an address the reader never signs
  in with, such as a work Outlook's. The check uses addresses, never a subject prefix, because a reader may use any
  language.
* **Forwards.** The designed intake paths are a mailbox's automatic forwarding and a subscription
  made with the deployment's address. A Gmail forwarding filter and an Outlook *redirect* keep the
  original From, so their mail is filed under its sender like a subscription's. An Outlook *forward*
  rule, and a manual forward, rewrite From to the reader and wrap the original in a block whose
  words are in the mailbox's language
  (https://support.microsoft.com/office/45aa9664-4911-4f96-9663-ece42816d746). `read_forward`
  reads that block in every language `forwarded_blocks` in `keywords.json` lists, whatever the
  subject says. A From-label line counts when a marker line is the last non-blank line among the
  three above it, or when a header label starts one of the three lines below it. A quoted sender
  that is itself an own address is skipped for the next block. The sender is then matched exactly,
  by `+tag`, or made a source, as a header From is; it is never the reader's own address. The
  article's text starts at the original message, after the block's header lines, so the reader's
  note and the wrapper stay out of the excerpt, and the canonical-link step reads the original
  sender's domain.
* **One matcher in every mode.** `_match_address` takes the source that names the address exactly,
  then one whose address equals it once each side drops its `+tag`, so a source for `list+ai@x.test` takes that
  list's mail and one for `list@x.test` takes the rest. Every address an `email_match` names is
  found whole, `from:(a@x.test OR b@y.test)` included. There is no substring match. The header
  From is checked against the reader's own addresses before it is matched, so a source that names
  one of them, such as one made before the address was known, never takes a forward.
* **A new source.** A run that meets an address no source matches writes a source named by its
  `+tag`-less address: type `newsletter`, lane `read`, `email_match` `from:<address>` and
  `review` `new`, through `D1SourceStore.insert`, which writes only under a name no row holds. The
  run's own copy of the sources gains it, so the sender's next message in the same run matches it.
  Both `CloudflareNewsletterSource` and `TenantNewsletterSource` do this. No source is made for a
  system sender, an address whose name is already a source's, an eleventh new sender in one run
  (`MAX_NEW_SOURCES_PER_RUN`), a hosted tenant at its 30 sources (the policy's `max_sources`,
  counted over every row but a stopped one, in the run and in `TenantDb.saveSource` alike), a
  write that fails, or one of the reader's own addresses. No mail is filtered by its subject: one
  that starts with a reply prefix (`Re:`, `回覆`) is a guess at a private reply, not certain
  content, so it makes its source and is read, from a new sender or a known one. A real private
  reply that becomes an article is the reader's to ignore or stop (the owner, 2026-11-27). A
  system sender's mail is handled as unmatched mail was before: held on a hosted tenant
  for `UNMATCHED_HOLD_DAYS`, acked on a self-hosted one. A preview resolves every message to the source
  a run would, writes no source and acks nothing.
* **Mail a run could not file stays queued.** A sender past either cap, a failed source write, a
  sender whose address is already another row's name, and a forward from an own address whose
  block cannot be read are kept, not acked, on both editions, and logged, so no issue is lost for
  want of a source. They are kept for `UNMATCHED_HOLD_DAYS` (8) from their arrival and then acked,
  on both editions. On a hosted tenant they are also stamped `held_at` and evicted first by a full
  queue. The newsletter Worker stamps each KV message `received_at` as it arrives; one queued
  before it did is held from its `date`, and one with neither stays queued, since an ack would lose
  it unseen. A cap backlog drains ten senders a run. `GET
  /newsletters` follows the KV list's cursor past each 1000-key page and reads the values by bulk
  get, 50 keys a call, so no backlog hides newer mail. Both live in `workers/newsletter`, which
  deploys outside any workflow: they take effect only once someone deploys that Worker by hand.
  Until then every KV message lacks `received_at`, so the hold counts from its `date`, which for
  an issue an auto-forward carries late can be days before it arrived, so it is acked that much
  sooner; and `GET /newsletters` still reads one 1000-key list page, one get a key, so past 1000
  queued keys newer mail stays hidden and the read can pass an invocation's 1000 operations.
* **System senders.** `system_senders` in `keywords.json` lists exact addresses whose mail never
  makes a source. It starts with `forwarding-noreply@google.com`: Gmail's forwarding confirmation
  is no newsletter, so it must not make a source the reader would then have to stop. On a hosted
  tenant it is held, so its code stays readable in the operator's report; a self-hosted run acks
  it, as it did before. No other classification of notices, receipts or transactional mail exists
  yet.
* **Review states.** A newsletter source's `config.review` is `new` (cyris made it and the
  reader has not decided), `kept` (kept or restored), `declined` (stopped), or absent (an ordinary
  source the reader or the operator added). `kept` reads as an ordinary source everywhere: no mark
  on the digest or the Sources tab, and its mail is filed. It is a value rather than an absent field
  because `cyris sources push` keeps a row only while it holds a review, so a keep that dropped the
  field was undone by the next push, and the sender's next mail made the source `new` again. The
  decisions are `keep`, which sets `kept`, `stop`, which sets `declined`, and `restore`, which
  sets `kept`, each from any state, and making one twice is making it once
  (`SOURCE_DECISIONS` in `src/cyris/domain/models.py` and `workers/app/src/source_review.js`). An
  RSS source takes no decision.
* **A stop keeps the row.** A stopped source keeps its name, `email_match`, lane, homepage and
  tags. Its sender's mail still matches it, so it makes no new source. A run acks that mail with no
  article and never holds it: the KV source's `POST /ack` deletes it, and a hosted run deletes it
  from the tenant's `newsletters`. A preview leaves it queued. A restored source takes its sender's
  next mail.
* **Retire stops a newsletter source.** `DELETE /api/sources/<name>` on both settings servers
  (TriageServer, `workers/app/src/hosted_shared_settings.js`) stops a newsletter source and still
  deletes an RSS one. `POST /api/sources` drops a `review` the client sends and keeps the one the row
  holds. `cyris sources push` keeps every row that holds a review and carries the stored review onto
  a row the file names, so a push neither wipes a stopped or kept row nor revives a source waiting
  for the reader. `POST /api/sources/<name>/review` takes a `{decision}` on both settings servers, and the
  Sources tab's Restore uses it.
* **The vote door carries the decision.** A digest page posts `{source, decision}` to the
  `/api/vote` it already posts votes to. A vote names no source, so a door tells the two apart by
  the `source` key. The answer is `{ok, source, review}`, or an error: `missing source` and `bad
  decision` (400), `no such source` (404), `not a newsletter source` (400) and `no source store`
  (503), or a store's own refusal: `source store failed` (502) when the app Worker's D1 call fails,
  and `not_seeded` (409) for a hosted tenant with no `sources` table yet. The door's `GET` probe adds `reviews`, the review each newsletter source holds, by name, for
  those that hold one, and the page repaints a mark it contradicts. Three doors answer
  `workers/app/test/source_review_cases.json` alike:
  * the self-hosted app Worker writes D1 itself over D1's REST API (`workers/app/src/d1_sources.js`),
    with the `CLOUDFLARE_ACCOUNT_ID`, `CYRIS_STORE_DATABASE_ID` and `CLOUDFLARE_API_TOKEN` it already
    reads `settings` with (`type_scale.js`, [ADR-0013](0013-reader-type-size-is-injected-by-the-worker.md)).
    One `UPDATE` names both the source and its type, so the check and the write are one statement. It
    wakes no container and calls no promote Worker. A vote still goes to the promote Worker, as before;
  * the hosted Worker (`workers/app/src/hosted_vote.js`) writes the tenant's row at once through
    `TenantDb.setSourceReview` and reads `TenantDb.sourceReviews`, for the tenant of the session the
    page's vote token names. A decision is not queued like a vote: it is the tenant's own `sources`
    row, which the Worker already writes for the settings page. Unlike a vote, it stamps no
    `triaged_at` and writes no topic vote, so no Python rule has to apply it;
  * a local `cyris triage-ui` writes its own store.
* **The digest.** The run reads the sources as the store holds them when it renders, so a pebble
  whose source holds a review carries a `New source` or `Stopped` mark, a source made in that run
  included. For a signed-in reader, with the gating that opens the votes, the mark opens a dialog
  that makes the decision (`docs/design/ui-language.md` §4 *source dialog*). When every item of
  the source in the issue was read from a forward from an own address, the dialog adds a line saying
  its mail arrives by forwarding (`digest.source_dialog_forwarded`). The newsletter source keeps
  those articles' URLs in memory for the run, and the writer reads them at render; nothing is
  stored, so a rerender or a regenerate shows no such line.

**Why the vote token may carry it.** ADR-0039 let a token that an injected script can read write
only a vote, because a vote is cheap to reverse. This record widens what the token writes to a
newsletter source's review, and reversibility still holds. Every decision is undone by another one,
from any state. No decision deletes the row, so nothing the reader or the operator set on the
source is lost. Nothing is queued, so there is no backlog to undo. The token reaches only its own
tenant's newsletter sources; an RSS source is refused.

One part cannot be undone: mail that arrives while a source is stopped is deleted at the next run,
and a restore does not bring it back. The owner accepted this on 2026-11-26.

**Why the app Worker and not the promote Worker.** The self-hosted page already posts to the app
Worker's `/api/vote`, and the app Worker already reads D1 over REST with the same three values. A
write there adds no secret and no binding. The promote Worker deploys outside any workflow, with no
gate, so a merged change to it is not live until someone deploys it by hand, and the page's
decisions would then fail. The app Worker deploys on push.

### Consequences

* Good, because forwarded and subscribed mail reaches the next digest with no source added first,
  on every edition, and a hosted tenant no longer needs the operator to add its newsletters.
* Good, because a stop is one decision that lasts. The row that stops the sender is the source
  itself, so no second table can drift from `sources`.
* Good, because a decision on the digest starts no container, and the self-hosted write ships with
  the app Worker.
* Good, because the exact whole-address match ends the substring misfile, and a preview files each
  message where a run does.
* Bad, because mail that arrives while a source is stopped is lost for good (above).
* Bad, because a forged or spam sender to a deployment's address now becomes a source, and its
  mail reaches the digest marked `New source`. Before, such mail reached no digest unless it
  matched a source, which ADR-0034's DMARC consequence relied on. Ten sources a run, and a hosted
  tenant's 30 sources, bound it. A stopped source holds no place under the cap, so a reader who stops
  the spam frees its places; a hosted tenant whose unstopped sources fill the cap makes no source for
  a real new sender, whose mail is then held 8 days.
* Bad, because mail a run cannot file is acked after 8 days on both editions, and no page lists it
  meanwhile: a forward in a wording no listed language reads, or a sender a full cap keeps out, is
  lost after its hold unless the reader acts in time without being told.
* Neutral, because a source made for one of the reader's own addresses before the address was known
  as theirs takes its mail only until the reader lists it as their own (`notify.own_addresses`):
  from then on the own-address check comes first and every forward from it is filed by the sender
  it quotes, so the row matches nothing, stopped or not.
* Bad, because a forward is read only in the languages `forwarded_blocks` lists, and several of
  their words are unverified (below). A forward in any other wording stays queued for 8 days.
* Bad, because a hosted run checks the 30-source cap against its own copy of the sources and writes
  through its gateway, not in the object's synchronous turn that `TenantDb.saveSource` holds. A
  settings save made during a run can therefore take the tenant past the cap.
* Bad, because a script injected into a tenant page can stop that tenant's newsletter sources for
  as long as its vote token lives (ADR-0039), and their mail is deleted meanwhile.
* Bad, because every self-hosted page probe now makes one D1 REST read for the reviews. A failed
  read leaves the marks as the run rendered them.
* Neutral, because the operator's `newsletter-source` route rewrites a row's whole config, so
  rewriting a source through it drops the source's review.
* Neutral, because no settings page deletes a newsletter source any more: Retire stops every one,
  with or without a review, since a row deleted is a row its sender's next mail makes again. A
  mistyped one is stopped, and `cyris sources push` without it removes it only if it holds no
  review.
* Neutral, because a page corrects only the marks it was rendered with: a source kept when the page
  was rendered and stopped later shows no mark there until the next issue.

### Confirmation

* `tests/test_newsletter_worker_source.py` and `tests/test_tenant_newsletter_source.py` hold the
  matcher and creation: the exact `+tag` source before the main address, a forward from an own
  address (any case, any `+tag`, any subject) filed by its quoted sender, each client and language
  sample of `forwarded_blocks`, an unreadable forward, a taken name and an over-cap or failed write
  kept unacked and acked after their hold, a source named for an own address taking no forward, an
  a `Re:` issue read from a new or a known sender, stopped sources holding no place under the cap,
  another sender's mail that quotes a forward filed under its own From, the wrapper stripped from the article, no source for a system sender, a
  declined source's mail acked unread (deleted, not held, on hosted), a new source still taking its
  mail, and a preview filing each message where a run does while writing nothing.
* `tests/test_hosted_run_wiring.py` and `workers/app/test/hosted.test.js` hold a tenant's run
  reading its own members' addresses, and no other tenant's, and the address the tenant entered on
  its settings page.
* `tests/test_source_store.py` holds `set_review`, the push that keeps reviewed rows, and a kept
  source outliving a push and taking its sender's next mail.
* `workers/newsletter/test/queue.test.js` holds `GET /newsletters` reading past one list page and
  `email()` stamping `received_at`; `workers/app/test/hosted_settings.test.js` holds a stopped
  source holding no place under `TenantDb.saveSource`'s cap.
* `workers/app/test/source_review_contract.test.js` runs every case of
  `workers/app/test/source_review_cases.json` against the app Worker over a D1 REST stand-in and
  against the hosted Worker over the tenant's object, and `tests/test_source_review_cases.py` runs
  them against TriageServer.
* `workers/app/test/shared_settings_cases.json`, run against both settings servers, holds the review
  route, a stop on `DELETE`, and a `POST /api/sources` that keeps the stored review.
* `tests/test_html_digest.py` holds the marks: what the store holds at each render, every item of
  the issue counted, no mark when the sources cannot be read, and the decision posted to the vote
  door only, and the forwarding line only when every item came forwarded.
* `scripts/settings_probe.py`'s `stop-keeps-a-newsletter-row` and `restore-a-stopped-source` drive
  the Sources tab in Chromium, and `scripts/digest_probe.py`'s `source-decision-shows-at-once`,
  `source-decision-refused-reverts`, `source-mark-corrected-by-probe` and
  `source-dialog-says-forwarded` drive the source dialog, and `notify-own-addresses-plain` the
  own-addresses field.
* Unverified here: the newsletter Worker's `received_at` stamp and cursor paging on a deployed
  Worker, which needs a deploy by hand (above).
* Unverified here: a decision through a deployed app Worker, which needs `CLOUDFLARE_API_TOKEN` to
  allow D1 writes, and through a deployed hosted Worker on a real page.

## Pros and Cons of the Options

### Make a `new` source, decide on the digest through the vote door, keep the row on a stop

* Good, because the reader decides where the issue is, and no container starts for it.
* Good, because each edition's door already exists and already admits only a signed-in reader.
* Bad, because the vote token now writes more than votes, and one part of a stop cannot be undone.

### Decide on `/settings` only

* Good, because the settings servers already write `sources`, and the token would write votes alone.
* Bad, because a self-hosted `/settings` visit wakes the container, which is slow. The owner rejected
  it on those grounds.
* Bad, because the reader meets the new source on the digest and would have to go elsewhere to
  decide on it.

### The promote Worker writes the self-hosted decision

* Good, because the promote Worker already takes the page's votes.
* Bad, because it deploys outside any workflow, so a change to it ships only when someone deploys it
  by hand, and until then the page's decisions fail.
* Bad, because it holds only its KV queue, and would need D1 credentials that the app Worker
  already holds.

### A ledger of declined senders, deleting the row

* Good, because the Sources tab would list only sources that are read.
* Bad, because a second table would have to agree with `sources` on every path that writes either:
  a push, a settings save, the operator's route and a run's creation.
* Bad, because a restore would rebuild the source from nothing, losing its lane, homepage and tags.

### Keep the substring sender match

* Good, because no existing `email_match` would need to name a whole address.
* Bad, because a substring can only add a wrong match, and with creation a wrong match also stops a
  sender from getting its own source.
* Bad, because a run and a preview would need two rules once creation decides by whole address.

## More Information

* Amends [ADR-0039](0039-a-hosted-vote-is-queued-by-the-worker-and-applied-by-the-run.md): the vote
  token also carries a source decision, which is written at once (above). Amends
  [ADR-0034](0034-a-hosted-tenants-newsletters-arrive-at-a-random-address.md): its *Sources*,
  *Pulling* and *Unmatched mail waits* no longer describe how a hosted tenant's mail finds its
  source. Amends [ADR-0040](0040-a-hosted-tenant-edits-its-settings-on-the-hosted-worker.md): a
  newsletter source on the settings page is stopped, not removed. A local install's door is
  [ADR-0044](0044-a-local-install-takes-votes.md)'s server, which takes the decision the same way.
* Where each word of `forwarded_blocks` comes from. Most are the forwarded headers in the test
  fixtures of crisp-oss/email-forward-parser (https://github.com/crisp-oss/email-forward-parser/tree/master/test/fixtures), by file:
  * en: `Forwarded message` and `From`, `Date`, `Subject`, `To`, `Cc` (`gmail_en_body.txt`);
    `Begin forwarded message` (`apple_mail_en_body.txt`); `Sent` (`outlook_live_body.txt`).
    Gmail keeps its English marker in every interface language (`gmail_de_body.txt`,
    `gmail_fr_body.txt`, `gmail_es_body.txt`).
  * de: `Anfang der weitergeleiteten Nachricht`, `Von`, `Betreff`, `Datum`, `An`, `Kopie`
    (`apple_mail_de_body.txt`, `new_outlook_2019_de_body.txt`).
  * fr: `Début du message réexpédié`, `De`, `Objet`, `Date`, `À`
    (`apple_mail_fr_body.txt`, `new_outlook_2019_fr_body.txt`).
  * es: `Inicio del mensaje reenviado`, `De`, `Asunto`, `Fecha`, `Para`
    (`apple_mail_es_body.txt`, `new_outlook_2019_es_body.txt`).
  * ja: `メッセージを転送`, `送信元`, `日付`, `件名`, `送信先` (`hubspot_ja_body.txt`, a HubSpot
    forward).
  * The subject prefixes `WG`, `TR` and `RV` (`outlook_live_de_subject.txt`,
    `outlook_live_fr_subject.txt`, `outlook_live_es_subject.txt`), kept for reply and forward
    subjects only; no forward is told by its subject.

  Outlook's header labels `Gesendet`, `Envoyé`, `Enviado` and the zh-Hans `发件人`, `发送时间`,
  `收件人`, `主题`, `抄送` are from mail-parser-reply's
  (https://github.com/alfonsrv/mail-parser-reply/blob/main/mailparser_reply/constants.py). The
  Japanese Outlook labels `差出人`, `送信日時`, `宛先` are from
  https://learn.microsoft.com/ja-jp/answers/questions/4565102/outlook, and `転送メッセージ` from
  https://www.cis.twcu.ac.jp/~konishi/lit20/lit20b.html. The zh-Hans `时间` is from a plugin's
  forwarded header (https://github.com/STARDUSTLC666/dsh-email/issues/40), not a mail client's.

  The zh-Hant `開始轉寄郵件`, `寄件人`, `標題`, `日期`, `收件人` and `回覆給` are from a real
  forward written by a zh-Hant mail client in the Apple Mail layout (a marker line, then quoted
  header lines) on 2026-11-26; `標題` and `回覆給` were missing until then, so the forward's
  remaining header lines leaked into its article text.

  Unverified: the ja `転送されたメッセージ` (Apple Mail), the zh-Hans `转发的邮件` (Gmail), and
  the zh-Hant `已轉寄郵件`, `轉寄的郵件`, `寄件者`, `寄件日期`, `收件者`, `主旨`, `副本`. A sample of a
  real forward would confirm or replace each.
* `docs/architecture.md` §3.1 describes the matcher and creation, §4 the review states of
  `sources`, §5 `MAX_NEW_SOURCES_PER_RUN`, `system_senders`, `forwarded_blocks`, `notify.own_addresses`
  and the hosted member addresses, and §6 the doors.
