---
status: accepted
date: 2026-11-03
decision-makers: ""
---

# A push to main releases and deploys

## Context and Problem Statement

Until 2026-11-03 the app Worker shipped through two workflows, and a person dispatched each:
`.github/workflows/release-image.yml` built and recorded the image, then
`.github/workflows/deploy.yml` deployed it. `deploy.yml`'s header gave two reasons for keeping
them apart. Building and deploying fail differently. And the timing was chosen by a person,
because `wrangler.toml` runs a cron every hour (`crons = ["0 * * * *"]`) and a deploy rolls the
container.

In practice every deploy waited for the user. The coding agent that lands most changes cannot
dispatch a workflow, so a merged change stayed off production until the user ran two
workflows by hand. The owner decided on 2026-11-03 that a push should reach production
without that step.

## Decision Drivers

* Fewer manual steps between a merge and production. Every deploy needed the user.
* "The image cannot be made" and "the image cannot go live" must stay two visible failures.
* A deploy must land the image recorded for the commit that was released. It must not land
  `:release`, and it must not land a newer head of main.
* A person must still be able to pin an exact image by digest (`docs/operations.md`
  *Going back*).

## Considered Options

* The release runs on push to main and on dispatch. A release that succeeds calls `deploy.yml`
  as a second job, unless a dispatch unticked its `deploy` input (chosen)
* Keep both workflows dispatch-only
* Deploy after release, but hold the deploy away from the cron hour
* Trigger `deploy.yml` with `workflow_run` when the release completes

## Decision Outcome

Chosen option: "release on push, then deploy as a called job". The owner decided three points
on 2026-11-03:

1. Deploy after release is on by default and optional. A dispatch of the release has a boolean
   `deploy` input, default true; false means no deploy. A push has no inputs and deploys.
2. The release also runs on every push to `main`, with no path filter. `workflow_dispatch`
   stays.
3. The hourly cron is not avoided. "The timing is chosen by a person" is retired as a reason.

A fourth point followed the same day's review: only the head of its ref deploys
automatically. A job, `current`, runs between the release and the deploy. It reads the head
of the run's ref (`git ls-remote origin "$GITHUB_REF"`) and lets the deploy run only if that
head equals `GITHUB_SHA`. Otherwise the deploy is skipped, with a notice naming both commits,
and the run stays green. The test is equality, not ancestry: an older commit is always an
ancestor of the head, so ancestry would stop nothing. The guard's answer counts only in the
attempt that gave it, so re-running only the deploy job cannot reuse an answer from when an
older commit was the head. `deploy.yml` dispatched by hand, a pin by digest included, does not
pass through it.

The other half of the old separation still holds. Build and deploy are separate jobs, so each
fails as its own red X. `deploy.yml` keeps its dispatch and its `image_tag` input for pinning.
Its resolve, derive, deploy and verify steps are unchanged.

A called workflow runs from the caller's commit and sees the caller's `github` context. Its
`GITHUB_SHA` is therefore the commit the release just tagged `image/<sha7>`. `workflow_run`
would set `GITHUB_SHA` to the head of the default branch and run `deploy.yml` from there. Its
resolve step would then need `github.event.workflow_run.head_sha` in place of `GITHUB_SHA`, and
the two runs would no longer share a concurrency group.

Whether a deploy is wanted is `github.event_name == 'push' || inputs.deploy`; the guard's two
outputs are added to it with `&&`. A push has no `deploy` input, and a property that does not
exist evaluates to an empty string. GitHub's loose comparison turns '' and false both into 0,
so the condition that reads more naturally, `inputs.deploy != false`, would skip every push.

### Consequences

* Good, because a merge reaches production with no person in the loop, and the agent's work is
  live when it lands.
* Good, because the release job runs `scripts/check.sh` before it builds. A push whose checks
  fail is never released, so it is never deployed.
* Bad, because any push to `main` deploys production, and `main` has no branch protection. The
  check gate inside the release is the only thing between a push and production.
* Bad, because a deploy can land in the minute before a publish hour and roll the container
  under that hour's run. The catch-up's retries (`CATCH_UP_HOURS`, `docs/architecture.md` §5)
  redo a run the roll kills. A digest can still be late.
* Bad, because a pin is temporary. An image pinned by dispatching `deploy.yml` with a digest stays
  live only until the next push to `main` deploys that push's commit.
