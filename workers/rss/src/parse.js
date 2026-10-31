// Feed parsing, kept separate from index.js so it is testable without the
// Workers runtime (index.js needs the D1 binding).
import { XMLParser } from "fast-xml-parser";

const parser = new XMLParser({
  ignoreAttributes: false,
  attributeNamePrefix: "@_",
  trimValues: true,
  // Off by default, which leaves `&#8217;` in a title as literal text; feedparser,
  // the direct-polling path, decodes it.
  htmlEntities: true,
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

// The ISO form keeps the insert filter against the retention cutoff a string compare.
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

// An http(s) URL resolved against the entry's own link, else null: a relative
// `src` names a file on the article's host, and a `data:` image is no address.
// A link that is itself relative is no base, and must not cost an absolute image.
function imageAddress(raw, base) {
  if (!raw) return null;
  try {
    const url = new URL(raw.replace(/&amp;/g, "&"), URL.canParse(base) ? base : undefined);
    return url.protocol === "http:" || url.protocol === "https:" ? url.toString() : null;
  } catch {
    return null;
  }
}

function isImage(node) {
  const medium = node?.["@_medium"];
  const type = node?.["@_type"] ?? "";
  return medium === "image" || type.startsWith("image/");
}

// The picture the feed names for its entry, as docs/architecture.md §4 *Article image
// URL* orders the places. Only the feed: the article page is never fetched for one.
// A `media:content` or `enclosure` counts only when it says it is an image, since
// either may as well be the episode's audio or video. YouTube's feed puts its media
// elements inside `media:group`, so that is read the same as the entry. A thumbnail
// nested in a `media:content` is the entry's too, as feedparser lists it, and so is
// an Atom `<link rel="enclosure">`, which feedparser lists among the enclosures.
function entryImage(entry, link) {
  const scopes = [entry, ...asArray(entry["media:group"])];
  const contents = scopes.flatMap((s) => asArray(s["media:content"]));
  const enclosures = [
    ...asArray(entry.enclosure),
    ...asArray(entry.link).filter((l) => l?.["@_rel"] === "enclosure"),
  ];
  const first = (nodes) =>
    nodes.map((n) => imageAddress(n?.["@_url"] ?? n?.["@_href"], link)).find(Boolean);
  const found =
    first([...scopes, ...contents].flatMap((s) => asArray(s?.["media:thumbnail"]))) ||
    first(contents.filter(isImage)) ||
    first(enclosures.filter(isImage));
  if (found) return found;
  const img = /<img\b[^>]*?\ssrc\s*=\s*["']([^"']+)["']/i.exec(entryContent(entry));
  return img ? imageAddress(img[1], link) : null;
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
    const link = entryLink(entry);
    const url = stripTrackingParams(link);
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
      image_url: entryImage(entry, link),
    });
  }
  return rows;
}
