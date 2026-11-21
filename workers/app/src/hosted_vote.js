// A hosted reader's vote: POST /api/vote, from the vote buttons of a page the Worker served them,
// and the GET the page makes first, to learn whether it may show them.
// Why the page carries a token and not the cookie, and why the vote is queued and not applied:
// docs/decisions/0039-a-hosted-vote-is-queued-by-the-worker-and-applied-by-the-run.md
// No workerd-only imports: vitest runs this under Node.
//
// A tenant's page is sandboxed with an opaque origin (SITE_POLICY, ADR-0033), so its requests
// carry no cookie and come from `Origin: null`. The Worker puts a vote token in each page it
// serves to a session (`voteToken`, tenant_registry.js), and the page sends it in
// `X-Cyris-Vote`. The credential is that header alone: this route never reads a cookie, so a
// form or fetch from any site, which a browser sends with the reader's cookie, votes nothing.
// The tenant is the live session the token names, never a field of the body, the path or a
// `Referer`.

import { PRIVATE_HEADERS, VOTE_PATH } from "./hosted_pages.js";
import { crossSite } from "./hosted_session.js";

export const VOTE_HEADER = "X-Cyris-Vote";

// The page's code reads this to tell a session that has ended from a refusal it cannot mend.
export const SESSION_ENDED = "vote_session_ended";

// What the page's capability probe reads (_probe_script.html.j2): signed in, with no Settings page
// on this Worker, so the vote buttons show and the links to Settings and the interest page do not.
const PROBE = { authorized: true, settings: false };

// The directions a hosted vote takes. The legacy "deep" the promote Worker still passes through
// (promotions.py) never had a hosted page.
const VOTES = ["up", "down"];

// A vote's JSON is a URL, a direction and a date: far under this, and a URL under 2 KiB is what
// a browser sends. A body is read only this far, before the token is checked, so a request of any
// length and any token buffers at most this much (`boundedText`).
export const MAX_BODY_BYTES = 4096;
const MAX_URL_LENGTH = 2048;

// Any origin may read the answer: the page is `Origin: null`, and nothing here is ambient. The
// token in the header is the credential, and a page that holds none gets a 401.
const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST",
  "Access-Control-Allow-Headers": `Content-Type, ${VOTE_HEADER}`,
  "Access-Control-Max-Age": "600",
};

const answer = (body, status = 200) =>
  new Response(body === null ? null : JSON.stringify(body), {
    status,
    headers: {
      ...(body === null ? {} : { "Content-Type": "application/json" }),
      "Cache-Control": "no-store",
      ...PRIVATE_HEADERS,
      ...CORS,
    },
  });

const ended = () => answer({ error: "sign in again to vote", code: SESSION_ENDED }, 401);

// The body as text, or null once it passes `max` bytes: a declared length past it is refused
// unread, and a body that sends more than it declared, or declares nothing, is read only until it
// passes, then cancelled.
export async function boundedText(request, max) {
  const declared = Number(request.headers.get("Content-Length"));
  if (declared > max) return null;
  if (!request.body) return "";
  const reader = request.body.getReader();
  const chunks = [];
  let size = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > max) {
      await reader.cancel().catch(() => {});
      return null;
    }
    chunks.push(value);
  }
  const bytes = new Uint8Array(size);
  let at = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, at);
    at += chunk.byteLength;
  }
  return new TextDecoder().decode(bytes);
}

// `deps`: `voteSession(token)`, the registry's live session `{tenant_id, ...}` or null, and
// `recordVote(tenant, {url, vote})`, which answers that tenant's object's `{queued}` or
// `{refused, status}`.
export async function handleVote(request, deps) {
  if (request.method === "OPTIONS") return answer(null, 204);
  const probe = request.method === "GET" || request.method === "HEAD";
  if (request.method !== "POST" && !probe) return answer({ error: "a vote takes POST" }, 405);
  // A browser at another site sends its own Origin. The sandboxed page's is the literal "null",
  // which `crossSite` also reads as another site, and which is the one cross-site caller that is
  // wanted; any other, and a request with no token, is refused before any session is read.
  if (crossSite(request) && request.headers.get("Origin") !== "null") {
    return answer({ error: "a vote is cast from the reader's own page" }, 403);
  }
  const token = request.headers.get(VOTE_HEADER);
  if (!token) return ended();
  // The page asks first whether it may show its vote buttons: yes for a live session's token.
  if (probe) return (await deps.voteSession(token)) ? answer(PROBE) : ended();

  let text;
  try {
    text = await boundedText(request, MAX_BODY_BYTES);
  } catch {
    return answer({ error: "the body could not be read" }, 400);
  }
  if (text === null) return answer({ error: "the body is too large" }, 413);
  let body;
  try {
    body = JSON.parse(text);
  } catch {
    return answer({ error: "the body is not JSON" }, 400);
  }
  const { url, vote } = body ?? {};
  if (typeof url !== "string" || !url || url.length > MAX_URL_LENGTH || !VOTES.includes(vote)) {
    return answer({ error: `the body must be {url, vote: ${VOTES.join(" | ")}}` }, 400);
  }

  const session = await deps.voteSession(token);
  if (!session) return ended();
  const queued = await deps.recordVote(session.tenant_id, { url, vote });
  return queued.refused ? answer({ error: queued.refused }, queued.status) : answer({ ok: true });
}
