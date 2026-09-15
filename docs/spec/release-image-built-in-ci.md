---
id: release-image-built-in-ci
status: accepted
scope:
  - ".github/workflows/*.yml"
  - "wrangler.toml"
  - "Dockerfile"
  - "docs/architecture.md"
verify: check:uv run pytest tests/test_release_image.py -q -k release_workflow
related: [wrangler-toml-stays-fork-neutral, image-carries-its-git-sha, revert-carries-the-image]
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

The binding now exists: the `release_workflow` selection of `tests/test_release_image.py`
asserts that the dispatch-only trigger, the check gate, the build, both pushes and the
registry read-back are all in the workflow and in that order. The selection is narrow on
purpose — the same module also guards the Dockerfile invariant and the documentation
prose, and binding this entry to all of it would report the spec as violated when an
unrelated sentence changes.
