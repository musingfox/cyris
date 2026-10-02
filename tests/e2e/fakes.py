"""Every edge the end-to-end suite fakes, as one mitmproxy addon.

mitmdump loads this file (`tests/e2e/harness.py` starts it through `uvx`), so it runs
on mitmproxy's own Python and imports nothing from cyris or the test venv. The
proxy is started with `connection_strategy=lazy` and every request is answered in
the `request` hook, so no request is ever forwarded to a real host.

The APIs mirrored here, by route name:

- `d1_query`: Cloudflare D1's REST query endpoint, over a sqlite file the test owns.
  It refuses what D1 refuses and sqlite would not: more than 100 bound parameters,
  and a compound SELECT of five or more terms (see tests/fakes.py).
- `pages_*`: Cloudflare Pages direct upload, the steps `adapters/output/pages_deploy.py`
  takes, for a project that exists and has no deployment yet. `pages_site` serves
  what the last deployment uploaded, so the publish receipt reads the real page.
- `workers_domains`, `email_send`: the Workers custom-domain list and Email Sending.
- `discord_webhook`: a Discord webhook post.
- `gemini_generate`: Gemini's generateContent. It answers by the prompt kind it
  finds in the system prompt, from rules the test scripts (see `_llm_answer`).
- `rss_articles`, `newsletter_*`, `promote_*`: the three Workers cyris pulls from,
  on the hosts the test configured.

Each request is appended to the run's record as one JSON line: method, host, path,
query, headers, body, and the route that answered it. A request no route claims
gets a 502 and the route `null`.
"""

import base64
import json
import re
import sqlite3
from email import message_from_bytes, policy
from email.utils import parsedate_to_datetime
from pathlib import Path

from mitmproxy import ctx, http

CF = "api.cloudflare.com"
CF_ACCOUNT = r"/client/v4/accounts/[^/]+"
PAGES_PROJECT = CF_ACCOUNT + r"/pages/projects/[^/]+"
GEMINI = "generativelanguage.googleapis.com"

# D1's own limits (adapters/store/d1.py, tests/fakes.py).
D1_MAX_BOUND_PARAMS = 100
D1_COMPOUND_SELECT_LIMIT = 5

# The JSON key each system prompt asks the model to answer under is what tells the
# four prompt kinds apart (service_layer/prompts.py), not the prose around it.
PROMPT_KINDS = (
    ("scoring", '"scores"'),
    ("filter", '"selected"'),
    ("cluster", '"clusters"'),
    ("summarize", '"sections"'),
)
_PROMPT_LINE = re.compile(r"^\[([^\]]+)\] (.*)$")
_SOURCED_TITLE = re.compile(r"^\((.*?)\) (.*)$")


def _json(status, body):
    return status, json.dumps(body, ensure_ascii=False).encode(), "application/json"


def _cf(result):
    return _json(200, {"success": True, "errors": [], "messages": [], "result": result})


def _cf_error(status, message):
    return _json(status, {"success": False, "errors": [{"code": 7500, "message": message}]})


