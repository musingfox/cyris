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
| test_anthropic_client.py | `test_parses_text_and_usage` :40 | no | no | Added `call_count == 1`, the `x-api-key` header, the `model` and the `messages` |
| test_anthropic_client.py | `test_reads_past_a_leading_thinking_block` :56 `test_joins_every_text_block` :67 `test_warns_when_truncated` :76 `test_warns_on_a_refusal` :85 | no | n.a. | Added `call_count == 1` |
| test_bootstrap_run_log.py | `test_failure_mail_uses_the_account_token_not_the_ai_or_embedding_ones` :101 | yes | yes | None |
| test_doctor.py | `test_a_url_that_is_not_a_webhook_is_rejected_without_a_request` :743 `test_the_masked_value_is_refused_without_a_request` :756 (these two pin zero) `test_a_json_body_that_is_not_an_object_is_reported_not_raised` :773 `test_being_rate_limited_says_so_in_discords_own_words` :831 | yes | n.a. | None |
| test_doctor.py | `TestDeploymentProvenance::test_the_login_cookie_carries_to_the_build_read` :650 (aiohttp server) | no | yes | Added `hits == ["/login", "/api/build"]` |
| test_doctor.py | `test_a_live_webhook_is_named_back_to_the_reader` :789 | no | no | Added `seen == [webhook URL]`, which pins the token-bearing path |
| test_doctor.py | `test_a_wrong_token_is_reported_with_its_status` :803 `test_a_deleted_webhook_is_reported_with_its_status` :817 `test_an_unreachable_discord_is_a_failed_check_not_an_exception` :847 `test_egress_probe_reads_colo_and_location_from_the_trace` :1116 `test_egress_probe_reports_rather_than_raises` :1130 | no | n.a. | Added `len(seen) == 1`, with a list added at `:847`, `:1116` and `:1130` |
| test_embedding_probe.py | `test_a_missing_token_fails_without_a_request` :77 (zero) | yes | n.a. | None |
| test_embedding_probe.py | `test_workers_ai_answering_is_ok_with_its_dimensions` :51 | no | no | Added `[request] = requests` and the Bearer token |
| test_embedding_probe.py | `test_a_refusal_carries_the_providers_words_and_not_the_key` :64 `test_a_transport_error_is_a_failure_not_an_exception` :90 `test_a_rate_limited_embedder_fails_within_the_probe_bound` :103 | no | n.a. | Added `len(requests) == 1`. At `:103` the 3 s backoff outlasts the 0.2 s bound |
| test_embedding.py | `test_the_gemini_embedder_sends_its_key_as_a_header_not_a_query_string` :47 `test_a_repeated_text_is_embedded_once_per_call` :90 | yes | yes | None |
| test_embedding.py | `test_workers_ai_returns_unit_vectors_and_records_what_it_charged` :68 | no | no | Added a request list, the method, the path and the Bearer token |
| test_embedding.py | `test_a_200_carrying_success_false_is_an_error` :109 `test_gemini_asks_for_truncated_dimensions_when_told_to` :126 | no | n.a. / yes | Added a request list and `len == 1` |
| test_embedding.py | `test_an_empty_input_never_reaches_the_api` :146 (zero) | no (adapter counter only) | n.a. | Added `seen == []` |
| test_gemini_client.py | `test_retries_on_503_then_succeeds` :91 `test_raises_after_retries_exhausted` :102 `test_raises_on_client_error_without_retry` :113 | yes | n.a. | None |
| test_gemini_client.py | `test_complete_parses_text_and_usage` :28 `test_complete_omits_optional_fields` :76 | no | yes | Added `call_count == 1` |
| test_gemini_client.py | `test_output_tokens_include_thinking` :53 `test_structured_client_error_keeps_details_out_of_exception_text` :125 `test_a_blocked_prompt_names_the_block_reason` :160 | no | n.a. | Bound the route and added `call_count == 1` |
| test_http_client.py | `test_get_success` :13 | no | n.a. | Bound the route and added `call_count == 1` |
| test_log_redaction.py | `test_the_real_send_path_logs_no_token` :107 `test_a_failed_send_logs_neither_the_error_url_nor_the_traceback_token` :155 | no | n.a. | Added a request list and `len == 1` |
| test_mail.py | `TestDigestMail::test_no_recipient_sends_nothing` :244 `TestAlertMail::test_empty_recipient_sends_nothing` :354 (zero) `TestAlertMail::test_refusal_is_logged_not_raised` :360 | yes | n.a. | None |
| test_mail.py | `TestSendMail::test_a_delivered_message_posts_the_fields_and_says_so` :36 `TestSendMail::test_the_html_part_rides_along_when_given` :68 `TestDigestMail::test_the_degraded_verdict_reaches_the_html_body` :222 `TestDigestMailText::test_the_sent_text_part_carries_the_verdict` :309 `TestAlertMail::test_subject_and_text_redact_webhook_tokens` :384 | no | yes | Added `call_count == 1` |
| test_mail.py | `TestDigestMail::test_it_sends_through_the_rest_api` :198 `TestAlertMail::test_posts_plain_text_without_html` :330 | no | partial (no credential) | Added `call_count == 1` and the Bearer token |
| test_mail.py | `TestSendMail::test_a_queued_message_counts_as_sent` :57 `TestSendMail::test_a_permanent_bounce_under_success_is_still_a_failure` :81 `TestSendMail::test_a_refusal_carries_the_api_message` :92 `TestSendMail::test_a_body_that_is_not_json_names_the_status` :113 `TestSendMail::test_an_unreachable_api_is_named` :122 `TestDigestMail::test_a_failure_is_logged_and_not_raised` :250 | no | n.a. | Bound the route and added `call_count == 1` |
| test_newsletter_worker_source.py | `test_matched_sender_acked` :37 | no (`ack.called`) | partial (no credential) | Added the pull and ack counts, and the Bearer token on both requests |
| test_newsletter_worker_source.py | `test_unknown_sender_skipped_but_acked` :65 `test_private_reply_not_ingested_but_acked` :188 `test_non_string_subject_does_not_raise_and_still_acks` :212 `test_malformed_item_missing_id_does_not_crash_batch_acks_goods` :238 | no (pull unbound, `ack.called`) | yes | Added `pull.call_count == 1` and `ack.call_count == 1` |
| test_newsletter_worker_source.py | `test_empty_queue_no_ack` :109 `test_a_failed_pull_raises_and_acks_nothing` :123 `test_a_preview_pulls_without_acking` :278 | no (pull unbound; `not ack.called` pins zero) | n.a. | Added `pull.call_count == 1` |
| test_notify.py | `TestSendDiscord::test_empty_webhook_posts_nothing` :591 `TestSendDiscordAlert::test_empty_webhook_posts_nothing` :722 (zero) `TestSendDiscordAlert::test_posts_plain_content` :689 `TestSendDiscordAlert::test_http_error_is_logged_not_raised` :735 `TestSendDiscordAlert::test_connect_error_is_logged_not_raised` :753 | yes | yes / n.a. | None |
| test_notify.py | `TestSendDiscord::test_posts_degraded_payload` :537 | no | partial (body only) | Added `len == 1`, the method and the webhook URL |
| test_notify.py | `TestSendDiscord::test_posts_healthy_payload_without_content` :565 `TestSendDiscordAlert::test_truncates_content_to_discord_limit` :705 `TestDiscordAlertRedaction::test_body_redacts_a_token_in_the_text` :780 `TestDiscordAlertRedaction::test_body_redacts_a_token_in_the_subject` :796 | no | yes | Added `len(requests) == 1` |
| test_notify.py | `TestDiscordAlertRedaction::test_failure_log_redacts_the_webhook_token` :812 | no | n.a. | Added a request list and `len == 1` |
| test_openai_client.py | `test_retries_a_429_then_succeeds` :104 `test_a_4xx_reason_reaches_the_caller` :114 | yes | n.a. | None |
| test_openai_client.py | `test_parses_content_and_usage` :30 `test_sends_max_completion_tokens_not_the_deprecated_name` :51 `test_explicit_max_tokens_maps_onto_the_current_parameter` :63 `test_asks_for_low_reasoning_effort_and_json` :72 | no | yes | Added `call_count == 1` |
| test_openai_client.py | `test_warns_when_truncated` :85 `test_warns_when_the_reply_is_empty` :94 | no | n.a. | Bound the route and added `call_count == 1` |
| test_pages_deploy.py | `test_a_one_bucket_deploy_makes_the_requests_its_worst_case_counts` :107 (total equals `DEPLOY_REQUESTS`) `test_has_deployments_asks_for_one_page_of_the_project_list` :313 `test_a_deployment_is_reread_by_its_id` :374 | yes | n.a. / yes | None |
| test_pages_deploy.py | `test_only_the_files_the_account_lacks_are_uploaded` :63 | no | partial (no credential) | Added the per-route list with each `Authorization` (API token or upload JWT) |
| test_pages_deploy.py | `test_a_deploy_returns_cloudflares_verdict_on_the_deployment` :160 `test_only_deploy_success_is_landed` :189 `test_a_deployment_without_a_stage_is_neither_landed_nor_failed` :201 `test_a_deployment_result_without_an_id_is_an_error` :214 | no | n.a. | `_deploying` keeps `handler.requests`. Each test asserts `NOTHING_MISSING_REQUESTS` (4) |
| test_pages_deploy.py | `test_a_step_that_answers_success_false_is_an_error` :225 `test_has_deployments_is_false_when_the_list_is_empty` :273 `test_has_deployments_raises_when_the_project_is_missing` :284 `test_has_deployments_raises_when_result_is_missing` :300 `test_an_error_carries_the_status_it_was_answered_with` :328 `test_a_refused_creation_is_an_error_rather_than_a_silent_no_op` :360 `test_rereading_a_deployment_cloudflare_does_not_know_is_an_error` :388 | no | n.a. | Added a request list and `len == 1` |
| test_pages_deploy.py | `test_has_deployments_is_true_when_the_list_holds_one` :261 | no | no | Added `len == 1` and the Bearer token |
| test_pages_deploy.py | `test_creating_the_project_names_it_and_its_production_branch` :344 | yes | partial (no credential) | Added the Bearer token |
| test_rss_worker_source.py | `test_window_bounds_go_out_as_utc_millis` :80 | yes | yes | None |
| test_rss_worker_source.py | `test_rows_map_to_articles_with_tier_from_config` :35 `test_the_read_asks_for_the_workers_ceiling_not_the_run_cap` :94 `test_unknown_source_falls_back_to_filter_tier` :110 `test_a_failed_buffer_read_raises` :124 `test_malformed_row_is_skipped_not_fatal` :139 `test_a_read_that_fills_the_ceiling_is_logged` :154 `test_a_read_below_the_ceiling_logs_no_warning` :173 | no | n.a., or yes at `:94` (limit) | Bound the route and added `call_count == 1` |
| test_run_digest.py | `test_a_dead_rss_buffer_alerts_as_a_failed_fetch` :2014 | no | n.a. | Bound the route and added `call_count == 1` |
| test_settings_api.py | `TestEmailSettingsWrite::test_an_empty_recipient_turns_mail_off_without_sending` :1083 `TestEmailSettingsWrite::test_a_recipient_without_a_sender_is_refused` :1097 `TestEmailSettingsWrite::test_without_cloudflare_credentials_the_pair_is_refused` :1112 (zero, via `respx.calls.call_count`) | yes | n.a. | None |
| test_settings_api.py | `TestVoteSimilarity::test_a_refusal_never_carries_the_key` :600 | no | n.a. | Added a request list and `len == 1` |
| test_settings_api.py | `TestEmailSettingsWrite::test_a_pair_the_test_message_reaches_is_stored` :1034 | no | partial (no credential) | Added `call_count == 1` and the Bearer token |
| test_settings_api.py | `TestEmailSettingsWrite::test_a_pair_cloudflare_refuses_is_never_stored` :1061 | no | n.a. | Bound the route and added `call_count == 1` |
| test_trial_api.py | `test_a_token_with_every_permission_passes` :81 `test_each_missing_permission_is_named` :97 `test_every_missing_permission_is_named_at_once` :105 `test_mail_is_probed_only_when_asked` :113 `test_the_mail_probe_sends_nothing_a_mail_could_be_made_of` :120 `test_an_answer_that_is_not_json_is_not_a_grant` :128 `test_the_token_is_sent_and_never_printed` :151 | no | yes at `:120` (body) and `:151` (token), else n.a. | Bound the fake. Asserts `len(cf.requests)` at 4 with `--email`, else 3 |
| test_trial_api.py | `test_every_probe_targets_the_trial` :135 | no (a set of paths) | yes | Added `len(cf.requests) == len(paths)`, so each path runs exactly once |
| test_trial_api.py | `test_no_token_on_stdin_is_refused` :160 (zero) | no | n.a. | Added `cf.requests == []` |
| test_trial_api.py | `test_the_wait_ends_at_the_first_row_newer_than_the_one_before` :217 `test_the_new_row_shows_its_columns_and_its_counts` :228 `test_a_count_both_a_column_and_in_the_summary_shows_once` :238 `test_a_failed_publish_points_at_the_pages_permission` :248 `test_an_ok_run_does_not_mention_pages` :258 `test_the_wait_gives_up_at_its_timeout_and_names_workers_logs` :265 `test_an_empty_table_counts_as_run_zero` :276 `test_a_refused_query_fails_without_the_token` :287 `test_the_wait_only_reads` :296 | no | yes at `:296` (SELECT only), else n.a. | Added `len(d1.sql)`: 3 at `:217`, 5 at `:265`, 1 per read elsewhere |
| test_worker_domains.py | `test_the_worker_is_named_and_its_hostnames_come_back_sorted` :19 | no | yes | Added `call_count == 1` |
| test_worker_domains.py | `test_a_worker_with_no_custom_domain_has_none` :30 `test_a_token_without_the_permission_raises_the_api_message` :38 `test_an_unreachable_api_raises_rather_than_answering_none` :48 | no | n.a. | Bound the route and added `call_count == 1` |
| test_workers_ai_client.py | `test_retries_on_429_then_succeeds` :155 `test_does_not_retry_a_403` :170 | yes | n.a. | None |
| test_workers_ai_client.py | `test_reads_the_openai_shaped_choices_and_usage` :31 `test_always_sends_max_tokens` :86 `test_asks_for_low_reasoning_effort` :96 `test_explicit_max_tokens_wins` :112 | no | yes | Added `call_count == 1` |
| test_workers_ai_client.py | `test_falls_back_to_the_flat_response_field` :57 `test_reencodes_a_response_that_is_not_a_string` :70 `test_warns_when_the_reply_was_truncated` :121 `test_neurons_are_reported_per_response_and_summed_by_the_caller` :131 `test_raises_with_the_reason_cloudflare_gave` :180 `test_a_4xx_body_reaches_the_caller` :190 `test_an_empty_reply_is_reported_rather_than_returned_blank` :205 | no | n.a. | Bound the route and added the count, which is 2 at `:131` and 1 elsewhere |

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
