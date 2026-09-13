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


RULES_RECEIPT = "rules.json"
COMPUTED_RECEIPT = "computed.json"

# `css_rules` keeps only a top-level `body` in declaration order, because its
# background shorthand must precede its longhands; every other rule is a set.
ORDERED_KEYS = frozenset({"body"})


def _normalize(key: str, value: object) -> tuple[str, ...]:
    """Render one rule's declarations so equality means what the parser meant."""
    if isinstance(value, dict):
        return tuple(f"{name}: {entry}" for name, entry in sorted(value.items()))
    entries = [str(entry) for entry in value] if isinstance(value, list) else [str(value)]
    return tuple(entries) if key in ORDERED_KEYS else tuple(sorted(entries))


def _delta(expected: tuple[str, ...], actual: tuple[str, ...]) -> list[str]:
    lines = [f"      expected only: {entry}" for entry in expected if entry not in actual]
    lines += [f"      actual only:   {entry}" for entry in actual if entry not in expected]
    if lines:
        return lines
    # Same declarations in a different order, which for `body` is the whole
    # point: only the order says whether the shorthand still precedes its
    # longhands. Printing the sets would show nothing at all.
    return [
        f"      expected order: {', '.join(expected)}",
        f"      actual order:   {', '.join(actual)}",
    ]


def load_allowed(path: Path) -> dict[str, dict]:
    """Read the per-page allow-list of added keys, removed keys and changed values."""
    allowed = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(allowed, dict):
        raise SystemExit(f"{path}: allow-list must be a JSON object keyed by page")
    for page, entry in allowed.items():
        if not isinstance(entry, dict):
            raise SystemExit(f"{path}: {page} must map added/removed/changed")
        for bucket in ("added", "removed"):
            if bucket in entry and not isinstance(entry[bucket], list):
                raise SystemExit(f"{path}: {page}.{bucket} must be a JSON list of keys")
        if "changed" in entry and not isinstance(entry["changed"], dict):
            raise SystemExit(f"{path}: {page}.changed must map key to its exact after value")
    return allowed


def compare_page(before: dict, after: dict, allowance: dict) -> list[str]:
    """Report where one page differs beyond what its allow-list enumerates.

    A `changed` entry is checked against its exact after value rather than by
    key alone: naming a rule without pinning its declarations would let that
    rule quietly lose one and still pass.
    """
    allowed_added = set(allowance.get("added") or ())
    allowed_removed = set(allowance.get("removed") or ())
    allowed_changed = allowance.get("changed") or {}

    problems: list[str] = []
    for key in sorted(set(after) - set(before)):
        if key not in allowed_added:
            problems.append(f"  + {key} is not in this page's `added` list")
    for key in sorted(set(before) - set(after)):
        if key not in allowed_removed:
            problems.append(f"  - {key} is not in this page's `removed` list")
    for key in sorted(set(before) & set(after)):
        was = _normalize(key, before[key])
        now = _normalize(key, after[key])
        if was == now:
            continue
        if key not in allowed_changed:
            problems.append(f"  ~ {key} changed and is not in this page's `changed` map")
            problems.extend(_delta(was, now))
            continue
        expected = _normalize(key, allowed_changed[key])
        if expected != now:
            problems.append(f"  ~ {key} did not change to the value `changed` names")
            problems.extend(_delta(expected, now))
    return problems


def _compare_receipt(before_dir: Path, after_dir: Path, name: str, allowed: dict) -> int:
    before = json.loads((before_dir / name).read_text(encoding="utf-8"))
    after = json.loads((after_dir / name).read_text(encoding="utf-8"))
    failures = 0
    for page in sorted(set(before) | set(after)):
        problems = compare_page(before.get(page, {}), after.get(page, {}), allowed.get(page, {}))
        if problems:
            print(f"{name} :: {page}")
            print("\n".join(problems))
            # Detail lines are indented further; they explain a difference
            # rather than being one, so the count stays a count of rules.
            failures += sum(1 for line in problems if not line.startswith("      "))
    return failures


def compare(before_dir: Path, after_dir: Path, allow_path: Path | None) -> int:
    """Require the two snapshots equal modulo the allow-list; return the exit status.

    The computed receipt is compared with no allowance at all. Every allow-list
    entry is an argument that some declaration change is a computed no-op, so
    letting a carve-out reach this layer would excuse the very thing it exists
    to prove.
    """
    allowed = load_allowed(allow_path) if allow_path else {}
    failures = _compare_receipt(before_dir, after_dir, RULES_RECEIPT, allowed)

    computed = [directory / COMPUTED_RECEIPT for directory in (before_dir, after_dir)]
    if all(path.exists() for path in computed):
        failures += _compare_receipt(before_dir, after_dir, COMPUTED_RECEIPT, {})
    elif any(path.exists() for path in computed):
        print(f"{COMPUTED_RECEIPT}: present in only one snapshot")
        failures += 1

    if failures:
        print(f"{failures} unallowed difference(s)")
        return 1
    print("snapshots are equal modulo the allow-list")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    snapshot_parser = subparsers.add_parser("snapshot", help="write rendered pages and rules JSON")
    snapshot_parser.add_argument("output_dir", type=Path)
    compare_parser = subparsers.add_parser(
        "compare", help="require two snapshots to be equal modulo an allow-list"
    )
    compare_parser.add_argument("before_dir", type=Path, help="snapshot taken before the change")
    compare_parser.add_argument("after_dir", type=Path, help="snapshot taken after the change")
    compare_parser.add_argument(
        "--allow",
        type=Path,
        default=None,
        metavar="PATH",
        help="JSON of {page: {added: [key], removed: [key], changed: {key: after value}}}",
    )
    args = parser.parse_args()

    if args.command == "snapshot":
        snapshot(args.output_dir)
    elif args.command == "compare":
        raise SystemExit(compare(args.before_dir, args.after_dir, args.allow))


if __name__ == "__main__":
    main()
