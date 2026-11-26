// A reader's decision on a newsletter source, as the review it leaves, from whatever state the
// source is in: kept and restored are "kept", which reads as an ordinary source, and stopped is
// "declined" (ADR-0050). The settings servers' review route takes these, as `SOURCE_DECISIONS` in
// src/cyris/domain/models.py does; workers/app/test/shared_settings_cases.json holds both servers
// to the same answers.
// No workerd-only imports: vitest runs this under Node.

export const SOURCE_DECISIONS = { keep: "kept", stop: "declined", restore: "kept" };

export const NEWSLETTER = "newsletter";

export const isDecision = (decision) => typeof decision === "string" && Object.hasOwn(SOURCE_DECISIONS, decision);
