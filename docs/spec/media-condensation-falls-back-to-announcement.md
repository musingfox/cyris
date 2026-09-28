---
id: media-condensation-falls-back-to-announcement
status: proposed
scope:
  - "src/cyris/service_layer/run_digest.py"
  - "src/cyris/service_layer/ports.py"
  - "src/cyris/bootstrap.py"
  - "src/cyris/settings_fields.json"
verify: null
related: [media-condensed-once-before-scoring, youtube-content-not-scraped]
source: multimedia-sources-podcast-youtube
adr: null
---

Condensation is additive: when it is switched off or fails, the item reaches the
digest exactly as it would without condensation, with its original content, and
switched off means no model call is made.

The condensation model is a grade-D setting, set on `/settings` and verified
against the live API before it is stored in D1 `settings`; an empty value is a
valid value meaning "announce only", distinct from a missing key. The condenser
writes to the store only on success, so a failure leaves the show notes or the
video description in place — the same stance as `service_layer/degrade.py`,
where a run with no LLM still lists excerpts.

Out of scope: a missing key still stops the run, as every grade-D key does
(`docs/architecture.md` §5). Alerting on condensation failures is not this
entry's concern.

A violation is silent twice over. A condenser that falls back to the digest
model when its own setting is empty keeps spending after the user switched it
off — the only switch they have once Gemini's YouTube preview starts charging —
and nothing on the page shows it. A condenser that drops or blanks an item on
failure removes an episode from a digest that still looks complete.

Born prose: the setting does not exist yet. The binding is a test with an empty
setting asserting zero calls to a recording fake, and one with a failing fake
asserting the item is in the digest with its original content.
