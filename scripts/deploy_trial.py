#!/usr/bin/env python3
"""Deploy the image a release recorded to one trial, and check the trial serves it.

The staging step of a release (docs/operations.md): after "Release container
image" has run on a commit, this deploys that commit's image to the trial before
anyone deploys production. The digest comes from the commit's `image/<sha7>` tag,
read the way `.github/workflows/deploy.yml` reads it: the tag is fetched by name,
and the digest is taken only from a message naming this exact commit.

The commit is this checkout's HEAD, and the tracked tree must be clean: wrangler
bundles each Worker's code from the checkout, so a checkout on another commit
would pair the image with another commit's Worker. The trial's app config is
rendered again with the new digest from the wizard's saved inputs
(`.env.trial-<slug>-wizard`), whose IMAGE_TAG and IMAGE_DIGEST are then updated,
so a later wizard run does not render the old image back.

The rss and vote Workers deploy too when their directory changed since the commit
each was last deployed from, or when --rss or --promote asks; --app-only skips
both. That commit is kept per Worker in the wizard state (RSS_WORKER_COMMIT,
PROMOTE_WORKER_COMMIT) and moves only when that Worker deploys, so a skipped or
failed Worker deploy is offered again next time; before the first run it is the
commit IMAGE_TAG names, the one the wizard provisioned with.

After the deploy it waits, asks https://<slug>.<domain>/api/build through Access
with the service token in `.env.trial-<slug>.access`, and fails unless it names
this commit. The wait is
deploy.yml's: a warm instance keeps answering from the image it started with.

--dry-run resolves the image, renders the config and runs `wrangler deploy
--dry-run` for each Worker it would deploy; it deploys nothing and saves nothing.

Usage:
  deploy_trial.py --slug S [--rss] [--promote] [--app-only] [--dry-run] [--wait SECONDS]
"""

import argparse
import re
import subprocess
import sys
import time
from collections.abc import Callable

import httpx
import provision_trial
from provision_trial import wrangler, wrangler_env

ROOT = provision_trial.ROOT
WAIT_SECONDS = 360
ATTEMPTS = 2
REQUEST_TIMEOUT = 120
# The service token's two values, as the trial wizard saves them, and the header each goes in.
ACCESS_HEADERS = {
    "CYRIS_ACCESS_CLIENT_ID": "CF-Access-Client-Id",
    "CYRIS_ACCESS_CLIENT_SECRET": "CF-Access-Client-Secret",
}
WORKER_DIRS = {"rss": "workers/rss", "promote": "workers/promote"}
STATE_KEYS = ("DOMAIN", "ACCOUNT_ID", "TRIAL_KV_ID", "TRIAL_D1_ID")
BASELINE_KEYS = {"rss": "RSS_WORKER_COMMIT", "promote": "PROMOTE_WORKER_COMMIT"}
UNKNOWN = "unknown"


class DeployError(Exception):
    pass


Runner = Callable[[list[str]], subprocess.CompletedProcess]


def image_tag(commit: str) -> str:
    return f"image/{commit[:7]}"


def recorded_digest(message: str, commit: str) -> str | None:
    """The digest an `image/<sha7>` tag message records for exactly `commit`, else None.

    The same match as deploy.yml's resolve step, so a sha7 collision cannot hand over
    another commit's image.
    """
    found = re.search(rf"commit={re.escape(commit)} digest=(sha256:[0-9a-f]{{64}})", message)
    return found.group(1) if found else None


def build_matches(online: str, commit: str) -> bool:
    """Whether /api/build's git_sha names the deployed commit. An empty sha names none."""
    return bool(online) and online == commit


def workers_to_deploy(
    changed: dict[str, bool | None], forced: set[str], app_only: bool
) -> dict[str, str]:
    """Each optional Worker deployed, with why. `changed[w]` is None when it cannot be told."""
    if app_only:
        return {}
    out = {}
    for worker, diff in changed.items():
        if worker in forced:
            out[worker] = "asked for"
        elif diff is None:
            out[worker] = "the commit it was last deployed from is unknown"
        elif diff:
            out[worker] = f"{WORKER_DIRS[worker]}/ changed"
    return out


def write_state(slug: str, values: dict[str, str]) -> None:
    """Replace or append KEY=value lines, as the wizard's write_env does."""
    path = ROOT / f".env.trial-{slug}-wizard"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    kept = [line for line in lines if line.split("=", 1)[0] not in values]
    path.write_text("\n".join([*kept, *(f"{k}={v}" for k, v in values.items())]) + "\n")


