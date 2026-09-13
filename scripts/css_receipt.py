#!/usr/bin/env python3
"""Create deterministic HTML and CSS-rule receipts for digest templates."""

import argparse
import json
import sys
from pathlib import Path

# pytest adds tests/ to sys.path, but this standalone CLI must do so explicitly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))

from css_rules import parse_style_block, receipt_fixtures  # noqa: E402


def _json_rules(rules: dict[str, set[str] | list[str]]) -> dict[str, list[str]]:
    """Make parser output deterministic and JSON-serializable."""
    return {
        key: value if isinstance(value, list) else sorted(value) for key, value in rules.items()
    }


def snapshot(output_dir: Path) -> None:
    """Write frozen rendered pages and their parsed CSS rules to ``output_dir``."""
    output_dir.mkdir(parents=True, exist_ok=True)
    pages = dict(zip(("index", "digest", "raw"), receipt_fixtures(), strict=True))
    for name, html in pages.items():
        (output_dir / f"{name}.html").write_text(html, encoding="utf-8")
    rules = {name: _json_rules(parse_style_block(html)) for name, html in pages.items()}
    (output_dir / "rules.json").write_text(
        json.dumps(rules, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    snapshot_parser = subparsers.add_parser("snapshot", help="write rendered pages and rules JSON")
    snapshot_parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()

    if args.command == "snapshot":
        snapshot(args.output_dir)


if __name__ == "__main__":
    main()
