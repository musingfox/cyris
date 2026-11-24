"""scripts/seed_trial.py: what save exports, what restore refuses, leaves and writes.

Production and the trial are each a FakeWrangler from tests/test_reset_trial.py: an
in-memory SQLite built from the real schema. Here it also answers `d1 export` with a
dump in wrangler's shape, and `d1 execute --file` as one transaction, as D1 imports.
"""

import json
import re
import sqlite3
from pathlib import Path

import pytest
from test_reset_trial import LLM, PROMOTE_CONFIG, READER, RSS_CONFIG, FakeWrangler
from trial_tree import SLUG, load, make_tree

pytestmark = pytest.mark.unit

SHA = "dff17391d7fb9a7c28c18e04e5bdf503ed563b3c"


# D1's limit, restated: https://developers.cloudflare.com/d1/platform/limits/
STATEMENT_LIMIT = 100_000


def _statements(sql: str) -> list[str]:
    out, buf = [], ""
    for line in sql.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            out.append(buf.strip())
            buf = ""
    return out


class SeedWrangler(FakeWrangler):
    def __init__(self) -> None:
        super().__init__()
        self.db.execute("UPDATE digest_runs SET build_sha = ?, dry_run = 0", [SHA])
        self.db.execute("INSERT INTO sources (name, url) VALUES ('it''s', 'https://e.x/a\nb')")

    def __call__(self, cmd: list[str]) -> str:
        if cmd[2:4] == ["d1", "export"]:
            self.calls.append(cmd)
            tables = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "--table"]
            lines = ["PRAGMA defer_foreign_keys=TRUE;"]
            for table in tables:
                cols = [c["name"] for c in self.db.execute(f"PRAGMA table_info({table})")]
                quoted = ",".join(f'"{c}"' for c in cols)
                values = " || ',' || ".join(f"quote({c})" for c in cols)
                for (row,) in self.db.execute(f"SELECT {values} FROM {table}"):
                    lines.append(f'INSERT INTO "{table}" ({quoted}) VALUES({row});')
            Path(cmd[cmd.index("--output") + 1]).write_text("\n".join(lines) + "\n")
            return "a signed download URL\n"
        if cmd[2:4] == ["d1", "execute"] and "--file" in cmd:
            self.calls.append(cmd)
            sql = Path(cmd[cmd.index("--file") + 1]).read_text(encoding="utf-8")
            for statement in _statements(sql):
                if len(statement.encode()) > STATEMENT_LIMIT:
                    raise RuntimeError("statement too long: SQLITE_TOOBIG")
            self.db.executescript(f"BEGIN;\n{sql}\nCOMMIT;")
            return "Executed\n"
        return super().__call__(cmd)

    def rows_of(self, table: str) -> list[tuple]:
        return [tuple(r) for r in self.db.execute(f"SELECT * FROM {table} ORDER BY rowid")]


@pytest.fixture
def seed(tmp_path: Path, monkeypatch):
    tree = make_tree(tmp_path, "reset_trial", "seed_trial")
    load(tree, "reset_trial", monkeypatch)
    return load(tree, "seed_trial", monkeypatch)


@pytest.fixture
def seeds(tmp_path: Path) -> Path:
    return tmp_path / "seeds"


def _main(seed, wrangler, seeds: Path, *args: str) -> tuple[int, list[str]]:
    out: list[str] = []
    return seed.main(list(args), run=wrangler, say=out.append, seeds=seeds), out


def _saved(seed, seeds: Path) -> SeedWrangler:
    production = SeedWrangler()
    code, out = _main(seed, production, seeds, "save", "--name", "base")
    assert code == 0, out
    return production


# --- save


def test_save_exports_only_the_cleared_tables_from_production(seed, seeds: Path) -> None:
    production = _saved(seed, seeds)

    (export,) = [c for c in production.calls if c[2:4] == ["d1", "export"]]
    assert [export[i + 1] for i, a in enumerate(export) if a == "--table"] == list(seed.CLEARED)
    assert "--no-schema" in export and "--remote" in export
    assert export[export.index("--config") + 1] == "workers/rss/wrangler.toml"
    assert export[export.index("--env-file") + 1] == "/dev/null"
    assert export[4] == "cyris-rss"


