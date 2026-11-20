// The hosted Worker's email() handler: every message one catch-all Email Routing rule sends it,
// for any address of the operator's inbound domain. The envelope recipient, `message.to`, names
// the tenant through the registry (inbound_address.js, tenant_registry.js); the From header,
// which any sender can write, never does. A message is stored only in that tenant's own Durable
// Object (tenant_inbound.js), with the record the newsletter Worker stores
// (workers/newsletter/src/record.js, imported, not copied).
//
// The recipient's domain is not checked. Only a rule on a zone of the operator's own account can
// route mail here, the address alone carries the 128 bits that make it unguessable, and which
// inbound domain the service uses is not decided yet (ADR-0034); the change that decides it adds
// the domain as a setting and checks it here.
// docs/decisions/0034-a-hosted-tenants-newsletters-arrive-at-a-random-address.md.
// No workerd-only imports: vitest runs this under Node.

import { newsletterRecord } from "../../newsletter/src/record.js";
import { recipientAddress } from "./inbound_address.js";

// The largest message parsed, under Cloudflare's 25 MiB: a newsletter links its images, and a
// message far over this is not one, so it is dropped before any parse spends the CPU.
export const MAX_RAW_BYTES = 5 * 1024 * 1024;

// The largest record stored, under a Durable Object's 2 MB row: a newsletter's text and HTML
// together are rarely past 300 KB.
export const MAX_RECORD_BYTES = 1024 * 1024;

// Cloudflare's own verdicts, from the topmost Authentication-Results, which Cloudflare prepends
// and alone may be trusted: only when its authserv-id is mx.cloudflare.net. Any lower one was
// written by whoever handled the message before, the sender included.
// https://developers.cloudflare.com/email-service/api/route-emails/email-handler/
export function authResults(headers) {
  const first = headers.find((header) => header?.key?.toLowerCase() === "authentication-results");
  const value = typeof first?.value === "string" ? first.value.trim() : "";
  if (!/^mx\.cloudflare\.net\s*;/i.test(value)) return null;
  const verdict = (method) => value.match(new RegExp(`\\b${method}=([a-z]+)`, "i"))?.[1].toLowerCase() ?? null;
  return { spf: verdict("spf"), dkim: verdict("dkim"), dmarc: verdict("dmarc") };
}

const recordBytes = (record) => new TextEncoder().encode(JSON.stringify(record)).length;

// Takes one message, and answers what became of it: `{tenant, id}` when it is stored, `{dropped}`
// when it is not. A dropped message is accepted and discarded, never rejected, whatever dropped
// it: an address no tenant holds, a revoked one, a subaddress or a malformed recipient, a message
// too large, a full queue. The product owner decided this on 2026-11-20, so the answer a sender
// gets says nothing about whether an address exists. `setReject` is never called and nothing is
// forwarded. `deps`: `tenantFor(address)`, the registry's tenant for an address or null, and
// `store(tenant, id, record)`, the tenant's object's answer. The log names no address and holds
// no content: a drop for an address no tenant holds names nothing else.
export async function receiveInbound(message, deps) {
  const drop = (why, tenant) => {
    console.log("inbound dropped", tenant ? { tenant, why } : { why });
    return { dropped: why };
  };
  const address = recipientAddress(message.to);
  const tenant = address ? await deps.tenantFor(address) : null;
  if (!tenant) return drop("no such address");
  if (typeof message.rawSize === "number" && message.rawSize > MAX_RAW_BYTES) return drop("too large", tenant);
  const { id, record, headers } = await newsletterRecord(message);
  if (recordBytes(record) > MAX_RECORD_BYTES) return drop("too large", tenant);
  const stored = await deps.store(tenant, id, record);
  if (stored.refused) return drop(stored.refused, tenant);
  if (stored.duplicate) {
    console.log("inbound duplicate", { tenant });
    return { tenant, id, duplicate: true };
  }
  // The verdicts are logged, not acted on: the operator reads what a forwarded issue gets here
  // (docs/hosted-tenants.md) before any rule is written against them.
  console.log("inbound stored", { tenant, auth: authResults(headers) });
  return { tenant, id };
}