def fetch_build_sha(base: str, headers: dict[str, str], client: httpx.Client | None = None) -> str:
    """Ask the trial through Access, with its service token, which commit its image is from."""
    with client or httpx.Client(timeout=REQUEST_TIMEOUT, follow_redirects=False) as c:
        build = c.get(f"{base}/api/build", headers=headers)
        if build.status_code != 200:
            raise DeployError(f"{base}/api/build answered {build.status_code}")
        return str(build.json().get("git_sha", "")).strip()


class Deploy:
    def __init__(self, args, run: Runner, say: Callable[[str], None]) -> None:
        self.args = args
        self.slug = args.slug
        self._run = run
        self.say = say

    def sh(self, cmd: list[str], *, check: bool = True) -> str:
        done = self._run(cmd)
        if check and done.returncode != 0:
            raise DeployError(f"{' '.join(cmd[:4])} … failed ({done.returncode}): {done.stderr}")
        return done.stdout.strip() if done.returncode == 0 else ""

    def commit(self) -> str:
        commit = self.sh(["git", "rev-parse", "HEAD"])
        dirty = self.sh(["git", "status", "--porcelain", "--untracked-files=no"])
        if dirty:
            raise DeployError(
                f"the checkout has uncommitted changes, so it is not {commit}:\n{dirty}"
            )
        return commit

    def digest(self, commit: str) -> str:
        tag = image_tag(commit)
        self.sh(
            ["git", "fetch", "-q", "--no-tags", "origin", f"+refs/tags/{tag}:refs/tags/{tag}"],
            check=False,
        )
        message = self.sh(["git", "tag", "-l", "--format=%(contents)", tag], check=False)
        digest = recorded_digest(message, commit)
        if digest is None:
            raise DeployError(
                f"no {tag} tag records an image for {commit}; run the release"
                " (Release container image) on this commit first"
            )
        return digest

    def baselines(self, state: dict[str, str]) -> dict[str, str | None]:
        """The commit each optional Worker was last deployed from; None when unknown.

        IMAGE_TAG is only the fallback for a state this script has not written yet:
        it moves with every app deploy, which a skipped or failed Worker deploy does not.
        """
        out: dict[str, str | None] = {}
        for worker, key in BASELINE_KEYS.items():
            if key in state:
                out[worker] = None if state[key] == UNKNOWN else state[key]
            elif tag := state.get("IMAGE_TAG"):
                verify = ["git", "rev-parse", "-q", "--verify", f"{tag}^{{commit}}"]
                out[worker] = self.sh(verify, check=False) or None
            else:
                out[worker] = None
        return out

    def changed(self, baselines: dict[str, str | None], commit: str) -> dict[str, bool | None]:
        out: dict[str, bool | None] = {}
        for worker, path in WORKER_DIRS.items():
            before = baselines[worker]
            if not before:
                out[worker] = None
                continue
            done = self._run(["git", "diff", "--quiet", before, commit, "--", path])
            out[worker] = None if done.returncode not in (0, 1) else done.returncode == 1
        return out

    def render(self, state: dict[str, str], digest: str) -> dict[str, dict]:
        missing = [k for k in STATE_KEYS if not state.get(k)]
        if missing:
            raise DeployError(
                f".env.trial-{self.slug}-wizard lacks {', '.join(missing)}: run the wizard first"
            )
        code = provision_trial.main(
            [
                "render",
                "--slug",
                self.slug,
                "--domain",
                state["DOMAIN"],
                "--account-id",
                state["ACCOUNT_ID"],
                "--image-digest",
                digest,
                "--kv-id",
                state["TRIAL_KV_ID"],
                "--d1-id",
                state["TRIAL_D1_ID"],
            ]
        )
        if code != 0:
            raise DeployError("provision_trial.py render failed")
        try:
            configs = provision_trial.load_rendered(self.slug)
        except provision_trial.ProductionNameError as exc:
            raise DeployError(f"refused: {exc}") from None
        image = configs["app"]["containers"][0]["image"]
        if not image.endswith(f"@{digest}"):
            raise DeployError(f"the rendered app config names {image}, not {digest}")
        return configs

    def deploy(self, kind: str) -> None:
        config = provision_trial.rendered_paths(self.slug)[kind]
        cmd = wrangler(config, "deploy")
        # As deploy.yml: a gradual rollout would leave two images answering /api/build.
        if kind == "app":
            cmd += ["--containers-rollout", "immediate"]
        if self.args.dry_run:
            cmd.append("--dry-run")
        self.say("$ " + " ".join(cmd))
        self.sh(cmd)

    def access_headers(self) -> dict[str, str]:
        name = f".env.trial-{self.slug}.access"
        values = provision_trial.env_file(name)
        missing = [var for var in ACCESS_HEADERS if not values.get(var)]
        if missing:
            raise DeployError(f"{name} holds no {' or '.join(missing)} to sign in with")
        return {header: values[var] for var, header in ACCESS_HEADERS.items()}

    def verify(
        self,
        commit: str,
        base: str,
        headers: dict[str, str],
        fetch: Callable[[str, dict[str, str]], str],
        sleep: Callable[[float], None],
    ) -> None:
        online = ""
        for attempt in range(1, ATTEMPTS + 1):
            self.say(f"waiting {self.args.wait}s for the rollout (attempt {attempt}/{ATTEMPTS})")
            sleep(self.args.wait)
            try:
                online = fetch(base, headers)
            # ValueError: a 200 whose body is not JSON, such as a page served while rolling.
            except (DeployError, httpx.HTTPError, ValueError) as exc:
                self.say(f"  {exc}")
                online = ""
            self.say(f"  {base} reports {online or 'nothing'}, deployed {commit}")
            if build_matches(online, commit):
                return
        raise DeployError(f"{base} does not report the deployed commit {commit}")


