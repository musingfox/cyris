// Shim around the defuddle library: read HTML from stdin, emit JSON to stdout.
// Usage: bun defuddle_extract.mjs <base-url> < page.html
// The CLI's stdin/file modes lack a base-URL argument, so relative links break
// and site-specific extractors never match; calling the library directly with
// an explicit URL keeps both. Invoked by cyris.adapters.fetch.defuddle.
import { parseHTML } from "linkedom";
import { Defuddle } from "defuddle/node";

const url = process.argv[2];
if (!url) {
  console.error("usage: defuddle_extract.mjs <base-url> < page.html");
  process.exit(2);
}

const html = await new Promise((resolve, reject) => {
  let data = "";
  process.stdin.setEncoding("utf8");
  process.stdin.on("data", (chunk) => (data += chunk));
  process.stdin.on("end", () => resolve(data));
  process.stdin.on("error", reject);
});

// Mirror the defuddle CLI's parseLinkedomHTML: polyfill DOM APIs defuddle's
// internals expect, and set doc.URL so site-specific extractors match.
const { document } = parseHTML(html);
if (!document.styleSheets) document.styleSheets = [];
if (document.defaultView && !document.defaultView.getComputedStyle) {
  document.defaultView.getComputedStyle = () => ({ display: "" });
}
document.URL = url;

const result = await Defuddle(document, url, {
  markdown: true,
  separateMarkdown: true,
});

process.stdout.write(
  JSON.stringify({
    title: result.title ?? "",
    author: result.author ?? "",
    published: result.published ?? "",
    wordCount: result.wordCount ?? 0,
    content: result.content ?? "",
  }),
);
