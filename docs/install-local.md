# Installing cyris locally

A local install runs the whole pipeline on your machine: feeds are polled when the
digest runs, state is JSON files under `agent-vault/`, and the digest is an HTML file on
disk. It is the same pipeline, prompts and digest as the Cloudflare install; what it
gives up is listed under [What a local install cannot do](#what-a-local-install-cannot-do).

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

- **`cyris.toml`** holds every runtime setting. Each key in it is required: a key left
  out stops the run and is named, because no value for one lives in code.
- **`.env`** holds secrets. Locally only the key for your LLM provider matters; leave
  the Cloudflare names blank.
- **`sources.yaml`** is the source list.

## 3. Edit `cyris.toml`

Change these; the rest works as shipped.

| Key | Set it to |
|---|---|
| `[llm_provider] provider` | `anthropic`, `gemini`, `openai` or `workers_ai`, matching the key you put in `.env`; or `none` |
| `[llm_provider] model` | A model of that provider (the example is an Anthropic model), or `""` with `none` |
| `[general] timezone` | Your IANA zone, such as `Europe/Berlin`. The example is `Asia/Taipei` |
| `[general] digest_schedule` | The local hours to publish, such as `["08:00", "20:00"]`, read in that timezone |
| `[digest] output_language` | A BCP 47 tag for headlines and summaries, such as `en`. There is no default; the example uses `zh-Hant` |

Optional: `[notify] discord_webhook_url` for a Discord message per digest, and
`[digest] style_prompt` for your own tone or focus. Keep `[store] backend = "json"` and
`[html_output] enabled = true`.

Then put the matching key in `.env` (`ANTHROPIC_API_KEY`, `GEMINI_API_KEY`,
`OPENAI_API_KEY`, or `CLOUDFLARE_AI_TOKEN` plus `CLOUDFLARE_ACCOUNT_ID` for
`workers_ai`), and replace the sample feeds in `sources.yaml` with yours. Each source
takes a tier; see [How sources are processed](../README.md#how-sources-are-processed).
Email-only newsletters (`type: newsletter`) are inert locally.

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

- `agent-vault/html/<date>-<period>.html`: the digest
- `agent-vault/html/<date>-<period>-raw.html`: every article the run judged, including
  what the digest dropped
- `agent-vault/html/index.html`: the archive; open this one in a browser
- `agent-vault/articles/`: the article store, and `agent-vault/usage.jsonl`, the LLM
  spend (written once a run calls an LLM, so absent with provider `none`)

## 7. Schedule it

Pick one of the two. Both run hourly with `--if-due`, which does nothing except on
the `digest_schedule` hours and picks `--period` from which hour it is.

**cron.** Cron starts in your home directory with a minimal `PATH`, so change into the
repo and give `uv` by its absolute path (`command -v uv` prints it):

```cron
0 * * * * cd /path/to/cyris && /path/to/uv run cyris run --if-due >> "$HOME/cyris.log" 2>&1
```

A fixed `cyris run` line per hour, without `--if-due` or `--period`, labels every issue
`morning`, and the evening run overwrites that morning's digest.

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

## 8. `/settings`, read-only

```sh
uv run cyris triage-ui
```

This serves `http://127.0.0.1:8766/settings`, which shows every runtime setting and the
source list. On the `json` backend the page is read-only: `cyris.toml` and
`sources.yaml` are the only place to change anything. It does not serve the digest
pages, so the Settings link on a page opened from disk goes nowhere.

## What a local install cannot do

- **Votes.** 👍/👎 go through the Cloudflare app Worker and `workers/promote`. Locally,
  `cyris articles accept|reject` records the same decision.
- **Email-only newsletters.** Receiving mail needs `workers/newsletter` and your own
  domain with Cloudflare Email Routing. Newsletters that publish RSS are ordinary feeds
  and work locally.
- **An archive with a URL.** The archive is a file on your disk.
- **The RSS buffer.** A feed only publishes its current snapshot, and a busy one holds
  2–4 hours of it, so a digest-time poll misses part of a 24-hour window. The buffer
  (`workers/rss`) closes that gap, but it reads its feed list from D1 and needs Workers
  Paid, so it belongs to the Cloudflare track.

## Moving to Cloudflare later

The article store, the runtime settings and the source list can all be copied into D1
without overwriting anything there.

1. Do steps 1 to 3 of [the Cloudflare guide](install-cloudflare.md): the D1 database,
   the Pages project name and the API token.
2. Add `CYRIS_STORE_DATABASE_ID`, `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_API_TOKEN` to
   `.env`. Leave `[store] backend = "json"` for now. From here on, run wrangler in this
   directory with `--env-file /dev/null`, or it picks up that token.
3. Copy and compare:

   ```sh
   uv run cyris store migrate     # the JSON store into D1; safe to re-run
   uv run cyris store diff        # article by article; silence means they agree
   uv run cyris settings push     # every runtime setting D1 lacks, from cyris.toml
   uv run cyris sources push      # make D1's source table match sources.yaml
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
- **`No sources in sources.yaml.`**: add at least one source.
- **`Not a digest hour (…)`**: `--if-due` outside the schedule, which is normal. Use
  `--period` to run now.
- **The evening digest replaced the morning one**: the scheduled command lacks
  `--if-due`.
- **compose ignores an edit to `cyris.toml`**: `docker compose up -d --force-recreate`.
- **`Promotion sync not configured` every hour in the compose log**: harmless; there is
  no vote Worker locally.
- **doctor warns `rss buffer — not configured`**: informational on a local install.
- **doctor says to set the Discord webhook on `/settings`**: locally it is
  `[notify] discord_webhook_url` in `cyris.toml`.
