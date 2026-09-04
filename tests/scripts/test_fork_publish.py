"""Publishing contracts through the CLI, real Git repos and fake remote services."""

import os
from pathlib import Path
import subprocess
import sys
import shutil
import json
import time
import shlex

import pytest


PUBLISHER = Path(__file__).resolve().parents[2] / "scripts/fork_publish.py"
DESTINATION = "git@github.com:joshuafielden43/hermes-agent.git"

LEGACY_HOOK = """#!/usr/bin/env bash
# Auto-push config commits. Backgrounded + quiet so it never blocks a commit.
# Pushes the current branch to origin; silent on success, logs on failure.
( git push origin HEAD >/dev/null 2>>"$(git rev-parse --show-toplevel)/.git/autopush.log" \\
  || echo "$(date): autopush failed (see autopush.log)" >&2 ) &
"""


def run(repo, *args, input=None, check=True):
    return subprocess.run(
        args,
        cwd=repo,
        input=input,
        text=True,
        capture_output=True,
        check=check,
        timeout=15,
    )


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")
    run(tmp_path, "git", "init", "-b", "codex/test")
    run(tmp_path, "git", "config", "user.name", "Publisher Test")
    run(tmp_path, "git", "config", "user.email", "test@example.invalid")
    run(tmp_path, "git", "remote", "add", "fork", DESTINATION)
    shutil.copyfile(
        PUBLISHER.parents[1] / ".git-publishing.json", tmp_path / ".git-publishing.json"
    )
    run(tmp_path, "git", "commit", "--allow-empty", "-m", "initial")
    return tmp_path


@pytest.mark.parametrize(
    "remote,url,allowed",
    [
        ("fork", DESTINATION, True),
        ("upstream", "https://github.com/NousResearch/hermes-agent.git", False),
        ("origin", DESTINATION, False),
        (DESTINATION, DESTINATION, False),
        ("fork", "ssh://git@evil.invalid/repo", False),
    ],
)
def test_guard_accepts_only_the_named_owned_fork(repo, remote, url, allowed):
    sha = run(repo, "git", "rev-parse", "HEAD").stdout.strip()
    result = run(
        repo,
        sys.executable,
        str(PUBLISHER),
        "guard",
        remote,
        url,
        input=f"HEAD {sha} refs/heads/codex/test {'0' * 40}\n",
        check=False,
    )
    assert (result.returncode == 0) is allowed, result.stderr


def seed_publisher(repo):
    (repo / "scripts").mkdir()
    shutil.copyfile(PUBLISHER, repo / "scripts/fork_publish.py")
    run(repo, "git", "add", "scripts/fork_publish.py")
    run(repo, "git", "commit", "-m", "publisher")


def test_installer_scopes_hooks_and_destinations_to_this_worktree(repo, tmp_path):
    seed_publisher(repo)
    other = tmp_path / "sibling"
    run(repo, "git", "worktree", "add", "-b", "codex/other", str(other))
    run(repo, "git", "remote", "add", "source", "ssh://lab.invalid/source")
    run(
        repo,
        "git",
        "remote",
        "add",
        "upstream",
        "https://github.com/NousResearch/hermes-agent.git",
    )
    result = run(repo, sys.executable, str(PUBLISHER), "install", check=False)
    assert result.returncode == 0, result.stderr
    assert (
        run(repo, "git", "config", "--get", "remote.pushDefault").stdout.strip()
        == "fork"
    )
    assert run(repo, "git", "remote", "get-url", "--push", "source").stdout.startswith(
        "disabled:"
    )
    assert (
        run(other, "git", "remote", "get-url", "--push", "source").stdout.strip()
        == "ssh://lab.invalid/source"
    )
    assert (
        run(other, "git", "config", "--get", "core.hooksPath", check=False).returncode
        != 0
    )
    hooks = Path(run(repo, "git", "config", "--get", "core.hooksPath").stdout.strip())
    assert os.access(hooks / "post-commit", os.X_OK)
    assert os.access(hooks / "pre-push", os.X_OK)


