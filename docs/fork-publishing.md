# ADR: fork CI and automatic commit publishing

Date: 2026-09-03. Task: Vikunja 2089, under 2082.

Status: CI is live. The tracked publisher and installer are implemented and
installed in the reconciliation worktree on 2026-09-03. Other worktrees are
unchanged. This installation-record commit exercises live automatic publication;
its exact-SHA receipt and hosted CI result are recorded in task 2089.

## Decision and implementation order

1. Adapt the house Python CI template to Hermes: every branch push and PR,
   read-only token, immutable action pins, locked dependencies, Ruff, type
   checking, and the bounded collar suite. Add publishing-hook contracts with
   the hook implementation.
   Retain upstream workflows and the separate main-only image build. This
   workflow does not deploy, create PRs, promote main, or require model keys.
2. The second source is the existing owned GitHub fork, not a new remote:
   `fork` at `git@github.com:joshuafielden43/hermes-agent.git`. Never publish to
   `origin` (the development VM) or `upstream` (NousResearch).
3. Track the publisher, hook entry points and installer in this repository.
   Install only into this worktree's Git configuration, preserving other
   worktrees and refusing to replace unrelated hooks. Pin default pushes to
   `fork`; make origin/upstream read-only for this worktree.
4. On commit, capture the immutable SHA and attached branch. Queue a detached
   publisher from that commit's source. Push `SHA:refs/heads/BRANCH`, never a
   later `HEAD`. A dirty checkout is neither staged nor committed by the hook.
   Detached HEAD does not auto-publish. Rebase/cherry-pick intermediate commits
   do not auto-publish; explicitly queue the final result when ready.
5. Pre-push enforces the exact destination and branch-only, non-deleting,
   fast-forward updates. Reject URL rewrites and multiple effective URLs.
   A separate immutable guard protects queued jobs across checkout changes.
6. Record push and CI states separately in private Git-local receipts. Watch
   the explicit fork and exact SHA, require Fork CI, and examine every observed
   workflow. Missing, failed, cancelled or malformed runs are not success.
   Failed publication leaves the commit intact and provides an explicit retry.

## Boundaries

The post-commit hook cannot roll back a commit; a queue/push failure is reported,
not hidden as commit failure. The worker is detached, not a durable scheduler:
machine shutdown may interrupt it. Status and retry are explicit commands.
Retry accepts failed receipts only; interrupted nonterminal jobs require checking
that the worker is gone and explicitly queuing the desired attached HEAD again.
Rapid commits can race: an older non-fast-forward push records failure rather
than forcing history or silently substituting a newer commit.

Hooks do not magically follow clones. Each checkout must run the tracked
installer. Local hooks cannot stop deliberate `--no-verify`, changing Git
configuration, replacing the hooks, using another clone, or direct GitHub API
writes. Stronger prevention needs scoped credentials/server authorization.

No existing quality or secret hook is removed. Installing into a checkout with
unrelated active hooks stops for integration rather than silently replacing it.
Reinstallation also verifies the installed directory's content hashes; added or
modified hooks require explicit integration, including changes to owned entries.
The publisher does not auto-stage files, create commits, wake inactive agents,
or send credentials to CI. Publishing is not deployment or production approval.

## CI implementation

The workflow is `.github/workflows/fork-ci.yml`; its local equivalent is
`bash scripts/check_fork.sh`. It covers the maintained collar surface, not
the entire upstream repository. On 2026-09-03 that command passed Ruff, focused
type checks, and 498 tests with one skip. Separate Standards and Spec reviews
reported zero findings for this CI slice. Hosted execution is verified after
pushing the exact commit; local success alone is not a hosted CI receipt.

## Publishing commands

Run from the intended worktree, after committing the publisher source:

```sh
python3 scripts/fork_publish.py install
python3 scripts/fork_publish.py status
python3 scripts/fork_publish.py retry /absolute/path/to/receipt.json
# Explicitly queue HEAD after a history operation or initial installation:
python3 scripts/fork_publish.py queue
```

Installation generates executable hook entry points from the tracked installer
and snapshots the committed publisher under this worktree's private Git directory.
Each commit gets another immutable worker snapshot and a JSON receipt plus log.
Reinstall after changing the installer/queue/guard to update the installed hooks.
The worker uses Git and an authenticated `gh` on PATH, with noninteractive SSH;
it does not copy credentials into the repository. Its Python interpreter must
remain available. A successful push becomes `watching`, not `passed`, until
two consecutive checks see the same all-successful exact-SHA workflow set,
including Fork CI. The coverage cap is 100 runs and the deadline is 30 minutes;
both fail closed. Workflows triggered later are outside this bounded observation.
Previously observed runs remain mandatory: disappearance fails closed rather
than silently reducing the workflow set. Queue preparation failures also retain
a failed receipt for retry of the original commit.

No hook is removed from an existing hook chain: installation refuses unrelated
active hooks. `origin` and `upstream` receive disabled push URLs only in this
worktree, and the pre-push hook also rejects direct URLs and other remote names.

## Installation verification

Publisher implementation: `3c4d3a094c39` (2026-09-03). Local verification passed
Ruff, focused type checks, and 524 tests with one skip across 11 modules,
including 26 publisher contracts. Standards and Spec re-reviews reported zero
unresolved findings after regression-tested corrections.

The reconciliation worktree's `config.worktree` pins default/current-branch
pushes to `fork`, disables origin/upstream push URLs, and selects the generated
hooks. The sibling `hermes-agent-json-response-contract-local` checkout retained
its original remote URLs and unset hooks path. A direct guard invocation with
`origin` was rejected without contacting a remote. The next publication is the
real post-commit proof, not a deployment or main-branch promotion.
