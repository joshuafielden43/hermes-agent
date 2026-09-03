#!/usr/bin/env python3
"""Owned-fork publication and exact-commit CI receipts. Standard library only."""

from pathlib import Path
import os
import shlex
import subprocess
import sys
import tempfile
import json
import re
import time
import hashlib

REPOSITORY = "joshuafielden43/hermes-agent"
DESTINATION = f"git@github.com:{REPOSITORY}.git"


def command(*args, input=None):
    result = subprocess.run(
        args, input=input, text=True, capture_output=True, timeout=120
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"Command failed: {args[0]}")
    return result.stdout.strip()


def git(*args):
    return command("git", *args)


def setting(key):
    result = subprocess.run(
        ["git", "config", "--get", key], text=True, capture_output=True, timeout=10
    )
    if result.returncode not in (0, 1):
        raise RuntimeError("Cannot read Git configuration")
    return result.stdout.strip()


def state_directory():
    path = Path(git("rev-parse", "--absolute-git-dir")) / "fork-publish"
    path.mkdir(mode=0o700, exist_ok=True)
    return path


def snapshot(sha, path=None):
    source = git("show", f"{sha}:scripts/fork_publish.py")
    if path is None:
        path = Path(tempfile.mkdtemp(prefix=f"{sha[:12]}-", dir=state_directory()))
    (path / "fork_publish.py").write_text(source + "\n")
    launch = f"exec {shlex.quote(sys.executable)} {shlex.quote(str(path / 'fork_publish.py'))}"
    for name, action in (("post-commit", "queue"), ("pre-push", "guard")):
        hook = path / name
        hook.write_text(f'#!/bin/sh\n{launch} {action} "$@"\n')
        hook.chmod(0o700)
    return path


def hooks_digest(path):
    files = {}
    for entry in sorted(path.iterdir()):
        if not entry.is_file() or entry.is_symlink():
            raise ValueError(
                "Unexpected entry in installed hooks; integrate explicitly"
            )
        files[entry.name] = hashlib.sha256(entry.read_bytes()).hexdigest()
    return json.dumps(files, sort_keys=True)


def install():
    guard("fork", DESTINATION, "")
    current = setting("core.hooksPath")
    owned = setting("forkPublish.hooksPath")
    hooks = Path(current or git("rev-parse", "--git-path", "hooks"))
    if current == owned and owned:
        if hooks_digest(hooks) != setting("forkPublish.hooksDigest"):
            raise ValueError(
                "Installed hooks changed; integrate explicitly before reinstalling"
            )
    else:
        if hooks.exists() and any(
            p.is_file() and not p.name.endswith(".sample") for p in hooks.iterdir()
        ):
            raise ValueError(
                "Refusing to replace unrelated hooks; integrate them explicitly"
            )
    if setting("core.worktree") or setting("core.bare") == "true":
        raise ValueError(
            "Explicit core.worktree/bare configuration needs manual migration"
        )
    # Adding worktree configuration support leaves sibling worktrees unchanged.
    runtime = snapshot(git("rev-parse", "HEAD"))
    git("config", "--local", "extensions.worktreeConfig", "true")
    config_path = Path(git("rev-parse", "--git-path", "config.worktree"))
    original = config_path.read_bytes() if config_path.exists() else None
    try:
        values = {
            "remote.fork.pushurl": DESTINATION,
            "remote.fork.mirror": "false",
            "remote.pushDefault": "fork",
            "push.default": "current",
        }
        for remote in git("remote").splitlines():
            if remote in {"origin", "upstream"}:
                values[f"remote.{remote}.pushurl"] = "disabled://read-only"
        for branch in git(
            "for-each-ref", "--format=%(refname:short)", "refs/heads"
        ).splitlines():
            values[f"branch.{branch}.pushRemote"] = "fork"
        for key, value in values.items():
            git("config", "--worktree", "--replace-all", key, value)
        guard("fork", DESTINATION, "")
        git("config", "--worktree", "forkPublish.hooksPath", str(runtime))
        git("config", "--worktree", "forkPublish.hooksDigest", hooks_digest(runtime))
        git("config", "--worktree", "core.hooksPath", str(runtime))
    except Exception:
        if original is None:
            config_path.unlink(missing_ok=True)
        else:
            config_path.write_bytes(original)
        raise
    print(f"Fork publishing hooks installed for this worktree: {runtime}")


