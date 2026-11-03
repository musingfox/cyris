#!/usr/bin/env python3
"""Save production's reader content as a seed, and restore a trial to it.

A release is checked on a trial after a push to main has deployed it to
production (docs/operations.md). `reset_trial.py` takes a trial back to no sources; this
takes it back to a known state with real content instead, so the same check can
be run again from the same start.

save copies, from production's D1, every row of the tables `reset_trial.py`
clears, as data-only SQL, into SEEDS/<name>-<sha7>.sql, where <sha7> is the commit
production's newest run was built from. A table production does not have yet is
printed as absent and left out. It only reads production. Never into the
repo: the repo is public and the rows are real articles. `wrangler d1 export`
blocks production's other queries while it runs, so keep it off the hour (the rss
poll) and the publish hours.

restore clears those tables in the trial's D1 and its pending votes, as
`reset_trial.py` does, then writes the seed's rows, in one file import, which
D1 runs atomically (https://blog.cloudflare.com/building-d1-a-global-database). It
keeps every `settings` row, `usage_log`, `schema_migrations` and `pages_*`:
production's settings hold its notification targets, and its Pages rows name
another Pages project. The
trial's next run publishes every seeded issue its Pages project lacks.

Without --yes, restore is a dry run: it prints each table's rows now and in the
seed, and writes nothing.

Usage:
  seed_trial.py save --name N
  seed_trial.py restore --slug S --name N          dry run
  seed_trial.py restore --slug S --name N --yes    write
"""

import argparse
import functools
import os
import re
import sys
import tempfile
import tomllib
from collections import Counter
from collections.abc import Callable
from pathlib import Path

import provision_trial
import reset_trial
from provision_trial import wrangler, wrangler_env
from reset_trial import CLEARED, ResetError, Runner, Trial, clear_sql, count_sql

ROOT = provision_trial.ROOT
PRODUCTION_CONFIG = "workers/rss/wrangler.toml"
SEEDS = Path.home() / "cyris-trial-seeds"
NAME = re.compile(r"[a-z0-9][a-z0-9-]*")
SHA7 = "[0-9a-f]" * 7
INSERT = re.compile(r'^INSERT INTO "(\w+)" \(', re.M)
# https://developers.cloudflare.com/d1/platform/limits/ : 100 KB per SQL statement.
STATEMENT_LIMIT = 100_000
CHUNK = 50_000


def seed_counts(text: str) -> Counter:
    """Rows per table in a `wrangler d1 export` dump: one INSERT per row."""
    return Counter(INSERT.findall(text))


def _quoted_end(sql: str, i: int) -> int:
    """The index just past the quoted string opening at `i` (a quote inside doubled)."""
    j = i + 1
    while True:
        j = sql.index("'", j) + 1
        if sql[j : j + 1] != "'":
            return j
        j += 1


def _values(statement: str) -> list[str]:
    """The values of one dump INSERT's `VALUES(...)`, each as written: a literal, or an
    expression such as `replace('a\\nb','\\n',char(10))`, which is how the dump writes
    a newline."""
    i = start = statement.index(" VALUES(") + len(" VALUES(")
    out, depth = [], 0
    while True:
        ch = statement[i]
        if ch == "'":
            i = _quoted_end(statement, i)
            continue
        if ch == "(":
            depth += 1
        elif ch in ",)" and depth == 0:
            out.append(statement[start:i])
            if ch == ")":
                return out
            start = i + 1
        elif ch == ")":
            depth -= 1
        i += 1


def _chunks(value: str) -> list[str]:
    """A long value cut into values of about CHUNK bytes whose concatenation is it.

    Its first quoted string is cut, and each piece keeps the expression around it. The
    dump picks each `replace()` marker so that it never occurs in the text by chance
    (`\\n`, else `\\012`, and so on), so no cut may fall inside one.
    """
    q = value.index("'")
    end = _quoted_end(value, q)
    prefix, suffix = value[:q], value[end:]
    markers = re.findall(r"'([^']+)',char\(\d+\)", suffix)
    text = value[q + 1 : end - 1].replace("''", "'")

    def splits_a_marker(at: int) -> bool:
        return any(m in text[max(0, at - len(m) + 1) : at + len(m) - 1] for m in markers)

    pieces, start, size = [], 0, 0
    for at, ch in enumerate(text):
        if size >= CHUNK and not splits_a_marker(at):
            pieces.append(text[start:at])
            start, size = at, 0
        size += len(ch.encode()) * (2 if ch == "'" else 1)
    pieces.append(text[start:])
    return [prefix + reset_trial._quote(p) + suffix for p in pieces]


