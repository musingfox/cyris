# Audit: tests-pin-every-outgoing-request, 2026-10-08

The audit covers the rule in `docs/spec/tests-pin-every-outgoing-request.md` on branch
`test/pin-outgoing-requests`, which forks from `main` at f3b4c23. Line numbers point at
that branch. The "count" and "content" columns give the state on `main` before the audit.
The "action" column says what the branch added.

## Scope

`grep -rlE "respx|MockTransport" tests/*.py` finds 19 files. The spec says 18 because,
on 2026-10-02 at 3fdd1e2, the same grep run over `tests/` also matched `tests/CLAUDE.md`,
which leaves 17 test files. Two files began faking an edge after that date:

- `test_trial_api.py` added `MockTransport` in 1f6a624 on 2026-10-06.
- `test_anthropic_client.py` added respx in 816abf3 on 2026-10-07.

`test_trial_runbook.py` was deleted in 307274d, but it never matched the grep. One more
fake sits outside the grep. `test_doctor.py:650` stands up a local aiohttp `TestServer`
as the deployment's `/login` and `/api/build`, and it is included below. Monkeypatched
callables such as `send_discord_alert` in `test_pre_run_alert.py` replace a function, not
an HTTP edge, so the audit leaves them out.

This audit also found that every file here uses the bare `respx.mock`. That is respx's
global router, which respx 0.23.1 builds with `assert_all_called=False`
(`respx/api.py:8`). An unused route therefore never failed a test. Only a call to
`respx.mock()` with parentheses gets the default `True`.

## Summary

- 19 files and 149 tests fake an edge. A parametrized test counts once.
- 30 tests already pinned the count, with the content they needed.
- 119 tests were brought up to the rule.
- 0 tests are out of scope. The "parser fed a canned response" exclusion never applies,
  because every test here gets its response by having cyris send a request.
- 0 tests are left unfixed.
- No sabotage exposed a production bug.

## Inventory