def test_save_skips_a_cleared_table_production_lacks(seed, seeds: Path) -> None:
    production = SeedWrangler()
    production.db.execute("DROP TABLE suppressions")

    code, out = _main(seed, production, seeds, "save", "--name", "base")

    assert code == 0, out
    (export,) = [c for c in production.calls if c[2:4] == ["d1", "export"]]
    exported = [export[i + 1] for i, a in enumerate(export) if a == "--table"]
    assert exported == [t for t in seed.CLEARED if t != "suppressions"]
    assert re.fullmatch(r"  suppressions\s+absent", out[1 + seed.CLEARED.index("suppressions")])


def test_save_names_the_seed_after_productions_commit(seed, seeds: Path) -> None:
    _saved(seed, seeds)

    assert [p.name for p in seeds.iterdir()] == [f"base-{SHA[:7]}.sql"]


def test_save_never_writes_to_production(seed, seeds: Path) -> None:
    production = _saved(seed, seeds)

    assert production.writes() == []


def test_save_refuses_a_name_already_saved(seed, seeds: Path, capsys) -> None:
    _saved(seed, seeds)
    production = SeedWrangler()

    code, _ = _main(seed, production, seeds, "save", "--name", "base")

    assert code == 1
    assert "already" in capsys.readouterr().err
    assert production.calls == []


def test_save_refuses_without_a_commit_to_name_it_after(seed, seeds: Path, capsys) -> None:
    production = SeedWrangler()
    production.db.execute("DELETE FROM digest_runs")

    code, _ = _main(seed, production, seeds, "save", "--name", "base")

    assert code == 1
    assert "no run: no commit" in capsys.readouterr().err
    assert not seeds.exists()


@pytest.mark.parametrize("name", ["Base", "../x", "", "x_y"])
def test_a_name_outside_the_rule_is_refused_before_any_call(seed, seeds: Path, name) -> None:
    production = SeedWrangler()

    assert _main(seed, production, seeds, "save", "--name", name)[0] == 1
    assert production.calls == []


# --- restore: refusals


def test_restore_refuses_a_seed_that_writes_another_table(seed, seeds: Path, capsys) -> None:
    _saved(seed, seeds)
    path = next(seeds.iterdir())
    path.write_text(
        path.read_text() + 'INSERT INTO "settings" ("key","value","updated_at") VALUES(1,2,3);\n'
    )
    trial = SeedWrangler()

    code, _ = _main(seed, trial, seeds, "restore", "--slug", SLUG, "--name", "base", "--yes")

    assert code == 1
    assert "settings" in capsys.readouterr().err
    assert trial.calls == []


def test_restore_refuses_a_name_with_no_seed(seed, seeds: Path, capsys) -> None:
    seeds.mkdir()
    trial = SeedWrangler()

    code, _ = _main(seed, trial, seeds, "restore", "--slug", SLUG, "--name", "base")

    assert code == 1
    assert "0 seeds named base" in capsys.readouterr().err
    assert trial.calls == []


def test_restore_refuses_a_slug_never_rendered(seed, seeds: Path, capsys) -> None:
    _saved(seed, seeds)
    trial = SeedWrangler()

    code, _ = _main(seed, trial, seeds, "restore", "--slug", "bob", "--name", "base", "--yes")

    assert code == 1
    assert "not found" in capsys.readouterr().err
    assert trial.calls == []


# --- restore: dry run


def test_the_dry_run_writes_nothing_and_prints_rows_now_and_in_the_seed(seed, seeds: Path) -> None:
    _saved(seed, seeds)
    trial = SeedWrangler()
    trial.db.execute("DELETE FROM sources WHERE name = 'it''s'")
    before = {t: trial.count(t) for t in seed.CLEARED}

    code, out = _main(seed, trial, seeds, "restore", "--slug", SLUG, "--name", "base")

    assert code == 0
    assert trial.writes() == [] and [c for c in trial.calls if "--file" in c] == []
    assert {t: trial.count(t) for t in seed.CLEARED} == before
    n = len(seed.CLEARED)
    shown = dict(re.fullmatch(r"  (\w+)\s+(\S+ -> \d+)", line).groups() for line in out[1 : 1 + n])
    assert shown["sources"] == "1 -> 2"
    assert out[-1].startswith("Dry run: nothing was written")


