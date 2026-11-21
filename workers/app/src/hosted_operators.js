// Who the hosted Worker's operator routes admit: not every identity the Access application lets
// through, but only the ones `CYRIS_HOSTED_OPERATORS` names. A valid Access JWT says Access signed
// someone in; this says that someone is the operator. So a reader, or anyone else, added to the
// Access application by mistake still opens no operator route. Why a list in the Worker and not
// Access's policy alone: docs/decisions/0037-the-hosted-operator-routes-admit-only-named-identities.md.
// No workerd-only imports: vitest runs this under Node.

// The setting, grade B: a comma-separated list, each entry either an operator's Access email or
// a service token's Client ID, which Access signs into the token as `common_name`.
export const OPERATORS = "CYRIS_HOSTED_OPERATORS";

const NOT_AN_OPERATOR =
  "This Access sign-in is valid, but its identity is not one the hosted Worker's operators list names.";

// The list as the Worker compares it: emails lower-cased, as Access may case an address either
// way; Client IDs exactly. An entry with an @ is an email, any other a Client ID, so neither kind
// of entry can admit the other kind of identity.
export function operatorList(raw) {
  const entries = String(raw ?? "")
    .split(",")
    .map((entry) => entry.trim())
    .filter(Boolean);
  return {
    emails: new Set(entries.filter((entry) => entry.includes("@")).map((entry) => entry.toLowerCase())),
    clients: new Set(entries.filter((entry) => !entry.includes("@"))),
  };
}

// The detail a 403 names when `env` names no operator, else null: with none named, every
// operator route is refused, never opened to whoever Access admits.
export function unsetOperators(env) {
  const { emails, clients } = operatorList(env[OPERATORS]);
  return emails.size + clients.size === 0 ? `The Worker has no ${OPERATORS} set, so no operator can be admitted.` : null;
}

// accessRefusal's `admit` (router.js): null for a verified token whose email, or whose
// `common_name`, the list names; else the detail a 403 names.
export function admitOperator(env) {
  const { emails, clients } = operatorList(env[OPERATORS]);
  return (claims) => {
    const email = typeof claims?.email === "string" ? claims.email.toLowerCase() : "";
    const client = typeof claims?.common_name === "string" ? claims.common_name : "";
    if ((email && emails.has(email)) || (client && clients.has(client))) return null;
    return NOT_AN_OPERATOR;
  };
}
