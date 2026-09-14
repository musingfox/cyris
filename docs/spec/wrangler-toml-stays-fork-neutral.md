---
id: wrangler-toml-stays-fork-neutral
status: accepted
scope:
  - "wrangler.toml"
  - ".github/workflows/*.yml"
  - "scripts/*.sh"
verify: check:grep -q '^image = "./Dockerfile"' wrangler.toml && ! grep -qE "registry[.]cloudflare[.]com|account_id" wrangler.toml
related: [release-image-built-in-ci]
source: release-image-build-in-ci
adr: null
---

The tracked `wrangler.toml` must stay deployable by a stranger who has only
cloned this repository: its `[[containers]] image` stays `"./Dockerfile"`, and
no account id, registry URI, or other deployer identity appears anywhere in the
file.

The release path gets its own configuration instead. A release deploy renders a
derived config — same file with `image` replaced by
`registry.cloudflare.com/<ACCOUNT_ID>/<IMAGE>:<TAG>` — from the account id the
CI environment supplies, and passes it with `wrangler deploy --config`. The
derived file is a build artifact: generated, used, never committed.

Out of scope: everything grade B already lives outside this file (Worker
secrets, `DIGEST_ORIGIN`, custom domains), and this entry does not change that.
A fork that wants to pin a registry image in its own checkout is free to — the
constraint binds this repository's tracked file, not a downstream copy.

A violation is silent because the deployer who breaks it is the one person for
whom it still works: their account id resolves, their deploy succeeds, and CI
stays green. The breakage appears only in someone else's clone, as a Deploy to
Cloudflare button that builds an image into an account that is not theirs, or a
`wrangler deploy` that 404s on a registry path they cannot read. Nothing in this
repository observes that.
