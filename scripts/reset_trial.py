#!/usr/bin/env python3
"""Clear one trial deployment back to no sources, waiting for onboarding.

A trial is verified after each release before production gets the image
(docs/operations.md), and a verification that walks /onboarding needs a trial
that has not been onboarded yet. This empties the trial's reader content and
resets the settings the onboarding wizard asks for, so its tester (or the owner)
walks the wizard again from the first step.

Cleared, in the trial's D1: every row of the tables listed at the end of this
help. In `settings`, only the keys listed there, written back to what
`cyris.toml.example` ships. In the vote Worker's KV: every pending vote.

Kept: `usage_log` (what the trial has spent), `schema_migrations` (the schema steps
already run), every other `settings` key (the LLM provider and model the owner
pushed among them), the Workers, their secrets, the Access policy and the Pages
project. `pages_manifest`, `pages_deploy_receipt` and the digests already published
to the trial's Pages project are kept too: whether a reset should take the archive
with it is not decided, so this leaves it, and the next issue publishes beside the
old ones.

Without --yes it is a dry run: it prints the row count of every table it would
touch and the SQL it would send, and writes nothing. With --yes it prints the same
counts, then writes. A flag rather than a prompt, because the session that runs it
is not always a terminal: a prompt there is answered by a pipe or hangs, and a
dry run that is the default cannot write by accident.

Usage:
  reset_trial.py --slug S          dry run
  reset_trial.py --slug S --yes    write
"""

import argparse
import json
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import provision_trial
from provision_trial import wrangler, wrangler_env

ROOT = provision_trial.ROOT
SHIPPED_SETTINGS = "cyris.toml.example"

# Reader content: what a tester's onboarding and the runs after it wrote. Every table
# the schema creates is either here or in KEPT; tests/test_reset_trial.py holds the
# two lists to src/cyris/adapters/store/schema.sql and the rss Worker's own SCHEMA.
CLEARED = (
    "sources",
    "articles",
    "stored_articles",
    "digests",
    "digest_runs",
    "digest_inputs",
    "stories",
    "story_members",
    "topics",
    "topic_members",
    "topic_tags",
    "topic_votes",
    "tags",
    "article_tags",
    "areas",
    "area_birth_threshold",
    "tracked_topics",
    "blind_labels",
    "vote_similarity_shadow",
    "suppressions",
    "feed_health",
)
KEPT = {
    "usage_log": "what the trial has spent",
    "settings": "only the onboarding keys below are reset",
    "pages_manifest": "the published archive's file list; not decided, so kept",
    "pages_deploy_receipt": "this D1 has published its Pages project; kept with the manifest",
    "schema_migrations": "the schema steps this D1 has run; cleared, the next boot reruns them",
}

# The keys static/onboarding.js writes: language and timezone, the publish hours, the
# Discord webhook and the mail pair, and vote similarity, which its votes step turns
# on with the embedder already stored (so provider, model and max_seeds stay).
ONBOARDING_KEYS = (
    "digest.output_language",
    "general.timezone",
    "general.digest_schedule",
    "notify.discord_webhook_url",
    "notify.email_to",
    "notify.email_from",
    "vote_similarity.enabled",
)

# workers/promote/src/index.js keys each pending vote under this prefix.
VOTE_PREFIX = "promote:"


class ResetError(Exception):
    pass


Runner = Callable[[list[str]], str]


def shipped_defaults(path: Path | None = None) -> dict[str, object]:
    """Each onboarding key's value in the shipped `cyris.toml.example`."""
    doc = tomllib.loads((path or ROOT / SHIPPED_SETTINGS).read_text(encoding="utf-8"))
    out: dict[str, object] = {}
    for key in ONBOARDING_KEYS:
        node: object = doc
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                raise ResetError(f"{SHIPPED_SETTINGS} sets no {key}")
            node = node[part]
        out[key] = node
    return out


def _quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def count_sql(tables: list[str]) -> str:
    """One row, one column per table. Scalar subqueries, never `UNION ALL`: D1 refuses a
    compound SELECT past five terms, and the list is sixteen."""
    return "SELECT " + ", ".join(f"(SELECT COUNT(*) FROM {t}) AS {t}" for t in tables)


def settings_sql() -> str:
    keys = ", ".join(_quote(k) for k in ONBOARDING_KEYS)
    return f"SELECT key, value FROM settings WHERE key IN ({keys}) ORDER BY key"


def clear_sql(present: list[str]) -> list[str]:
    """One DELETE per present table, in CLEARED's order.

    `areas.merged_into` points at another `areas` row, so it is emptied before the
    rows go; D1 enforces foreign keys.
    """
    statements = []
    for table in CLEARED:
        if table not in present:
            continue
        if table == "areas":
            statements.append("UPDATE areas SET merged_into = NULL")
        statements.append(f"DELETE FROM {table}")
    return statements


def reset_sql(present: list[str], defaults: dict[str, object], now: str) -> list[str]:
    """The statements the write sends, in order: the clear, then settings."""
    statements = clear_sql(present)
    for key in ONBOARDING_KEYS:
        statements.append(
            "INSERT INTO settings (key, value, updated_at) VALUES "
            f"({_quote(key)}, {_quote(json.dumps(defaults[key]))}, {_quote(now)}) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at"
        )
    return statements


def _json(stdout: str):
    """wrangler's --json answer, after any banner line it printed first."""
    for i, line in enumerate(stdout.splitlines()):
        if line.startswith(("[", "{")):
            return json.loads("\n".join(stdout.splitlines()[i:]))
    raise ResetError(f"wrangler printed no JSON: {stdout.strip()[:200]!r}")


