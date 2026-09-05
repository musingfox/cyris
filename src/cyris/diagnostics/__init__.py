"""Tools that inspect the deployment rather than run it.

`doctor` asks whether this deployment will work; `compare` runs one window
through two wirings and reports where they differ. Neither is part of making a
digest, and both need what the core is forbidden to touch: the composition root
and the adapters it builds.

The rule for this layer: it may import `bootstrap`, `adapters`, `service_layer`,
`domain` and `config`; only `entrypoints/` may import it. `tests/test_core_imports.py`
enforces both directions.
"""