class Fakes:
    def __init__(self):
        self.script = {}
        self.db = None
        self.record_path = None
        self.seq = 0
        self.assets = {}  # Pages asset hash -> bytes
        self.deployed = {}  # path -> hash, from the last deployment
        self.routes = self._routes()

    def load(self, loader):
        loader.add_option("e2e_script", str, "", "The scenario file the harness wrote")

    def configure(self, updated):
        if "e2e_script" not in updated or not ctx.options.e2e_script:
            return
        self.script = json.loads(Path(ctx.options.e2e_script).read_text(encoding="utf-8"))
        self.record_path = Path(self.script["record"])
        self.db = sqlite3.connect(self.script["d1_sqlite"], check_same_thread=False)
        self.db.row_factory = sqlite3.Row

    def running(self):
        names = [name for name, *_ in self.routes]
        Path(self.script["routes_out"]).write_text(json.dumps(names), encoding="utf-8")
        Path(self.script["ready"]).write_text("ready", encoding="utf-8")

    def done(self):
        if self.db is not None:
            self.db.close()

    # ---- routing ---------------------------------------------------------

    def _routes(self):
        return [
            ("d1_query", "POST", CF, CF_ACCOUNT + r"/d1/database/[^/]+/query", self.d1_query),
            ("pages_deployments_list", "GET", CF, PAGES_PROJECT + r"/deployments", self.no_list),
            ("pages_upload_token", "GET", CF, PAGES_PROJECT + r"/upload-token", self.token),
            (
                "pages_check_missing",
                "POST",
                CF,
                r"/client/v4/pages/assets/check-missing",
                self.check_missing,
            ),
            ("pages_upload", "POST", CF, r"/client/v4/pages/assets/upload", self.upload),
            (
                "pages_upsert_hashes",
                "POST",
                CF,
                r"/client/v4/pages/assets/upsert-hashes",
                self.upsert,
            ),
            ("pages_deployment_create", "POST", CF, PAGES_PROJECT + r"/deployments", self.deploy),
            (
                "pages_deployment_get",
                "GET",
                CF,
                PAGES_PROJECT + r"/deployments/[^/]+",
                self.deployment,
            ),
            ("pages_project_create", "POST", CF, CF_ACCOUNT + r"/pages/projects", self.ok),
            ("pages_site", "GET", "pages_host", r"/.*", self.site),
            ("workers_domains", "GET", CF, CF_ACCOUNT + r"/workers/domains", self.domains),
            ("email_send", "POST", CF, CF_ACCOUNT + r"/email/sending/send", self.mail),
            ("discord_webhook", "POST", "discord.com", r"/api/webhooks/[^/]+/[^/]+", self.discord),
            (
                "gemini_generate",
                "POST",
                GEMINI,
                r"/v1beta/models/[^/:]+:generateContent",
                self.gemini,
            ),
            ("rss_articles", "GET", "rss_host", r"/articles", self.rss_articles),
            ("newsletter_list", "GET", "newsletter_host", r"/newsletters", self.newsletters),
            ("newsletter_ack", "POST", "newsletter_host", r"/ack", self.worker_ack),
            ("promote_list", "GET", "promote_host", r"/promotions", self.promotions),
            ("promote_ack", "POST", "promote_host", r"/ack", self.worker_ack),
        ]

    def _claim(self, request):
        """The route that answers this request, or None. A host key names a scripted host."""
        dropped = set(self.script.get("drop", []))
        for name, method, host, path, handler in self.routes:
            host = self.script["hosts"].get(host, host)
            if (
                name not in dropped
                and request.method == method
                and request.pretty_host == host
                and re.fullmatch(path, request.path.split("?", 1)[0])
            ):
                return name, handler
        return None, None

    def request(self, flow: http.HTTPFlow) -> None:
        request = flow.request
        route, handler = self._claim(request)
        entry = {
            "seq": self.seq,
            "route": route,
            "method": request.method,
            "host": request.pretty_host,
            "path": request.path.split("?", 1)[0],
            "query": [[k, v] for k, v in request.query.items(multi=True)],
            "headers": {k.lower(): v for k, v in request.headers.items()},
            "body": request.get_text(strict=False) or "",
        }
        self.seq += 1
        if request.headers.get("content-type", "").startswith("multipart/form-data"):
            entry["form"] = {
                k.decode(): v.decode() for k, v in request.multipart_form.items(multi=True)
            }
        if handler is None:
            status, body, content_type = _cf_error(502, "e2e: no fake claims this request")
        else:
            try:
                status, body, content_type = handler(request, entry)
            except Exception as e:  # noqa: BLE001 - a fake's bug must reach the record
                entry["error"] = f"{type(e).__name__}: {e}"
                status, body, content_type = _cf_error(500, entry["error"])
        entry["status"] = status
        with self.record_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        headers = {"Content-Type": content_type} if content_type else {}
        flow.response = http.Response.make(status, body, headers)

    # ---- D1 ----------------------------------------------------------------

    def d1_query(self, request, entry):
        payload = json.loads(request.get_text())
        sql, params = payload["sql"], payload.get("params") or []
        if len(params) > D1_MAX_BOUND_PARAMS:
            return _cf_error(400, "too many SQL variables: SQLITE_ERROR")
        if sql.upper().count(" UNION ALL ") >= D1_COMPOUND_SELECT_LIMIT:
            return _cf_error(400, "too many terms in compound SELECT: SQLITE_ERROR")
        try:
            try:
                cursor = self.db.execute(sql, params)
            except sqlite3.ProgrammingError as e:
                # The REST endpoint runs a whole script in one POST; sqlite3.execute
                # takes one statement, so a params-free script goes to executescript.
                if "one statement at a time" not in str(e) or params:
                    raise
                self.db.executescript(sql)
                self.db.commit()
                return _cf([{"results": [], "success": True, "meta": {"changes": 0}}])
            rows = [dict(row) for row in cursor.fetchall()]
            self.db.commit()
        except sqlite3.Error as e:
            return _cf_error(400, f"{e}: SQLITE_ERROR")
        meta = {"changes": max(cursor.rowcount, 0)}
        return _cf([{"results": rows, "success": True, "meta": meta}])

    # ---- Pages -------------------------------------------------------------

    def no_list(self, request, entry):
        return _cf([])

    def token(self, request, entry):
        return _cf({"jwt": self.script["pages_upload_jwt"]})

    def check_missing(self, request, entry):
        hashes = json.loads(request.get_text())["hashes"]
        return _cf([h for h in hashes if h not in self.assets])

    def upload(self, request, entry):
        for asset in json.loads(request.get_text()):
            self.assets[asset["key"]] = base64.b64decode(asset["value"])
        return _cf({"successful_key_count": len(self.assets)})

    def upsert(self, request, entry):
        return _cf(True)

    def _deployment_record(self):
        project = self.script["pages_project"]
        return {
            "id": "e2e-deployment",
            "url": f"https://e2e-deployment.{project}.pages.dev",
            "latest_stage": {"name": "deploy", "status": "success"},
        }

    def deploy(self, request, entry):
        self.deployed = json.loads(entry["form"]["manifest"])
        return _cf(self._deployment_record())

    def deployment(self, request, entry):
        return _cf(self._deployment_record())

    def ok(self, request, entry):
        return _cf({})

    def site(self, request, entry):
        """Serve the deployed bytes, by clean URL as Pages does."""
        path = entry["path"]
        for candidate in (path, f"{path}.html", f"{path.rstrip('/')}/index.html"):
            digest = self.deployed.get(candidate)
            if digest in self.assets:
                return 200, self.assets[digest], "text/html; charset=utf-8"
        return 404, b"not found", "text/plain"

    # ---- other Cloudflare APIs and Discord ---------------------------------

    def domains(self, request, entry):
        service = request.query.get("service", "")
        return _cf([{"hostname": h, "service": service} for h in self.script["worker_domains"]])

    def mail(self, request, entry):
        recipient = json.loads(request.get_text())["to"]
        return _cf({"delivered": [recipient], "permanent_bounces": [], "queued": []})

    def discord(self, request, entry):
        return 204, b"", None

    # ---- Workers -----------------------------------------------------------

    def rss_articles(self, request, entry):
        return _json(200, self.script["rss_rows"])

    def newsletters(self, request, entry):
        return _json(200, [_worker_item(Path(p)) for p in self.script["newsletters"]])

    def worker_ack(self, request, entry):
        return _json(200, {"ok": True})

    def promotions(self, request, entry):
        return _json(200, self.script["promotions"])

    # ---- Gemini ------------------------------------------------------------

    def gemini(self, request, entry):
        body = json.loads(request.get_text())
        system = body.get("system_instruction", {}).get("parts", [{}])[0].get("text", "")
        prompt = body["contents"][0]["parts"][0]["text"]
        kind = next((k for k, key in PROMPT_KINDS if key in system), None)
        articles = _prompt_articles(prompt, sourced=kind != "scoring")
        entry["llm"] = {"kind": kind, "articles": articles}
        if kind is None:
            return _json(
                400,
                {
                    "error": {
                        "code": 400,
                        "status": "INVALID_ARGUMENT",
                        "message": "e2e: unknown prompt kind",
                    }
                },
            )
        answer = _llm_answer(kind, prompt, articles, self.script["llm"])
        return _json(
            200,
            {
                "candidates": [
                    {
                        "content": {
                            "role": "model",
                            "parts": [{"text": json.dumps(answer, ensure_ascii=False)}],
                        },
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {"promptTokenCount": 1000, "candidatesTokenCount": 200},
            },
        )


def _prompt_articles(prompt, *, sourced):
    """[[id, source, title], ...] for each article the prompt lists, in order."""
    articles = []
    for line in prompt.splitlines():
        match = _PROMPT_LINE.match(line)
        if not match:
            continue
        article_id, rest = match.groups()
        source = None
        if sourced:
            sourced_match = _SOURCED_TITLE.match(rest)
            if sourced_match:
                source, rest = sourced_match.groups()
        articles.append([article_id, source, rest])
    return articles


def _llm_answer(kind, prompt, articles, rules):
    """A schema-valid answer built from the article positions the prompt names.

    `rules` is the test's script: a score per title, the clusters and the filter's
    picks by title, the summaries per title and per topic group, and the format the
    model "rewrites" a position into before echoing it.
    """
    rewrite = rules["rewrite_id"].format
    titles = [title for _, _, title in articles]
    if kind == "scoring":
        return {
            "scores": [
                {"id": article_id, "score": rules["scores"][title], "language": "en", "tags": []}
                for article_id, _, title in articles
                if title in rules["scores"]
            ]
        }
    if kind == "cluster":
        clusters, clustered = [], set()
        for cluster in rules["clusters"]:
            positions = [i for i, title in enumerate(titles) if title in cluster["titles"]]
            if positions:
                clustered.update(positions)
                clusters.append(
                    {
                        "heading": cluster["heading"],
                        "summary": cluster["summary"],
                        "article_ids": [rewrite(i) for i in positions],
                        "tags": cluster["tags"],
                    }
                )
        rest = [i for i in range(len(titles)) if i not in clustered]
        return {"clusters": clusters, "unclustered_ids": rest}
    if kind == "filter":
        picks = [t for t in rules["filter_select"] if t in titles]
        return {
            "selected": [
                {
                    "id": rewrite(titles.index(t)),
                    "title": t,
                    "summary": rules["filter_summaries"][t],
                    "source": articles[titles.index(t)][1],
                }
                for t in picks
            ],
            "rejected_count": len(titles) - len(picks),
        }
    tag = prompt.splitlines()[0].split(":", 1)[1].strip()
    group = rules["groups"][tag]
    return {
        "sections": [
            {
                "heading": group["heading"],
                "summary": group["summary"],
                "summaries": {
                    str(i): rules["own_summaries"][t]
                    for i, t in enumerate(titles)
                    if t in rules["own_summaries"]
                },
                "article_ids": list(range(len(titles))),
            }
        ]
    }


def _worker_item(eml):
    """One queued newsletter as workers/newsletter stores it, parsed from an .eml file."""
    message = message_from_bytes(eml.read_bytes(), policy=policy.default)
    html_part = message.get_body(preferencelist=("html",))
    text_part = message.get_body(preferencelist=("plain",))
    return {
        "id": f"nl:{eml.stem}",
        "from": message["From"].addresses[0].addr_spec,
        "subject": str(message["Subject"]),
        "html": html_part.get_content() if html_part else "",
        "text": text_part.get_content() if text_part else "",
        "date": parsedate_to_datetime(message["Date"]).isoformat(),
        "headers": {},
        "raw_size": len(eml.read_bytes()),
    }


addons = [Fakes()]
