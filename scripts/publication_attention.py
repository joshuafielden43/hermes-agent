#!/usr/bin/env python3
"""Universal, local-only SessionStart warning for unresolved publication jobs."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time


def main():
    payload = json.load(sys.stdin)
    result = subprocess.run(
        ["git", "rev-parse", "--absolute-git-dir"],
        cwd=payload.get("cwd", os.getcwd()),
        capture_output=True,
        text=True,
        timeout=5,
    )
    if result.returncode:
        return
    directory = Path(result.stdout.strip()) / "fork-publish"
    if not directory.exists():
        return
    notices = []
    for path in sorted(directory.glob("*/receipt.json")):
        try:
            receipt = json.loads(path.read_text())
            state, sha = receipt["state"], receipt["sha"]
            if not re.fullmatch(r"[a-f0-9]{40,64}", sha):
                raise ValueError("invalid SHA")
            if state == "passed":
                continue
            age = time.time() - float(receipt["created"])
            if state in {"queued", "pushing", "watching"}:
                alive = False
                pid = receipt.get("pid")
                if type(pid) is int and pid > 0:
                    try:
                        os.kill(pid, 0)
                        alive = True
                    except PermissionError:
                        alive = True
                    except ProcessLookupError:
                        pass
                if age < 60 or (alive and age < 2100):
                    continue
                state = "interrupted or overdue"
            elif state != "failed":
                state = "unrecognized receipt state"
            notices.append(f"{sha[:12]}: {state}")
        except (ValueError, KeyError, TypeError, OSError):
            notices.append("Unreadable publication receipt needs inspection")
    if not notices:
        return
    # Deduplicate in this session only; another session must see unresolved work.
    session = str(payload.get("session_id", ""))
    key = hashlib.sha256((session + json.dumps(notices)).encode()).hexdigest()
    seen_path = directory / "attention-seen.json"
    try:
        seen = json.loads(seen_path.read_text())
        if not isinstance(seen, list):
            seen = []
    except (OSError, ValueError):
        seen = []
    if session and key in seen:
        return
    context = "Publication needs attention:\n" + "\n".join(notices[:20])
    if len(notices) > 20:
        context += f"\n{len(notices) - 20} more unresolved receipts."
    context += "\nInspect Git-local fork-publish receipts before reporting publication clean. Do not automatically retry or change history."
    print(
        json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": context,
            }
        })
    )
    if session:
        temporary = directory / f"attention-seen.{os.getpid()}.tmp"
        temporary.write_text(json.dumps((seen + [key])[-100:]))
        temporary.chmod(0o600)
        temporary.replace(seen_path)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # No raw exceptions: hook input or receipt errors may contain secrets.
        print(
            json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": "Publication attention check could not complete; inspect local receipts manually.",
                }
            })
        )
