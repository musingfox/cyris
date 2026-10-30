"""scripts/reset_trial.py: what it refuses, what a dry run leaves, what the write sends.

wrangler is a fake that runs each D1 command against an in-memory SQLite built from
the real schema (foreign keys on and compound SELECTs capped, as D1 has them) and
answers KV commands from a dict, so the SQL the script sends is the SQL that is checked.
"""

import json
import re
import sqlite3
import tomllib
from pathlib import Path

import pytest
from fakes import CompoundSelectLimitedD1
from trial_tree import ROOT, SLUG, load, make_tree

pytestmark = pytest.mark.unit

RSS_CONFIG = f"workers/rss/wrangler.trial-{SLUG}.toml"
PROMOTE_CONFIG = f"workers/promote/wrangler.trial-{SLUG}.toml"
SCHEMA = ROOT / "src/cyris/adapters/store/schema.sql"
RSS_WORKER = ROOT / "workers/rss/src/index.js"
LLM = {"llm_provider.provider": "gemini", "llm_provider.model": "gemini-2.5-flash"}
READER = {
    "digest.output_language": "ja",
    "general.timezone": "Europe/Berlin",
    "general.digest_schedule": ["07:00"],
    "notify.discord_webhook_url": "https://discord.com/api/webhooks/1/x",
    "notify.email_to": "t@example.com",
    "notify.email_from": "cyris@example.com",
    "vote_similarity.enabled": True,
}
# Columns whose CHECK refuses the filler value.
FILL = {("areas", "status"): "active", ("topic_tags", "kind"): "entity"}


def _rss_ddl() -> list[str]:
    return re.findall(r"`(CREATE [^`]+)`", RSS_WORKER.read_text(encoding="utf-8"))


class FakeWrangler:
    def __init__(self) -> None:
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA.read_text(encoding="utf-8"))
        for statement in _rss_ddl():
            self.db.execute(statement)
        self.kv = {"promote:aa": "{}", "promote:bb": "{}", "other:cc": "{}"}
        self.calls: list[list[str]] = []
        self.seed()

    def seed(self) -> None:
        tables = [
            r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        ]
        for table in tables:
            if table.startswith("sqlite_") or table == "settings":
                continue
            cols = [
                c
                for c in self.db.execute(f"PRAGMA table_info({table})")
                if c["notnull"] and c["dflt_value"] is None and not c["pk"] or c["pk"]
            ]
            names = [c["name"] for c in cols if not (c["pk"] and c["type"] == "INTEGER")]
            values = [
                FILL.get((table, c["name"]), 1 if c["type"] in ("INTEGER", "REAL") else "x")
                for c in cols
                if c["name"] in names
            ]
            marks = ", ".join("?" * len(names))
            self.db.execute(f"INSERT INTO {table} ({', '.join(names)}) VALUES ({marks})", values)
        self.db.execute("INSERT INTO areas (name, status, merged_into) VALUES ('b', 'merged', 1)")
        for key, value in (LLM | READER).items():
            self.db.execute(
                "INSERT INTO settings VALUES (?, ?, '2026-10-01T00:00:00+00:00')",
                [key, json.dumps(value)],
            )

    def __call__(self, cmd: list[str]) -> str:
        self.calls.append(cmd)
        if cmd[2:4] == ["d1", "execute"]:
            sql = cmd[cmd.index("--command") + 1]
            CompoundSelectLimitedD1.refuse_compound(sql)
            if "--yes" in cmd:
                with self.db:
                    self.db.executescript(sql)
                return json.dumps([{"results": [], "success": True}])
            rows = [dict(r) for r in self.db.execute(sql)]
            return "banner line\n" + json.dumps([{"results": rows, "success": True}])
        if cmd[2:5] == ["kv", "key", "list"]:
            prefix = cmd[cmd.index("--prefix") + 1]
            return json.dumps([{"name": k} for k in self.kv if k.startswith(prefix)])
        if cmd[2:5] == ["kv", "bulk", "delete"]:
            for key in json.loads(Path(cmd[5]).read_text(encoding="utf-8")):
                del self.kv[key]
            return ""
        raise AssertionError(f"unexpected command {cmd}")

    def count(self, table: str) -> int:
        return self.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def settings(self) -> dict:
        return {r["key"]: json.loads(r["value"]) for r in self.db.execute("SELECT * FROM settings")}

    def writes(self) -> list[list[str]]:
        return [c for c in self.calls if "--yes" in c or c[2:5] == ["kv", "bulk", "delete"]]


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    return make_tree(tmp_path, "reset_trial")


