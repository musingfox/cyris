"""`cyris doctor` — the checks, and what each verdict tells the reader to do."""

from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from cyris.adapters.gemini_client import GeminiAPIError
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


async def test_llm_probe_exposes_structured_gemini_error_details(monkeypatch) -> None:
    request = httpx.Request("POST", "https://generativelanguage.googleapis.com")
    response = httpx.Response(400, request=request)

    class FakeLLM:
        model = "gemini-2.5-flash"

        async def complete(self, prompt, *, max_tokens):
            raise GeminiAPIError(
                code=400,
                status="INVALID_ARGUMENT",
                message=f"The model is invalid; {doctor.LLM_PROBE_PROMPT}. Keep this context.",
                request=request,
                response=response,
            )

    monkeypatch.setattr("cyris.bootstrap.build_llm", lambda _cfg: FakeLLM())
    cfg = LLMProviderConfig(provider="gemini", api_key="key")

    check = await doctor.probe_llm(cfg)

    assert check.status == "fail"
    assert doctor.LLM_PROBE_PROMPT not in check.detail
    assert "[probe text redacted]" in check.detail
    assert "code=400" in check.detail
    assert "status=INVALID_ARGUMENT" in check.detail
    assert "The model is invalid;" in check.detail
    assert "Keep this context." in check.detail


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
    assert "baked defaults" not in result.stdout


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

    async def test_a_deployment_older_than_the_endpoint_is_not_blamed_on_its_hostname(
        self, monkeypatch
    ) -> None:
        """The 2026-09-15 observation against the real deployment.

        Sign-in succeeded and `/api/build` answered 404, so the host is right
        and the image is simply older than the endpoint. Answering that with
        the workers.dev hint would send the reader after the wrong cause.
        """

        async def absent(_base: str) -> str:
            raise doctor._EndpointAbsentError("https://x.workers.dev")

        monkeypatch.setenv("CYRIS_UI_TOKEN", "t" * 32)
        monkeypatch.setattr(doctor, "_fetch_build_sha", absent)
        check = await doctor._check_deployment("https://x.workers.dev")
        assert check.status == "fail"
        assert "predates the endpoint" in check.detail
        assert "workers.dev" not in check.fix

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


def _discord_transport(responses: list, seen: list) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return responses.pop(0)

    return httpx.MockTransport(handler)


async def test_a_url_that_is_not_a_webhook_is_rejected_without_a_request() -> None:
    seen: list[str] = []

    check = await doctor.probe_discord(
        "https://example.com/foo", transport=_discord_transport([], seen)
    )

    assert check.name == "discord probe"
    assert check.status == "fail"
    assert check.detail == "not a Discord webhook URL"
    assert seen == []


async def test_the_masked_value_is_refused_without_a_request() -> None:
    """The settings page prefills the mask; saving it unedited must not cost a
    round-trip to Discord only to be told the token is wrong."""
    from cyris.adapters.notify import WEBHOOK_MASK

    seen: list[str] = []

    check = await doctor.probe_discord(
        f"https://discord.com/api/webhooks/1/{WEBHOOK_MASK}",
        transport=_discord_transport([], seen),
    )

    assert check.status == "fail"
    assert check.detail == "that is the masked value, not a webhook URL"
    assert seen == []


async def test_a_json_body_that_is_not_an_object_is_reported_not_raised() -> None:
    """An intercepting proxy answering with a JSON array is enough: `.get` on it
    would throw, and a probe that throws is a worse diagnostic than one that
    reports."""
    seen: list[str] = []
    responses = [httpx.Response(500, json=["upstream is unhappy"])]

    check = await doctor.probe_discord(
        "https://discord.com/api/webhooks/1/tok",
        transport=_discord_transport(responses, seen),
    )

    assert check.status == "fail"
    assert len(seen) == 1


async def test_a_live_webhook_is_named_back_to_the_reader() -> None:
    seen: list[str] = []
    responses = [httpx.Response(200, json={"id": "123", "name": "digest-bot"})]

    check = await doctor.probe_discord(
        "https://discord.com/api/webhooks/123/probe-live",
        transport=_discord_transport(responses, seen),
    )

    assert check.status == "ok"
    assert "digest-bot" in check.detail


