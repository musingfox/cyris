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
                            --kv-id ID [--newsletter-kv-id ID] --d1-id UUID
"""

import argparse
import os
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
NEWSLETTER = "workers/newsletter/wrangler.toml"

_LABEL = r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
_DOMAIN = rf"^{_LABEL}(\.{_LABEL})+$"
SLUG = r"[a-z][a-z0-9]{1,19}"
# The wizard state key whose presence alone says the trial has a newsletter Worker.
NEWSLETTER_KV_KEY = "TRIAL_NEWSLETTER_KV_ID"


class DriftError(Exception):
    pass


class ProductionNameError(Exception):
    """A rendered trial config names a resource that is production's, or not this trial's."""


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
        "newsletter": _get(NEWSLETTER, _load(NEWSLETTER), "name"),
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


def _render_newsletter(args, n: dict[str, str], t: dict[str, str]) -> str:
    text = _read(NEWSLETTER)
    old_id = _get(NEWSLETTER, _load(NEWSLETTER), "kv_namespaces", 0, "id")
    newsletter_name = t["newsletter"]
    text = _replace_line(
        text, NEWSLETTER, f'name = "{n["newsletter"]}"', f'name = "{newsletter_name}"'
    )
    text = _replace_line(
        text,
        NEWSLETTER,
        f'  {{ binding = "NEWSLETTERS", id = "{old_id}" }}',
        f'  {{ binding = "NEWSLETTERS", id = "{args.newsletter_kv_id}" }}',
    )
    got = tomllib.loads(text)
    _expect(NEWSLETTER, "name", got.get("name"), newsletter_name)
    _expect(
        NEWSLETTER,
        "kv_namespaces",
        got.get("kv_namespaces"),
        [{"binding": "NEWSLETTERS", "id": args.newsletter_kv_id}],
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
        "newsletter": f"{n['newsletter']}-{slug}",
    }


def newsletter_path(slug: str) -> str:
    return f"workers/newsletter/wrangler.trial-{slug}.toml"


def has_newsletter(slug: str) -> bool:
    """Whether the trial has a newsletter Worker: its wizard state holds the KV id, nothing else."""
    return bool(wizard_state(slug).get(NEWSLETTER_KV_KEY))


def newsletter_note(slug: str) -> str:
    """One line for a newsletter config on disk that the wizard state has no KV id for, else "".

    Such a config is never read or validated and nothing is done to it: a trial wired before
    the newsletter Worker has no id, and a stray file does not give it one.
    """
    if has_newsletter(slug) or not (ROOT / newsletter_path(slug)).exists():
        return ""
    return (
        f"{newsletter_path(slug)} exists but .env.trial-{slug}-wizard has no {NEWSLETTER_KV_KEY}:"
        " it is left alone, and so is its NEWSLETTERS KV. To give this trial a newsletter Worker,"
        f" save that KV's id as {NEWSLETTER_KV_KEY} in the wizard state and run this again."
    )


def rendered_paths(slug: str) -> dict[str, str]:
    """Where `render` writes a trial's configs, relative to the repo root.

    The app, promote and rss configs always; the newsletter config only when the wizard
    state holds the trial's newsletter KV id and the config has been rendered.
    """
    paths = {
        "app": f"wrangler.trial-{slug}.toml",
        "promote": f"workers/promote/wrangler.trial-{slug}.toml",
        "rss": f"workers/rss/wrangler.trial-{slug}.toml",
    }
    if has_newsletter(slug) and (ROOT / newsletter_path(slug)).exists():
        paths["newsletter"] = newsletter_path(slug)
    return paths


def production_names() -> set[str]:
    """Every resource name and id the tracked templates give production."""
    n = _template_names()
    d1 = _rss_d1()
    kv = _get(PROMOTE, _load(PROMOTE), "kv_namespaces", 0, "id")
    newsletter_kv = _get(NEWSLETTER, _load(NEWSLETTER), "kv_namespaces", 0, "id")
    return {*n.values(), d1["database_name"], d1["database_id"], kv, newsletter_kv}


def load_rendered(slug: str) -> dict[str, dict]:
    """A trial's rendered configs, parsed, once each names only this trial's resources.

    The newsletter config is checked when the wizard state holds the trial's KV id for it, and
    left out, unread, when it does not.

    The scripts that act on a live trial (`reset_trial.py`, `deploy_trial.py`) call
    this before any wrangler command, so a slug that fails the naming rule, a config
    that was never rendered, or one edited to name a production resource stops them.
    """
    if not re.fullmatch(SLUG, slug):
        raise ProductionNameError(f"slug {slug!r} must match {SLUG}")
    want = _names(slug, _template_names())
    taken = production_names()
    out: dict[str, dict] = {}
    for kind, rel in rendered_paths(slug).items():
        try:
            out[kind] = tomllib.loads((ROOT / rel).read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ProductionNameError(
                f"{rel} not found: run this from the checkout that provisioned {slug},"
                " or render its configs with `provision_trial.py render` first"
            ) from None
    d1 = (out["rss"].get("d1_databases") or [{}])[0]
    kv = (out["promote"].get("kv_namespaces") or [{}])[0]
    containers = out["app"].get("containers") or [{}]
    named = {
        f"{rendered_paths(slug)['app']} name": (out["app"].get("name"), want["app"]),
        f"{rendered_paths(slug)['app']} containers[0].name": (
            containers[0].get("name"),
            want["container"],
        ),
        f"{rendered_paths(slug)['promote']} name": (out["promote"].get("name"), want["promote"]),
        f"{rendered_paths(slug)['rss']} name": (out["rss"].get("name"), want["rss"]),
        f"{rendered_paths(slug)['rss']} database_name": (d1.get("database_name"), want["d1"]),
    }
    ids = [
        (f"{rendered_paths(slug)['rss']} database_id", d1.get("database_id")),
        (f"{rendered_paths(slug)['promote']} kv_namespaces[0].id", kv.get("id")),
    ]
    if "newsletter" in out:
        rel = rendered_paths(slug)["newsletter"]
        newsletter_kv = (out["newsletter"].get("kv_namespaces") or [{}])[0]
        named[f"{rel} name"] = (out["newsletter"].get("name"), want["newsletter"])
        ids.append((f"{rel} kv_namespaces[0].id", newsletter_kv.get("id")))
        if newsletter_kv.get("binding") != "NEWSLETTERS":
            raise ProductionNameError(
                f"{rel} kv_namespaces[0].binding is {newsletter_kv.get('binding')!r},"
                " expected 'NEWSLETTERS'"
            )
    for label, (got, expected) in named.items():
        if got in taken:
            raise ProductionNameError(f"{label} is {got!r}, a production name")
        if got != expected:
            raise ProductionNameError(f"{label} is {got!r}, expected {expected!r}")
    for label, got in ids:
        if not got or got in taken:
            raise ProductionNameError(f"{label} is {got!r}, production's or empty")
    return out


def env_file(rel: str) -> dict[str, str]:
    """A KEY=value file the wizard wrote at the repo root, last line wins; {} when absent."""
    path = ROOT / rel
    if not path.exists():
        return {}
    pairs = (line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines())
    return {kv[0]: kv[1] for kv in pairs if len(kv) == 2}


def wizard_state(slug: str) -> dict[str, str]:
    """The wizard's saved inputs, ids and image for a trial."""
    return env_file(f".env.trial-{slug}-wizard")