* Bad, because "staging first, production after" no longer holds for a push. Production gets
  the commit before anyone can check it on the trial deployment.
* Neutral, because a dispatch of the release on a branch other than `main` also deploys by
  default. That branch's head is what goes live unless `deploy` is unticked.
* Neutral, because releases are serialised by the workflow's `release-image` concurrency
  group, which now spans the deploy job too. GitHub's concurrency docs say runs in a group
  "are processed in first-in-first-out (FIFO) order according to the time each one started
  waiting on the concurrency group", and "Since the actual start time of a job or run may
  vary, ordering is not guaranteed." The guard is why that order no longer matters for
  production: an older commit whose run reaches the deploy after a newer push skips its
  deploy, because it is no longer the head.
* Bad, because only one run waits in a group, and any release that queues replaces it. That
  includes a dispatch with `deploy` unticked, or one on another branch. A push whose waiting
  run is replaced is never released, so it never deploys; dispatching the release on `main`
  recovers it. A `deploy.yml` dispatch queued behind a release's waiting deploy cancels that
  deploy in the shared `deploy-app` group, and the other way round, if a called job's own
  group holds the call, which is unverified (see below).
* Neutral, because a re-run keeps the run's original `GITHUB_SHA` and `GITHUB_REF`. Without
  the guard, re-running an old release run would deploy its old commit. With it, the re-run
  skips its deploy unless that commit is still the head. Re-running only a failed deploy job
  skips it too, so a deploy is retried by re-running all jobs or by dispatching `deploy.yml`.
* Bad, because a commit deploys only from its own release. When a newer push's release fails,
  the older commit has already skipped its deploy, and production keeps what it had.

### Confirmation

`tests/test_release_image.py` pins the triggers, the `deploy` input, the deploy job and its
condition. It evaluates the condition under GitHub's documented comparison rules for a push, a
dispatch with `deploy` true, and one with `deploy` false, each with the guard answering current,
stale, or from an earlier attempt. It runs the guard's step against a stubbed `git ls-remote`:
the head, a moved ref, a deleted ref, an annotated tag and a failed read. `tests/test_deploy_path.py` pins
`deploy.yml`'s dispatch with `image_tag`, its `workflow_call` and its job-level concurrency.

## Pros and Cons of the Options

### Release on push, then deploy as a called job

* Good, because the called workflow sees the caller's commit, so the existing resolve step
  deploys the released image unchanged.
* Good, because one run shows both jobs, and each fails on its own.
* Bad, because `deploy.yml`'s concurrency had to move from workflow level to its job, and
  that it holds for the call is not documented outright. The workflow-syntax docs'
  `jobs.<job_id>.concurrency` admits one job at a time to a group. The reusable-workflow page
  lists that key only for the calling job. Only its warning against giving the called and
  caller workflows the same group implies that a called job's own group applies. It stays
  unverified until a real run shows a release's deploy waiting on a dispatched one.

### Keep both workflows dispatch-only

* Good, because a person chooses every deploy and its hour.
* Bad, because every deploy needs the user. This is the cost the decision removes.

### Deploy after release, but hold the deploy away from the cron hour

* Good, because a roll would not land just before a publish hour.
* Bad, because a merge waits for a safe minute before it goes live, and the owner chose not to
  avoid the cron.

### Trigger `deploy.yml` with `workflow_run`

* Good, because the two workflows stay fully separate runs.
* Bad, because its `GITHUB_SHA` is the default branch's head, not the released commit. Getting
  that wrong deploys the wrong image, or fails to resolve one.
* Bad, because it fires only from the default branch's copy of the workflow, and its chains stop
  at three levels.

## More Information

* GitHub docs: [Reusing workflow configurations](https://docs.github.com/en/actions/reference/workflows-and-actions/reusing-workflow-configurations),
  [Events that trigger workflows](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows),
  [Evaluate expressions](https://docs.github.com/en/actions/reference/workflows-and-actions/expressions),
  [Control workflow concurrency](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency),
  [Workflow syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax),
  [Contexts reference](https://docs.github.com/en/actions/reference/workflows-and-actions/contexts),
  [Pass job outputs](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/pass-job-outputs),
  [Re-running workflows and jobs](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/re-run-workflows-and-jobs).
* `docs/spec/release-image-built-in-ci.md` is the release's spec. `docs/operations.md` is the
  procedure.