async def test_a_wrong_token_is_reported_with_its_status() -> None:
    seen: list[str] = []
    responses = [httpx.Response(401, json={"message": "Invalid Webhook Token"})]

    check = await doctor.probe_discord(
        "https://discord.com/api/webhooks/123/probe-bad-token",
        transport=_discord_transport(responses, seen),
    )

    assert check.status == "fail"
    assert "401" in check.detail


async def test_a_deleted_webhook_is_reported_with_its_status() -> None:
    seen: list[str] = []
    responses = [httpx.Response(404, json={"message": "Unknown Webhook"})]

    check = await doctor.probe_discord(
        "https://discord.com/api/webhooks/123/probe-deleted",
        transport=_discord_transport(responses, seen),
    )

    assert check.status == "fail"
    assert "404" in check.detail


async def test_being_rate_limited_says_so_in_discords_own_words() -> None:
    """The body carries the reason, so it is read before the status is judged —
    a 429 with no explanation reads as a broken webhook, which it is not."""
    seen: list[str] = []
    responses = [httpx.Response(429, json={"message": "You are being rate limited."})]

    check = await doctor.probe_discord(
        "https://discord.com/api/webhooks/123/probe-limited",
        transport=_discord_transport(responses, seen),
    )

    assert check.status == "fail"
    assert "You are being rate limited." in check.detail
    assert len(seen) == 1


async def test_an_unreachable_discord_is_a_failed_check_not_an_exception() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    check = await doctor.probe_discord(
        "https://discord.com/api/webhooks/123/probe-offline",
        transport=httpx.MockTransport(handler),
    )

    assert check.status == "fail"
    assert "boom" in check.detail


