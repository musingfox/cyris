"""`cyris doctor` — the checks, and what each verdict tells the reader to do."""

from pathlib import Path
from unittest.mock import patch

import pytest

from cyris.config import AppConfig, Config, LLMProviderConfig
from cyris.diagnostics import doctor
from cyris.domain.models import SourceConfig, Tier


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any D1 a check reaches for is local sqlite, never the real API.

    Without this the backend="d1" cases hit api.cloudflare.com with a bogus
    token and pay the client's full retry backoff for it.

    Patch the name, never `D1Client.__new__`: the class does not define one, so
    monkeypatch's undo writes `object.__new__` onto it as a real attribute, and
    from then on constructing a `D1Client` with keywords raises "object.__new__()
    takes exactly one argument" — in whichever unrelated test happens to run next.
    """
    from fakes import SqliteD1

    db = SqliteD1()
    monkeypatch.setattr("cyris.adapters.store.d1.D1Client", lambda **_kw: db)
    return db


def _config(tmp_path: Path, **app_kwargs) -> Config:
    app = AppConfig(**app_kwargs)
    app.agent_vault.path = tmp_path / "agent-vault"
    # A setup with neither sink is its own failure (`digest output`), and every
    # test here is about something else.
    app.html_output.enabled = True
    return Config(
        app=app,
        sources={"Feed": SourceConfig(name="Feed", url="https://a.test/feed", tier=Tier.FILTER)},
    )


def _by_name(checks: list[doctor.Check], name: str) -> doctor.Check:
    return next(c for c in checks if c.name.startswith(name))


async def test_a_clean_local_setup_has_no_failures(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")
    cfg = _config(tmp_path, llm_provider=LLMProviderConfig(provider="anthropic"))

    checks = await doctor.run_checks(cfg)

    assert [c for c in checks if c.status == "fail"] == []


async def test_the_vault_check_is_gone_on_d1_and_creates_no_directory(tmp_path: Path) -> None:
    """The probe used to mkdir the very directory it asked about."""
    cfg = _config(tmp_path)
    cfg.app.store.backend = "d1"
    cfg.app.agent_vault.path = tmp_path / "never-created"

    checks = await doctor.run_checks(cfg)

    assert [c for c in checks if c.name == "agent vault"] == []
    assert not (tmp_path / "never-created").exists()


async def test_a_d1_store_with_unpushed_sources_warns(tmp_path: Path) -> None:
    """The Worker would be polling its bundled snapshot; that is not visible anywhere else."""
    cfg = _config(tmp_path)
    cfg.app.store.backend = "d1"
    cfg.sources_origin = "sources.yaml"

    check = _by_name(await doctor.run_checks(cfg), "sources")

    assert check.status == "warn"
    assert "cyris sources push" in check.fix


async def test_the_sources_check_names_where_they_came_from(tmp_path: Path) -> None:
    check = _by_name(await doctor.run_checks(_config(tmp_path)), "sources")

    assert "from sources.yaml" in check.detail


async def test_no_sources_is_a_failure(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    cfg.sources = {}

    assert _by_name(await doctor.run_checks(cfg), "sources").status == "fail"


async def test_a_provider_without_its_key_names_the_variable(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    cfg = _config(tmp_path, llm_provider=LLMProviderConfig(provider="gemini"))

    check = _by_name(await doctor.run_checks(cfg), "llm provider")

    assert check.status == "fail"
    assert "GEMINI_API_KEY" in check.fix


async def test_workers_ai_without_an_account_id_fails(tmp_path: Path, monkeypatch) -> None:
    """A token alone is not enough: the Workers AI REST path is per-account."""
    monkeypatch.setenv("CLOUDFLARE_AI_TOKEN", "ai-token")
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    cfg = _config(tmp_path, llm_provider=LLMProviderConfig(provider="workers_ai"))

    check = _by_name(await doctor.run_checks(cfg), "llm provider")

    assert check.status == "fail"
    assert "CLOUDFLARE_ACCOUNT_ID" in check.fix


async def test_no_provider_is_a_warning_not_a_failure(tmp_path: Path) -> None:
    """Degraded mode is a choice; it must not read as a broken deployment."""
    check = _by_name(await doctor.run_checks(_config(tmp_path)), "llm provider")

    assert check.status == "warn"


async def test_an_unwired_rss_buffer_warns_with_the_measured_cost(tmp_path: Path) -> None:
    check = _by_name(await doctor.run_checks(_config(tmp_path)), "rss buffer")

    assert check.status == "warn"
    assert "95 of the 179" in check.fix


async def test_a_working_account_token_is_not_called_invalid(tmp_path: Path, monkeypatch) -> None:
    """The bug this replaced: /user/tokens/verify rejects account-owned tokens,
    so doctor called a token invalid three lines under a check that had just
    used it successfully."""
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "account-owned")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct")
    monkeypatch.setattr(
        "cyris.adapters.cloudflare.check_pages_access",
        lambda *_a: (True, "can publish to cyris-digest"),
    )
    cfg = _config(tmp_path)
    cfg.app.promote.publish_enabled = True
    cfg.app.promote.pages_project = "cyris-digest"

    check = _by_name(await doctor.run_checks(cfg), "publishing")

    assert check.status == "ok"


async def test_a_token_without_pages_permission_says_so(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "d1-only")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct")
    monkeypatch.setattr(
        "cyris.adapters.cloudflare.check_pages_access",
        lambda *_a: (False, "Authentication error"),
    )
    cfg = _config(tmp_path)
    cfg.app.promote.publish_enabled = True
    cfg.app.promote.pages_project = "cyris-digest"

    check = _by_name(await doctor.run_checks(cfg), "publishing")

    assert check.status == "fail"
    assert "Pages permission" in check.fix


async def test_a_missing_publish_token_names_its_variable(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    cfg = _config(tmp_path)
    cfg.app.promote.publish_enabled = True

    check = _by_name(await doctor.run_checks(cfg), "publishing")

    assert check.status == "fail"
    assert "CLOUDFLARE_API_TOKEN" in check.fix


async def test_publishing_disabled_is_a_skip(tmp_path: Path) -> None:
    check = _by_name(await doctor.run_checks(_config(tmp_path)), "publishing")

    assert check.status == "skip"


async def test_an_unreachable_store_fails_rather_than_reading_as_empty(
    tmp_path: Path, monkeypatch
) -> None:
    from cyris.adapters.store.d1 import D1Error

    def explode(_cfg):
        raise D1Error("no such table: stored_articles")

    monkeypatch.setattr("cyris.bootstrap.build_store", explode)
    cfg = _config(tmp_path)
    cfg.app.store.backend = "d1"

    check = _by_name(await doctor.run_checks(cfg), "article store")

    assert check.status == "fail"
    assert "no such table" in check.detail


def test_the_command_renders_every_status_and_exits_nonzero_on_failure(monkeypatch) -> None:
    """The report renders by marker lookup, so an unmapped status would raise."""
    from typer.testing import CliRunner

    from cyris.entrypoints.cli import app

    async def fake_checks(_cfg, _path=None, _deployment=""):
        return [
            doctor.Check("fine", "ok", "all good"),
            doctor.Check("partial", "warn", "degraded", "do this"),
            doctor.Check("absent", "skip", "not configured"),
            doctor.Check("broken", "fail", "it is broken", "fix it like this"),
        ]

    monkeypatch.setattr("cyris.diagnostics.doctor.run_checks", fake_checks)
    monkeypatch.setattr("cyris.bootstrap.load_effective_config", lambda *a, **k: None)

    result = CliRunner().invoke(app, ["doctor"])

    assert result.exit_code == 1
    assert "✓ fine" in result.stdout
    assert "! partial" in result.stdout
    assert "– absent" in result.stdout
    assert "✗ broken" in result.stdout
    assert "fix it like this" in result.stdout
    assert "do this" in result.stdout
    # A passing check's hint would just be noise.
    assert "1 problem(s)" in result.stdout


def test_the_command_exits_zero_when_nothing_is_broken(monkeypatch) -> None:
    from typer.testing import CliRunner

    from cyris.entrypoints.cli import app

    async def fake_checks(_cfg, _path=None, _deployment=""):
        return [doctor.Check("fine", "ok", "all good")]

    monkeypatch.setattr("cyris.diagnostics.doctor.run_checks", fake_checks)
    monkeypatch.setattr("cyris.bootstrap.load_effective_config", lambda *a, **k: None)

    result = CliRunner().invoke(app, ["doctor"])

    assert result.exit_code == 0
    assert "Ready to run." in result.stdout


def test_the_command_does_not_claim_a_config_file_it_never_read(monkeypatch, tmp_path) -> None:
    """No green `config —` banner: the `config file` check is what reports this."""
    from typer.testing import CliRunner

    from cyris.entrypoints.cli import app

    async def fake_checks(cfg, _path=None, _deployment=""):
        return [doctor._check_config_file(cfg, None)]

    monkeypatch.setattr("cyris.diagnostics.doctor.run_checks", fake_checks)

    result = CliRunner().invoke(
        app,
        [
            "doctor",
            "--config",
            str(tmp_path / "nope.toml"),
            "--sources",
            str(tmp_path / "nope.yaml"),
        ],
    )

    assert "✓ config —" not in result.stdout
    assert "! config file — not found" in result.stdout


async def test_a_config_key_this_build_cannot_see_is_a_failure(tmp_path: Path) -> None:
    """The 2026-08-25→27 split: the config asked for D1, the image had no `[store]`.

    Pydantic ignores unknown tables, so the setting vanished and doctor stayed
    green for two days while every run wrote to the wrong store.
    """
    config_path = tmp_path / "cyris.toml"
    config_path.write_text('[general]\ntimezone = "Asia/Taipei"\n\n[from_the_future]\nx = 1\n')

    check = _by_name(await doctor.run_checks(_config(tmp_path), config_path), "build")

    assert check.status == "fail"
    assert "[from_the_future]" in check.detail


async def test_the_resolved_store_class_is_reported_not_the_configured_name(
    tmp_path: Path,
) -> None:
    check = _by_name(await doctor.run_checks(_config(tmp_path)), "store wiring")

    assert check.status == "ok"
    assert "ArticleStore" in check.detail


async def test_a_missing_config_file_on_d1_is_ok_and_names_the_environment(
    tmp_path: Path,
) -> None:
    """A container that ships no file is not a host that forgot the file."""
    cfg = _config(tmp_path)
    cfg.app.store.backend = "d1"
    # The premise of this case: the identity arrives as env, not as a file.
    cfg.app.store.database_id = "db"
    cfg.app.store.account_id = "acct"
    cfg.app.store.api_token = "tok"
    cfg.config_file_found = False

    # Unpatched, `_check_store` counts rows over the network on these
    # placeholder credentials and comes back `fail` — tolerating that name below
    # would mean tolerating a real store regression too. Nothing else here does
    # IO: the wiring check only constructs the store.
    with patch.object(
        doctor, "_check_store", lambda _cfg: doctor.Check("article store (d1)", "ok", "stubbed")
    ):
        checks = await doctor.run_checks(cfg)
    check = _by_name(checks, "config file")

    assert check.status == "ok"
    assert "environment" in check.detail.lower()
    assert {c.name for c in checks if c.status in ("warn", "fail")} <= {
        "sources",
        "llm provider",
        "rss buffer",
    }


async def test_a_missing_config_file_on_json_warns_to_set_cyris_env(
    tmp_path: Path,
) -> None:
    cfg = _config(tmp_path)
    cfg.config_file_found = False

    check = _by_name(await doctor.run_checks(cfg), "config file")

    assert check.status == "warn"
    assert "CYRIS_" in check.fix


async def test_a_found_config_file_is_named_in_the_check(tmp_path: Path) -> None:
    config_path = tmp_path / "cyris.toml"
    config_path.write_text('[general]\ntimezone = "Asia/Taipei"\n')
    cfg = _config(tmp_path)
    cfg.config_file_found = True

    check = _by_name(await doctor.run_checks(cfg, config_path), "config file")

    assert check.status == "ok"
    assert str(config_path) in check.detail


async def test_settings_without_a_config_file_do_not_claim_cyris_toml(
    tmp_path: Path,
) -> None:
    cfg = _config(tmp_path)
    cfg.config_file_found = False
    cfg.settings_from_d1 = []

    check = _by_name(await doctor.run_checks(cfg), "settings")

    assert "cyris.toml" not in check.detail


async def test_a_run_with_nowhere_to_put_the_digest_is_a_failure(tmp_path: Path) -> None:
    """Neither sink means the run reports ok and leaves only store rows."""
    cfg = _config(tmp_path)
    cfg.app.html_output.enabled = False
    cfg.app.promote.publish_enabled = False

    check = _by_name(await doctor.run_checks(cfg), "digest output")

    assert check.status == "fail"
    assert "html_output" in check.fix and "publish_enabled" in check.fix


async def test_publishing_alone_is_not_a_sink(tmp_path: Path) -> None:
    """`build_deps` builds the publisher inside the `html_output.enabled` branch,
    so publish_enabled on its own publishes nothing — the check has to say so
    rather than report a sink that does not exist."""
    cfg = _config(tmp_path)
    cfg.app.html_output.enabled = False
    cfg.app.promote.publish_enabled = True

    check = _by_name(await doctor.run_checks(cfg), "digest output")

    assert check.status == "fail"
    assert "no effect" in check.fix


async def test_publishing_is_named_when_it_is_on(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    cfg.app.promote.publish_enabled = True

    check = _by_name(await doctor.run_checks(cfg), "digest output")

    assert check.status == "ok"
    assert "Pages" in check.detail


class TestDeploymentProvenance:
    """`doctor` on a laptop reports the laptop, so dating production is a comparison.

    The online half has one source — the deployment reporting the sha baked into
    its own image — because Cloudflare documents no way to read the image a
    Worker version references (docs/spec/revert-carries-the-image.md).
    """

    @staticmethod
    def _repo(tmp_path: Path, commits: int) -> list[str]:
        import subprocess

        run = lambda *a: subprocess.run(  # noqa: E731 - one line, used four times below
            a, cwd=tmp_path, check=True, capture_output=True, text=True
        )
        run("git", "init", "-q", "-b", "main")
        run("git", "config", "user.email", "t@test")
        run("git", "config", "user.name", "t")
        shas = []
        for i in range(commits):
            (tmp_path / f"{i}.txt").write_text(str(i), encoding="utf-8")
            run("git", "add", "-A")
            # --no-verify: a global hook may reject a subject that is not
            # Conventional Commits, and these messages are fixture noise.
            run("git", "commit", "-qm", f"chore: c{i}", "--no-verify")
            shas.append(
                subprocess.run(
                    ("git", "rev-parse", "HEAD"),
                    cwd=tmp_path,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
            )
        return shas

    def test_the_same_commit_is_reported_as_the_same_commit(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        head = self._repo(tmp_path, 1)[-1]
        monkeypatch.chdir(tmp_path)
        check = doctor._compare_build_sha("https://x.workers.dev", head)
        assert check.status == "ok"
        assert head[:7] in check.detail

    def test_a_checkout_ahead_of_production_counts_the_commits_and_warns(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Warn, not fail: local ahead of production is every working day.

        A check that is red every day is a check nobody reads, and this one has
        to be worth pasting into a support conversation.
        """
        shas = self._repo(tmp_path, 4)
        monkeypatch.chdir(tmp_path)
        check = doctor._compare_build_sha("https://x.workers.dev", shas[0])
        assert check.status == "warn"
        assert "3 commits ahead" in check.detail

    def test_a_deployment_newer_than_the_checkout_says_so(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        import subprocess

        shas = self._repo(tmp_path, 3)
        subprocess.run(
            ("git", "checkout", "-q", shas[0]), cwd=tmp_path, check=True, capture_output=True
        )
        monkeypatch.chdir(tmp_path)
        check = doctor._compare_build_sha("https://x.workers.dev", shas[-1])
        assert check.status == "warn"
        assert "2 commits ahead of HEAD" in check.detail

    def test_an_image_with_no_sha_is_the_failure_this_check_exists_for(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        self._repo(tmp_path, 1)
        monkeypatch.chdir(tmp_path)
        check = doctor._compare_build_sha("https://x.workers.dev", "")
        assert check.status == "fail"
        assert "--build-arg" in check.fix

    def test_a_commit_this_checkout_never_had_is_unanswerable(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        self._repo(tmp_path, 1)
        monkeypatch.chdir(tmp_path)
        check = doctor._compare_build_sha("https://x.workers.dev", "0" * 40)
        assert check.status == "fail"

    def test_without_a_checkout_the_sha_is_still_reported(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Inside the container there is no git tree — the sha alone still answers."""
        monkeypatch.chdir(tmp_path)
        check = doctor._compare_build_sha("https://x.workers.dev", "a" * 40)
        assert check.status == "ok"
        assert "aaaaaaa" in check.detail

    async def test_a_deployment_that_does_not_answer_fails_and_names_the_hostname_to_use(
        self, monkeypatch
    ) -> None:
        async def refuse(_base: str) -> str:
            raise RuntimeError("/login answered 302, not a session")

        monkeypatch.setenv("CYRIS_UI_TOKEN", "t" * 32)
        monkeypatch.setattr(doctor, "_fetch_build_sha", refuse)
        check = await doctor._check_deployment("https://digest.example.com/")
        assert check.status == "fail"
        assert "workers.dev" in check.fix

    async def test_the_login_cookie_carries_to_the_build_read(self, monkeypatch) -> None:
        """The handshake over real HTTP, because the rest of this class stubs it.

        `router.js` answers /login with a 302 that sets the session cookie and
        then checks that cookie on /api/build. The client must not follow the
        redirect and must still carry the cookie forward — one assumption about
        httpx's jar that no amount of monkeypatching would catch.
        """
        from aiohttp import web
        from aiohttp.test_utils import TestServer

        async def login(request: web.Request) -> web.Response:
            form = await request.post()
            if form.get("token") != "s3cret":
                return web.Response(status=401)
            return web.Response(
                status=302, headers={"Location": "/", "Set-Cookie": "cyris_session=ok; Path=/"}
            )

        async def build(request: web.Request) -> web.Response:
            if request.cookies.get("cyris_session") != "ok":
                return web.json_response({"error": "unauthorized"}, status=401)
            return web.json_response({"git_sha": "f" * 40})

        app = web.Application()
        app.router.add_post("/login", login)
        app.router.add_get("/api/build", build)
        server = TestServer(app)
        await server.start_server()
        try:
            monkeypatch.setenv("CYRIS_UI_TOKEN", "s3cret")
            assert await doctor._fetch_build_sha(str(server.make_url("")).rstrip("/")) == "f" * 40
        finally:
            await server.close()

    async def test_a_missing_ui_token_is_named_rather_than_blamed_on_the_hostname(
        self, monkeypatch
    ) -> None:
        """A fault in this machine's .env must not read as a fault in the URL."""

        async def never_called(_base: str) -> str:
            raise AssertionError("no request should be made without a token")

        monkeypatch.delenv("CYRIS_UI_TOKEN", raising=False)
        monkeypatch.setattr(doctor, "_fetch_build_sha", never_called)
        check = await doctor._check_deployment("https://x.workers.dev")
        assert check.status == "fail"
        assert "CYRIS_UI_TOKEN" in check.fix
        assert "workers.dev" not in check.fix

    async def test_not_passing_a_deployment_skips_rather_than_guessing(
        self, tmp_path: Path
    ) -> None:
        cfg = _config(tmp_path)
        checks = await doctor.run_checks(cfg)
        check = _by_name(checks, "deployment image")
        assert check.status == "skip"
        assert "--deployment" in check.fix