def _snapshot(slug: str) -> dict[str, bytes]:
    paths = provision_trial.rendered_paths(slug).values()
    return {rel: (ROOT / rel).read_bytes() for rel in paths}


def _restore(snapshot: dict[str, bytes]) -> None:
    for rel, data in snapshot.items():
        (ROOT / rel).write_bytes(data)


def _runner(env: dict[str, str]) -> Runner:
    def run(cmd: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT, env=env)

    return run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog=__doc__.split("\n\n", 1)[1],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--slug", required=True, help="the trial's slug, e.g. trial1")
    parser.add_argument("--rss", action="store_true", help="deploy the rss Worker too")
    parser.add_argument("--promote", action="store_true", help="deploy the vote Worker too")
    parser.add_argument("--app-only", action="store_true", help="deploy the app Worker alone")
    parser.add_argument("--dry-run", action="store_true", help="deploy nothing, save nothing")
    parser.add_argument("--wait", type=float, default=WAIT_SECONDS, help="seconds per attempt")
    return parser


def main(
    argv: list[str] | None = None,
    run: Runner | None = None,
    say: Callable[[str], None] = print,
    fetch: Callable[[str, dict[str, str]], str] = fetch_build_sha,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    args = build_parser().parse_args(argv)
    if args.app_only and (args.rss or args.promote):
        print("--app-only cannot be combined with --rss or --promote", file=sys.stderr)
        return 2
    try:
        provision_trial.load_rendered(args.slug)
        step = Deploy(args, run or _runner(wrangler_env(args.slug)), say)
        commit = step.commit()
        # Read before anything deploys, so a deploy that cannot be checked never starts.
        headers = {} if args.dry_run else step.access_headers()
        digest = step.digest(commit)
        say(f"{image_tag(commit)}: {commit} -> {digest}")
        state = provision_trial.wizard_state(args.slug)
        rendered = _snapshot(args.slug)
        app_deployed = False
        try:
            configs = step.render(state, digest)
            forced = {w for w in WORKER_DIRS if getattr(args, w)}
            baselines = step.baselines(state)
            changed = step.changed(baselines, commit)
            extra = workers_to_deploy(changed, forced, args.app_only)
            for worker in WORKER_DIRS:
                why = f"deploy ({extra[worker]})" if worker in extra else "unchanged, skipped"
                say(f"{worker} Worker: {why}")
            step.sh(["bun", "install", "--frozen-lockfile"])
            step.deploy("app")
            app_deployed = not args.dry_run
        finally:
            # The configs on disk keep naming the image the trial runs.
            if not app_deployed:
                _restore(rendered)
        if app_deployed:
            # IMAGE_TAG moves now; each Worker's baseline is pinned first, so it does not.
            pinned = {
                key: baselines[worker] or UNKNOWN
                for worker, key in BASELINE_KEYS.items()
                if key not in state
            }
            image = {"IMAGE_TAG": image_tag(commit), "IMAGE_DIGEST": digest}
            write_state(args.slug, image | pinned)
        if "rss" in extra:
            step.sh(["bun", "install", "--frozen-lockfile", "--cwd", WORKER_DIRS["rss"]])
        for worker in extra:
            step.deploy(worker)
            if not args.dry_run:
                write_state(args.slug, {BASELINE_KEYS[worker]: commit})
        if args.dry_run:
            say("Dry run: nothing was deployed, and the configs and wizard state are unchanged.")
            return 0
        host = configs["app"]["routes"][0]["pattern"]
        step.verify(commit, f"https://{host}", headers, fetch, sleep)
        say(f"https://{host} serves {commit}: the trial runs this release.")
        return 0
    except (DeployError, provision_trial.ProductionNameError, provision_trial.DriftError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
