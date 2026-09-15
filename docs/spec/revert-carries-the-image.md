---
id: revert-carries-the-image
status: proposed
scope:
  - ".github/workflows/*.yml"
  - "wrangler.toml"
  - "scripts/*.sh"
  - "docs/architecture.md"
verify: check:uv run pytest tests/test_release_image.py -q -k refuses_to_republish
related: [release-image-built-in-ci, image-carries-its-git-sha]
source: verify-container-rollback
adr: null
---

Returning the deployment to an earlier state must return the container image with
it, and the revert counts as done only when the deployment can be asked which
image it is now running — the revert command exiting 0 is not that answer.

The image cannot be recreated on demand: the base is a moving tag and the apt
packages are unpinned, so the release workflow's `existing` step refuses to
rebuild an already-published `:<sha>` rather than publish a different image under
the same name. Going back is therefore always a reference to an image already in
the registry — the immutable `:<sha>`, whose digest the `image/<short-sha>` git
tag records — never a rebuild of the old commit. The question is answered on the
delivery side, from the image the deployed version serves or the sha the
container reports of itself; `CYRIS_GIT_SHA` is baked into the image for that.

Out of scope: which command moves the reference. Whether `wrangler rollback`
restores the image alongside the Worker version, or the reference has to be moved
by a pinned deploy naming the digest, is what `verify-container-rollback` settles.
Out of scope too, and deliberately so: whether the reverted code then produces a
good digest. That is a different question with a different failure, and a revert
judged by a pipeline run would read an hour with nothing to fetch as a failed
revert — leaving nobody able to say whether the deploy or the product was at
fault. A run receipt's build sha corroborates a revert afterwards; it never
defines one. A locally built development image is out of scope as well, and this
entry does not let one near production.

A violation is silent because a deployment running the wrong image is a working
deployment. The revert reports success, the container starts, the digest is
scored and the pages publish; nothing anywhere logs that the code producing them
is not the code that was asked for. The only thing that differs is the image's
identity, and nobody asks for it until something has already gone wrong — which
is how production came to run 121 commits behind on 2026-09-14, undatable until
someone went looking.

The binding is partial on purpose. The half that exists today is the premise:
`test_release_workflow_refuses_to_republish_a_published_commit` holds the workflow
to refusing a rebuild, which is what leaves a reference as the only way back. The
delivery-side read cannot be bound yet, because nothing reads the baked sha — the
`Dockerfile` sets it and no code consumes it — and the revert procedure is not
chosen. When `verify-container-rollback` reports which command carries the image
and something reads that sha, this `verify` moves to a check on it.
