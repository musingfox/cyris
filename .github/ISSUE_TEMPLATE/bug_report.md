---
name: Bug report
about: Something in the pipeline behaves differently than documented
labels: bug
---

**What happened, and what you expected instead**

**Which command**
`cyris run` / `cyris doctor` / `cyris triage-ui` (`/settings`) / a Worker / other:

**Relevant output**
Re-run with `--verbose` if you can, and paste the log around the failure.

```
```

**Environment**
- cyris version or commit:
- Python / how you run it (uv on the host, docker compose, Cloudflare):
- LLM provider (from `/settings`, or `[llm_provider] provider` in `cyris.toml`):

**Config**
The output of `cyris doctor`, and the relevant settings and source entry — from
`/settings` on a Cloudflare deployment, from `cyris.toml` and `sources.yaml` on a
local one — with API keys, tokens, and any subscriber-specific feed URL removed:
a personalised feed URL identifies you.