# --- restore: the write


def test_yes_makes_the_trial_hold_the_seeds_rows(seed, seeds: Path) -> None:
    production = _saved(seed, seeds)
    trial = SeedWrangler()
    trial.db.execute("INSERT INTO sources (name) VALUES ('trial only')")

    code, out = _main(seed, trial, seeds, "restore", "--slug", SLUG, "--name", "base", "--yes")

    assert code == 0, out
    for table in seed.CLEARED:
        assert trial.rows_of(table) == production.rows_of(table), table
    assert out[-1] == f"Restored {SLUG} to base-{SHA[:7]}.sql."


def test_yes_keeps_settings_usage_and_pages_rows(seed, seeds: Path) -> None:
    _saved(seed, seeds)
    trial = SeedWrangler()
    trial.db.execute(
        "UPDATE settings SET value = ? WHERE key = 'notify.discord_webhook_url'",
        [json.dumps("https://discord.com/api/webhooks/2/trial")],
    )
    kept = {
        t: trial.rows_of(t)
        for t in ("settings", "usage_log", "pages_manifest", "pages_deploy_receipt")
    }

    _main(seed, trial, seeds, "restore", "--slug", SLUG, "--name", "base", "--yes")

    assert {t: trial.rows_of(t) for t in kept} == kept
    assert trial.settings()["notify.discord_webhook_url"].endswith("/2/trial")
    assert set(trial.settings()) == set(LLM | READER)


def test_yes_is_one_import_then_one_kv_delete(seed, seeds: Path) -> None:
    _saved(seed, seeds)
    trial = SeedWrangler()

    _main(seed, trial, seeds, "restore", "--slug", SLUG, "--name", "base", "--yes")

    imports = [c for c in trial.calls if "--file" in c]
    assert len(imports) == 1 and "--remote" in imports[0]
    assert imports[0][imports[0].index("--config") + 1] == RSS_CONFIG
    assert imports[0][4] == "cyris-app-alice"
    (kv_delete,) = [c for c in trial.writes() if c[2] == "kv"]
    assert kv_delete[kv_delete.index("--config") + 1] == PROMOTE_CONFIG
    assert set(trial.kv) == {"other:cc"}


def test_a_name_that_prefixes_another_finds_only_its_own_seed(seed, seeds: Path) -> None:
    _saved(seed, seeds)
    production = SeedWrangler()
    assert _main(seed, production, seeds, "save", "--name", "base-2")[0] == 0

    assert seed.find_seed(seeds, "base").name == f"base-{SHA[:7]}.sql"


# --- rows past D1's statement limit


def test_a_row_past_the_statement_limit_is_restored_whole(seed, seeds: Path) -> None:
    production = SeedWrangler()
    long = "it's a line\n" * 30_000
    production.db.execute("UPDATE stored_articles SET content = ?", [long])
    assert _main(seed, production, seeds, "save", "--name", "base")[0] == 0
    trial = SeedWrangler()

    code, out = _main(seed, trial, seeds, "restore", "--slug", SLUG, "--name", "base", "--yes")

    assert code == 0, out
    assert trial.rows_of("stored_articles") == production.rows_of("stored_articles")
    assert "1 rows run past" in "\n".join(out)


def test_a_replace_expression_is_cut_outside_its_marker(seed) -> None:
    # The dump writes a newline as a marker its replace() turns back, here `\\012`. With
    # CHUNK - 2 bytes before it, a cut by size alone lands between its `0` and `1`.
    text = "x" * (seed.CHUNK - 2) + "\n" + "y" * seed.CHUNK
    value = "replace(" + seed.reset_trial._quote(text.replace("\n", "\\012")) + ",'\\012',char(10))"
    dump = f'INSERT INTO "stored_articles" ("url","content") VALUES(\'u\',{value});\n'
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE stored_articles (url TEXT PRIMARY KEY, content TEXT)")

    fitted, split = seed.fit_statements(dump, lambda _table: ["url"])

    assert split == 1
    assert all(len(s.encode()) <= STATEMENT_LIMIT for s in _statements(fitted))
    db.executescript(fitted)
    assert db.execute("SELECT content FROM stored_articles").fetchone()[0] == text