@pytest.fixture
def reset(tree: Path, monkeypatch):
    return load(tree, "reset_trial", monkeypatch)


def _main(reset, wrangler: FakeWrangler, *args: str) -> tuple[int, list[str]]:
    out: list[str] = []
    code = reset.main(
        ["--slug", SLUG, *args],
        run=wrangler,
        say=out.append,
        now=lambda: "2026-10-21T00:00:00+00:00",
    )
    return code, out


def _shipped(key: str):
    node = tomllib.loads((ROOT / "cyris.toml.example").read_text(encoding="utf-8"))
    for part in key.split("."):
        node = node[part]
    return node


# --- refusals


@pytest.mark.parametrize("slug", ["Trial1", "t", "cyris-rss", "cyris-app"])
def test_a_slug_outside_the_naming_rule_is_refused_before_any_call(reset, slug, capsys) -> None:
    wrangler = FakeWrangler()

    assert reset.main(["--slug", slug, "--yes"], run=wrangler, say=print) == 1
    assert "refused" in capsys.readouterr().err
    assert wrangler.calls == []


def test_a_slug_never_rendered_is_refused(reset, capsys) -> None:
    wrangler = FakeWrangler()

    assert reset.main(["--slug", "bob", "--yes"], run=wrangler, say=print) == 1
    assert "not found" in capsys.readouterr().err
    assert wrangler.calls == []


@pytest.mark.parametrize(
    "rel,old,new",
    [
        (RSS_CONFIG, 'database_name = "cyris-app-alice"', 'database_name = "cyris-rss"'),
        (RSS_CONFIG, 'name = "cyris-rss-alice"', 'name = "cyris-rss"'),
        (PROMOTE_CONFIG, 'name = "cyris-promote-alice"', 'name = "cyris-promote"'),
    ],
)
def test_a_config_naming_a_production_resource_is_refused(
    reset, tree: Path, rel: str, old: str, new: str, capsys
) -> None:
    path = tree / rel
    text = path.read_text(encoding="utf-8")
    assert text.count(old) == 1
    path.write_text(text.replace(old, new), encoding="utf-8")
    wrangler = FakeWrangler()

    assert reset.main(["--slug", SLUG, "--yes"], run=wrangler, say=print) == 1
    assert "production name" in capsys.readouterr().err
    assert wrangler.calls == []


def test_the_production_database_id_is_refused(reset, tree: Path, capsys) -> None:
    prod_id = tomllib.loads((ROOT / "workers/rss/wrangler.toml").read_text())["d1_databases"][0][
        "database_id"
    ]
    path = tree / RSS_CONFIG
    text = path.read_text(encoding="utf-8")
    path.write_text(re.sub(r'database_id = "[^"]+"', f'database_id = "{prod_id}"', text))
    wrangler = FakeWrangler()

    assert reset.main(["--slug", SLUG, "--yes"], run=wrangler, say=print) == 1
    assert "production's" in capsys.readouterr().err
    assert wrangler.calls == []


@pytest.mark.parametrize(
    "key,value",
    [
        ("TRIAL_D1_ID", "99999999-2222-4333-8444-555555555555"),
        ("TRIAL_KV_ID", "0" * 32),
        ("TRIAL_D1_ID", ""),
    ],
)
def test_ids_that_differ_from_the_wizard_state_are_refused(
    reset, tree: Path, key: str, value: str, capsys
) -> None:
    state = tree / f".env.trial-{SLUG}-wizard"
    lines = [ln for ln in state.read_text().splitlines() if not ln.startswith(f"{key}=")]
    state.write_text("\n".join([*lines, *([f"{key}={value}"] if value else [])]) + "\n")
    wrangler = FakeWrangler()

    assert reset.main(["--slug", SLUG, "--yes"], run=wrangler, say=print) == 1
    assert f"{key}=" in capsys.readouterr().err
    assert wrangler.calls == []