| File | Test | Count | Content | Action |
|---|---|---|---|---|
| test_anthropic_client.py | `:40` | no | no | Added `call_count == 1`, the `x-api-key` header, the `model` and the `messages` |
| test_anthropic_client.py | `:56` `:67` `:76` `:85` | no | n.a. | Added `call_count == 1` |
| test_bootstrap_run_log.py | `:101` | yes | yes | None |
| test_doctor.py | `:743` `:756` (zero) `:773` `:831` | yes | n.a. | None |
| test_doctor.py | `:650` (aiohttp server) | no | yes | Added `hits == ["/login", "/api/build"]` |
| test_doctor.py | `:789` | no | no | Added `seen == [webhook URL]`, which pins the token-bearing path |
| test_doctor.py | `:803` `:817` `:847` `:1116` `:1130` | no | n.a. | Added `len(seen) == 1`, with a list added at `:847`, `:1116` and `:1130` |
| test_embedding_probe.py | `:77` (zero) | yes | n.a. | None |
| test_embedding_probe.py | `:51` | no | no | Added `[request] = requests` and the Bearer token |
| test_embedding_probe.py | `:64` `:90` `:103` | no | n.a. | Added `len(requests) == 1`. At `:103` the 3 s backoff outlasts the 0.2 s bound |
| test_embedding.py | `:47` `:90` | yes | yes | None |
| test_embedding.py | `:68` | no | no | Added a request list, the method, the path and the Bearer token |
| test_embedding.py | `:109` `:126` | no | n.a. / yes | Added a request list and `len == 1` |
| test_embedding.py | `:146` (zero) | no (adapter counter only) | n.a. | Added `seen == []` |
| test_gemini_client.py | `:91` `:102` `:113` | yes | n.a. | None |
| test_gemini_client.py | `:28` `:76` | no | yes | Added `call_count == 1` |
| test_gemini_client.py | `:53` `:125` `:160` | no | n.a. | Bound the route and added `call_count == 1` |
| test_http_client.py | `:13` | no | n.a. | Bound the route and added `call_count == 1` |
| test_log_redaction.py | `:107` `:155` | no | n.a. | Added a request list and `len == 1` |
| test_mail.py | `:244` `:354` (zero) `:360` | yes | n.a. | None |
| test_mail.py | `:36` `:68` `:222` `:309` `:384` | no | yes | Added `call_count == 1` |
| test_mail.py | `:198` `:330` | no | partial (no credential) | Added `call_count == 1` and the Bearer token |
| test_mail.py | `:57` `:81` `:92` `:113` `:122` `:250` | no | n.a. | Bound the route and added `call_count == 1` |
| test_newsletter_worker_source.py | `:37` | no (`ack.called`) | partial (no credential) | Added the pull and ack counts, and the Bearer token on both requests |
| test_newsletter_worker_source.py | `:65` `:188` `:212` `:238` | no (pull unbound, `ack.called`) | yes | Added `pull.call_count == 1` and `ack.call_count == 1` |
| test_newsletter_worker_source.py | `:109` `:123` `:278` | no (pull unbound; `not ack.called` pins zero) | n.a. | Added `pull.call_count == 1` |
| test_notify.py | `:591` `:722` (zero) `:689` `:735` `:753` | yes | yes / n.a. | None |
| test_notify.py | `:537` | no | partial (body only) | Added `len == 1`, the method and the webhook URL |
| test_notify.py | `:565` `:705` `:780` `:796` | no | yes | Added `len(requests) == 1` |
| test_notify.py | `:812` | no | n.a. | Added a request list and `len == 1` |
| test_openai_client.py | `:104` `:114` | yes | n.a. | None |
| test_openai_client.py | `:30` `:51` `:63` `:72` | no | yes | Added `call_count == 1` |
| test_openai_client.py | `:85` `:94` | no | n.a. | Bound the route and added `call_count == 1` |
| test_pages_deploy.py | `:107` (total equals `DEPLOY_REQUESTS`) `:313` `:374` | yes | n.a. / yes | None |
| test_pages_deploy.py | `:63` | no | partial (no credential) | Added the per-route list with each `Authorization` (API token or upload JWT) |
| test_pages_deploy.py | `:160` `:189` `:201` `:214` | no | n.a. | `_deploying` keeps `handler.requests`. Each test asserts `NOTHING_MISSING_REQUESTS` (4) |
| test_pages_deploy.py | `:225` `:273` `:284` `:300` `:328` `:360` `:388` | no | n.a. | Added a request list and `len == 1` |
| test_pages_deploy.py | `:261` | no | no | Added `len == 1` and the Bearer token |
| test_pages_deploy.py | `:344` | yes | partial (no credential) | Added the Bearer token |
| test_rss_worker_source.py | `:80` | yes | yes | None |
| test_rss_worker_source.py | `:35` `:94` `:110` `:124` `:139` `:154` `:173` | no | n.a., or yes at `:94` (limit) | Bound the route and added `call_count == 1` |
| test_run_digest.py | `:2014` | no | n.a. | Bound the route and added `call_count == 1` |
| test_settings_api.py | `:1083` `:1097` `:1112` (zero, via `respx.calls.call_count`) | yes | n.a. | None |
| test_settings_api.py | `:600` | no | n.a. | Added a request list and `len == 1` |
| test_settings_api.py | `:1034` | no | partial (no credential) | Added `call_count == 1` and the Bearer token |
| test_settings_api.py | `:1061` | no | n.a. | Bound the route and added `call_count == 1` |
| test_trial_api.py | `:81` `:97` `:105` `:113` `:120` `:128` `:151` | no | yes at `:120` (body) and `:151` (token), else n.a. | Bound the fake. Asserts `len(cf.requests)` at 4 with `--email`, else 3 |
| test_trial_api.py | `:135` | no (a set of paths) | yes | Added `len(cf.requests) == len(paths)`, so each path runs exactly once |
| test_trial_api.py | `:160` (zero) | no | n.a. | Added `cf.requests == []` |
| test_trial_api.py | `:217` `:228` `:238` `:248` `:258` `:265` `:276` `:287` `:296` | no | yes at `:296` (SELECT only), else n.a. | Added `len(d1.sql)`: 3 at `:217`, 5 at `:265`, 1 per read elsewhere |
| test_worker_domains.py | `:19` | no | yes | Added `call_count == 1` |
| test_worker_domains.py | `:30` `:38` `:48` | no | n.a. | Bound the route and added `call_count == 1` |
| test_workers_ai_client.py | `:155` `:170` | yes | n.a. | None |
| test_workers_ai_client.py | `:31` `:86` `:96` `:112` | no | yes | Added `call_count == 1` |
| test_workers_ai_client.py | `:57` `:70` `:121` `:131` `:180` `:190` `:205` | no | n.a. | Bound the route and added the count, which is 2 at `:131` and 1 elsewhere |

## Sabotage receipts

The auditor backed up each production file with `cp`, ran the sabotage, ran the tests
named here, and restored the file with `cp`. After each restore, `git diff -- src/ scripts/`
was empty. "New" means one of this branch's assertions failed. "Pre-existing" means an
assertion already on `main` failed.

