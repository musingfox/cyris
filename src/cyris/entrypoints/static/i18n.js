import { MessageFormat } from "./vendor/messageformat.js";

// The browser messages of the catalog this page was worded from, each with the tag of the
// catalog it came from (ADR-0027). The server matched the language and filled in English;
// the browser only formats. One file per catalog, cached and revalidated like any /static/ file.
const SOURCE = document.querySelector('meta[name="i18n-catalog"]').content;
const response = await fetch(SOURCE);
if (!response.ok) throw new Error(`${SOURCE} answered ${response.status}`);
const CATALOG = await response.json();
const formatters = new Map();

// By default a missing variable prints as `{$x}` with a console warning; Python raises.
const raise = (error) => { throw error; };

// With `elements`, a message that marks up a span, `{#link}…{/link}`, comes back as nodes
// to append: each span is the element `elements` gives under its name, holding the span's
// text, so the script supplies the element and the catalog never holds HTML.
export function t(key, values = {}, elements = null) {
  const message = CATALOG[key];
  if (!message) throw new Error(`${key} is not in ${SOURCE}`);
  if (!formatters.has(key)) {
    // Python formats with no bidi isolation while every catalog is left-to-right.
    formatters.set(key, new MessageFormat(message.tag, message.text, { bidiIsolation: "none" }));
  }
  if (!elements) return formatters.get(key).format(values, raise);
  const nodes = document.createDocumentFragment();
  const open = [nodes];
  for (const part of formatters.get(key).formatToParts(values, raise)) {
    const here = open[open.length - 1];
    if (part.type !== "markup") {
      here.append(part.value ?? part.parts.map((inner) => inner.value).join(""));
    } else if (part.kind === "close") {
      open.pop();
    } else {
      const element = elements[part.name];
      if (!element) throw new Error(`${key} marks up ${part.name}, which the page does not supply`);
      here.append(element);
      if (part.kind === "open") open.push(element);
    }
  }
  return nodes;
}
