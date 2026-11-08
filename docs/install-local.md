# Installing cyris locally

A local install runs the whole pipeline on your machine: feeds are polled hourly and when the
digest runs, state is one SQLite file, and the digest is an HTML page its own server shows you. It
is the same pipeline, prompts and digest as the Cloudflare install; what it gives up is listed under
[What a local install cannot do](#what-a-local-install-cannot-do).

You copy one file, put your keys in another, start the server and answer the wizard. Everything
else, your settings and your feeds included, is saved through the wizard into the SQLite file
([ADR-0025](decisions/0025-a-local-install-keeps-its-state-in-sqlite.md)).

## Prerequisites

Either of these runs the same install:

- **`uv`**: Python 3.12+ and [uv](https://github.com/astral-sh/uv). Every command runs on this
  machine.
- **`docker compose`**: Docker with the compose plugin. Every command runs in a container.

The file is opened from one side only, so pick one and stay on it: see
[Where the state is](#where-the-state-is).

## 1. Clone and copy the example

```sh
git clone https://github.com/musingfox/cyris.git && cd cyris
cp cyris.toml.example cyris.toml
```

`cyris.toml` works as shipped, so there is nothing to edit. It names the `sqlite` store and turns
on the HTML pages, and it holds no runtime setting: those live in the store, and `cyris doctor`
fails a file that sets one, because nothing reads it there.

**With `uv`**, install the dependencies:

```sh
uv sync
```

**With `docker compose`**, the two containers bind-mount these, and a missing source would stop
them: Docker creates a directory where the file should be, and refuses to start without `.env`.

```sh
cp .env.example .env
cp sources.example.yaml sources.yaml
mkdir -p agent-vault
```

A SQLite install does not read `sources.yaml`; your feeds are saved by the wizard. The file only
has to exist for the mount, and the copy is empty.

## 2. Put your keys in `.env`

The wizard saves your settings but never takes a secret, so keys come from `.env`, which git
ignores. With `uv`, start it the same way, `cp .env.example .env`. Set what you use, and leave the
rest blank:

| You want | Set |
|---|---|
| A model to write the digest | One of `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `OPENAI_API_KEY`, `CLOUDFLARE_AI_TOKEN` plus `CLOUDFLARE_ACCOUNT_ID` for `workers_ai`, or `CLOUDFLARE_AIG_TOKEN` plus `CLOUDFLARE_ACCOUNT_ID` for `ai_gateway`, which also needs the provider's key stored in your gateway: see [Running the LLM through AI Gateway](operations.md#running-the-llm-through-ai-gateway) |
| Your votes to matter | An embedder's key: `GEMINI_API_KEY`, or `CLOUDFLARE_EMBEDDING_API_TOKEN` plus `CLOUDFLARE_ACCOUNT_ID` |
| A digest by mail | `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_API_TOKEN`, a token carrying Email Sending: Edit |

None of these is required. With no model key the wizard offers provider None, and the digest lists
plain excerpts. With no embedder key the first-issue step says vote similarity is still off, and the
issue is made anyway. The server reads `.env` when it starts, so restart it after an edit.

## 3. Start the server

**With `uv`:**

```sh
uv run cyris triage-ui
```

**With `docker compose`:**

```sh
docker compose up -d
```

This builds the image and starts two containers: `ui`, the server, and `cyris`, the scheduler of
step 5. The server is published on this machine only; set `CYRIS_UI_PORT` to publish another host
port.

Either way the wizard is now at <http://127.0.0.1:8766/onboarding>.

## 4. Walk the wizard

It asks for your language and timezone, the hours to publish, your feeds, a few votes on their
articles, where to notify you, and a model, and it pre-fills the tuned settings it does not ask
about. Its last step starts the first issue and shows it to you. Nothing is written by hand.

To check an install, `uv run cyris doctor`, or under compose `docker compose exec cyris cyris doctor`.
It exits non-zero on anything that would break a run, says what to fix, and ends with `Ready to run.`
when there is nothing left. It creates nothing.

Before the wizard, one failure is expected: `settings` names every key the wizard is about to
save, and `sources` warns that it is waiting for onboarding. Before the server's first start,
`article store (sqlite)` also says `not created yet`, because the server creates the file. If it
says that afterwards, the command is looking in another directory.

## 5. Schedule it

Both ways run hourly: `cyris ingest` stores what the feeds hold, then `cyris run --if-due` does
nothing except on the hours you chose and picks `--period` from which hour it is. A feed keeps only
a few hours of entries, so without the hourly `ingest` a digest misses what a busy feed dropped since
the last one.

**With `docker compose`** there is nothing to do: the `cyris` container runs supercronic, which fires
`cyris ingest`, then `cyris run --if-due`, and `cyris promote-sync` every hour. The compose `TZ` only
changes log timestamps; the schedule follows the timezone you saved.

**With `uv`**, use cron. Cron starts in your home directory with a minimal `PATH`, so change into the
repo and give `uv` by its absolute path (`command -v uv` prints it):

```cron
0 * * * * cd /path/to/cyris && { /path/to/uv run cyris ingest; /path/to/uv run cyris run --if-due; } >> "$HOME/cyris.log" 2>&1
```

Keep `uv run cyris triage-ui` running as well, as a login item or a service, to read the archive.

A fixed `cyris run` line per hour, without `--if-due` or `--period`, labels every issue `morning`, and
the evening run overwrites that morning's digest. To make an issue now, run `cyris run --period morning`;
`--period` is `morning` or `evening` and names the issue, and `--force` reprocesses a window already
processed.

## Where the state is

**With `uv`**, the state is `state/cyris.sqlite3` beside `cyris.toml`, which git ignores. It holds your
Discord webhook URL and your mail addresses, so do not share it. `--config` and `--sources` default to
`cyris.toml` and `sources.yaml` in the current directory; `.env` and a relative `[store] sqlite_path`
are found beside the config, and `[agent_vault] path` and `[html_output] output_dir` are relative to the
current directory, so run every command from the repo root. The pages are under `agent-vault/`, which
git also ignores:

- `agent-vault/html/<date>-<period>.html`: the digest
- `agent-vault/html/<date>-<period>-raw.html`: every article the run judged, including what the
  digest dropped
- `agent-vault/html/index.html`: the archive, which the server shows at `http://127.0.0.1:8766/`

**With `docker compose`**, both containers run on one file, `/app/state/cyris.sqlite3`, in the named
volume `cyris-state`, and `./agent-vault` is bind-mounted for the pages. Leave `[store] sqlite_path` and
`CYRIS_STORE_SQLITE_PATH` unset: a path outside `/app/state` puts the file in the container's own layer,
and a recreate loses it. The volume survives `docker compose down` and `up -d --force-recreate`; only
`docker compose down -v` deletes it. Every command runs in a container:

```sh
docker compose exec cyris cyris doctor
docker compose exec cyris cyris run --period morning
```

Never open that file from the host, with `sqlite3`, a database browser or `cp` out of the volume's
directory, and never bind-mount it: a macOS host process and a container on one file corrupt it, even
when the host only reads
([ADR-0025](decisions/0025-a-local-install-keeps-its-state-in-sqlite.md)). Backup steps come in a later
change; until then a `cp` of the file can lose recent writes. After editing `cyris.toml`, run
`docker compose up -d --force-recreate`: a plain `up -d` keeps the container reading the old file.

## The archive and `/settings`

The server shows the archive at `http://127.0.0.1:8766/`: the issue list, each issue and its All
articles page, from `[html_output] output_dir`. It also serves
`http://127.0.0.1:8766/settings`, which changes every runtime setting and the source list; the
Settings and Archive links move between the two. A change takes effect on the next run. The vote
buttons and the Interest map stay hidden here (see below); an issue written by an earlier version
still shows its vote buttons, and a click reports a failure.

Set `[html_output] base_url = "http://127.0.0.1:8766"` in `cyris.toml` and each notification carries
a Read online link to its issue. That address opens only on this machine, so with it empty, the
default, a notification has no link. A page opened from disk, rather than through this server, has a
Settings link that goes nowhere, and keeps the type size 1.

The server answers only requests that name it, so a web page open in your browser cannot read your
settings or spend your LLM key through it. A request whose `Host` is not `127.0.0.1`, `localhost` or
`[::1]` (any port), or the exact address given to `--host`, gets a `403` naming what was refused, and
so does a write from a page this server did not serve. curl and scripts that send no `Origin` are not
held to the second rule.

`--host` takes one address: with `--host 192.168.1.5`, the server answers at
`http://192.168.1.5:8766`, and a name for that machine, such as a Tailscale or `.local` name, gets
the `403`. `--host 0.0.0.0` listens everywhere and still answers only on the three loopback names, so
another device reaching it gets the `403`.

## What a local install cannot do

- **Votes on an issue.** 👍/👎 on a digest go through the Cloudflare app Worker and `workers/promote`.
  Locally, the wizard's votes and `cyris articles accept|reject` record the same decision.
- **Email-only newsletters.** Receiving mail needs `workers/newsletter` and your own domain with
  Cloudflare Email Routing. Newsletters that publish RSS are ordinary feeds and work locally.
- **An archive you can reach from anywhere.** The server shows the archive only while it runs, at
  `127.0.0.1` by default, so another device cannot read it. `--host` with an address opens the archive
  and `/settings` to everyone who can reach that address, with no sign-in.

## Moving a json install to SQLite

An install that keeps its state in JSON files under `agent-vault/` moves all of it into the SQLite file
with one command: the articles, `topics.json`, `usage.jsonl`, the runtime settings and
`[[tracked_topics]]` from `cyris.toml`, and `sources.yaml`. It changes nothing on the json side.

1. **Name `backend = "json"` under `[store]` in `cyris.toml`, if it does not say so.** With no line the
   install now reads SQLite, and the move refuses, because it finds no json install to read.
2. Stop the scheduler and the server, so nothing writes the json files during the move.
3. Copy and compare, from the repo root:

   ```sh
   uv run cyris store migrate --to sqlite   # everything into the file; safe to re-run
   uv run cyris store diff --to sqlite      # they agree when only local and differing are 0
   ```

   Under compose, in a one-off container, which mounts the same volumes:

   ```sh
   docker compose stop
   docker compose run --rm cyris cyris store migrate --to sqlite
   ```

   The move only adds, and a row the file already holds is kept and named if it differs. Anything
   that cannot be read stops it before it writes, naming what.
4. Set `backend = "sqlite"`, and delete the runtime settings and `[[tracked_topics]]` from `cyris.toml`:
   `cyris doctor` fails `file settings` while the file still sets one, and the move's last lines name
   them. Keep a copy of `agent-vault/`, `cyris.toml` and `sources.yaml` until you trust the result.
5. Start the scheduler and the server again, under compose with `docker compose up -d --force-recreate`.
   From here on `/settings` changes everything, and `sources.yaml` is no longer read.

## Moving to Cloudflare later

The runtime settings and the source list can be copied into D1 from files; the articles in a SQLite
file cannot yet, because that copy is not built
([ADR-0025](decisions/0025-a-local-install-keeps-its-state-in-sqlite.md)). A Cloudflare install starts its
store empty.

1. Do steps 1 to 3 of [the Cloudflare guide](install-cloudflare.md): the D1 database, the Pages project
   name and the API token.
2. Stop the local scheduler: remove the cron line, or `docker compose down`. Two schedulers publishing
   one Pages project is the failure mode.
3. Continue the Cloudflare guide from step 4. Its wizard takes your settings and feeds again, or set the
   clone up as in [Running the CLI against the deployment](install-cloudflare.md#running-the-cli-against-the-deployment)
   and push them from files.

A D1 deployment reads runtime settings from D1 only, and `cyris doctor` fails while a `cyris.toml` still
sets them.

## Troubleshooting

- **`Missing settings in the SQLite store: …`**: the wizard has not finished. Open `/onboarding`, or set
  the keys on `/settings`. If the line before it is `Config file not found: cyris.toml`, the command ran
  outside the repo root; in cron, add `cd /path/to/cyris &&`.
- **doctor: `config file — not found`**: the same, or a mistyped `--config`.
- **doctor: `article store (sqlite) — not created yet` on an install you already use**: the command is
  looking at another path. A relative `[store] sqlite_path` starts at the directory of `cyris.toml`.
- **`[store] sqlite_path … is inside the agent vault`**: set `[store] sqlite_path` to a file outside the
  vault. A vault at the repo root holds the default path too.
- **doctor: `provider is anthropic but ANTHROPIC_API_KEY is empty`**: put the key in `.env` and restart
  the server, or pick None on `/settings`.
- **`waiting for onboarding`**: the store holds no source, so the run ends having fetched nothing. Add one
  on `/onboarding` or `/settings`.
- **`Not a digest hour (…)`**: `--if-due` outside the schedule, which is normal. Use `--period` to run
  now.
- **The evening digest replaced the morning one**: the scheduled command lacks `--if-due`.
- **`docker compose up` says a mount is not a file, or `cyris.toml` is a directory**: a source was missing
  the first time. Remove the directory Docker made, then do step 1's copies.
- **compose ignores an edit to `cyris.toml`**: `docker compose up -d --force-recreate`.
- **`Promotion sync not configured` every hour in the compose log**: harmless; there is no vote Worker
  locally.
- **doctor warns `rss buffer — not configured`**: informational on a local install that runs `cyris
  ingest` hourly, as step 5 schedules it.
