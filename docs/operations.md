# Operating a Cloudflare deployment

For a deployment that is already running. Installing it is
[install-cloudflare.md](install-cloudflare.md); the Worker itself is described in
[workers/app/README.md](../workers/app/README.md).

## Two ways to ship a new image

**`wrangler deploy` from a checkout.** The same command as the first install, from the
repo root:

```sh
bunx wrangler deploy --env-file /dev/null
```

It rebuilds the image locally and rolls the container. A deploy replaces running
instances, so avoid landing one just before a publish hour.

**The GitHub Actions release path.** Two workflows, both started by hand
(`workflow_dispatch`), never on push:

- `.github/workflows/release-image.yml` ("Release container image") runs
  `scripts/check.sh`, builds a `linux/amd64` image with the commit baked in
  (`GIT_SHA`), pushes it to the account's Cloudflare registry as `:<sha>` and
  `:release`, and records the image digest in a git tag `image/<short-sha>`. It
  skips the build and push for a commit it has already published, because the image
  is not reproducible.
- `.github/workflows/deploy.yml` ("Deploy the container Worker") deploys an image that
  is already in the registry. Its `image_tag` input is `release` by default, or a
  `sha256:…` digest to pin one exact image.

On a fork they need, under Settings → Secrets and variables → Actions:

| Kind | Name | Value |
|---|---|---|
| Variable | `CLOUDFLARE_ACCOUNT_ID` | Your account id |
| Secret | `CLOUDFLARE_CONTAINERS_TOKEN` | An API token with Workers Scripts → Edit plus the Containers edit permission |
| Variable | `CYRIS_DEPLOYMENT_URL` | The app Worker's `workers.dev` URL, for the deploy's check that production serves the new image |
| Secret | `CYRIS_UI_TOKEN` | The same value as the `cyris-app` Worker secret; replace both together when you rotate it |

`CLOUDFLARE_CONTAINERS_TOKEN` is for CI only and never enters the container. After `wrangler deploy`, the deploy workflow waits six minutes, logs in to `CYRIS_DEPLOYMENT_URL` and fails unless `/api/build` names the commit baked into the deployed image; it asks once more after another six minutes before giving up.

`scripts/check.sh` runs the test suite; the real-newsletter tests skip when their samples are absent.

Both paths push to the same registry repository, named by `[[containers]] name` in
`wrangler.toml`, so whichever deployed last is what runs.

## Which image is running

```sh
CYRIS_UI_TOKEN=<token> uv run cyris doctor --deployment https://cyris-app.<subdomain>.workers.dev
```

It logs in, asks the deployment which commit its image was built from, and compares
that with the checkout you run it in. It also prints a `last run` line: the commit,
time and status of the last production run, read from D1 `digest_runs`. It needs the
`.env` described in
[Running the CLI against the deployment](install-cloudflare.md#running-the-cli-against-the-deployment).

- An image built by `wrangler deploy` has no commit baked in, so the check fails with
  "runs an image that cannot name its commit". Only the release workflow bakes one.
- Use the `workers.dev` URL. Behind Cloudflare Access the login cannot succeed.
- The answer comes from whichever instance replied. A warm instance keeps reporting
  the image it started from until it sleeps after five idle minutes, and each request
  renews that timer, so asking again and again keeps the old answer alive.

## Going back

`wrangler rollback` restores the Worker's code and configuration but not the container
image; the image belongs to a separate container application that only
`wrangler deploy` updates. To put an earlier image back:

**On the release path**, deploy it by digest. The image cannot be rebuilt identically,
because the base image is a moving tag and the apt packages are unpinned, which is why
the release workflow refuses to republish a commit.

```sh
git fetch --tags
git tag -l --format='%(contents)' image/<short-sha>    # … digest=sha256:…
gh workflow run "Deploy the container Worker" -f image_tag=sha256:<digest>
```

The workflow's `verify` step confirms the rollout; `cyris doctor --deployment` answers the same question by hand, allowing for the warm-instance lag above.

**On the `wrangler deploy` path**, there is no recorded digest: check out the older
commit and `bunx wrangler deploy --env-file /dev/null` again. That builds a new image
from the old source, not the image that ran before.

Background: this was measured on a live deployment. The Worker was rolled back to a
version created before the running image was ever deployed, and 37 minutes later the
deployment still ran the newer image. `wrangler versions view --json` shows a
container's `class_name` and `name` and no image, and `--containers-rollout` exists on
`wrangler deploy` and on no other command.

## Cutting over from a local install

A local install and the Cloudflare deployment run the same pipeline. Once both point at
the same D1 and the same Pages project, **two schedulers publishing one archive is the
failure mode**: each publish is a full snapshot of the site, and the two would race.

1. Stop the local scheduler first: remove the cron line, or `docker compose down` in
   the local checkout.
2. Verify the cloud side: `POST /run?period=morning` (or `evening`) as in
   [step 7 of the install](install-cloudflare.md#7-run-the-first-digest-now), then read
   the `run_summary` line in Workers Logs and open the issue in the archive.
3. From then on, run `cyris` locally only for commands that do not publish:
   `doctor`, `articles`, `sources`, `settings push`, `store diff`.

## Routine

- **Rotating `CYRIS_UI_TOKEN`** ends every session at once; that is how a session is
  revoked. Set the new value with `wrangler secret put` and log in again.
- **Logs** are in Workers Logs for 7 days (`bunx wrangler tail cyris-app --env-file
  /dev/null` for live output). The permanent record of runs and spend is D1:
  `digest_runs` and `usage_log`.
