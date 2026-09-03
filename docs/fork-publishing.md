# ADR: fork CI and automatic commit publishing

Date: 2026-09-03. Task: Vikunja 2089, under 2082.

Status: CI is the first implementation slice. The publishing-hook decisions
below are approved direction, not a claim that hooks are already installed.

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

Hooks do not magically follow clones. Each checkout must run the tracked
installer. Local hooks cannot stop deliberate `--no-verify`, changing Git
configuration, replacing the hooks, using another clone, or direct GitHub API
writes. Stronger prevention needs scoped credentials/server authorization.

No existing quality or secret hook is removed. Installing into a checkout with
unrelated active hooks stops for integration rather than silently replacing it.
The publisher does not auto-stage files, create commits, wake inactive agents,
or send credentials to CI. Publishing is not deployment or production approval.

## CI implementation

The workflow is `.github/workflows/fork-ci.yml`; its local equivalent is
`bash scripts/check_fork.sh`. It covers the maintained collar surface, not
the entire upstream repository. On 2026-09-03 that command passed Ruff, focused
type checks, and 498 tests with one skip. Separate Standards and Spec reviews
reported zero findings for this CI slice. Hosted execution is verified after
pushing the exact commit; local success alone is not a hosted CI receipt.