def wrangler(config: str, *args: str) -> list[str]:
    """Every wrangler call on a trial: its own config, and never this checkout's `.env`."""
    return ["bunx", "wrangler", *args, "--config", config, "--env-file", "/dev/null"]


def wrangler_env(slug: str, base: dict[str, str] | None = None) -> dict[str, str]:
    """The environment wrangler runs in: the login, never a token the shell exported.

    A `CLOUDFLARE_API_TOKEN` beats both `--env-file /dev/null` and the OAuth login
    (docs/trial-deployment.md), so it is dropped. The account comes from the
    wizard's state, as the wizard itself exports it.
    """
    env = dict(base if base is not None else os.environ)
    env.pop("CLOUDFLARE_API_TOKEN", None)
    account = wizard_state(slug).get("ACCOUNT_ID")
    if account:
        env["CLOUDFLARE_ACCOUNT_ID"] = account
    return env


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
    print(f"TRIAL_NEWSLETTER_WORKER={t['newsletter']}")
    print(f"TRIAL_NEWSLETTER_KV_TITLE={t['newsletter']}")
    return 0


def cmd_render(args, parser) -> int:
    n = _template_names()
    if args.d1_id == _rss_d1()["database_id"]:
        parser.error("argument --d1-id: that is the template's database id, not a trial's own")
    template_kv_ids = {
        _get(rel, _load(rel), "kv_namespaces", 0, "id") for rel in (PROMOTE, NEWSLETTER)
    }
    for flag, value in (("--kv-id", args.kv_id), ("--newsletter-kv-id", args.newsletter_kv_id)):
        if value in template_kv_ids:
            parser.error(
                f"argument {flag}: that is a template's KV namespace id, not a trial's own"
            )
    if args.newsletter_kv_id == args.kv_id:
        parser.error("argument --newsletter-kv-id: must differ from --kv-id")
    t = _names(args.slug, n)
    paths = rendered_paths(args.slug)
    outputs = {
        paths["app"]: _render_app(args, n, t),
        paths["promote"]: _render_promote(args, n, t),
        paths["rss"]: _render_rss(args, n, t),
    }
    if args.newsletter_kv_id:
        outputs[newsletter_path(args.slug)] = _render_newsletter(args, n, t)
    for rel, text in outputs.items():
        (ROOT / rel).write_text(text, encoding="utf-8")
        print(rel)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    slug = _pattern(SLUG, "lowercase letter then 1-19 lowercase letters or digits")
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
            # Optional: deploy_trial.py re-renders a trial with what its wizard state
            # holds, and a trial wired before the newsletter Worker has no such id.
            p.add_argument(
                "--newsletter-kv-id",
                type=_pattern(r"[0-9a-f]{32}", "32 lowercase hex"),
            )
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
