"""A local `cyris triage-ui` serves the archive `HtmlDigestWriter` wrote.

Each request goes through the application's own dispatch with no socket, as
`tests/test_triage_server_origin.py` does, so routing and the Host check run as they do on
a live server. The probe tests run the pages' real probe script under a JavaScript runtime
against an answer: the server's own for the local edition, the Worker's for the cloud one.
"""

import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from multidict import CIMultiDict

from cyris.adapters.output.html_digest import HtmlDigestWriter
from cyris.domain.models import (
    ArticleState,
    DigestContent,
    Lane,
    StoredArticle,
    UsageStats,
)
from cyris.entrypoints.triage_server import NO_ISSUE_YET, TriageServer, type_scale_style

pytestmark = pytest.mark.integration

ROOT = Path(__file__).parents[1]
DATE = "2026-04-15"
PERIOD = "evening"
OWN = "127.0.0.1:8766"
ISSUE = f"{DATE}-{PERIOD}"
RAW = f"{ISSUE}-raw"
TYPE_SCALE_KEY = "digest.type_scale"
# The Worker's answer, `workers/app/src/router.js` `handleVote`: no `votes` field.
WORKER_ANSWER = {"authorized": True}


def _write_archive(html_dir: Path) -> None:
    """An archive as `cyris run` leaves it: an index, an issue and its All articles page."""
    writer = HtmlDigestWriter(html_dir)
    content = DigestContent(
        date=DATE,
        period=PERIOD,
        sources_processed=1,
        articles_received=1,
        articles_included=1,
        usage=UsageStats(),
    )
    now = datetime.now(UTC)
    writer.write_raw(
        DATE,
        PERIOD,
        [
            StoredArticle(
                url="https://example.com/a",
                original_id=1,
                title="Article",
                content="",
                published_at=now,
                source_name="Src",
                source_lane=Lane.SKIM,
                state=ArticleState.PENDING,
                first_seen_at=now,
            )
        ],
    )
    writer.write(content, raw_page=True)


@pytest.fixture
def html_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "html"
    _write_archive(directory)
    return directory


async def _get(
    server: TriageServer, path: str, method: str = "GET", **headers: str
) -> web.StreamResponse:
    """Dispatch one request. A header is named as a keyword: `host="evil.example"`."""
    app = server._app
    if not app.frozen:
        app.freeze()
    named = CIMultiDict({"Host": OWN})
    named.update((name.replace("_", "-"), value) for name, value in headers.items())
    try:
        return await app._handle(make_mocked_request(method, path, headers=named, app=app))
    except web.HTTPException as refusal:
        # A live server turns the router's own 404 and 405 into the response they are.
        return refusal


def _server(html_dir: Path | None, **values: object) -> TriageServer:
    return TriageServer(html_dir=html_dir, values=values)


class TestTheTable:
    @pytest.mark.parametrize("path", ["/", "/index.html"])
    async def test_the_root_and_index_html_answer_the_index(self, html_dir, path):
        response = await _get(_server(html_dir), path)

        assert response.status == 200
        assert response.body == (html_dir / "index.html").read_bytes()
        assert f'href="{ISSUE}.html"' in response.text

    @pytest.mark.parametrize("path", [f"/{ISSUE}", f"/{ISSUE}.html"])
    async def test_an_issue_answers_with_or_without_its_suffix(self, html_dir, path):
        response = await _get(_server(html_dir), path)

        assert response.status == 200
        assert response.body == (html_dir / f"{ISSUE}.html").read_bytes()

    @pytest.mark.parametrize("path", [f"/{RAW}", f"/{RAW}.html"])
    async def test_the_all_articles_page_answers(self, html_dir, path):
        response = await _get(_server(html_dir), path)

        assert response.status == 200
        assert response.body == (html_dir / f"{RAW}.html").read_bytes()

    async def test_a_name_without_a_page_is_404(self, html_dir):
        response = await _get(_server(html_dir), "/2026-01-01-morning")

        assert response.status == 404

    async def test_the_index_is_404_with_the_next_step_when_there_is_no_issue(self, tmp_path):
        response = await _get(_server(tmp_path / "empty"), "/")

        assert response.status == 404
        assert response.content_type == "text/plain"
        assert response.text == NO_ISSUE_YET
        assert "cyris run --period morning" in response.text

    async def test_a_directory_is_404(self, html_dir):
        (html_dir / "folder.html").mkdir()

        assert (await _get(_server(html_dir), "/folder")).status == 404
        assert (await _get(_server(html_dir), "/folder.html")).status == 404

    async def test_a_page_answers_as_html_that_is_never_cached(self, html_dir):
        response = await _get(_server(html_dir), "/")

        assert response.headers["Content-Type"] == "text/html; charset=utf-8"
        assert response.headers["Cache-Control"] == "no-cache"

    async def test_head_answers_the_same_headers_as_get(self, html_dir):
        server = _server(html_dir)

        for path in ("/", f"/{ISSUE}.html"):
            got = await _get(server, path)
            head = await _get(server, path, method="HEAD")

            assert head.status == 200
            assert head.headers["Content-Type"] == got.headers["Content-Type"]
            assert head.headers["Cache-Control"] == "no-cache"
        assert (await _get(server, "/missing", method="HEAD")).status == 404

    async def test_a_foreign_host_is_refused_on_a_page(self, html_dir):
        for path in ("/", f"/{ISSUE}"):
            response = await _get(_server(html_dir), path, host="evil.example")

            assert response.status == 403
            assert json.loads(response.text)["ok"] is False