def fit_statements(text: str, keys: Callable[[str], list[str]]) -> tuple[str, int]:
    """The seed with every INSERT past D1's statement limit split into a smaller INSERT
    and UPDATEs that append each long string back in pieces; and how many were split.

    `wrangler d1 export` writes a row as one statement, values inline, but D1 stored a
    row whose strings run past the limit through bound parameters, which count nothing
    toward it, so its own export can hold a statement its import refuses
    (SQLITE_TOOBIG). `keys` names a table's primary-key columns, for the UPDATEs' WHERE.
    """
    starts = [m.start() for m in INSERT.finditer(text)]
    if not starts:
        return text, 0
    parts, split = [text[: starts[0]]], 0
    for a, b in zip(starts, [*starts[1:], len(text)], strict=True):
        statement = text[a:b]
        if len(statement.rstrip().encode()) <= STATEMENT_LIMIT:
            parts.append(statement)
            continue
        table = INSERT.match(statement).group(1)
        head = statement[: statement.index(" VALUES(")]
        columns = re.findall(r'"(\w+)"', head)[1:]
        values = _values(statement)
        where = " AND ".join(f'"{k}" = {values[columns.index(k)]}' for k in keys(table))
        updates = []
        for col, literal in zip(columns, values, strict=True):
            if "'" in literal and len(literal.encode()) > CHUNK:
                if col in keys(table):
                    raise ResetError(f"refused: a {table} key is past D1's statement limit")
                values[columns.index(col)] = "''"
                updates += [
                    f'UPDATE "{table}" SET "{col}" = "{col}" || {p} WHERE {where};\n'
                    for p in _chunks(literal)
                ]
        insert = f"{head} VALUES({','.join(values)});\n"
        if len(insert.encode()) > STATEMENT_LIMIT:
            raise ResetError(f"refused: a {table} row still runs past D1's statement limit")
        parts += [insert, *updates]
        split += 1
    return "".join(parts), split


def find_seed(seeds: Path, name: str) -> Path:
    found = sorted(seeds.glob(f"{name}-{SHA7}.sql"))
    if len(found) != 1:
        raise ResetError(f"{len(found)} seeds named {name} in {seeds}: need exactly one")
    return found[0]


def save(name: str, seeds: Path, run: Runner, say: Callable[[str], None]) -> int:
    if list(seeds.glob(f"{name}-{SHA7}.sql")):
        raise ResetError(f"a seed named {name} is already in {seeds}; pick another name")
    config = tomllib.loads((ROOT / PRODUCTION_CONFIG).read_text(encoding="utf-8"))
    d1 = config["d1_databases"][0]["database_name"]

    def query(sql: str) -> list[dict]:
        cmd = wrangler(
            PRODUCTION_CONFIG, "d1", "execute", d1, "--remote", "--json", "--command", sql
        )
        return reset_trial._json(run(cmd))[0]["results"]

    rows = query("SELECT build_sha FROM digest_runs WHERE dry_run = 0 ORDER BY id DESC LIMIT 1")
    if not rows:
        raise ResetError(f"{d1} has no run: no commit to name the seed after")
    # Production's schema can lag the repo's: a table a newer release adds is absent
    # until that release deploys, and `d1 export --table` fails on it.
    found = {r["name"] for r in query("SELECT name FROM sqlite_master WHERE type = 'table'")}
    present = [t for t in CLEARED if t in found]
    seeds.mkdir(parents=True, exist_ok=True)
    path = seeds / f"{name}-{rows[0]['build_sha'][:7]}.sql"
    tables = [arg for t in present for arg in ("--table", t)]
    run(
        wrangler(
            PRODUCTION_CONFIG,
            "d1",
            "export",
            d1,
            "--remote",
            "--no-schema",
            *tables,
            "--output",
            str(path),
        )
    )
    counts = seed_counts(path.read_text(encoding="utf-8"))
    say(f"Saved {path} ({path.stat().st_size} bytes) from {d1}, rows per table:")
    for table in CLEARED:
        say(f"  {table:<24} {counts[table] if table in found else 'absent'}")
    return 0


