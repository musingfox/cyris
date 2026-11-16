import { MessageFormat } from "./vendor/messageformat.js";

// The messages this page's script formats, each with the tag of the catalog it was worded
// from (ADR-0027). The server matched the language; the browser only formats.
const CATALOG = JSON.parse(document.getElementById("i18n-catalog").textContent);
const formatters = new Map();

// By default a missing variable prints as `{$x}` with a console warning; Python raises.
const raise = (error) => { throw error; };

export function t(key, values = {}) {
  const message = CATALOG[key];
  if (!message) throw new Error(`${key} is not in this page's i18n-catalog`);
  if (!formatters.has(key)) {
    // Python formats with no bidi isolation while every catalog is left-to-right.
    formatters.set(key, new MessageFormat(message.tag, message.text, { bidiIsolation: "none" }));
  }
  return formatters.get(key).format(values, raise);
}
