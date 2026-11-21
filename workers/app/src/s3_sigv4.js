// AWS Signature Version 4 for one S3 request, with Web Crypto only: how the daily backup puts its
// off-Cloudflare copy into Backblaze B2, whose S3-compatible API takes only v4 signatures
// (hosted_backup.js, ADR-0041). The signed payload is the body's own SHA-256, never
// UNSIGNED-PAYLOAD, so a body changed on the way is refused by the store.
// https://www.backblaze.com/docs/cloud-storage-call-the-s3-compatible-api

const encoder = new TextEncoder();
const ALGORITHM = "AWS4-HMAC-SHA256";

const hex = (buffer) => [...new Uint8Array(buffer)].map((byte) => byte.toString(16).padStart(2, "0")).join("");

export const sha256Hex = async (data) =>
  hex(await crypto.subtle.digest("SHA-256", typeof data === "string" ? encoder.encode(data) : data));

async function hmac(key, text) {
  const imported = await crypto.subtle.importKey(
    "raw",
    typeof key === "string" ? encoder.encode(key) : key,
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  return crypto.subtle.sign("HMAC", imported, encoder.encode(text));
}

// RFC 3986's unreserved characters stay; every other byte is %XX in upper case, as SigV4 requires.
const uriEncode = (text) =>
  encodeURIComponent(text).replace(/[!'()*]/g, (ch) => `%${ch.charCodeAt(0).toString(16).toUpperCase()}`);

// An S3 object path, each segment encoded once: S3 is the one service whose path is not encoded twice.
export const objectPath = (segments) => `/${segments.map((segment) => segment.split("/").map(uriEncode).join("/")).join("/")}`;

const amzDate = (now) => now.toISOString().replace(/[-:]/g, "").replace(/\.\d{3}/, "");

// The headers that sign `method url` for `service` in `region`: `headers` plus x-amz-date,
// x-amz-content-sha256 and Authorization. Every header passed in is signed, and host always is.
export async function signRequest({ method, url, headers = {}, payloadHash, region, service = "s3", accessKeyId, secretAccessKey, now = new Date() }) {
  const target = new URL(url);
  const stamp = amzDate(now);
  const day = stamp.slice(0, 8);
  const all = { ...headers, host: target.host, "x-amz-content-sha256": payloadHash, "x-amz-date": stamp };
  const names = Object.keys(all)
    .map((name) => name.toLowerCase())
    .sort();
  const lower = Object.fromEntries(Object.entries(all).map(([name, value]) => [name.toLowerCase(), value]));
  const canonicalHeaders = names.map((name) => `${name}:${String(lower[name]).trim().replace(/\s+/g, " ")}\n`).join("");
  const signedHeaders = names.join(";");
  const query = [...target.searchParams]
    .map(([key, value]) => [uriEncode(key), uriEncode(value)])
    .sort(([a, x], [b, y]) => (a < b ? -1 : a > b ? 1 : x < y ? -1 : x > y ? 1 : 0))
    .map(([key, value]) => `${key}=${value}`)
    .join("&");
  const canonicalRequest = [method, target.pathname, query, canonicalHeaders, signedHeaders, payloadHash].join("\n");
  const scope = `${day}/${region}/${service}/aws4_request`;
  const stringToSign = [ALGORITHM, stamp, scope, await sha256Hex(canonicalRequest)].join("\n");
  let key = await hmac(`AWS4${secretAccessKey}`, day);
  for (const part of [region, service, "aws4_request"]) key = await hmac(key, part);
  const signature = hex(await hmac(key, stringToSign));
  return {
    ...headers,
    "x-amz-content-sha256": payloadHash,
    "x-amz-date": stamp,
    authorization: `${ALGORITHM} Credential=${accessKeyId}/${scope}, SignedHeaders=${signedHeaders}, Signature=${signature}`,
  };
}

// The region a B2 S3 endpoint names, `https://s3.<region>.backblazeb2.com`, or null for any other URL.
export function b2Region(endpoint) {
  let url;
  try {
    url = new URL(endpoint);
  } catch {
    return null;
  }
  const match = /^s3\.([a-z0-9-]+)\.backblazeb2\.com$/.exec(url.hostname);
  return url.protocol === "https:" && match && (url.pathname === "/" || url.pathname === "") ? match[1] : null;
}

// One S3 PutObject of `body` (bytes) at `key` in `bucket`, path-style on `endpoint`, as a Request;
// `sha256` is the body's hex SHA-256 when the caller already has it.
export async function putObjectRequest({ endpoint, region, bucket, key, body, contentType, accessKeyId, secretAccessKey, sha256, now }) {
  const url = `${endpoint.replace(/\/+$/, "")}${objectPath([bucket, key])}`;
  const payloadHash = sha256 ?? (await sha256Hex(body));
  const headers = await signRequest({
    method: "PUT",
    url,
    headers: { "content-type": contentType },
    payloadHash,
    region,
    accessKeyId,
    secretAccessKey,
    now,
  });
  return new Request(url, { method: "PUT", headers, body });
}