def fake_services(repo, monkeypatch):
    bare = repo / "remote.git"
    run(repo, "git", "init", "--bare", str(bare))
    bin_dir = repo / "fake-bin"
    bin_dir.mkdir()
    ssh = bin_dir / "ssh"
    ssh.write_text(
        f"#!{sys.executable}\n"
        + """import os, sys
assert "git@github.com" in sys.argv
op = sys.argv[-1]
assert "joshuafielden43/hermes-agent.git" in op
program = "git-receive-pack" if op.startswith("git-receive-pack") else "git-upload-pack"
os.execvp(program, [program, os.environ["FIXTURE_BARE"]])
"""
    )
    ssh.chmod(0o700)
    gh = bin_dir / "gh"
    gh.write_text(
        f"#!{sys.executable}\n"
        + """import json, sys, os
assert sys.argv[1:3] == ["run", "list"]
assert sys.argv[sys.argv.index("--repo") + 1] == "github.com/joshuafielden43/hermes-agent"
sha = sys.argv[sys.argv.index("--commit") + 1]
if "FIXTURE_RUN_SEQUENCE" in os.environ:
    from pathlib import Path
    counter = Path(os.environ["FIXTURE_BARE"]) / "gh-count"
    count = int(counter.read_text()) if counter.exists() else 0
    sequence = json.loads(os.environ["FIXTURE_RUN_SEQUENCE"])
    counter.write_text(str(count + 1))
    print(json.dumps(sequence[min(count, len(sequence) - 1)]).replace("SHA_PLACEHOLDER", sha))
    sys.exit(0)
if "FIXTURE_RUNS" in os.environ:
    print(os.environ["FIXTURE_RUNS"].replace("SHA_PLACEHOLDER", sha))
    sys.exit(0)
print(json.dumps([{"databaseId": 42, "headSha": sha, "status": "completed", "conclusion": "success",
"workflowName": "Fork CI", "url": "https://github.com/joshuafielden43/hermes-agent/actions/runs/42"}]))
"""
    )
    gh.chmod(0o700)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "ssh")
    monkeypatch.setenv("GIT_SSH_COMMAND", shlex.quote(str(ssh)))
    monkeypatch.setenv("GIT_SSH_VARIANT", "ssh")
    monkeypatch.setenv("FIXTURE_BARE", str(bare))
    return bare


def wait_for_receipt(repo):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        result = run(repo, sys.executable, str(PUBLISHER), "status", check=False)
        assert result.returncode == 0, result.stderr
        receipts = json.loads(result.stdout)
        if receipts and receipts[-1]["state"] in {"passed", "failed"}:
            return receipts[-1]
        time.sleep(0.1)
    pytest.fail("Publisher did not reach a terminal receipt")


def test_commit_pushes_captured_sha_and_branch_despite_checkout_change(
    repo, monkeypatch
):
    seed_publisher(repo)
    bare = fake_services(repo, monkeypatch)
    run(repo, sys.executable, str(PUBLISHER), "install")
    run(repo, "git", "commit", "--allow-empty", "-m", "publish this commit")
    sha = run(repo, "git", "rev-parse", "HEAD").stdout.strip()
    run(repo, "git", "switch", "--detach", "HEAD~2")
    (repo / "uncommitted.txt").write_text("must stay local")
    receipt = wait_for_receipt(repo)
    assert receipt["state"] == "passed", receipt
    assert receipt["sha"] == sha
    assert receipt["ref"] == "refs/heads/codex/test"
    assert receipt["destination"] == DESTINATION
    assert run(bare, "git", "rev-parse", "refs/heads/codex/test").stdout.strip() == sha
    assert (
        "uncommitted.txt" not in run(bare, "git", "ls-tree", "--name-only", sha).stdout
    )