# --- dry run


def test_without_yes_nothing_is_written(reset) -> None:
    wrangler = FakeWrangler()
    before = {t: wrangler.count(t) for t in reset.CLEARED}

    code, out = _main(reset, wrangler)

    assert code == 0
    assert wrangler.writes() == []
    assert {t: wrangler.count(t) for t in reset.CLEARED} == before
    assert wrangler.settings() == LLM | READER
    assert set(wrangler.kv) == {"promote:aa", "promote:bb", "other:cc"}
    assert out[-1].startswith("Dry run: nothing was written")


def test_the_dry_run_prints_every_table_count_before_anything(reset) -> None:
    wrangler = FakeWrangler()
    wrangler.db.execute("INSERT INTO sources (name) VALUES ('second')")

    _, out = _main(reset, wrangler)

    n = len(reset.CLEARED)
    counts = dict(re.fullmatch(r"  (\w+)\s+(\d+)", line).groups() for line in out[1 : 1 + n])
    assert counts == {t: "2" if t in ("sources", "areas") else "1" for t in reset.CLEARED}
    assert "KV PROMOTIONS" in out[n + 9] and "2 pending votes" in out[n + 9]


def test_a_table_the_database_lacks_is_reported_and_not_sent(reset) -> None:
    wrangler = FakeWrangler()
    wrangler.db.execute("DROP TABLE blind_labels")

    code, out = _main(reset, wrangler, "--yes")

    assert code == 0
    assert any(re.fullmatch(r"  blind_labels\s+absent: nothing to clear", line) for line in out)
    write = wrangler.writes()[0]
    assert "blind_labels" not in write[write.index("--command") + 1]


# --- the write


def test_yes_clears_reader_content_and_keeps_the_rest(reset) -> None:
    wrangler = FakeWrangler()

    code, out = _main(reset, wrangler, "--yes")

    assert code == 0, out
    assert {t: wrangler.count(t) for t in reset.CLEARED} == dict.fromkeys(reset.CLEARED, 0)
    assert {t: wrangler.count(t) for t in reset.KEPT if t != "settings"} == {
        "usage_log": 1,
        "pages_manifest": 1,
        "pages_deploy_receipt": 1,
        "schema_migrations": 1,
    }
    assert set(wrangler.kv) == {"other:cc"}
    assert out[-1] == f"Reset {SLUG}: it now waits for onboarding."


def test_yes_resets_onboarding_keys_to_the_shipped_file_and_keeps_llm(reset) -> None:
    wrangler = FakeWrangler()

    _main(reset, wrangler, "--yes")

    assert wrangler.settings() == LLM | {k: _shipped(k) for k in READER}


def test_every_wrangler_call_names_the_trial_config_and_no_env_file(reset) -> None:
    wrangler = FakeWrangler()

    _main(reset, wrangler, "--yes")

    assert wrangler.calls
    for cmd in wrangler.calls:
        assert cmd[:2] == ["bunx", "wrangler"]
        config = cmd[cmd.index("--config") + 1]
        assert config == (PROMOTE_CONFIG if cmd[2] == "kv" else RSS_CONFIG)
        assert cmd[cmd.index("--env-file") + 1] == "/dev/null"
        assert "--remote" in cmd
    d1 = {c[c.index("execute") + 1] for c in wrangler.calls if c[2] == "d1"}
    assert d1 == {"cyris-app-alice"}