def _keys(trial: Trial, table: str) -> list[str]:
    sql = f"SELECT name FROM pragma_table_info('{table}') WHERE pk > 0 ORDER BY pk"
    return [r["name"] for r in trial.rows(sql)]


def restore(
    slug: str, name: str, write: bool, seeds: Path, run: Runner, say: Callable[[str], None]
) -> int:
    path = find_seed(seeds, name)
    text = path.read_text(encoding="utf-8")
    counts = seed_counts(text)
    stray = sorted(set(counts) - set(CLEARED))
    if stray:
        raise ResetError(f"refused: {path} writes {', '.join(stray)}, which a seed never holds")
    trial = Trial(slug, run)
    found = {r["name"] for r in trial.rows("SELECT name FROM sqlite_master WHERE type = 'table'")}
    missing = sorted(t for t in counts if t not in found)
    if missing:
        raise ResetError(f"refused: {trial.d1} has no {', '.join(missing)} table for the seed")
    present = [t for t in CLEARED if t in found]
    text, split = fit_statements(text, functools.cache(lambda table: _keys(trial, table)))
    now = trial.rows(count_sql(present))[0] if present else {}
    say(f"D1 {trial.d1} ({trial.rss_config}), rows now -> rows in {path.name}:")
    for table in CLEARED:
        say(f"  {table:<24} {now.get(table, 'absent')} -> {counts[table]}")
    votes = trial.votes()
    say(f"KV {trial.kv_binding} ({trial.promote_config}): {len(votes)} pending votes to delete")
    say(f"{split} rows run past D1's statement limit; each is sent as an INSERT and UPDATEs.")
    say(
        "Kept: every settings row, usage_log, schema_migrations, pages_manifest, "
        "pages_deploy_receipt."
    )
    if not write:
        say("Dry run: nothing was written. Run again with --yes to write.")
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "restore.sql"
        clear = "".join(f"{s};\n" for s in clear_sql(present))
        script.write_text("PRAGMA defer_foreign_keys=TRUE;\n" + clear + text, encoding="utf-8")
        run(
            wrangler(
                trial.rss_config,
                "d1",
                "execute",
                trial.d1,
                "--remote",
                "--yes",
                "--file",
                str(script),
            )
        )
    if votes:
        trial.delete_votes(votes)
    after = trial.rows(count_sql(present))[0] if present else {}
    off = {t: n for t, n in after.items() if n != counts[t]}
    listed = trial.votes()
    # WHY a warning, not a failure: the import succeeded. An rss poll can
    # buffer articles after it, and KV listing is eventually consistent.
    if off or listed:
        print(
            f"warning: after the restore, {off or 'no table'} differs from the seed and"
            f" {len(listed)} votes still list in KV. Run the dry run again in a minute.",
            file=sys.stderr,
        )
    say(f"Restored {slug} to {path.name}.")
    return 0


def _production_env() -> dict[str, str]:
    """The login, never a token the shell exported (see provision_trial.wrangler_env)."""
    env = dict(os.environ)
    env.pop("CLOUDFLARE_API_TOKEN", None)
    return env


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog=__doc__.split("\n\n", 1)[1] + f"\nSeeded tables: {', '.join(CLEARED)}.\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("save", "restore"):
        p = sub.add_parser(command)
        p.add_argument("--name", required=True, help="the seed's name, e.g. before-1-4")
        if command == "restore":
            p.add_argument("--slug", required=True, help="the trial's slug, e.g. trial1")
            p.add_argument("--yes", action="store_true", help="write; without it, a dry run")
    return parser


def main(
    argv: list[str] | None = None,
    run: Runner | None = None,
    say: Callable[[str], None] = print,
    seeds: Path = SEEDS,
) -> int:
    args = build_parser().parse_args(argv)
    try:
        if not NAME.fullmatch(args.name):
            raise ResetError(f"refused: name {args.name!r} is not lowercase letters, digits, -")
        if args.command == "save":
            return save(args.name, seeds, run or reset_trial._runner(_production_env()), say)
        runner = run or reset_trial._runner(wrangler_env(args.slug))
        return restore(args.slug, args.name, args.yes, seeds, runner, say)
    except ResetError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