def test_installer_preserves_unrelated_hooks(repo):
    seed_publisher(repo)
    hook = repo / ".git/hooks/pre-push"
    hook.write_text("#!/bin/sh\nexit 0\n")
    before = (repo / ".git/config").read_bytes()
    result = run(repo, sys.executable, str(PUBLISHER), "install", check=False)
    assert result.returncode != 0
    assert "unrelated hooks" in result.stderr
    assert (repo / ".git/config").read_bytes() == before
    assert hook.read_text() == "#!/bin/sh\nexit 0\n"


@pytest.mark.parametrize("history", ["detached", "rebase"])
def test_queue_skips_detached_and_intermediate_history(repo, history):
    seed_publisher(repo)
    if history == "detached":
        run(repo, "git", "switch", "--detach", "HEAD")
    else:
        (repo / ".git/rebase-merge").mkdir()
    result = run(repo, sys.executable, str(PUBLISHER), "queue")
    assert "skipped" in result.stdout or "History operation" in result.stdout
    assert json.loads(run(repo, sys.executable, str(PUBLISHER), "status").stdout) == []


@pytest.mark.parametrize(
    "case", ["rewrite", "multiple_urls", "deletion", "tag", "non_fast_forward"]
)
def test_guard_rejects_dangerous_configuration_and_ref_updates(repo, case):
    sha = run(repo, "git", "rev-parse", "HEAD").stdout.strip()
    previous, ref = "0" * 40, "refs/heads/codex/test"
    if case == "rewrite":
        run(
            repo,
            "git",
            "config",
            "url.ssh://evil.invalid/.pushInsteadOf",
            "git@github.com:",
        )
    elif case == "multiple_urls":
        run(repo, "git", "config", "--add", "remote.fork.pushurl", DESTINATION)
        run(
            repo,
            "git",
            "config",
            "--add",
            "remote.fork.pushurl",
            "ssh://evil.invalid/repo",
        )
    elif case == "deletion":
        sha = "0" * 40
    elif case == "tag":
        ref = "refs/tags/v1"
    else:
        run(repo, "git", "commit", "--allow-empty", "-m", "newer")
        previous = run(repo, "git", "rev-parse", "HEAD").stdout.strip()
    result = run(
        repo,
        sys.executable,
        str(PUBLISHER),
        "guard",
        "fork",
        DESTINATION,
        input=f"HEAD {sha} {ref} {previous}\n",
        check=False,
    )
    assert result.returncode != 0


@pytest.mark.parametrize(
    "case",
    ["missing", "other_workflow", "failed_sibling", "wrong_sha", "malformed", "limit"],
)
def test_ci_requires_complete_success_for_the_exact_commit(repo, monkeypatch, case):
    fake_services(repo, monkeypatch)
    good = {
        "databaseId": 42,
        "headSha": "SHA_PLACEHOLDER",
        "status": "completed",
        "conclusion": "success",
        "workflowName": "Fork CI",
        "url": "https://github.com/joshuafielden43/hermes-agent/actions/runs/42",
    }
    runs = []
    if case == "other_workflow":
        runs = [{**good, "workflowName": "Unrelated"}]
    elif case == "failed_sibling":
        runs = [
            good,
            {
                **good,
                "databaseId": 43,
                "workflowName": "Sibling",
                "conclusion": "failure",
                "url": "https://github.com/joshuafielden43/hermes-agent/actions/runs/43",
            },
        ]
    elif case == "wrong_sha":
        runs = [{**good, "headSha": "f" * 40}]
    elif case == "limit":
        runs = [good] * 100
    monkeypatch.setenv(
        "FIXTURE_RUNS", "not json" if case == "malformed" else json.dumps(runs)
    )
    sha = run(repo, "git", "rev-parse", "HEAD").stdout.strip()
    result = run(repo, sys.executable, str(PUBLISHER), "ci", sha, check=False)
    if case in {"missing", "other_workflow"}:
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["state"] == "watching"
    else:
        assert result.returncode != 0


