// One inbound message as a queue stores it: this Worker's KV value (index.js), and a hosted
// tenant's `newsletters` row (workers/app/src/hosted_inbound.js), which imports this rather than
// copying it, so both queues hand a run the same fields.
import PostalMime from "postal-mime";

// Email headers whose values we keep (all others are stored as key-only).
const VALUE_HEADERS = new Set([
  "archived-at",
  "list-id",
  "list-post",
  "list-archive",
  "list-help",
  "x-mc-user",
  "x-campaignid",
  "x-campaign",
  "feedback-id",
  "content-type",
  "x-mailer",
]);

export function safeHeaders(headers) {
  return (Array.isArray(headers) ? headers : [])
    .filter((header) => typeof header?.key === "string")
    .map((header) => {
      const record = { key: header.key };
      if (VALUE_HEADERS.has(header.key.toLowerCase()) && typeof header.value === "string") {
        record.value = header.value;
      }
      return record;
    });
}

export async function sha256(text) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// The message parsed: its id, the record a queue stores, and every header in the order the
// message carries them, which only the hosted handler reads (for Cloudflare's own
// Authentication-Results, the topmost one).
export async function newsletterRecord(message) {
  const parsed = await PostalMime.parse(message.raw);
  // Prefer the original sender's From header (survives Gmail auto-forward);
  // fall back to the envelope sender.
  const from = parsed.from?.address || message.from || "";
  const record = {
    from,
    subject: parsed.subject || "",
    html: parsed.html || "",
    text: parsed.text || "",
    date: (parsed.date ? new Date(parsed.date) : new Date()).toISOString(),
    headers: safeHeaders(parsed.headers),
    raw_size: typeof message.rawSize === "number" ? message.rawSize : null,
  };
  // Dedup by sender+subject+date so a re-delivered copy overwrites, not duplicates.
  const id = `nl:${await sha256(`${from}|${record.subject}|${record.date}`)}`;
  return { id, record, headers: Array.isArray(parsed.headers) ? parsed.headers : [] };
}