class TestOnlyHtmlInTheDirectoryIsServed:
    @pytest.mark.parametrize(
        "path",
        [
            "/..%2f..%2fetc%2fpasswd",
            "/..%2f..%2fetc%2fpasswd.html",
            "/%2e%2e%2f%2e%2e%2fetc%2fpasswd",
            "/..",
            "/../index.html",
            "/a/b",
            f"/{ISSUE}/",
            f"/{ISSUE}.html/x",
            "/a%2fb",
            "/.hidden",
            "/.hidden.html",
            "/-lead",
            "/index.html.html",
            "/index.txt",
            "/a.b",
            "/%00",
            "/index%00",
        ],
    )
    async def test_a_path_that_is_not_one_plain_name_reaches_no_file(self, tmp_path, path):
        directory = tmp_path / "html"
        _write_archive(directory)
        (directory / ".hidden.html").write_text("hidden", encoding="utf-8")
        (tmp_path / "a.html").write_text("outside", encoding="utf-8")
        (tmp_path / "b.html").write_text("outside", encoding="utf-8")

        response = await _get(_server(directory), path)

        assert response.status == 404
        assert "outside" not in (response.text or "")
        assert "hidden" not in (response.text or "")

    async def test_a_symlink_out_of_the_directory_is_404(self, tmp_path):
        directory = tmp_path / "html"
        _write_archive(directory)
        outside = tmp_path / "secret.html"
        outside.write_text("<html><head></head>secret</html>", encoding="utf-8")
        (directory / "leak.html").symlink_to(outside)
        (directory / "dirleak.html").symlink_to(tmp_path, target_is_directory=True)

        for path in ("/leak", "/leak.html", "/dirleak", "/dirleak.html"):
            response = await _get(_server(directory), path)

            assert response.status == 404
            assert "secret" not in (response.text or "")

    async def test_a_symlink_to_another_file_that_is_not_html_is_404(self, html_dir):
        (html_dir / "notes.txt").write_text("plain notes", encoding="utf-8")
        (html_dir / "notes.html").symlink_to(html_dir / "notes.txt")

        response = await _get(_server(html_dir), "/notes")

        assert response.status == 404

    async def test_a_symlink_to_a_page_inside_the_directory_is_served(self, html_dir):
        (html_dir / "latest.html").symlink_to(html_dir / f"{ISSUE}.html")

        response = await _get(_server(html_dir), "/latest")

        assert response.status == 200
        assert response.body == (html_dir / f"{ISSUE}.html").read_bytes()

    async def test_a_file_that_is_not_html_is_never_served(self, html_dir):
        (html_dir / "style.css").write_text("body{}", encoding="utf-8")
        (html_dir / "favicon.svg").write_text("<svg/>", encoding="utf-8")

        for path in ("/style.css", "/style", "/favicon.svg.html"):
            assert (await _get(_server(html_dir), path)).status == 404


class TestEveryExistingRouteKeepsWinning:
    @pytest.fixture
    def shadowing(self, html_dir: Path) -> TriageServer:
        """Every name an existing route owns, with a page of that name in the directory."""
        for name in ("settings", "labels", "onboarding", "interest", "static"):
            (html_dir / f"{name}.html").write_text(
                f"<html><head></head>SHADOW-{name}</html>", encoding="utf-8"
            )
        return _server(html_dir)

    @pytest.mark.parametrize("name", ["settings", "labels", "onboarding"])
    async def test_a_container_page_is_not_shadowed(self, shadowing, name):
        response = await _get(shadowing, f"/{name}")

        # 200 for the page itself; a partial server's /onboarding sends the reader to /settings.
        assert response.status in (200, 302)
        assert f"SHADOW-{name}" not in (response.text or "")

    async def test_the_interest_map_is_not_shadowed(self, shadowing):
        response = await _get(shadowing, "/interest")

        # A partial server has no reader: the route's own 409, not the shadowing page.
        assert response.status == 409
        assert "SHADOW-interest" not in response.text

    async def test_the_favicon_static_files_and_the_api_are_not_shadowed(self, shadowing):
        favicon = await _get(shadowing, "/favicon.svg")
        asset = await _get(shadowing, "/static/settings.js")
        build = await _get(shadowing, "/api/build")
        vote = await _get(shadowing, "/api/vote")

        assert favicon.content_type == "image/svg+xml"
        assert isinstance(asset, web.FileResponse)
        assert "git_sha" in json.loads(build.text)
        assert json.loads(vote.text) == {"authorized": True, "votes": False}

    async def test_a_page_beside_them_is_still_served(self, shadowing):
        assert (await _get(shadowing, f"/{ISSUE}")).status == 200


