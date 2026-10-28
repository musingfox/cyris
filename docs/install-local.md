# Installing cyris locally

A local install runs the whole pipeline on your machine: the digest is an HTML file on
disk, and the store is either JSON files or one SQLite file under `agent-vault/`. It is
the same pipeline, prompts and digest as the Cloudflare install. The store you pick in
step 3 decides how much of the web UI comes with it, and what no local install can do
is listed under [What a local install cannot do](#what-a-local-install-cannot-do).

## Prerequisites

- Python 3.12+ and [uv](https://github.com/astral-sh/uv)
- One LLM API key (Anthropic, Google Gemini, OpenAI, or a Cloudflare Workers AI token),
  or none: provider `none` lists plain excerpts
- Docker, only if you schedule with `docker compose`

## 1. Clone and install

```sh
git clone https://github.com/musingfox/cyris.git && cd cyris
uv sync
```

## 2. Copy the three example files

```sh
cp cyris.toml.example cyris.toml
cp .env.example .env
cp sources.example.yaml sources.yaml
```

- **`cyris.toml`** holds every runtime setting. Each runtime setting is required: one
  left out stops the run and is named, because no value for one lives in code. The
  deployment keys around them do have defaults, and one of those matters: without
  `[html_output] enabled = true` a run succeeds and writes no digest.
- **`.env`** holds secrets. Locally only the key for your LLM provider matters; leave
  the Cloudflare names blank.
- **`sources.yaml`** is the source list, and it starts empty: a new deployment ships
  with no feed ([ADR-0020](decisions/0020-new-deployments-start-from-onboarding.md)). The
  Cloudflare install asks for feeds in its onboarding wizard, and so does a local
  `"sqlite"` install (step 8); on `"json"` there is no wizard and you write the feeds here
  (step 3).

## 3. Edit `cyris.toml`

Change these; the rest works as shipped.

| Key | Set it to |
|---|---|
| `[llm_provider] provider` | `anthropic`, `gemini`, `openai`, `workers_ai` or `ai_gateway`, matching the key you put in `.env`; or `none` |
| `[llm_provider] model` | A model of that provider (the example is an Anthropic model), or `""` with `none` |
| `[general] timezone` | Your IANA zone, such as `Europe/Berlin`. The example is `Asia/Taipei` |
| `[general] digest_schedule` | The local hours to publish, such as `["08:00", "20:00"]`, read in that timezone |
| `[digest] output_language` | A BCP 47 tag for headlines and summaries, such as `en`. There is no default; the example uses `zh-Hant` |

`[notify] discord_webhook_url` and `[digest] style_prompt` are required keys for optional
features: set the webhook for a Discord message per digest and the prompt for your own tone
or focus, or keep each as `""` to leave it off. Keep `[html_output] enabled = true`, and
pick the store, which decides how much of the deployment you get:

| `[store] backend` | What you get |
|---|---|
| `"json"` | The smallest install. Settings in `cyris.toml`, sources in `sources.yaml`, articles as JSON files. `triage-ui` is read-only and there are no votes. |
| `"sqlite"` | The Cloudflare deployment's tables in one SQLite file (`agent-vault/cyris.db`): everything below — the writable `/settings`, the onboarding wizard, votes from the digest pages, `/labels`, `/interest`, and the archive over HTTP. |

`"json"` needs nothing else. For `"sqlite"`, do this once:

```sh
uv run cyris settings push   # cyris.toml's runtime settings into agent-vault/cyris.db
uv run cyris sources push    # sources.yaml's list into the same file
```

From then on the database is the settings' and the source list's one home, exactly as
D1 is on Cloudflare: a later edit to `cyris.toml` or `sources.yaml` changes nothing, and
`cyris doctor` fails on the now-ignored values until you delete them from the file. Feed
picking also works through the wizard (step 8) instead of `sources push`.

Then put the matching key in `.env` (`ANTHROPIC_API_KEY`, `GEMINI_API_KEY`,
`OPENAI_API_KEY`, `CLOUDFLARE_AI_TOKEN` plus `CLOUDFLARE_ACCOUNT_ID` for
`workers_ai`, or `CLOUDFLARE_AIG_TOKEN` plus `CLOUDFLARE_ACCOUNT_ID` for `ai_gateway`, which also
needs the provider's key stored in your gateway:
see [Running the LLM through AI Gateway](operations.md#running-the-llm-through-ai-gateway)).
Provider `openai_compatible` points at any endpoint that answers OpenAI's
chat-completions shape — a LAN or localhost service included. On `/settings`
the pair is a choice only: the endpoint, the model and the key all come from
the environment (`OPENAI_COMPATIBLE_BASE_URL`/`_MODEL`/`_API_KEY` for the LLM,
an `OPENAI_COMPATIBLE_EMBEDDING_*` set of the same three for the embedder,
keys optional), and each pair can be tried with one real call from its Test
button without saving.

On `"json"`, add the feeds you read under `sources:` in `sources.yaml`, replacing its
empty `[]`; `"sqlite"` keeps them in the database (step 8's wizard, or the `sources push`
above). The file's commented entries show each shape a source takes. For ready-made
entries, `src/cyris/onboarding_feeds.json` lists the publishers' own feeds the onboarding
wizard offers; each entry there is already a source in this shape but for `topics`, the
list's own grouping, which a source does not have (leave it out), and YAML reads a JSON
object as written, so a copied entry goes in as `- {"name": …, "url": …}`. Each source
takes a lane; see [How sources are processed](sources.md). Email-only newsletters
(`type: newsletter`) are inert locally. Until a source is listed, `cyris doctor` warns
that it is waiting for onboarding and every run ends having fetched nothing.

## 4. Check the configuration

```sh
uv run cyris doctor
```

It exits non-zero on anything that would break a run and says what to fix. Ends with
`Ready to run.` when there is nothing left.

## 5. Run the first digest

```sh
uv run cyris run --period morning
```

`--period` is `morning` or `evening` and names the issue; it defaults to `morning`, so
a second run on the same day without it rewrites the morning issue. Running again
inside the same window prints `No pending articles to process.`; `--force` reprocesses
the window.

Run every `cyris` command from the repo root. `--config` and `--sources` default to
`cyris.toml` and `sources.yaml` in the current directory, `.env` is read from beside the
config file, and `[agent_vault] path` and `[html_output] output_dir` are relative to the
current directory too.

## 6. Where the output lands

Everything is under `agent-vault/`, which git ignores:

- `agent-vault/html/<date>-<period>.html`: the digest, and `<date>-<period>-raw.html`:
  every article the run judged, including what the digest dropped
- `agent-vault/html/index.html`: the archive; open this one in a browser, or serve it
  with `triage-ui` (step 8)
- `agent-vault/cyris.db`: with `"sqlite"`, the settings, the sources, the articles and
  the run history in one file
- `agent-vault/articles/`: with `"json"`, the article store, and `agent-vault/usage.jsonl`
  (both backends), the LLM spend — written once a run calls an LLM, so absent with
  provider `none`

## 7. Schedule it

Pick one of the three, or of the first two on Windows. All run hourly with `--if-due`,
which does nothing except on the `digest_schedule` hours and picks `--period` from which
hour it is.

**cron.** Cron starts in your home directory with a minimal `PATH`, so change into the
repo and give `uv` by its absolute path (`command -v uv` prints it):

```cron
0 * * * * cd /path/to/cyris && /path/to/uv run cyris run --if-due >> "$HOME/cyris.log" 2>&1
```

A fixed `cyris run` line per hour, without `--if-due` or `--period`, labels every issue
`morning`, and the evening run overwrites that morning's digest.

**systemd (Linux).** User units, no root:

```sh
uv run cyris schedule install-systemd
```

This writes `cyris-run.service` and `cyris-run.timer` to `~/.config/systemd/user/`,
enables the timer and prints the `loginctl enable-linger` line to run once so the
tick survives logout. Pass `--dest DIR` to write the units without enabling them,
and `--buffer` to add the hourly feed poll the next paragraph describes.

**docker compose.**

```sh
docker compose up -d
```

This builds the image and runs supercronic inside it, which fires `cyris run --if-due`
and `cyris promote-sync` every hour. The container reads `.env` (the file must exist)
and bind-mounts `cyris.toml`, `sources.yaml` and `./agent-vault`, so the store and the
HTML stay on your disk. After editing `cyris.toml` or `sources.yaml`, run
`docker compose up -d --force-recreate`: a plain `up -d` keeps the container reading
the old file. The compose `TZ` only changes log timestamps; the schedule follows
`[general] timezone`.

With `"sqlite"` (or `"d1"`), set `[rss] local_buffer = true` and the digest reads the
same table the Cloudflare Worker fills — hourly, on this machine, by `cyris buffer`
instead of a cron line. A run then no longer polls at digest time: it reads what the
polls collected. With that store, `docker compose` (or systemd `--buffer`) can schedule
it; without one, `cyris buffer` prints that nothing reads the buffer it writes.

## 8. The web UI

```sh
uv run cyris triage-ui
```

On `"sqlite"` this is the deployment's surface, the one the app Worker fronts on
Cloudflare:

- `/settings` — the writable version of cyris.toml's runtime settings, the source list,
  and every other page's settings. The **Model** page names provider and model
  — an `openai_compatible` choice locks both, its environment naming them —
  with a **Test** button per pair under its own fields; the **Theme** page
  re-skins the settings page as it picks, and Save stores the palette
  (`default`, `light`, `one_dark`, `catppuccin`, `nord`, `tokyo_night`,
  `github_dark`), injected into every page on read, published issues included
- `/onboarding` — the feed-picking wizard, ending in a first run of its own
- `/` and `/<date>-<period>` — the archive and each issue, served out of the output
  directory, so the digest's 👍/👎 and its Settings link work, writing straight to the
  store — the Worker's KV hop and its `promote-sync` are the only parts skipped
- `POST /run` — start a run the same minute, as the Worker does for the wizard's first
  issue and a rerun; `triage-ui` is already running the host's `cyris`

Set `[promote] local_origin = "http://<host>:8766"` so a Discord notification or a digest
mail links to that server — with nothing publishing pages, there is no other URL to link.

On the `"json"` backend the server serves only `/settings`, `/labels` and `/interest`,
and they are read-only: `cyris.toml` and `sources.yaml` are the only place to change
anything. There is no onboarding wizard — `/onboarding` sends you back to `/settings`,
because the wizard writes settings this backend has nowhere to keep — and the digest
pages are not served.

Readers who need nothing but the pages get a smaller server, with no settings and
no votes to mistype a URL into:

```sh
uv run cyris serve --host <interface> --port 8767
```

It serves the same `/` and `/<date>-<period>` URLs out of the same directory on
either backend, reading each page from disk per request, and answers nothing
else: the vote probe among the 404s is what hides the digest's buttons on a
page it serves. Bind it to the interface your readers reach — `127.0.0.1`,
a LAN address, or a tailnet IP — which is all the access control it has.

## What a local install cannot do

- **Email-only newsletters.** Receiving mail needs `workers/newsletter` and your own
  domain with Cloudflare Email Routing. Newsletters that publish RSS are ordinary feeds
  and work locally.
- **A public URL.** The pages exist on your machine, not on Cloudflare Pages, so
  the `serve` origin is the only URL they have — reachable by whoever can reach
  that origin. The Cloudflare deployment's Access gate in front of the archive,
  and its Workers/Container runs off a published image, have no local
  counterpart.
- **Digest mail from nowhere.** A digest mail goes out through Cloudflare's Email
  service, which needs a Cloudflare account and an Email Routing address — locally as
  much as on Cloudflare. Discord messages need nothing but the webhook.

## Moving to Cloudflare later

The article store, the runtime settings and the source list can all be copied into D1.
The store and the settings are added without overwriting anything there; the source
list replaces D1's, so a source D1 holds that `sources.yaml` lacks is removed.

1. Do steps 1 to 3 of [the Cloudflare guide](install-cloudflare.md): the D1 database,
   the Pages project name and the API token.
2. Add `CYRIS_STORE_DATABASE_ID`, `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_API_TOKEN` to
   `.env`. Leave `[store] backend = "json"` for now. From here on, run wrangler in this
   directory with `--env-file /dev/null`, or it picks up that token.
3. Copy and compare:

   ```sh
   uv run cyris store migrate     # the JSON store into D1; safe to re-run
   uv run cyris store diff        # article by article; they agree when only local, only d1 and differing are all 0
   uv run cyris settings push     # every runtime setting D1 lacks, from cyris.toml
   uv run cyris sources push      # make D1's source table match sources.yaml, removals included
   ```

4. Stop the local scheduler: remove the cron line, or `docker compose down`. Two
   schedulers publishing one Pages project is the failure mode, and after the switch
   the two backends would split decisions that neither can merge back.
5. Continue the Cloudflare guide from step 4. Step 6 finds the settings and sources
   already in D1.

To keep using this clone's CLI afterwards, set it up as in
[Running the CLI against the deployment](install-cloudflare.md#running-the-cli-against-the-deployment).
A D1 deployment reads runtime settings from D1 only, and `cyris doctor` fails while a
`cyris.toml` still sets them.

## Troubleshooting

- **`Missing from cyris.toml: …`**: a key is missing. If the line before it is
  `Config file not found: cyris.toml`, the command ran outside the repo root; in cron,
  add `cd /path/to/cyris &&`.
- **doctor: `provider is anthropic but ANTHROPIC_API_KEY is empty`**: put the key in
  `.env`, or set `provider = "none"` and `model = ""`.
- **`waiting for onboarding`**: `sources.yaml` holds no source, so the run ends
  having fetched nothing. Add at least one source.
- **A run succeeds but `agent-vault/html/` stays empty**: set `[html_output] enabled =
  true`; left out, it defaults to off.
- **`Not a digest hour (…)`**: `--if-due` outside the schedule, which is normal. Use
  `--period` to run now.
- **The evening digest replaced the morning one**: the scheduled command lacks
  `--if-due`.
- **compose ignores an edit to `cyris.toml`**: `docker compose up -d --force-recreate`.
- **`Promotion sync not configured` every hour in the compose log**: harmless; there is
  no vote Worker locally.
- **doctor warns `rss buffer — not configured`**: with `"sqlite"`, set
  `[rss] local_buffer = true` and schedule `cyris buffer` hourly; on `"json"` a
  digest-time poll is the only option there is.
- **doctor says to set the Discord webhook on `/settings`**: on `"sqlite"` that is the
  place; on `"json"` it is `[notify] discord_webhook_url` in `cyris.toml`.
- **A `cyris.toml` edit changes nothing on `"sqlite"`**: the settings and sources live in
  `agent-vault/cyris.db` after the one-time push, and the file's values are the ignored
  ones `doctor` fails on. Edit them on `/settings`, or start over with
  `cyris.toml` + `cyris settings push`.
- **`systemctl --user` says the unit files don't exist**: the command wrote them for the
  user it ran as and the clone it ran in; re-run it from the clone, and check
  `loginctl enable-linger` so the timers survive logout.
