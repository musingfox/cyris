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

- **`uv`**: Python 3.12+ and [uv](https://github.com/astral-sh/uv) on macOS or Linux. Every
  command runs on this machine. Windows is not supported: the digest-run lock uses `fcntl` and the
  hourly schedule runs through `sh`. On Windows, use `docker compose` or WSL.
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
mkdir -p agent-vault
```

Your feeds are saved by the wizard, so there is no list of sources to copy.

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
issue is made anyway. The server reads `.env` when it starts. After an edit, with `uv`, stop it and run
`uv run cyris triage-ui` again; under compose, run `docker compose up -d --force-recreate`, because
`env_file` applies when a container is created and `docker compose restart` keeps the old values.

## 3. Start the server

**With `uv`:**

```sh
uv run cyris triage-ui
```

**With `docker compose`:**

```sh
docker compose up -d
```

This builds the image and starts two containers: `ui`, the server, and `cyris`, the scheduler
([step 5](#5-the-schedule)). The server is published on this machine only; set `CYRIS_UI_PORT` to publish another host
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

## 5. The schedule

There is nothing to set up. Both ways run hourly, on the hour: `cyris ingest` stores what the feeds
hold, then `cyris run --if-due` does nothing except on the hours you chose and picks `--period` from
which hour it is, then `cyris promote-sync` runs. A feed keeps only a few hours of entries, so without
the hourly `ingest` a digest misses what a busy feed dropped since the last one.

**The schedule runs only while the server or its container runs. A machine or a server that is off
fetches nothing**, and an hour it misses is not made up: what a feed dropped meanwhile is gone, and a
digest hour it slept through has no issue.

**With `uv`**, `uv run cyris triage-ui` runs the hour itself, by this machine's clock, and logs it where
the server logs. Keep it running from the repo root, as a login item or a service. Stopping it does not
stop a run already under way. If an earlier version of this guide had you add a cron line, remove it.
`--no-schedule` turns the schedule off, for a store something else schedules.

**With `docker compose`**, the `cyris` container runs supercronic, which fires the same commands, and
the `ui` container runs the server with `--no-schedule`. The compose `TZ` only changes log timestamps;
the schedule follows the timezone you saved.

To make an issue now, run `cyris run --period morning`; `--period` is `morning` or `evening` and names
the issue, and `--force` reprocesses a window already processed.

One store runs one `cyris run` at a time, whether the hourly tick or you started it. A
`cyris run --period morning` that starts while another is under way does nothing and says so in the
log. The hourly run waits instead: when a run you started by hand is still going at a digest hour, it
tries again every few seconds for up to 30 minutes, then makes that hour's issue unless it already
exists. The issue may come out late, after its hour has ended, and it keeps the date and period of the
hour the run started in, even when the wait crossed midnight. If the other run is still going after 30 minutes, the log says the hourly run waited and gave
up, and that hour has no issue; its articles wait for the next one. An hourly run whose issue already
exists does nothing, and `cyris run --period morning` is how to make that issue again.

`cyris regenerate --mode rerun --publish` takes no such lock, so do not start it while a run is
under way.

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

An empty `cyris.sqlite3.run-lock` sits beside the file on either install. It is what keeps one digest
at a time, holds nothing, and needs no backup.

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
([ADR-0025](decisions/0025-a-local-install-keeps-its-state-in-sqlite.md)). To copy it, see
[Backing up](#backing-up). After editing `cyris.toml`, run `docker compose up -d --force-recreate`: a
plain `up -d` keeps the container reading the old file.

## Backing up

The SQLite file holds your settings, feeds, articles, votes and run history. `.env`, `cyris.toml` and
`agent-vault/` are not in it, so copy them as ordinary files.

Do not `cp` the file: recent writes sit beside it in `cyris.sqlite3-wal`, and a copy of the file alone
loses them. `VACUUM INTO` writes one complete file, and it is safe while the server and the scheduler
are running. Python's `sqlite3` module runs it on both installs, and the image has no `sqlite3` tool.

**With `uv`** (with a `[store] sqlite_path` of your own, use that path):

```sh
uv run python -m sqlite3 state/cyris.sqlite3 "VACUUM INTO 'state/cyris-backup.sqlite3'"
```

If that answers `output file already exists`, move the last backup away and run it again.

**With `docker compose`:**

```sh
mkdir -p state
docker compose exec cyris python -m sqlite3 /app/state/cyris.sqlite3 "VACUUM INTO '/tmp/cyris-backup.sqlite3'"
docker compose cp cyris:/tmp/cyris-backup.sqlite3 ./state/cyris-backup.sqlite3
docker compose exec cyris rm /tmp/cyris-backup.sqlite3
```

The second command replaces an earlier `state/cyris-backup.sqlite3` on the host.

Either way the backup is `state/cyris-backup.sqlite3`, which git ignores. It holds your Discord webhook
URL and your mail addresses, so keep it as you keep `.env`. Copy it off this machine, because a copy on
the same disk does not survive that disk.

### Restoring

Stop every process that has the file open first. One left running writes its old state back over the
restored file. The steps below replace the store, so anything written after the backup is gone.

**With `uv`**, stop `uv run cyris triage-ui`, which stops the schedule, and wait for a run it started to
end: it carries on without the server, and `pgrep -fl "cyris (ingest|run|promote-sync)"` prints nothing
once it has. Then:

```sh
rm -f state/cyris.sqlite3-wal state/cyris.sqlite3-shm
cp state/cyris-backup.sqlite3 state/cyris.sqlite3
```

Start the server again, which starts the schedule again.

**With `docker compose`:**

```sh
docker compose stop
docker compose run --rm --no-deps --entrypoint sh -v ./state/cyris-backup.sqlite3:/tmp/cyris-backup.sqlite3:ro cyris -c 'rm -f /app/state/cyris.sqlite3-wal /app/state/cyris.sqlite3-shm && cp /tmp/cyris-backup.sqlite3 /app/state/cyris.sqlite3'
docker compose up -d
```

The same steps restore into a new clone, or after `docker compose down -v`: run `docker compose up -d`
once so that the volume exists, then follow them. Then run `cyris doctor`, which reports the feeds and
articles it finds.

## The archive and `/settings`

The server shows the archive at `http://127.0.0.1:8766/`: the issue list, each issue and its All
articles page, from `[html_output] output_dir`. It also serves
`http://127.0.0.1:8766/settings`, which changes every runtime setting and the source list; the
Settings and Archive links move between the two. A change takes effect on the next run.

Each issue and its All articles page show the 👍/👎 buttons and the Interest link, as the cloud
edition's do, an issue written by an earlier version included. The server writes a vote into the
SQLite file as you click: the article is accepted or rejected as your decision, which no run
changes, and the next run's vote similarity and order read it
([ADR-0044](decisions/0044-a-local-install-takes-votes.md)). A vote counts only through this
server: a page opened from disk has no buttons.

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

- **Email-only newsletters.** Receiving mail needs `workers/newsletter` and your own domain with
  Cloudflare Email Routing. Newsletters that publish RSS are ordinary feeds and work locally.
- **An archive you can reach from anywhere.** The server shows the archive only while it runs, at
  `127.0.0.1` by default, so another device cannot read it. `--host` with an address opens the archive
  and `/settings` to everyone who can reach that address, with no sign-in.

## Moving to Cloudflare later

The runtime settings and the source list can be copied into D1 from files; the articles in a SQLite
file cannot yet, because that copy is not built
([ADR-0025](decisions/0025-a-local-install-keeps-its-state-in-sqlite.md)). A Cloudflare install starts its
store empty.

1. Do steps 1 to 3 of [the Cloudflare guide](install-cloudflare.md): the D1 database, the Pages project
   name and the API token.
2. Stop the local scheduler: stop `uv run cyris triage-ui`, or `docker compose down`. Two schedulers
   publishing one Pages project is the failure mode. A server pointed at D1 does not schedule anything.
3. Continue the Cloudflare guide from step 4. Its wizard takes your settings and feeds again, or set the
   clone up as in [Running the CLI against the deployment](install-cloudflare.md#running-the-cli-against-the-deployment)
   and push them from files.

A D1 deployment reads runtime settings from D1 only, and `cyris doctor` fails while a `cyris.toml` still
sets them.

## Troubleshooting

- **`Missing settings in the SQLite store: …`**: the wizard has not finished. Open `/onboarding`, or set
  the keys on `/settings`. If the line before it is `Config file not found: cyris.toml`, the command ran
  outside the repo root; start a login item or a service for the server from there as well.
- **doctor: `config file — not found`**: the same, or a mistyped `--config`.
- **doctor: `article store (sqlite) — not created yet` on an install you already use**: the command is
  looking at another path. A relative `[store] sqlite_path` starts at the directory of `cyris.toml`.
- **`[store] sqlite_path … is inside the agent vault`**: set `[store] sqlite_path` to a file outside the
  vault. A vault at the repo root holds the default path too.
- **doctor: `provider is anthropic but ANTHROPIC_API_KEY is empty`**: put the key in `.env` and start the
  server again (under compose, `docker compose up -d --force-recreate`), or pick None on `/settings`.
- **`waiting for onboarding`**: the store holds no source, so the run ends having fetched nothing. Add one
  on `/onboarding` or `/settings`.
- **`Not a digest hour (…)`**: `--if-due` outside the schedule, which is normal. Use `--period` to run
  now.
- **`A digest is already running on this store`**, **`… issue already exists; this tick does
  nothing`** or **`Waited 30 minutes for another digest to finish`**: the rule of
  [step 5](#5-the-schedule). The first is a `--period` run refused while another is under way. The last
  means the other run was still going 30 minutes after the hourly run began waiting, so the hourly run
  gave up and that hour has no issue: when the other run ends, `cyris run --period morning` (or
  `evening`, whichever hour was missed) makes it.
- **The evening digest replaced the morning one**: a command you scheduled yourself lacks `--if-due`:
  without it or `--period`, every issue is labelled `morning`.
- **No new articles, and nothing in the log on the hour**: the server is not running, or it was started
  with `--no-schedule`.
- **`docker compose up` says a mount is not a file, or `cyris.toml` is a directory**: a source was missing
  the first time. Remove the directory Docker made, then do step 1's copies.
- **compose ignores an edit to `cyris.toml`**: `docker compose up -d --force-recreate`.
- **`Promotion sync not configured` every hour in the log**: harmless; the server takes your votes
  itself, so there is no vote Worker to pull them from.
- **doctor warns `rss buffer — not configured`**: informational on a local install, which runs `cyris
  ingest` hourly ([step 5](#5-the-schedule)).
