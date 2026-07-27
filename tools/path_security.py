"""Shared path validation helpers for tool implementations.

Extracts the ``resolve() + relative_to()`` and ``..`` traversal check
patterns previously duplicated across skill_manager_tool, skills_tool,
skills_hub, cronjob_tools, and credential_files.

Also holds the per-task write-root registry used by delegated children
(#1612). Policies are keyed by runtime task_id, not thread-local state.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Iterable, Optional, Sequence

logger = logging.getLogger(__name__)

_task_write_roots: dict[str, tuple[str, ...]] = {}
_task_write_roots_lock = threading.Lock()


def validate_within_dir(path: Path, root: Path) -> Optional[str]:
    """Ensure *path* resolves to a location within *root*.

    Returns an error message string if validation fails, or ``None`` if the
    path is safe.  Uses ``Path.resolve()`` to follow symlinks and normalize
    ``..`` components.

    Usage::

        error = validate_within_dir(user_path, allowed_root)
        if error:
            return json.dumps({"error": error})
    """
    try:
        resolved = path.resolve()
        root_resolved = root.resolve()
        resolved.relative_to(root_resolved)
    except (ValueError, OSError) as exc:
        return f"Path escapes allowed directory: {exc}"
    return None


def has_traversal_component(path_str: str) -> bool:
    """Return True if *path_str* contains ``..`` traversal components.

    Quick check for obvious traversal attempts before doing full resolution.
    """
    parts = Path(path_str).parts
    return ".." in parts


def set_task_write_roots(task_id: str, roots: Sequence[str] | None) -> None:
    """Register resolved write roots for *task_id*. Empty/None clears."""
    if not task_id:
        return
    with _task_write_roots_lock:
        if not roots:
            _task_write_roots.pop(task_id, None)
            return
        cleaned: list[str] = []
        for root in roots:
            text = str(root or "").strip()
            if not text:
                continue
            try:
                cleaned.append(str(Path(text).expanduser().resolve()))
            except (OSError, ValueError):
                continue
        if cleaned:
            _task_write_roots[task_id] = tuple(dict.fromkeys(cleaned))
        else:
            _task_write_roots.pop(task_id, None)


def clear_task_write_roots(task_id: str) -> None:
    """Drop write-root policy for *task_id*."""
    if not task_id:
        return
    with _task_write_roots_lock:
        _task_write_roots.pop(task_id, None)


def get_task_write_roots(task_id: str) -> tuple[str, ...] | None:
    """Return registered roots for *task_id*, or None when unrestricted."""
    if not task_id:
        return None
    with _task_write_roots_lock:
        return _task_write_roots.get(task_id)


def resolve_write_roots(
    roots: Iterable[str] | None,
    *,
    workspace: str | None = None,
    parent_roots: Sequence[str] | None = None,
) -> list[str]:
    """Resolve requested roots against *workspace*; nest under *parent_roots*."""
    if not roots:
        return []
    base = Path(workspace).expanduser().resolve() if workspace else Path.cwd().resolve()
    parent = []
    for item in parent_roots or ():
        try:
            parent.append(str(Path(item).expanduser().resolve()))
        except (OSError, ValueError):
            continue

    resolved: list[str] = []
    for raw in roots:
        text = str(raw or "").strip()
        if not text:
            continue
        path = Path(text).expanduser()
        if not path.is_absolute():
            path = base / path
        try:
            real = str(path.resolve())
        except (OSError, ValueError):
            continue
        if parent and not any(
            real == root or real.startswith(root + os.sep) for root in parent
        ):
            # Non-widening: drop roots outside the parent's own allowance.
            continue
        resolved.append(real)
    # de-dupe, preserve order
    return list(dict.fromkeys(resolved))


def write_root_denial(path: str, task_id: str = "default", *, verb: str = "Write") -> Optional[str]:
    """Return an error when *task_id* has write roots and *path* is outside them."""
    roots = get_task_write_roots(task_id)
    if not roots:
        return None
    try:
        target = str(Path(path).expanduser().resolve(strict=False))
    except (OSError, ValueError):
        return f"{verb} denied: path could not be resolved under task write roots."
    for root in roots:
        if target == root or target.startswith(root + os.sep):
            return None
    joined = os.pathsep.join(roots)
    return (
        f"{verb} denied: '{path}' is outside task write roots ({joined})."
    )