async def test_a_set_webhook_is_reported_without_its_url_or_a_home(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    cfg.app.notify.discord_webhook_url = "https://discord.com/api/webhooks/123/abcTOKEN"

    check = _by_name(await doctor.run_checks(cfg), "discord")

    assert check.status == "ok"
    assert check.detail == "webhook set"
    said = f"{check.name} {check.detail} {check.fix}"
    assert "abcTOKEN" not in said
    assert "CYRIS_DISCORD_WEBHOOK_URL" not in said


async def test_an_empty_webhook_is_notifications_off(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    cfg.app.notify.discord_webhook_url = ""

    check = _by_name(await doctor.run_checks(cfg), "discord")

    assert check.status == "skip"
    assert check.detail == "off — runs finish without a message"
    assert check.fix == "Set one on /settings to get a message per digest."
    assert "CYRIS_DISCORD_WEBHOOK_URL" not in check.fix


async def test_a_missing_webhook_defers_to_the_settings_check(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    cfg.missing_settings = ["notify.discord_webhook_url"]

    check = _by_name(await doctor.run_checks(cfg), "discord")

    assert check.status == "skip"
    assert check.detail == "not set — see settings"


async def test_a_config_left_on_the_old_stanza_fails_the_build_check(
    tmp_path: Path, monkeypatch
) -> None:
    """A stanza this build moved must be loud, even nested one level down.

    `[general.notify]` sits under a table name that is still valid, so a
    top-level-only comparison reports green while the webhook silently stops
    arriving — the 2026-08-25 failure the build check exists to prevent.
    """
    config_path = tmp_path / "cyris.toml"
    config_path.write_text(
        '[general]\ntimezone = "Asia/Taipei"\n\n'
        '[general.notify]\ndiscord_webhook_url = "https://discord.com/api/webhooks/1/stale"\n\n'
        f'[agent_vault]\npath = "{tmp_path / "agent-vault"}"\n'
    )
    from cyris.config import load_config

    sources_path = tmp_path / "sources.yaml"
    sources_path.write_text(
        "sources:\n  - name: Origin Feed\n    url: https://origin.test/feed\n    tier: filter\n"
    )
    cfg = load_config(config_path=config_path, sources_path=sources_path)
    cfg.app.html_output.enabled = True

    checks = await doctor.run_checks(cfg, config_path)

    build = _by_name(checks, "build")
    assert build.status == "fail"
    assert "[general] notify" in build.detail
    assert _by_name(checks, "discord").status == "skip"


async def test_the_run_reports_one_discord_check_and_a_missing_webhook_is_not_a_failure(
    tmp_path: Path, monkeypatch
) -> None:

    checks = await doctor.run_checks(_config(tmp_path))

    discord = [c for c in checks if c.name == "discord"]
    assert len(discord) == 1
    assert discord[0].status == "skip"


class TestLastRun:
    """`--deployment` also says which sha production's last run executed."""

    @staticmethod
    def _d1_config(tmp_path: Path) -> Config:
        cfg = _config(tmp_path)
        cfg.app.store.backend = "d1"
        cfg.app.store.database_id = "db"
        cfg.app.store.account_id = "acct"
        cfg.app.store.api_token = "tok"
        return cfg

    @staticmethod
    def _with_run(monkeypatch, build_sha: str | None):
        from fakes import SqliteD1

        from cyris.adapters.store.runs import D1RunLog

        db = SqliteD1()
        if build_sha is not None:
            D1RunLog(db, build_sha).record({"status": "ok", "period": "morning", "dry_run": False})
        monkeypatch.setattr("cyris.bootstrap.build_d1_client", lambda _cfg: db)
        return db

    async def test_not_asked_skips(self, tmp_path: Path) -> None:
        checks = await doctor.run_checks(_config(tmp_path), deployment_url="")
        assert _by_name(checks, "last run").status == "skip"

    def test_a_json_store_has_no_run_history(self, tmp_path: Path) -> None:
        assert doctor._check_last_run(_config(tmp_path), "abc").status == "skip"

    def test_no_recorded_run_skips(self, tmp_path: Path, monkeypatch) -> None:
        self._with_run(monkeypatch, None)
        assert doctor._check_last_run(self._d1_config(tmp_path), "abc").status == "skip"

    def test_a_last_run_on_the_deployed_sha_is_ok(self, tmp_path: Path, monkeypatch) -> None:
        from cyris.adapters.store.runs import D1RunLog

        db = self._with_run(monkeypatch, "abc1234def")
        check = doctor._check_last_run(self._d1_config(tmp_path), "abc1234def")
        assert check.status == "ok"
        assert "abc1234" in check.detail
        assert D1RunLog(db, "").last()["finished_at"] in check.detail

    def test_a_last_run_on_another_sha_warns(self, tmp_path: Path, monkeypatch) -> None:
        self._with_run(monkeypatch, "aaaaaaa1")
        check = doctor._check_last_run(self._d1_config(tmp_path), "bbbbbbb2")
        assert check.status == "warn"
        assert "aaaaaaa" in check.detail
        assert "bbbbbbb" in check.detail

    def test_a_last_run_without_a_sha_warns(self, tmp_path: Path, monkeypatch) -> None:
        self._with_run(monkeypatch, "")
        assert doctor._check_last_run(self._d1_config(tmp_path), "abc1234").status == "warn"

    def test_an_unreadable_run_table_warns(self, tmp_path: Path, monkeypatch) -> None:
        from cyris.adapters.store.d1 import D1Error

        class Down:
            def query(self, sql, params=None):
                raise D1Error("HTTP 500")

        monkeypatch.setattr("cyris.bootstrap.build_d1_client", lambda _cfg: Down())
        check = doctor._check_last_run(self._d1_config(tmp_path), "abc1234")
        assert check.status == "warn"
        assert "HTTP 500" in check.detail

    async def test_the_line_follows_the_deployment_image_from_one_fetch(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        calls: list[str] = []

        async def fetch(base: str) -> str:
            calls.append(base)
            return "abc1234def"

        monkeypatch.setenv("CYRIS_UI_TOKEN", "t" * 32)
        monkeypatch.setattr(doctor, "_fetch_build_sha", fetch)
        self._with_run(monkeypatch, "abc1234def")

        checks = await doctor.run_checks(
            self._d1_config(tmp_path), deployment_url="https://x.workers.dev"
        )

        names = [c.name for c in checks]
        assert names.index("last run") == names.index("deployment image") + 1
        assert _by_name(checks, "last run").status == "ok"
        assert len(calls) == 1


async def test_egress_probe_reads_colo_and_location_from_the_trace() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="fl=1\ncolo=YVR\nloc=CA\nip=1.2.3.4\n")

    assert await doctor.probe_egress(httpx.MockTransport(handler)) == {
        "colo": "YVR",
        "loc": "CA",
    }


async def test_egress_probe_reports_rather_than_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    result = await doctor.probe_egress(httpx.MockTransport(handler))

    assert "no route" in result["error"]


async def test_llm_probe_prompt_satisfies_openai_json_mode(monkeypatch) -> None:
    """OpenAIClient always sends `response_format: json_object`, and OpenAI 400s
    any such request whose messages never say "json" — so a bare "ping" made
    every OpenAI model look broken."""
    prompts = []

    class FakeLLM:
        model = "gpt-5-mini"

        async def complete(self, prompt, *, max_tokens):
            prompts.append(prompt)

    monkeypatch.setattr("cyris.bootstrap.build_llm", lambda _cfg: FakeLLM())

    check = await doctor.probe_llm(LLMProviderConfig(provider="openai", api_key="key"))

    assert check.status == "ok"
    assert "json" in prompts[0].lower()


async def test_the_llm_probe_skips_provider_none() -> None:
    check = await doctor.probe_llm(LLMProviderConfig(provider="none", model=""))

    assert check.status == "skip"
    assert check.detail == "none — no model to call"
    assert "ANTHROPIC_API_KEY" not in check.detail


def test_a_d1_deployment_missing_a_setting_fails_naming_it(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    cfg.app.store.backend = "d1"
    cfg.missing_settings = ["general.timezone"]

    assert doctor._check_settings(cfg) == doctor.Check(
        "settings",
        "fail",
        "missing in D1: general.timezone",
        "Set them on /settings, or run `cyris settings push`.",
    )


def test_a_complete_json_deployment_passes(tmp_path: Path) -> None:
    assert doctor._check_settings(_config(tmp_path)) == doctor.Check(
        "settings", "ok", "all 20 set in cyris.toml"
    )


def test_a_complete_d1_deployment_passes(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    cfg.app.store.backend = "d1"

    assert doctor._check_settings(cfg) == doctor.Check("settings", "ok", "all 20 set in D1")


async def test_a_json_deployment_without_a_file_fails_listing_every_key(
    tmp_path: Path, monkeypatch
) -> None:
    from cyris.config import GRADE_D_KEYS, load_config

    monkeypatch.delenv("CYRIS_STORE_BACKEND", raising=False)
    cfg = load_config(tmp_path / "nope.toml", tmp_path / "nope.yaml")

    checks = await doctor.run_checks(cfg)

    settings = _by_name(checks, "settings")
    assert settings.status == "fail"
    assert settings.detail == f"missing from cyris.toml: {', '.join(sorted(GRADE_D_KEYS))}"
    assert settings.fix == "cyris.toml.example lists every key."
    assert _by_name(checks, "config file").detail == "not found"


def test_doctor_on_an_empty_d1_fails_on_settings(tmp_path: Path, monkeypatch) -> None:
    from typer.testing import CliRunner

    from cyris.entrypoints.cli import app

    monkeypatch.setenv("CYRIS_STORE_BACKEND", "d1")
    monkeypatch.setenv("CYRIS_STORE_DATABASE_ID", "db")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "tok")

    result = CliRunner().invoke(
        app,
        ["doctor", "--config", str(tmp_path / "nope.toml"), "--sources", str(tmp_path / "s.yaml")],
    )

    assert result.exit_code == 1
    assert "✗ settings — missing in D1:" in result.stdout
