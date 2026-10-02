# How sources are processed

Each source declares a tier, in `sources.yaml` on a local install or on `/settings`
(Sources) on Cloudflare, and the tier decides how much LLM attention it gets:

| Tier | Processing | Example |
|------|-----------|---------|
| `filter` | Discard most; surface only significant headlines (<10% pass). News-tagged articles are clustered by topic | TechCrunch, 聯合新聞網 |
| `summarize` | Scored, then split by `[routing] summarize_score_threshold` into full summaries and brief mentions | Stratechery, Benedict Evans |
| `fan` | Passthrough. Never scored, filtered, or summarized | followed groups and newsletters |

An article moves `pending → accepted / rejected / awaiting_triage`. A run accepts only
what its issue shows; an article the per-issue cap cut stays pending, and the next run
in the window considers it again. A 👍/👎 on the
digest or the raw page, and `cyris articles accept|reject`, all stamp `triaged_at`, and
that stamp is what vote similarity later treats as a human decision.

**Paywalled sources are not supported.** cyris takes a feed at face value and will not
log in or carry a session. If you subscribe to something, look for the subscriber-only
RSS feed many paid publications issue (treat that URL as a secret), or route the email
edition through the newsletter path.
