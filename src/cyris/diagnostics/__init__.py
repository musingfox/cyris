"""Tools that inspect the deployment rather than run it.

`doctor` asks whether this deployment will work at all. Answering that is not
part of making a digest, and it needs what the core is forbidden to touch: the
composition root and the adapters it builds.

The rule for this layer: it may import `bootstrap`, `adapters`, `service_layer`,
`domain` and `config`; only `entrypoints/` may import it. `tests/test_core_imports.py`
enforces both directions.
"""
