// A hosted tenant's newsletter address: the local part of `<address>@<the operator's inbound
// domain>`, which one catch-all rule routes to the hosted Worker's email() handler
// (hosted_inbound.js). Why a random address per tenant and not one shared address:
// docs/decisions/0034-a-hosted-tenants-newsletters-arrive-at-a-random-address.md.
//
// The address is drawn from the runtime's CSPRNG alone: no tenant id, email or time goes into it,
// so knowing a tenant tells nothing about its address. Its only map to a tenant is the operator's
// registry (`inbound_addresses`, tenant_registry.js), which no run container can reach.
// No workerd-only imports: vitest runs this under Node.

// 16 random bytes are 128 bits, as many as a v4 UUID's 122 and more: past any guessing over SMTP,
// where every guess is one delivery attempt.
export const ADDRESS_BYTES = 16;

// RFC 4648 base32 in lower case: no "+" for a subscription form to refuse or strip, no
// look-alike case for a reader to mistype, and only characters every mail system takes in a
// local part.
const ALPHABET = "abcdefghijklmnopqrstuvwxyz234567";

// An address's shape: 128 bits are 26 base32 characters, the last carrying 3 bits.
export const ADDRESS = /^[a-z2-7]{26}$/;

// A fresh address. `random` fills a byte array, as crypto.getRandomValues does; a test passes
// its own.
export function randomAddress(random = (bytes) => crypto.getRandomValues(bytes)) {
  const bytes = random(new Uint8Array(ADDRESS_BYTES));
  let bits = 0;
  let value = 0;
  let out = "";
  for (const byte of bytes) {
    value = (value << 8) | byte;
    bits += 8;
    while (bits >= 5) {
      out += ALPHABET[(value >>> (bits - 5)) & 31];
      bits -= 5;
    }
  }
  if (bits > 0) out += ALPHABET[(value << (5 - bits)) & 31];
  return out;
}

// The address an envelope recipient names, or null when it cannot be one of ours: the local part,
// lowercased, since a sender may change its case. A subaddress (`<address>+x`) is not one.
export function recipientAddress(to) {
  if (typeof to !== "string") return null;
  const at = to.lastIndexOf("@");
  if (at <= 0) return null;
  const local = to.slice(0, at).trim().toLowerCase();
  return ADDRESS.test(local) ? local : null;
}