def test_failed_push_keeps_commit_and_can_retry_same_receipt(repo, monkeypatch):
    seed_publisher(repo)
    fake_services(repo, monkeypatch)
    transport = os.environ["GIT_SSH_COMMAND"]
    monkeypatch.setenv("GIT_SSH_COMMAND", "false")
    run(repo, sys.executable, str(PUBLISHER), "install")
    run(repo, "git", "commit", "--allow-empty", "-m", "preserve on failed push")
    sha = run(repo, "git", "rev-parse", "HEAD").stdout.strip()
    receipt = wait_for_receipt(repo)
    assert receipt["state"] == "failed"
    assert receipt.get("pushed") is not True
    assert run(repo, "git", "rev-parse", "HEAD").stdout.strip() == sha
    monkeypatch.setenv("GIT_SSH_COMMAND", transport)
    # Retry belongs to the original captured policy, not today's checkout.
    (repo / ".git-publishing.json").write_text("{}")
    path = str(Path(receipt["log"]).with_name("receipt.json"))
    result = run(repo, sys.executable, str(PUBLISHER), "retry", path, check=False)
    assert result.returncode == 0, result.stderr
    receipt = wait_for_receipt(repo)
    assert receipt["state"] == "passed", receipt
    assert receipt["sha"] == sha


@pytest.mark.parametrize("change", ["addition", "modification"])
def test_reinstall_refuses_changes_to_owned_hooks(repo, change):
    seed_publisher(repo)
    run(repo, sys.executable, str(PUBLISHER), "install")
    hooks = Path(run(repo, "git", "config", "--get", "core.hooksPath").stdout.strip())
    target = hooks / ("pre-commit" if change == "addition" else "pre-push")
    target.write_text("#!/bin/sh\n# integrated secret scanner\nexit 1\n")
    result = run(repo, sys.executable, str(PUBLISHER), "install", check=False)
    assert result.returncode != 0
    assert run(repo, "git", "config", "--get", "core.hooksPath").stdout.strip() == str(
        hooks
    )
    assert "secret scanner" in target.read_text()


def test_queue_guard_failure_has_retryable_receipt(repo, monkeypatch):
    seed_publisher(repo)
    fake_services(repo, monkeypatch)
    run(repo, "git", "config", "remote.fork.pushurl", "ssh://evil.invalid/repo")
    run(repo, sys.executable, str(PUBLISHER), "queue", check=False)
    receipt = wait_for_receipt(repo)
    assert receipt["state"] == "failed"
    sha = receipt["sha"]
    run(repo, "git", "config", "--unset", "remote.fork.pushurl")
    path = str(Path(receipt["log"]).with_name("receipt.json"))
    run(repo, sys.executable, str(PUBLISHER), "retry", path)
    receipt = wait_for_receipt(repo)
    assert receipt["state"] == "passed", receipt
    assert receipt["sha"] == sha


def test_ci_does_not_forget_observed_pending_sibling(repo, monkeypatch):
    seed_publisher(repo)
    fake_services(repo, monkeypatch)
    good = {
        "databaseId": 42,
        "headSha": "SHA_PLACEHOLDER",
        "status": "completed",
        "conclusion": "success",
        "workflowName": "Fork CI",
        "url": "https://github.com/joshuafielden43/hermes-agent/actions/runs/42",
    }
    sibling = {
        **good,
        "databaseId": 43,
        "status": "in_progress",
        "conclusion": None,
        "workflowName": "Sibling",
        "url": "https://github.com/joshuafielden43/hermes-agent/actions/runs/43",
    }
    monkeypatch.setenv("FIXTURE_RUN_SEQUENCE", json.dumps([[good, sibling], [good]]))
    run(repo, sys.executable, str(PUBLISHER), "queue")
    receipt = wait_for_receipt(repo)
    assert receipt["state"] == "failed", receipt
    assert "disappeared" in receipt["error"]


