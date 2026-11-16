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

export function t(key, values = {}) {
  const message = CATALOG[key];
  if (!message) throw new Error(`${key} is not in ${SOURCE}`);
  if (!formatters.has(key)) {
    // Python formats with no bidi isolation while every catalog is left-to-right.
    formatters.set(key, new MessageFormat(message.tag, message.text, { bidiIsolation: "none" }));
  }
  return formatters.get(key).format(values, raise);
}
