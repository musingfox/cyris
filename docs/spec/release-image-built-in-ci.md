---
id: release-image-built-in-ci
status: accepted
scope:
  - ".github/workflows/*.yml"
  - "wrangler.toml"
  - "Dockerfile"
  - "docs/architecture.md"
verify: null
related: [wrangler-toml-stays-fork-neutral, image-carries-its-git-sha]
source: release-image-build-in-ci
adr: null
---

The image a release deploys must be built and pushed by CI, not by a
workstation. No step on the deploy path may require a local Docker daemon, a
local registry login, or a local keychain.

A `workflow_dispatch` job in `.github/workflows/` builds the image with
`docker build --build-arg GIT_SHA=$GITHUB_SHA` and pushes it to the Cloudflare
registry under two tags: the immutable `:<sha>`, and the mutable `:release`
which the deployed configuration names. Pushing both is what makes the mutable
tag reversible — if a deploy has to be pinned, it is a one-string edit to an
already-published digest, not another build.

Out of scope: the deploy itself, which stays a separate step and a separate
ticket. Also out of scope: a developer building the image locally to test it —
that is not a release, and it produces an image this entry does not let near
production.

A violation is silent because a local build produces a working image. The 2026
-09-14 deployment proved the failure is the opposite one: three workstation-only
obstacles — a repo `.env` token wrangler prefers over OAuth without falling
back, a `docker login` returning `Keychain Error (-61)`, two Docker
installations fighting over a credential-helper symlink — each of which blocks
release entirely while leaving no trace in the repository, since nothing here
observes what a laptop can do.

This entry is prose because the workflow does not exist yet. Its binding, once
it does, is an assertion that the tracked `wrangler.toml` and the release
configuration disagree about `image` in exactly the documented way — which
`wrangler-toml-stays-fork-neutral` already half-checks; a `check:` here would
duplicate it until the deploy path is in scope and can be asserted directly.
