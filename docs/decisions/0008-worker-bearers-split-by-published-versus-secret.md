---
status: accepted
date: 2026-10-08
decision-makers: ""
---

# Credentials are split by published versus secret, not by count

## Context and Problem Statement

On the morning of 2026-08-30 the container carried twelve grade-C variables. Two of them were not
separate secrets at all, and the reduction to seven raised a general question: when should two
credentials be one value, and when must they stay apart?

* `CYRIS_D1_API_TOKEN` was `CLOUDFLARE_API_TOKEN` under another name, the same string twice in
  `.env`, so `StoreConfig`'s fallback chain had never once chosen its second branch.
* Three Worker bearers existed: `rss`, `newsletter` and `promote`. The `rss` and `newsletter`
  tokens are server-to-server and live in `.env` and the Worker secret store, so whoever reads
  one reads the other.
* The `promote` token is different in kind. Its up and down buttons run in the reader's browser,
  and until the private-votes change (2026-09-01) `_promote_script.html.j2` rendered the token
  into every digest and raw page. Those pages are public, so recovering the token took one `curl`.

A token's permissions cannot be read back from `/user/tokens/verify`, so the comparison was made
by asking the API:

| | D1 query | Workers AI | Pages project | upload-token | R2 |
|---|---|---|---|---|---|
| `CLOUDFLARE_API_TOKEN` | 200 | 401 | 200 | 200 | 403 |
| `CYRIS_D1_API_TOKEN` (deleted) | 200 | 401 | 200 | 200 | 403 |
| `CLOUDFLARE_EMBEDDING_API_TOKEN` | 403 | 200 | 403 | 403 | 403 |

## Considered Options

* One bearer for all three Workers ("three random values but never three trust domains")
* One bearer per Worker
* One value for the secret bearers, and a separate value for any token that has been published

## Decision Outcome

Chosen option: "split by published versus secret", because the dividing line is whether a
value has ever been printed on a public page, not how many values there are.

The single-bearer option was tried. On 2026-08-30 the three were merged into
`CYRIS_WORKER_TOKEN`, and they were unmerged the same evening after the 20:00 digest published the
shared value in plain HTML. For about an hour, the token printed on a public page was also
`newsletter`'s, whose `/ack` deletes a queue, and `rss`'s. The reasoning had been right about
`rss` and `newsletter` and wrong about `promote`, and the evidence was four lines into a template
nobody re-read.

* `rss` and `newsletter` share `CYRIS_WORKER_TOKEN`. Holding them apart bought independent
  rotation of keys nobody rotates.
* `promote` keeps `CYRIS_PROMOTE_TOKEN`. It is baked into every page published before 2026-09-01,
  and those pages are still served, so it is not a secret and cannot be treated as one. In the
  repair it went *back* to its original value rather than forward, because rotating it would
  break every vote button in the 58 published pages and rotating a public token buys nothing.
  `rss` and `newsletter` rotated to a fresh value, because theirs had been published.
* `CYRIS_D1_API_TOKEN` was deleted.
* `CLOUDFLARE_EMBEDDING_API_TOKEN` stays, because it is genuinely a different permission. The code
  refuses to fall back to `CLOUDFLARE_API_TOKEN` for inference, because that token answers 401 on
  Workers AI, which reads as a broken key rather than a missing permission.

### Consequences

* Good, because a leak of the public `promote` value reaches nothing that can delete or read.
* Good, because the receipt is checkable: the old `promote` token answers 200 on `/promotions`,
  and the leaked shared value answers 401 on both `newsletter` and `rss`.
* Bad, because a deployment keeps two Worker bearers and two Cloudflare tokens in step by hand.

### Confirmation

Votes go through the app Worker's `POST /api/vote`, which attaches the token server-side, and no
template reaches for a credential:
`tests/test_html_digest.py::test_no_template_reaches_for_a_credential`.

## More Information

Extracted on 2026-10-08 from `docs/architecture.md:517-587` (*Grade C is seven variables*) at
commit `c353d3f`. The current list of grade-C variables is `SECRETS` in
`workers/app/src/index.js` and `.env.example`, which `tests/test_deploy_inputs.py` holds in step.
