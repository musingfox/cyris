#!/usr/bin/env python3
"""Run Propose once against the production data, and report its prompt size and latency.

Reads the sources, the area names and the recent source titles the way /settings does,
calls the model once, and prints what it sent and how long the call took. Nothing is
written: the area table is only read, and the model's grouping is counted, never placed.

Usage:
    uv run python scripts/propose_rerun.py --config /absolute/path/to/cyris.toml

`sources.yaml` is parsed from beside the config, as `cyris.toml` is. The sources themselves
come from the store.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from cyris.adapters.store.areas import D1AreaStore
from cyris.adapters.store.source_titles import D1SourceTitles
from cyris.bootstrap import build_d1_client, build_llm, load_effective_config
from cyris.domain.models import SourceConfig
from cyris.service_layer import area_llm
from cyris.service_layer.ports import LLMClient, LLMResponse


class _Measured:
    """Passes every call through and keeps the length of the last prompt it carried."""

    def __init__(self, inner: LLMClient) -> None:
        self._inner = inner
        self.model = inner.model
        self.prompt_chars = 0

    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        self.prompt_chars = len(prompt)
        return await self._inner.complete(
            prompt, system=system, max_tokens=max_tokens, temperature=temperature
        )


@dataclass(frozen=True)
class Rerun:
    sources: int
    with_titles: int
    prompt_chars: int
    groups: int
    elapsed: float


async def rerun(
    llm: LLMClient,
    sources: Mapping[str, SourceConfig],
    titles: Mapping[str, Sequence[str]] | None,
    area_names: Sequence[str],
    clock: Callable[[], float],
) -> Rerun:
    measured = _Measured(llm)
    start = clock()
    grouping = await area_llm.propose_areas(measured, sources, titles=titles, area_names=area_names)
    elapsed = clock() - start
    return Rerun(
        sources=len(sources),
        with_titles=sum(1 for name in sources if titles and titles.get(name)),
        prompt_chars=measured.prompt_chars,
        groups=len(grouping),
        elapsed=elapsed,
    )


def main(argv: list[str] | None = None, *, clock: Callable[[], float] = time.perf_counter) -> int:
    parser = argparse.ArgumentParser(description="Run Propose once and report its size and latency")
    parser.add_argument("--config", type=Path)
    args = parser.parse_args(argv)
    if args.config is None:
        print("--config is required", file=sys.stderr)
        return 2

    config_path = args.config.resolve()
    try:
        with config_path.open("rb"):
            pass
    except OSError:
        print(f"cannot read --config {args.config}", file=sys.stderr)
        return 2
    print(f"config: {config_path}")

    try:
        cfg = load_effective_config(config_path, config_path.with_name("sources.yaml"))
        d1 = build_d1_client(cfg)
    except Exception as e:  # noqa: BLE001 - a config error can name a secret; the class is enough
        print(f"cannot load config: {type(e).__name__}", file=sys.stderr)
        return 2

    llm = build_llm(cfg.app.llm_provider)
    if llm is None:
        print("no LLM is configured, so Propose cannot run", file=sys.stderr)
        return 1

    try:
        areas = D1AreaStore(d1).list_areas()
        area_names = [
            area.name
            for area in sorted(areas, key=lambda a: (a.status != "active", a.position, a.id))
            if area.status in ("active", "proposed")
        ]
        titles = D1SourceTitles(d1).recent(datetime.now(UTC))
        report = asyncio.run(rerun(llm, cfg.sources, titles, area_names, clock))
    except Exception as e:  # noqa: BLE001 - only the class name leaves this script
        print(f"rerun failed: {type(e).__name__}", file=sys.stderr)
        return 1

    print(f"sources: {report.sources}")
    print(f"sources with titles: {report.with_titles}")
    print(f"prompt chars: {report.prompt_chars}")
    print(f"groups: {report.groups}")
    print(f"elapsed seconds: {report.elapsed:.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