class TestTypeSize:
    @pytest.mark.parametrize("path", ["/", f"/{ISSUE}", f"/{RAW}"])
    async def test_at_one_the_bytes_equal_the_file(self, html_dir, path):
        name = "index" if path == "/" else path.lstrip("/")
        response = await _get(_server(html_dir, **{TYPE_SCALE_KEY: 1}), path)

        assert response.body == (html_dir / f"{name}.html").read_bytes()

    async def test_without_a_value_the_bytes_equal_the_file(self, html_dir):
        response = await _get(_server(html_dir), "/")

        assert response.body == (html_dir / "index.html").read_bytes()

    @pytest.mark.parametrize(("scale", "written"), [(1.125, "1.125"), (0.875, "0.875")])
    @pytest.mark.parametrize("path", ["/", f"/{ISSUE}", f"/{RAW}"])
    async def test_another_size_is_carried_before_the_first_head_end(
        self, html_dir, path, scale, written
    ):
        name = "index" if path == "/" else path.lstrip("/")
        source = (html_dir / f"{name}.html").read_text(encoding="utf-8")
        style = f"<style>html:root{{--type-scale:{written}}}</style>"

        response = await _get(_server(html_dir, **{TYPE_SCALE_KEY: scale}), path)

        assert style == type_scale_style(scale)
        assert response.text == source.replace("</head>", f"{style}</head>", 1)
        assert response.text.count("--type-scale:" + written + "}") == 1

    async def test_a_save_changes_the_next_answer(self, html_dir):
        server = _server(html_dir, **{TYPE_SCALE_KEY: 1})
        assert "--type-scale:1.125}" not in (await _get(server, "/")).text

        server._values.update({TYPE_SCALE_KEY: 1.125})

        assert (
            "<style>html:root{--type-scale:1.125}</style></head>" in (await _get(server, "/")).text
        )

    async def test_a_page_with_no_head_end_is_sent_unchanged(self, html_dir):
        (html_dir / "bare.html").write_text("<p>no head</p>", encoding="utf-8")

        response = await _get(_server(html_dir, **{TYPE_SCALE_KEY: 1.125}), "/bare")

        assert response.body == b"<p>no head</p>"

    async def test_the_file_on_disk_never_changes(self, html_dir):
        before = (html_dir / "index.html").read_bytes()

        await _get(_server(html_dir, **{TYPE_SCALE_KEY: 1.125}), "/")

        assert (html_dir / "index.html").read_bytes() == before

    @pytest.mark.parametrize("value", ["1.125", "<script>", 2, None, True])
    async def test_a_value_that_is_not_a_step_injects_nothing(self, html_dir, value):
        response = await _get(_server(html_dir, **{TYPE_SCALE_KEY: value}), "/")

        assert response.body == (html_dir / "index.html").read_bytes()


class TestTheVoteProbeAnswer:
    @pytest.mark.parametrize("method", ["GET", "HEAD"])
    async def test_it_answers_authorized_without_votes(self, method):
        response = await _get(_server(None), "/api/vote", method=method)

        assert response.status == 200
        assert response.content_type == "application/json"
        assert json.loads(response.text) == {"authorized": True, "votes": False}

    async def test_a_vote_is_not_routed(self):
        response = await _get(_server(None), "/api/vote", method="POST")

        assert response.status == 405

    async def test_a_foreign_host_is_refused(self):
        assert (await _get(_server(None), "/api/vote", host="evil.example")).status == 403


def _runtime() -> str:
    runtime = shutil.which("node") or shutil.which("bun")
    assert runtime, "the probe tests need node or bun"
    return runtime


VOTE_CONTROLS = ("vote-group", "gated")
OPENED_BY_AUTHORIZED = ("settings", "archive", "brand")