def test_the_write_is_one_d1_call_then_one_kv_delete(reset) -> None:
    wrangler = FakeWrangler()

    _main(reset, wrangler, "--yes")

    d1_write, kv_delete = wrangler.writes()
    statements = d1_write[d1_write.index("--command") + 1].split("; ")
    deletes = [s.removeprefix("DELETE FROM ") for s in statements if s.startswith("DELETE")]
    assert deletes == list(reset.CLEARED)
    assert statements.index("UPDATE areas SET merged_into = NULL") < statements.index(
        "DELETE FROM areas"
    )
    upserts = [s for s in statements if s.startswith("INSERT INTO settings")]
    assert [re.search(r"VALUES \('([^']+)'", s).group(1) for s in upserts] == list(
        reset.ONBOARDING_KEYS
    )
    assert len(statements) == len(deletes) + 1 + len(upserts)
    assert kv_delete[2:5] == ["kv", "bulk", "delete"] and "--force" in kv_delete


def test_no_pending_vote_means_no_kv_delete(reset) -> None:
    wrangler = FakeWrangler()
    wrangler.kv = {}

    _main(reset, wrangler, "--yes")

    assert [c[2] for c in wrangler.writes()] == ["d1"]


class LaggingWrangler(FakeWrangler):
    """KV still lists the deleted votes, and a poll buffers an article after the delete."""

    def __call__(self, cmd: list[str]) -> str:
        if cmd[2:5] == ["kv", "bulk", "delete"]:
            self.calls.append(cmd)
            return ""
        out = super().__call__(cmd)
        if "--yes" in cmd:
            self.db.execute(
                "INSERT INTO articles (url, published_at, source_name, fetched_at)"
                " VALUES ('late', 'x', 'x', 'x')"
            )
        return out


def test_what_a_recount_still_sees_is_a_warning_not_a_failure(reset, capsys) -> None:
    code, out = _main(reset, LaggingWrangler(), "--yes")

    err = capsys.readouterr().err
    assert code == 0
    assert "warning:" in err and "'articles': 1" in err and "2 votes still list" in err
    assert out[-1] == f"Reset {SLUG}: it now waits for onboarding."


def test_an_exported_token_never_reaches_wrangler(reset, tree: Path) -> None:
    env = reset.provision_trial.wrangler_env(
        SLUG, {"CLOUDFLARE_API_TOKEN": "prod-SECRET", "PATH": "/bin"}
    )

    assert "CLOUDFLARE_API_TOKEN" not in env
    assert env["CLOUDFLARE_ACCOUNT_ID"] == "0123456789abcdef0123456789abcdef"


# --- the lists against the repo


def _schema_tables() -> set[str]:
    sql = SCHEMA.read_text(encoding="utf-8") + "\n".join(_rss_ddl())
    return set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", sql))


def test_every_table_the_schema_creates_is_cleared_or_kept(reset) -> None:
    assert not set(reset.CLEARED) & set(reset.KEPT)
    assert set(reset.CLEARED) | set(reset.KEPT) == _schema_tables()


def test_onboarding_keys_are_registry_keys_with_a_shipped_value(reset) -> None:
    registry = json.loads((ROOT / "src/cyris/settings_fields.json").read_text(encoding="utf-8"))

    assert set(reset.ONBOARDING_KEYS) <= set(registry)
    assert reset.shipped_defaults() == {k: _shipped(k) for k in reset.ONBOARDING_KEYS}


def test_every_key_the_onboarding_page_names_as_written_is_reset(reset) -> None:
    js = (ROOT / "src/cyris/entrypoints/static/onboarding.js").read_text(encoding="utf-8")
    plain = re.search(r"const PLAIN = \{(.*?)\};", js, re.S).group(1)
    mail = re.search(r"const MAIL = \{(.*?)\};", js, re.S).group(1)
    named = set(re.findall(r':\s*"([a-z_]+\.[a-z_]+)"', plain + mail))

    assert named
    assert named <= set(reset.ONBOARDING_KEYS)


def test_the_vote_prefix_is_the_promote_workers(reset) -> None:
    worker = (ROOT / "workers/promote/src/index.js").read_text(encoding="utf-8")

    assert f'prefix: "{reset.VOTE_PREFIX}"' in worker