def save(path, receipt):
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(receipt, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def inspect_ci(sha):
    runs = json.loads(
        command(
            "gh",
            "run",
            "list",
            "--repo",
            f"github.com/{REPOSITORY}",
            "--commit",
            sha,
            "--limit",
            "100",
            "--json",
            "databaseId,headSha,status,conclusion,workflowName,url",
        )
    )
    if not isinstance(runs, list) or len(runs) >= 100:
        raise ValueError("CI results malformed or coverage limit reached")
    ids = set()
    for run in runs:
        if (
            not isinstance(run, dict)
            or run.get("headSha") != sha
            or type(run.get("databaseId")) is not int
            or run["databaseId"] <= 0
            or run["databaseId"] in ids
            or run.get("url")
            != f"https://github.com/{REPOSITORY}/actions/runs/{run['databaseId']}"
            or run.get("status")
            not in {
                "queued",
                "in_progress",
                "completed",
                "waiting",
                "pending",
                "requested",
            }
            or not isinstance(run.get("workflowName"), str)
        ):
            raise ValueError("Invalid exact-SHA CI record")
        ids.add(run["databaseId"])
        if run["status"] == "completed" and run.get("conclusion") != "success":
            raise ValueError(
                f"CI failed: {run['workflowName']} {run.get('conclusion')} {run['url']}"
            )
    complete = bool(runs) and any(run["workflowName"] == "Fork CI" for run in runs)
    complete = complete and all(run["status"] == "completed" for run in runs)
    return {"state": "passed" if complete else "watching", "runs": runs}


def queue():
    branch = subprocess.run(
        ["git", "symbolic-ref", "--quiet", "HEAD"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if branch.returncode == 1:
        print("Detached HEAD: automatic publishing skipped")
        return
    if branch.returncode:
        raise ValueError("Cannot determine branch")
    for name in (
        "rebase-merge",
        "rebase-apply",
        "sequencer",
        "CHERRY_PICK_HEAD",
        "REVERT_HEAD",
    ):
        if Path(git("rev-parse", "--git-path", name)).exists():
            print("History operation in progress: queue the final result explicitly")
            return
    sha, ref = git("rev-parse", "HEAD"), branch.stdout.strip()
    runtime = Path(tempfile.mkdtemp(prefix=f"{sha[:12]}-", dir=state_directory()))
    path = runtime / "receipt.json"
    receipt = {
        "sha": sha,
        "ref": ref,
        "destination": DESTINATION,
        "state": "queued",
        "created": time.time(),
        "log": str(runtime / "worker.log"),
    }
    spawn_worker(path, receipt)


def retry(path):
    if path.parent.parent != state_directory().resolve() or path.name != "receipt.json":
        raise ValueError("Retry requires a receipt belonging to this worktree")
    receipt = json.loads(path.read_text())
    if receipt["state"] != "failed":
        raise ValueError("Only failed jobs can be retried")
    receipt.pop("error", None)
    receipt["state"] = "queued"
    spawn_worker(path, receipt)


def spawn_worker(path, receipt):
    runtime = path.parent
    save(path, receipt)
    try:
        snapshot(receipt["sha"], runtime)
        env = dict(os.environ)
        for name in git("rev-parse", "--local-env-vars").splitlines():
            env.pop(name, None)
        env["GIT_TERMINAL_PROMPT"] = "0"
        env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
        with open(runtime / "worker.log", "ab") as log:
            os.chmod(runtime / "worker.log", 0o600)
            child = subprocess.Popen(
                [sys.executable, str(runtime / "fork_publish.py"), "work", str(path)],
                cwd=git("rev-parse", "--show-toplevel"),
                env=env,
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
            )
        print(f"Queued fork push and CI watch (PID {child.pid}): {path}")
    except Exception as exc:
        save(path, {**receipt, "state": "failed", "error": str(exc)})
        raise


def work(path):
    receipt = json.loads(path.read_text())
    sha, ref = receipt["sha"], receipt["ref"]
    try:
        if (
            receipt["destination"] != DESTINATION
            or not re.fullmatch(r"[a-f0-9]{40,64}", sha)
            or not ref.startswith("refs/heads/")
        ):
            raise ValueError("Invalid publishing receipt")
        git("check-ref-format", ref)
        guard("fork", DESTINATION, "")
        receipt.update(state="pushing", pid=os.getpid())
        save(path, receipt)
        print(
            git("-c", f"core.hooksPath={path.parent}", "push", "fork", f"{sha}:{ref}"),
            flush=True,
        )
        receipt.update(state="watching", pushed=True)
        save(path, receipt)
        deadline = time.monotonic() + 1800
        previous = None
        observed = {run["databaseId"] for run in receipt.get("runs", [])}
        while time.monotonic() < deadline:
            result = inspect_ci(sha)
            ids = sorted(run["databaseId"] for run in result["runs"])
            if observed - set(ids):
                raise ValueError(
                    "Previously observed CI runs disappeared; coverage is incomplete"
                )
            observed.update(ids)
            receipt["runs"] = result["runs"]
            save(path, receipt)
            if result["state"] == "passed" and ids == previous:
                receipt["state"] = "passed"
                save(path, receipt)
                return
            previous = ids if result["state"] == "passed" else None
            time.sleep(5)
        raise TimeoutError(
            "No complete, successful exact-SHA CI coverage within 30 minutes"
        )
    except Exception as exc:
        receipt.update(state="failed", error=str(exc))
        save(path, receipt)
        raise


def guard(remote, destination, updates):
    if remote != "fork" or destination != DESTINATION:
        raise ValueError("Push blocked: only the named owned fork is permitted")
    for arguments in [("--all",), ("--push", "--all")]:
        if git("remote", "get-url", *arguments, "fork") != DESTINATION:
            raise ValueError(
                "Push blocked: fork must have exactly one pinned effective URL"
            )
    for line in updates.splitlines():
        fields = line.split()
        if len(fields) != 4:
            raise ValueError("Push blocked: malformed ref update")
        _, sha, ref, previous = fields
        if not ref.startswith("refs/heads/") or not sha.strip("0"):
            raise ValueError(
                "Push blocked: only non-deleting branch updates are permitted"
            )
        git("check-ref-format", ref)
        git("cat-file", "-e", f"{sha}^{{commit}}")
        if previous.strip("0"):
            git("merge-base", "--is-ancestor", previous, sha)


def main():
    if sys.argv[1] == "guard":
        guard(sys.argv[2], sys.argv[3], sys.stdin.read())
    elif sys.argv[1] == "install":
        install()
    elif sys.argv[1] == "queue":
        queue()
    elif sys.argv[1] == "work":
        work(Path(sys.argv[2]).resolve())
    elif sys.argv[1] == "retry":
        retry(Path(sys.argv[2]).resolve())
    elif sys.argv[1] == "status":
        receipts = [
            json.loads(path.read_text())
            for path in state_directory().glob("*/receipt.json")
        ]
        print(
            json.dumps(
                sorted(receipts, key=lambda receipt: receipt["created"]), indent=2
            )
        )
    elif sys.argv[1] == "ci":
        print(json.dumps(inspect_ci(sys.argv[2])))
    else:
        raise ValueError("Use install, queue, status, ci SHA, or retry RECEIPT")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"fork-publish: {exc}", file=sys.stderr)
        sys.exit(1)
