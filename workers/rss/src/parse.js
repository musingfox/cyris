// Feed parsing, kept separate from index.js so it is testable without the
// Workers runtime (index.js imports feeds.json and D1 bindings).
import { XMLParser } from "fast-xml-parser";

const parser = new XMLParser({
  ignoreAttributes: false,
  attributeNamePrefix: "@_",
  trimValues: true,
});

// Mirror of cyris's strip_tracking_params (adapters/fetch/email_parser.py).
// The URL is D1's primary key, so an unstripped ?utm_source= would store the
// same article twice and mismatch what the ArticleStore already holds.
// Kept separate so the Worker bundles without reaching into the Python package:
// it must equal `base_tracking_params` in src/cyris/adapters/fetch/keywords.json,
// and tests/test_email_parser.py fails if the two drift.
const TRACKING_KEYS = new Set(["e", "c", "fbclid", "gclid", "mc_cid", "mc_eid"]);

export function stripTrackingParams(url) {
  if (!url) return url;
  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    return url;
  }
  for (const key of [...parsed.searchParams.keys()]) {
    if (key.startsWith("utm_") || TRACKING_KEYS.has(key)) parsed.searchParams.delete(key);
  }
  return parsed.toString().replace(/\?$/, "");
}

function asArray(value) {
  if (value === undefined || value === null) return [];
  return Array.isArray(value) ? value : [value];
}

function text(value) {
  if (value === undefined || value === null) return "";
  if (typeof value === "object") return String(value["#text"] ?? "");
  return String(value);
}

// Atom puts the URL in <link href>, RSS in <link>text</link>; Atom feeds often
// carry several links and only rel="alternate" is the article.
function entryLink(entry) {
  const links = asArray(entry.link);
  for (const link of links) {
    if (typeof link === "object" && link["@_href"]) {
      const rel = link["@_rel"];
      if (!rel || rel === "alternate") return link["@_href"];
    }
  }
  for (const link of links) {
    if (typeof link === "string" && link) return link;
  }
  return text(entry.id) || "";
}

// ISO8601 at write time keeps the after/before query an ordered string compare.
function entryPublished(entry) {
  for (const key of ["pubDate", "published", "updated", "dc:date"]) {
    const raw = text(entry[key]);
    if (!raw) continue;
    const date = new Date(raw);
    if (!Number.isNaN(date.getTime())) return date.toISOString();
  }
  return null;
}

function entryContent(entry) {
  for (const key of ["content:encoded", "content", "summary", "description"]) {
    const value = text(entry[key]);
    if (value) return value;
  }
  return "";
}

function entryAuthor(entry) {
  const author = entry.author;
  if (author && typeof author === "object" && author.name) return text(author.name);
  return text(author) || text(entry["dc:creator"]) || null;
}

export function parseFeed(xml, sourceName) {
  const doc = parser.parse(xml);
  const entries = [
    ...asArray(doc?.rss?.channel?.item),
    ...asArray(doc?.feed?.entry),
    ...asArray(doc?.["rdf:RDF"]?.item), // RSS 1.0
  ];

  const rows = [];
  for (const entry of entries) {
    const url = stripTrackingParams(entryLink(entry));
    const publishedAt = entryPublished(entry);
    if (!url || !publishedAt) continue;
    rows.push({
      url,
      guid: text(entry.guid) || text(entry.id) || null,
      title: text(entry.title),
      content: entryContent(entry),
      author: entryAuthor(entry),
      published_at: publishedAt,
      source_name: sourceName,
    });
  }
  return rows;
}
