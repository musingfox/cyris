#!/usr/bin/env python3
"""Render the per-trial wrangler configs and print a trial's resource names.

A trial deployment shares the repository with production but must not share a
Worker, a container application, a KV namespace or a D1 database with it: one
`wrangler deploy` under a production name would replace the production Worker.
The tracked templates cannot carry trial identity (docs/spec/wrangler-toml-stays-
fork-neutral.md), so each trial gets generated copies beside them. They are
build artifacts, gitignored, never committed.

Every rewrite is an exact-line replacement that must match exactly once. A
template that drifted makes the script stop; re-serializing the parsed TOML
would drop the comments the templates carry.

Usage:
  provision_trial.py names  --slug S --pages-suffix HEX16
  provision_trial.py render --slug S --domain D --account-id ID --image-digest sha256:HEX
                            --kv-id ID --d1-id UUID
"""

import argparse
import re
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DERIVE = ROOT / "scripts/derive-wrangler-config.sh"
APP = "wrangler.toml"
PROMOTE = "workers/promote/wrangler.toml"
RSS = "workers/rss/wrangler.toml"

_LABEL = r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
_DOMAIN = rf"^{_LABEL}(\.{_LABEL})+$"


class DriftError(Exception):
    pass


def _pattern(regex: str, what: str):
    def check(value: str) -> str:
        if not re.fullmatch(regex, value):
            raise argparse.ArgumentTypeError(f"must match {regex} ({what})")
        return value

    return check


def _load(rel: str) -> dict:
    path = ROOT / rel
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise DriftError(f"{rel}: template not found") from None
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        at = re.search(r"line (\d+)", str(exc))
        line = f": {text.split(chr(10))[int(at.group(1)) - 1]!r}" if at else ""
        raise DriftError(f"{rel}: not valid TOML ({exc}){line}") from None


def _get(rel: str, doc: dict, *path: str | int):
    """Walk `path` through a parsed template, naming the file and the step that is missing."""
    node = doc
    for i, step in enumerate(path):
        try:
            node = node[step]
        except (KeyError, IndexError, TypeError):
            shown = "".join(f"[{s}]" if isinstance(s, int) else f".{s}" for s in path[: i + 1])
            raise DriftError(f"{rel}: expected `{shown.lstrip('.')}`, found none") from None
    return node


def _rss_d1() -> dict:
    _require_one(_read(RSS), RSS, "[[d1_databases]]")
    doc = _load(RSS)
    d1 = _get(RSS, doc, "d1_databases", 0)
    for key in ("binding", "database_name", "database_id"):
        _get(RSS, doc, "d1_databases", 0, key)
    return d1


def _template_names() -> dict[str, str]:
    _require_one(_read(APP), APP, "[[containers]]")
    app = _load(APP)
    return {
        "root": _get(APP, app, "name"),
        "container": _get(APP, app, "containers", 0, "name"),
        "promote": _get(PROMOTE, _load(PROMOTE), "name"),
        "rss": _get(RSS, _load(RSS), "name"),
    }


def _replace_line(text: str, rel: str, old: str, new: str) -> str:
    lines = text.split("\n")
    hits = [i for i, line in enumerate(lines) if line == old]
    if len(hits) != 1:
        raise DriftError(f"{rel}: expected exactly one line {old!r}, found {len(hits)}")
    lines[hits[0]] = new
    return "\n".join(lines)


def _require_one(text: str, rel: str, table: str) -> None:
    count = text.split("\n").count(table)
    if count != 1:
        raise DriftError(f"{rel}: expected exactly one {table} table, found {count}")


def _expect(rel: str, label: str, got, want) -> None:
    if got != want:
        raise DriftError(f"{rel}: {label} is {got!r}, expected {want!r}")


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _render_app(args, n: dict[str, str], t: dict[str, str]) -> str:
    app_name, container_name = t["app"], t["container"]
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "derived.toml"
        done = subprocess.run(
            ["bash", str(DERIVE), args.account_id, n["container"], args.image_digest, str(out)],
            capture_output=True,
            text=True,
        )
        if done.returncode != 0:
            raise DriftError(f"{APP}: derive-wrangler-config.sh refused: {done.stderr.strip()}")
        text = out.read_text(encoding="utf-8")
    _require_one(text, APP, "[[containers]]")
    text = _replace_line(text, APP, f'name = "{n["root"]}"', f'name = "{app_name}"')
    text = _replace_line(
        text,
        APP,
        f'CYRIS_APP_WORKER_NAME = "{n["root"]}"',
        f'CYRIS_APP_WORKER_NAME = "{app_name}"\nCYRIS_PRIVATE_ARCHIVE = "true"',
    )
    text = _replace_line(text, APP, f'name = "{n["container"]}"', f'name = "{container_name}"')
    host = f"{args.slug}.{args.domain}"
    text = text.rstrip("\n") + f'\n\n[[routes]]\npattern = "{host}"\ncustom_domain = true\n'
    got = tomllib.loads(text)
    tracked = _load(APP)
    _expect(APP, "name", got.get("name"), app_name)
    _expect(
        APP,
        "vars",
        got.get("vars"),
        {"CYRIS_APP_WORKER_NAME": app_name, "CYRIS_PRIVATE_ARCHIVE": "true"},
    )
    _expect(APP, "containers[0].name", got["containers"][0]["name"], container_name)
    _expect(
        APP,
        "containers[0].image",
        got["containers"][0]["image"],
        f"registry.cloudflare.com/{args.account_id}/{n['container']}@{args.image_digest}",
    )
    _expect(APP, "routes", got.get("routes"), [{"pattern": host, "custom_domain": True}])
    _expect(
        APP,
        "durable_objects",
        got.get("durable_objects"),
        tracked.get("durable_objects"),
    )
    return text


