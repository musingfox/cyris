// SigV4 (s3_sigv4.js) against signatures someone else computed: the GET and PUT Object worked
// examples Amazon publishes for S3's header-based SigV4
// (https://docs.aws.amazon.com/AmazonS3/latest/API/sig-v4-header-based-auth.html), and a
// PutObject to a B2 endpoint signed by botocore 1.44.9 (S3SigV4Auth, its clock pinned to the
// time below).
import { describe, expect, it } from "vitest";
import { b2Region, objectPath, putObjectRequest, sha256Hex, signRequest } from "../src/s3_sigv4.js";

const AWS = {
  accessKeyId: "AKIAIOSFODNN7EXAMPLE",
  secretAccessKey: "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
  region: "us-east-1",
  now: new Date("2013-05-24T00:00:00Z"),
};
const SCOPE = "Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request";

describe("an S3 request's SigV4 signature", () => {
  it("is the one Amazon's GET Object example gives", async () => {
    const headers = await signRequest({
      method: "GET",
      url: "https://examplebucket.s3.amazonaws.com/test.txt",
      headers: { Range: "bytes=0-9" },
      payloadHash: await sha256Hex(""),
      ...AWS,
    });

    expect(headers.authorization).toBe(
      `AWS4-HMAC-SHA256 ${SCOPE}, SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, ` +
        "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41",
    );
    expect(headers["x-amz-date"]).toBe("20130524T000000Z");
  });

  it("is the one Amazon's PUT Object example gives, its key's $ encoded once", async () => {
    const body = "Welcome to Amazon S3.";
    const headers = await signRequest({
      method: "PUT",
      url: `https://examplebucket.s3.amazonaws.com${objectPath(["test$file.text"])}`,
      headers: { Date: "Fri, 24 May 2013 00:00:00 GMT", "x-amz-storage-class": "REDUCED_REDUNDANCY" },
      payloadHash: await sha256Hex(body),
      ...AWS,
    });

    expect(await sha256Hex(body)).toBe("44ce7dd67c959e0d3524ffac1771dfbba87d2b6b4b4e99e42034a8b803f8b072");
    expect(headers.authorization).toBe(
      `AWS4-HMAC-SHA256 ${SCOPE}, SignedHeaders=date;host;x-amz-content-sha256;x-amz-date;x-amz-storage-class, ` +
        "Signature=98ad721746da40c64f1a55b78f14c238d841ea1380cd77a1b5971af0ece108bd",
    );
  });

  it("signs a backup's PutObject to B2 path-style as botocore does, over the body's own hash", async () => {
    const body = new Uint8Array([0x1f, 0x8b, ...new TextEncoder().encode(" backup bytes")]);
    const request = await putObjectRequest({
      endpoint: "https://s3.us-west-004.backblazeb2.com",
      region: "us-west-004",
      bucket: "cyris-backup",
      key: "2026-11-22/alpha.jsonl.gz",
      body,
      contentType: "application/gzip",
      accessKeyId: "004a1b2c3d4e5f60000000001",
      secretAccessKey: "K004secretEXAMPLEKEY/abc+def",
      now: new Date("2026-11-22T04:00:00Z"),
    });

    expect(request.method).toBe("PUT");
    expect(request.url).toBe("https://s3.us-west-004.backblazeb2.com/cyris-backup/2026-11-22/alpha.jsonl.gz");
    expect(request.headers.get("x-amz-content-sha256")).toBe(await sha256Hex(body));
    expect(request.headers.get("authorization")).toBe(
      "AWS4-HMAC-SHA256 Credential=004a1b2c3d4e5f60000000001/20261122/us-west-004/s3/aws4_request, " +
        "SignedHeaders=content-type;host;x-amz-content-sha256;x-amz-date, " +
        "Signature=66b2af3ee7694a2c5e0ca98c069a1b9564a016e15d1d66e8ad425f35204d7a0c",
    );
    expect(new Uint8Array(await request.arrayBuffer())).toEqual(body);
  });

  it("takes the region from a B2 endpoint and refuses any other URL", () => {
    expect(b2Region("https://s3.us-west-004.backblazeb2.com")).toBe("us-west-004");
    expect(b2Region("https://s3.eu-central-003.backblazeb2.com/")).toBe("eu-central-003");
    for (const other of ["http://s3.us-west-004.backblazeb2.com", "https://s3.us-west-004.backblazeb2.com/bucket", "https://example.com", "s3.us-west-004.backblazeb2.com", ""]) {
      expect(b2Region(other), other).toBeNull();
    }
  });
});
