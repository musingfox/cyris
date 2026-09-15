---
id: image-carries-its-git-sha
status: accepted
scope:
  - "Dockerfile"
  - "docker/entrypoint.sh"
  - "src/cyris/service_layer/run_digest.py"
  - ".github/workflows/*.yml"
verify: check:uv run pytest tests/test_release_image.py -q -k git_sha
related: [release-image-built-in-ci, revert-carries-the-image]
source: release-image-build-in-ci
adr: null
---

Every image that can reach production must be able to name the commit it was
built from, and the running container must be able to read that name without
network access.

The build passes the commit through a `GIT_SHA` build ARG which the Dockerfile
promotes to an `ENV` the process inherits. The `ARG`/`ENV` pair goes after the
`RUN uv sync --frozen --no-dev` layer: placed earlier it changes on every
commit and discards the dependency cache. `wrangler containers build` has no
`--build-arg`, so the build is `docker build --build-arg GIT_SHA=...` followed
by `wrangler containers push`, never `wrangler containers build`.

Out of scope: what reads the value. This entry requires only that it is present
and readable in the process environment; `run_summary` in `run_digest.py` and
the D1 run receipt are the consumers, and they are specified elsewhere. A local
`docker build` with no `--build-arg` legitimately produces an image with an
empty sha — that image is a development image and this entry does not let it
reach production.

A violation is silent because an image with no sha behaves exactly like one with
a sha: the digest runs, the pages publish, nothing logs a complaint. What is
lost is only the ability to ask a question, and the question is asked only when
something is already wrong. That is how production came to run 121 commits
behind on 2026-09-14 with nobody able to date it.

The binding now exists: `tests/test_release_image.py` asserts that both directives are present
and that they sit below the `uv sync` layer, and this entry's `verify:` runs the `git_sha`
selection of that module. The selection is deliberate — the module also guards the release
workflow and the documentation prose, and binding this entry to all of it would report the
spec as violated when an unrelated sentence changes, which is the false positive this entry
ruled out when it was written.
