# Fork collar maintenance

This fork carries the Hermes structured-output collar. It is not an upstream
submission branch. Do not open a pull request to `NousResearch/hermes-agent`.

## Refresh onto current upstream main

Run these commands from a clean collar checkout. Keep the previous branch as
the rollback point and use a date-stamped replacement branch.

```bash
git fetch --prune upstream main
git fetch --prune fork
git switch -c codex/api-response-contract-reconcile-YYYYMMDD
base=$(git merge-base HEAD upstream/main)
git rebase --onto upstream/main "$base"
```

Resolve conflicts by retaining both current-main behavior and these collar
invariants:

- Chat accepts `response_format`; Responses accepts `text.format`.
- Hermes independently validates final JSON and JSON Schema output.
- Non-streaming validation gets at most one format-only repair with tools off.
- Streaming holds assistant text until validation and never repairs.
- Failed or invalid output is neither leaked nor persisted.
- Provider/run failures remain `agent_error`; terminal validation failures are
  `structured_output_validation_failed`.
- The public sidecar never exposes credentials, provider replay state, or
  chain-of-thought.

Compare the replay before testing:

```bash
git range-diff OLD_BASE..OLD_TIP upstream/main..HEAD
git diff --check upstream/main...HEAD
```

## Bounded verification

Never run the repository-wide pytest suite locally. Count and then run the
affected modules listed in the workspace `TESTING.md`. Run Ruff on touched
Python files and verify `git diff --check`.

## Publish branch and image

Push only to the `fork` remote. First publish the dated rollback branch, then
fast-forward the fork's `main` to the verified candidate:

```bash
git push --set-upstream fork HEAD
git push fork HEAD:main
```

Never force-push `main`. If it is not a fast-forward, stop and reconcile the
fork state explicitly. Never create an upstream pull request.

That push runs `.github/workflows/fork-collar-image.yml` in
`joshuafielden43/hermes-agent`. The workflow builds Linux amd64 from the
candidate commit and publishes two GHCR tags:

```text
ghcr.io/joshuafielden43/hermes-agent:collar-latest
ghcr.io/joshuafielden43/hermes-agent:collar-<full-commit-sha>
```

The SHA tag is the deployment and rollback identity. Treat `collar-latest` as
a convenience pointer only. A green workflow and an inspectable registry
digest prove the image; a successful branch push alone does not.