def test_installer_preserves_active_precommit_guard(repo):
    seed_publisher(repo)
    precommit = repo / ".git/hooks/pre-commit"
    precommit.write_text("#!/bin/sh\necho existing-guard >&2\nexit 1\n")
    precommit.chmod(0o700)
    original = precommit.read_bytes()
    result = run(repo, sys.executable, str(PUBLISHER), "install", check=False)
    assert result.returncode == 0, result.stderr
    blocked = run(
        repo, "git", "commit", "--allow-empty", "-m", "must fail", check=False
    )
    assert blocked.returncode != 0
    assert "existing-guard" in blocked.stderr
    assert precommit.read_bytes() == original
    # A clean reinstall must retain the forwarding wrapper, too.
    run(repo, sys.executable, str(PUBLISHER), "install")
    blocked = run(repo, "git", "hook", "run", "pre-commit", check=False)
    assert blocked.returncode != 0
    assert "existing-guard" in blocked.stderr


@pytest.mark.parametrize("filename", [".env.example", "normal.txt", "normal.py"])
def test_template_credentials_are_blocked_without_echo(repo, filename, monkeypatch):
    token = "ghp_" + "A" * 36
    (repo / filename).write_text("TOKEN=" + token)
    run(repo, "git", "add", filename)
    if filename.endswith(".py"):
        fake_bin = repo / "fake-bin"
        fake_bin.mkdir()
        ruff = fake_bin / "ruff"
        ruff.write_text('#!/bin/sh\necho "$FIXTURE_TOKEN"\nexit 1\n')
        ruff.chmod(0o700)
        monkeypatch.setenv("FIXTURE_TOKEN", token)
        monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + os.environ["PATH"])
    result = run(repo, "bash", str(PUBLISHER.parent / "pre-commit-guards"), check=False)
    assert result.returncode != 0
    assert token not in result.stdout + result.stderr
    assert token[:20] not in result.stdout + result.stderr


def test_policy_selects_owned_destination_and_protects_main(repo):
    policy = {
        "repository": "example-owner/example-project",
        "remote": "backup",
        "workflow": "checks",
        "protected_branches": ["main", "master"],
    }
    (repo / ".git-publishing.json").write_text(json.dumps(policy))
    url = "git@github.com:example-owner/example-project.git"
    run(repo, "git", "remote", "add", "backup", url)
    sha = run(repo, "git", "rev-parse", "HEAD").stdout.strip()
    allowed = run(
        repo,
        sys.executable,
        str(PUBLISHER),
        "guard",
        "backup",
        url,
        input=f"HEAD {sha} refs/heads/codex/work {'0' * 40}\n",
        check=False,
    )
    assert allowed.returncode == 0, allowed.stderr
    blocked = run(
        repo,
        sys.executable,
        str(PUBLISHER),
        "guard",
        "backup",
        url,
        input=f"HEAD {sha} refs/heads/main {'0' * 40}\n",
        check=False,
    )
    assert blocked.returncode != 0
    assert "protected branch" in blocked.stderr


def test_outgoing_scan_catches_secret_removed_by_later_commit(repo):
    base = run(repo, "git", "rev-parse", "HEAD").stdout.strip()
    token = "ghp_" + "B" * 36
    (repo / ".env.example").write_text("TOKEN=" + token)
    run(repo, "git", "add", ".env.example")
    run(repo, "git", "commit", "-m", "imported history")
    run(repo, "git", "rm", ".env.example")
    run(repo, "git", "commit", "-m", "delete secret from tip")
    sha = run(repo, "git", "rev-parse", "HEAD").stdout.strip()
    result = run(
        repo,
        sys.executable,
        str(PUBLISHER),
        "guard",
        "fork",
        DESTINATION,
        input=f"HEAD {sha} refs/heads/codex/test {base}\n",
        check=False,
    )
    assert result.returncode != 0
    assert "credential" in result.stderr
    assert token[:20] not in result.stdout + result.stderr