# Stand-ins for the elements a page gives the probe, each in the state the page ships it in.
DRIVER = """
const els = {
  '.vote-group': [{ style: {} }],
  '.settings-link': [{ style: {} }],
  '.gated': [{ hidden: true }],
  '.archive-link': [{ hidden: true }],
  '[data-gated-href]': [{ href: '', dataset: { gatedHref: 'index.html' } }],
  '.signin-link': [{ hidden: true, href: '' }],
};
globalThis.document = { querySelectorAll: (selector) => els[selector] ?? [] };
globalThis.location = { pathname: '/settings', search: '' };
globalThis.fetch = async () => ({
  type: 'basic', status: %(status)d, ok: %(ok)s, json: async () => (%(answer)s),
});
%(probe)s
setTimeout(() => console.log(JSON.stringify({
  vote: els['.vote-group'][0].style.display === 'inline-flex',
  settings: els['.settings-link'][0].style.display === 'inline',
  gated: els['.gated'][0].hidden === false,
  archive: els['.archive-link'][0].hidden === false,
  brand: els['[data-gated-href]'][0].href === 'index.html',
})), 50);
"""


def _probe_opens(tmp_path: Path, answer: object, status: int = 200) -> dict[str, bool]:
    template = HtmlDigestWriter(tmp_path).env.get_template("_probe_script.html.j2").render()
    probe = template.replace("<script>", "").replace("</script>", "")
    script = tmp_path / "probe.js"
    script.write_text(
        DRIVER
        % {
            "status": status,
            "ok": "true" if status < 400 else "false",
            "answer": json.dumps(answer),
            "probe": probe,
        },
        encoding="utf-8",
    )
    done = subprocess.run(
        [_runtime(), str(script)], cwd=tmp_path, capture_output=True, text=True, timeout=60
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


class TestTheProbeSplit:
    async def test_the_local_answer_opens_settings_and_archive_and_leaves_votes_closed(
        self, tmp_path
    ):
        answer = json.loads((await _get(_server(None), "/api/vote")).text)

        assert _probe_opens(tmp_path, answer) == {
            "vote": False,
            "settings": True,
            "gated": False,
            "archive": True,
            "brand": True,
        }

    def test_the_workers_answer_still_opens_everything(self, tmp_path):
        """The cloud-regression guard: no `votes` field means no change."""
        assert _probe_opens(tmp_path, WORKER_ANSWER) == {
            "vote": True,
            "settings": True,
            "gated": True,
            "archive": True,
            "brand": True,
        }

    def test_the_workers_answer_is_still_exactly_authorized_true(self):
        router = (ROOT / "workers" / "app" / "src" / "router.js").read_text(encoding="utf-8")

        assert "return json({ authorized: true }, 200);" in router

    @pytest.mark.parametrize(
        ("answer", "status"),
        [({"authorized": False}, 200), ({"authorized": True}, 403), ({"error": "x"}, 404)],
    )
    def test_nothing_else_opens_anything(self, tmp_path, answer, status):
        assert set(_probe_opens(tmp_path, answer, status).values()) == {False}


class TestTheReviewedEdges:
    async def test_a_name_past_the_filesystem_limit_is_a_404(self, html_dir):
        response = await _get(_server(html_dir), "/" + "a" * 300)

        assert response.status == 404

    @pytest.mark.parametrize("path", ["/", "/index.html"])
    async def test_the_archive_address_says_what_to_do_before_the_first_issue(self, tmp_path, path):
        response = await _get(_server(tmp_path), path)

        assert response.status == 404
        assert "cyris run" in response.text

    @pytest.mark.parametrize("path", ["/", "/index.html"])
    async def test_with_pages_off_it_names_the_setting_a_run_would_not_fix(self, path):
        response = await _get(TriageServer(), path)

        assert response.status == 404
        assert "[html_output] enabled" in response.text

    async def test_only_the_first_head_end_takes_the_type_size(self, tmp_path):
        (tmp_path / "index.html").write_text("<head></head><pre></head></pre>", encoding="utf-8")
        server = TriageServer(html_dir=tmp_path, values={"digest.type_scale": 1.125})

        answer = (await _get(server, "/")).text

        assert answer.index("--type-scale") < answer.index("</head>")
        assert answer.count("--type-scale") == 1

    @pytest.mark.parametrize("path", ["/settings", "/labels"])
    async def test_the_servers_own_pages_carry_the_type_size(self, path):
        response = await _get(TriageServer(values={"digest.type_scale": 1.125}), path)

        assert "<style>html:root{--type-scale:1.125}</style></head>" in response.text

    async def test_behind_the_worker_the_worker_alone_sizes_them(self):
        server = TriageServer(values={"digest.type_scale": 1.125}, behind_app_worker=True)

        response = await _get(server, "/settings")

        assert "--type-scale:1.125}" not in response.text