def _render_promote(args, n: dict[str, str], t: dict[str, str]) -> str:
    text = _read(PROMOTE)
    old_id = _get(PROMOTE, _load(PROMOTE), "kv_namespaces", 0, "id")
    promote_name = t["promote"]
    text = _replace_line(text, PROMOTE, f'name = "{n["promote"]}"', f'name = "{promote_name}"')
    text = _replace_line(
        text,
        PROMOTE,
        f'  {{ binding = "PROMOTIONS", id = "{old_id}" }}',
        f'  {{ binding = "PROMOTIONS", id = "{args.kv_id}" }}',
    )
    got = tomllib.loads(text)
    _expect(PROMOTE, "name", got.get("name"), promote_name)
    _expect(
        PROMOTE,
        "kv_namespaces",
        got.get("kv_namespaces"),
        [{"binding": "PROMOTIONS", "id": args.kv_id}],
    )
    return text


def _render_rss(args, n: dict[str, str], t: dict[str, str]) -> str:
    text = _read(RSS)
    d1 = _rss_d1()
    rss_name, d1_name = t["rss"], t["d1"]
    text = _replace_line(text, RSS, f'name = "{n["rss"]}"', f'name = "{rss_name}"')
    text = _replace_line(
        text, RSS, f'database_name = "{d1["database_name"]}"', f'database_name = "{d1_name}"'
    )
    text = _replace_line(
        text, RSS, f'database_id = "{d1["database_id"]}"', f'database_id = "{args.d1_id}"'
    )
    got = tomllib.loads(text)
    _expect(RSS, "name", got.get("name"), rss_name)
    _expect(
        RSS,
        "d1_databases",
        got.get("d1_databases"),
        [{"binding": d1["binding"], "database_name": d1_name, "database_id": args.d1_id}],
    )
    return text


def _names(slug: str, n: dict[str, str]) -> dict[str, str]:
    return {
        "app": f"{n['root']}-{slug}",
        "container": f"{n['container']}-{slug}",
        "promote": f"{n['promote']}-{slug}",
        "d1": f"{n['root']}-{slug}",
        "rss": f"{n['rss']}-{slug}",
    }


def cmd_names(args) -> int:
    n = _template_names()
    t = _names(args.slug, n)
    print(f"TRIAL_APP_WORKER={t['app']}")
    print(f"TRIAL_CONTAINER={t['container']}")
    print(f"TRIAL_PROMOTE_WORKER={t['promote']}")
    print(f"TRIAL_KV_TITLE={t['promote']}")
    print(f"TRIAL_D1_NAME={t['d1']}")
    print(f"TRIAL_PAGES_PROJECT={n['root']}-{args.slug}-{args.pages_suffix}")
    print(f"TRIAL_RSS_WORKER={t['rss']}")
    return 0


def cmd_render(args, parser) -> int:
    n = _template_names()
    if args.d1_id == _rss_d1()["database_id"]:
        parser.error("argument --d1-id: that is the template's database id, not a trial's own")
    t = _names(args.slug, n)
    outputs = {
        f"wrangler.trial-{args.slug}.toml": _render_app(args, n, t),
        f"workers/promote/wrangler.trial-{args.slug}.toml": _render_promote(args, n, t),
        f"workers/rss/wrangler.trial-{args.slug}.toml": _render_rss(args, n, t),
    }
    for rel, text in outputs.items():
        (ROOT / rel).write_text(text, encoding="utf-8")
        print(rel)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    slug = _pattern(
        r"[a-z][a-z0-9]{1,19}", "lowercase letter then 1-19 lowercase letters or digits"
    )
    for name in ("names", "render"):
        p = sub.add_parser(name)
        p.add_argument("--slug", required=True, type=slug)
        if name == "names":
            p.add_argument(
                "--pages-suffix", required=True, type=_pattern(r"[0-9a-f]{16}", "16 lowercase hex")
            )
        else:
            p.add_argument("--domain", required=True, type=_pattern(_DOMAIN, "lowercase hostname"))
            p.add_argument("--account-id", required=True, type=_pattern(r"[0-9a-f]{32}", "32 hex"))
            p.add_argument(
                "--image-digest",
                required=True,
                type=_pattern(r"sha256:[0-9a-f]{64}", "sha256:<64 lowercase hex>"),
            )
            p.add_argument("--kv-id", required=True, type=_pattern(r"[0-9a-f]{32}", "32 hex"))
            p.add_argument(
                "--d1-id",
                required=True,
                type=_pattern(
                    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                    "lowercase UUID",
                ),
            )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return cmd_names(args) if args.command == "names" else cmd_render(args, parser)
    except DriftError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