def test_session_attention_warns_on_failure_not_success(repo):
    state = repo / ".git/fork-publish/job"
    state.mkdir(parents=True)
    receipt = {
        "sha": "a" * 40,
        "state": "failed",
        "created": time.time(),
        "error": "DO NOT ECHO ME",
    }
    (state / "receipt.json").write_text(json.dumps(receipt))
    script = str(PUBLISHER.parent / "publication_attention.py")
    payload = json.dumps({"cwd": str(repo), "session_id": "first"})
    result = run(repo, sys.executable, script, input=payload, check=False)
    assert result.returncode == 0, result.stderr
    assert "failed" in result.stdout and "DO NOT ECHO ME" not in result.stdout
    assert run(repo, sys.executable, script, input=payload).stdout == ""
    receipt["state"] = "passed"
    (state / "receipt.json").write_text(json.dumps(receipt))
    assert (
        run(
            repo,
            sys.executable,
            script,
            input=json.dumps({"cwd": str(repo), "session_id": "second"}),
        ).stdout
        == ""
    )


def test_missing_policy_fails_closed(repo):
    (repo / ".git-publishing.json").unlink()
    result = run(repo, sys.executable, str(PUBLISHER), "install", check=False)
    assert result.returncode != 0


def test_installed_policy_ignores_later_checkout_policy(repo, monkeypatch):
    seed_publisher(repo)
    fake_services(repo, monkeypatch)
    run(repo, sys.executable, str(PUBLISHER), "install")
    (repo / ".git-publishing.json").write_text("{}")
    run(repo, "git", "commit", "--allow-empty", "-m", "installed policy stays pinned")
    # Also exercise the installed public CLI after the checkout policy changes.
    hooks = Path(run(repo, "git", "config", "--get", "core.hooksPath").stdout.strip())
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        receipts = json.loads(
            run(repo, sys.executable, str(hooks / "fork_publish.py"), "status").stdout
        )
        if receipts and receipts[-1]["state"] in {"passed", "failed"}:
            assert receipts[-1]["state"] == "passed", receipts
            return
        time.sleep(0.2)
    pytest.fail("Installed worker did not finish")


def test_session_attention_reports_interrupted_worker(repo):
    state = repo / ".git/fork-publish/job"
    state.mkdir(parents=True)
    (state / "receipt.json").write_text(
        json.dumps({
            "sha": "b" * 40,
            "state": "watching",
            "created": time.time() - 4000,
        })
    )
    script = str(PUBLISHER.parent / "publication_attention.py")
    result = run(repo, sys.executable, script, input=json.dumps({"cwd": str(repo)}))
    assert "interrupted or overdue" in result.stdout


def test_missing_installed_policy_never_falls_back_to_checkout(repo, monkeypatch):
    seed_publisher(repo)
    fake_services(repo, monkeypatch)
    monkeypatch.setenv("GIT_SSH_COMMAND", "false")
    run(repo, sys.executable, str(PUBLISHER), "install")
    run(repo, sys.executable, str(PUBLISHER), "queue")
    receipt = wait_for_receipt(repo)
    assert receipt["state"] == "failed"
    path = Path(receipt["log"]).with_name("receipt.json")
    (path.parent / "publishing-policy.json").unlink()
    result = run(repo, sys.executable, str(PUBLISHER), "retry", str(path), check=False)
    assert result.returncode != 0
    assert "snapshot" in result.stderr
    result = run(
        repo,
        sys.executable,
        str(path.parent / "fork_publish.py"),
        "guard",
        "fork",
        DESTINATION,
        input="",
        check=False,
    )
    assert result.returncode != 0
    assert "policy" in result.stderr
