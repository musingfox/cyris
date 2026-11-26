// A reader's decision on a newsletter source, as the review it leaves, from whatever state the
// source is in: kept and restored are "kept", which reads as an ordinary source, and stopped is
// "declined" (ADR-0050). The vote doors
// (router.js, hosted_vote.js) and the settings servers' review route take these, as
// `SOURCE_DECISIONS` in src/cyris/domain/models.py does; workers/app/test/source_review_cases.json
// and shared_settings_cases.json hold every door to the same answers.
// No workerd-only imports: vitest runs this under Node.

export const SOURCE_DECISIONS = { keep: "kept", stop: "declined", restore: "kept" };

export const NEWSLETTER = "newsletter";

export const isDecision = (decision) => typeof decision === "string" && Object.hasOwn(SOURCE_DECISIONS, decision);

// The vote doors' answer to a page's `{source, decision}`, as [status, body]. `store` is the
// deployment's sources: `decide(name, review)` answers `{changed}` or `{refused, status}`, a
// refusal of "source_missing" or "not_newsletter" being the two a page can mend nothing about; a
// null `store` is a door with no sources to write.
export async function answerDecision(body, store) {
  const { source, decision } = body;
  if (typeof source !== "string" || !source) return [400, { error: "missing source" }];
  if (!isDecision(decision)) return [400, { error: "bad decision" }];
  if (!store) return [503, { error: "no source store" }];
  const review = SOURCE_DECISIONS[decision];
  const done = await store.decide(source, review);
  if (done.refused === "source_missing" || done.changed === 0) return [404, { error: "no such source" }];
  if (done.refused === "not_newsletter") return [400, { error: "not a newsletter source" }];
  if (done.refused) return [done.status ?? 503, { error: done.refused }];
  return [200, { ok: true, source, review }];
}

// Whether a vote door's POST body is a source decision rather than a vote: a vote never names a
// source.
export const isDecisionBody = (body) => body !== null && typeof body === "object" && !Array.isArray(body) && "source" in body;

// The review each source holds, by name, for the sources that hold one: what a page's probe reads
// to correct marks it was rendered with.
export const reviewsOf = (rows) =>
  Object.fromEntries(rows.filter(({ config }) => config?.review).map(({ name, config }) => [name, config.review]));