- `adapters/anthropic_client.py`: A duplicated `messages.create` failed 5 of 5 tests with
  `2 == 1` (new). Dropping the call failed 5 of 5 with an `AttributeError`. A changed
  message body failed `:40` on the new `messages` assertion.
- `adapters/gemini_client.py`: A duplicated post in the retry loop failed 8 of 8 tests
  (new, plus the pre-existing `4 == 2`). One attempt fewer failed `:102` with `1 == 2`
  (pre-existing).
- `adapters/openai_client.py`: A duplicated post failed 8 of 8 tests with `2 == 1`. Skipping
  the retry failed `:104` with a `RuntimeError`.
- `adapters/workers_ai_client.py`: A duplicated post failed 13 of 14 tests. The 14th test
  sends no request. Skipping the retry failed `:155` with a `RuntimeError`.
- `adapters/http_client.py`: A duplicated GET failed with `2 == 1`. A canned response with
  no request failed with `0 == 1`. Both failures are new.
- `adapters/cloudflare.py`: A duplicated GET failed 4 of 4 tests with `2 == 1` (new). A
  canned response failed 4 of 4: `0 == 1` (new), a wrong host list, and `DID NOT RAISE`.
  A wrong token failed `:19` on the pre-existing header assertion.
- `adapters/embedding.py` (test_embedding, test_embedding_probe): For Gemini, a resend
  failed 4 tests with `2 == 1`, and a canned response with no post failed 5 tests with
  `0 == 1`. For Workers AI, a resend failed 4 tests and a canned response failed 4 tests.
  The account id sent as the token failed 2 tests on the new header assertions.
- `adapters/fetch/rss_worker_source.py` (test_rss_worker_source, test_run_digest): A
  resend failed all 19 items and `:2014` with `2 == 1`. A canned read failed them with
  `0 == 1`.
- `adapters/fetch/newsletter_worker_source.py`: A duplicated ack failed 5 tests with
  `2 == 1`, where `ack.called` alone had passed. A dropped ack failed 5 tests with
  `0 == 1`. A duplicated pull failed 8 tests with `2 == 1`. All of these are new.
- `adapters/mail.py` (test_mail, test_bootstrap_run_log, test_settings_api): A duplicated
  send failed 16 tests in test_mail and test_bootstrap_run_log, plus 2 in
  test_settings_api, all with `2 == 1`. A canned response with no send failed 17 tests
  with `0 == 1`, plus `0 == 1` and `200 == 400` in test_settings_api. A wrong digest token
  failed `:198` on the new header assertion.
- `adapters/notify.py` (test_notify, test_log_redaction): A duplicated digest post failed
  4 tests with `2 == 1`, and a skipped digest post failed the same 4 with `0 == 1`. A
  duplicated alert post failed 6 tests with `2 == 1`, and a skipped alert post failed
  7 tests with `0 == 1`.
- `adapters/output/pages_deploy.py`: A duplicated upsert-hashes call failed 8 tests with
  `5 == 4`, the route list and `6 == 5`. A skipped upsert failed 8 tests with `3 == 4`. A
  `_call` that sends every request twice also failed all the single-request tests with
  `2 == 1`. The API token sent in place of the JWT failed `:63` on the route list.
- `diagnostics/doctor.py`: A duplicated Discord GET failed 5 tests, 3 on new assertions. A
  canned Discord response failed 4 tests with `0 == 1`. A duplicated egress GET failed
  `:1116`, and a duplicated `/api/build` GET failed `:650`.
- `adapters/embedding.py` (test_settings_api): A Gemini resend failed `:600` with `2 == 1`.
  A canned response failed it with `200 == 400`.
- `scripts/trial_api.py`: Probing each permission twice failed 11 tests with `8 == 4` or
  `6 == 3`. Skipping the D1 probe failed 11 tests with `3 == 4`, `2 == 3` or `0 == 1`. A
  duplicated digest_runs query failed 9 tests with `2 == 1`, `4 == 3` or `10 == 5`.

## Binding the count

`tests/test_respx_routes_pin_counts.py` checks every respx route. On this branch it passes,
and on `main` it flags 68 routes across 11 files. To show it fails, the auditor planted two
violations and restored each file with `cp` afterwards:

- Deleting `assert route.call_count == 1` from `test_http_client.py` made the guard fail
  with "call_count never read".
- Unbinding a route in `test_anthropic_client.py` made the guard fail with "route not
  bound to a name".

Its parametrized self-test keeps six planted cases as a lasting receipt. The spec's prose
records the conclusion.