class Trial:
    def __init__(self, slug: str, run: Runner) -> None:
        try:
            configs = provision_trial.load_rendered(slug)
        except (provision_trial.ProductionNameError, provision_trial.DriftError) as exc:
            raise ResetError(f"refused: {exc}") from None
        paths = provision_trial.rendered_paths(slug)
        state = provision_trial.wizard_state(slug)
        for rel, got, key in (
            (paths["rss"], configs["rss"]["d1_databases"][0]["database_id"], "TRIAL_D1_ID"),
            (paths["promote"], configs["promote"]["kv_namespaces"][0]["id"], "TRIAL_KV_ID"),
        ):
            if got != state.get(key):
                raise ResetError(
                    f"refused: {rel} names {got}, but .env.trial-{slug}-wizard has"
                    f" {key}={state.get(key) or '(none)'}: not provably this trial's"
                )
        self.rss_config = paths["rss"]
        self.promote_config = paths["promote"]
        self.d1 = configs["rss"]["d1_databases"][0]["database_name"]
        self.kv_binding = configs["promote"]["kv_namespaces"][0]["binding"]
        self._run = run

    def d1_query(self, sql: str, *, write: bool = False) -> list[dict]:
        flags = ["--yes"] if write else []
        cmd = wrangler(
            self.rss_config,
            "d1",
            "execute",
            self.d1,
            "--remote",
            "--json",
            *flags,
            "--command",
            sql,
        )
        return _json(self._run(cmd))

    def rows(self, sql: str) -> list[dict]:
        return self.d1_query(sql)[0]["results"]

    def votes(self) -> list[str]:
        cmd = wrangler(
            self.promote_config,
            "kv",
            "key",
            "list",
            "--binding",
            self.kv_binding,
            "--prefix",
            VOTE_PREFIX,
            "--remote",
        )
        return [k["name"] for k in _json(self._run(cmd))]

    def delete_votes(self, keys: list[str]) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            listing = Path(tmp) / "votes.json"
            listing.write_text(json.dumps(keys), encoding="utf-8")
            self._run(
                wrangler(
                    self.promote_config,
                    "kv",
                    "bulk",
                    "delete",
                    str(listing),
                    "--binding",
                    self.kv_binding,
                    "--remote",
                    "--force",
                )
            )


def survey(trial: Trial, say: Callable[[str], None]) -> tuple[list[str], list[str]]:
    """Print what the write would touch; return the present tables and the pending votes."""
    found = {r["name"] for r in trial.rows("SELECT name FROM sqlite_master WHERE type = 'table'")}
    present = [t for t in CLEARED if t in found]
    counts = trial.rows(count_sql(present))[0] if present else {}
    say(f"D1 {trial.d1} ({trial.rss_config}), rows each table holds now:")
    for table in CLEARED:
        shown = counts.get(table, "absent: nothing to clear")
        say(f"  {table:<24} {shown}")
    stored = {r["key"]: r["value"] for r in trial.rows(settings_sql())}
    defaults = shipped_defaults()
    say(f"settings, reset to {SHIPPED_SETTINGS} ({len(stored)} of these rows stored now):")
    for key in ONBOARDING_KEYS:
        say(f"  {key:<28} {stored.get(key, '(unset)')} -> {json.dumps(defaults[key])}")
    votes = trial.votes()
    say(f"KV {trial.kv_binding} ({trial.promote_config}): {len(votes)} pending votes")
    say("Kept: " + "; ".join(f"{t} ({why})" for t, why in KEPT.items()))
    say("Kept: the Workers, their secrets, the Access policy, the Pages project and its digests.")
    return present, votes


def _runner(env: dict[str, str]) -> Runner:
    def run(cmd: list[str]) -> str:
        done = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT, env=env)
        if done.returncode != 0:
            shown = " ".join(cmd[:4])
            raise ResetError(f"{shown} … failed ({done.returncode}): {done.stderr.strip()}")
        return done.stdout

    return run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog=__doc__.split("\n\n", 1)[1]
        + f"\nCleared tables: {', '.join(CLEARED)}.\n"
        + f"Reset settings keys: {', '.join(ONBOARDING_KEYS)}.\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--slug", required=True, help="the trial's slug, e.g. trial1")
    parser.add_argument("--yes", action="store_true", help="write; without it, a dry run")
    return parser


def main(
    argv: list[str] | None = None,
    run: Runner | None = None,
    say: Callable[[str], None] = print,
    now: Callable[[], str] = lambda: datetime.now(UTC).isoformat(timespec="seconds"),
) -> int:
    args = build_parser().parse_args(argv)
    try:
        trial = Trial(args.slug, run or _runner(wrangler_env(args.slug)))
        present, votes = survey(trial, say)
        statements = reset_sql(present, shipped_defaults(), now())
        say("SQL the write sends, in one call:")
        for statement in statements:
            say(f"  {statement};")
        if votes:
            say(f"and a KV bulk delete of the {len(votes)} pending votes.")
        if not args.yes:
            say("Dry run: nothing was written. Run again with --yes to write.")
            return 0
        trial.d1_query("; ".join(statements), write=True)
        if votes:
            trial.delete_votes(votes)
        left = trial.rows(count_sql(present))[0] if present else {}
        still = {t: n for t, n in left.items() if n}
        listed = trial.votes()
        # WHY a warning, not a failure: both writes succeeded, and a recount can still see
        # rows. KV listing is eventually consistent, so a deleted vote can list for a while,
        # and an rss poll already running can buffer articles between delete and recount.
        if still or listed:
            print(
                f"warning: after the reset, {still or 'no table'} still holds rows and"
                f" {len(listed)} votes still list in KV. Run the dry run again in a minute:"
                " what stays then is real.",
                file=sys.stderr,
            )
        say(f"Reset {args.slug}: it now waits for onboarding.")
        return 0
    except ResetError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
