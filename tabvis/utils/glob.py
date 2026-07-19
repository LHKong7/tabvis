"""Glob file search

The TS ``glob`` shells out to ripgrep ``--files --glob <pat> --sort=modified`` to list files
matching a glob pattern (oldest-modified-first), converts relative paths to absolute, then
applies ``{offset, limit}`` slicing and reports ``truncated``. This Python implementation preserves that
behavior:

- ``extract_glob_base_directory``: pulls the static prefix before the first glob metacharacter
  (``* ? [ {``) so absolute patterns can be rewritten to a search dir + relative pattern (rg's
  ``--glob`` only accepts relative patterns).
- ``glob``: builds the equivalent rg argv (``--no-ignore``/``--hidden`` gated by
  ``TABVIS_GLOB_NO_IGNORE`` / ``TABVIS_GLOB_HIDDEN``, both defaulting to *true*), shells out to ``rg``
  when available, else walks the tree in pure Python and filters via ``pathspec`` gitwildmatch
  (the same flavor rg's ``--glob`` uses), sorting by mtime ascending to mirror ``--sort=modified``.

Casing: Python identifiers are snake_case; the returned dict keeps the wire keys ``files`` /
``truncated`` from the TS contract.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from typing import Any

import pathspec

from tabvis.utils.abort import AbortSignal

# ---------------------------------------------------------------------------
# Env helpers.
# ---------------------------------------------------------------------------

_TRUTHY = {"1", "true", "yes", "on"}


def _is_env_truthy(value: str | bool | None) -> bool:
    if not value:
        return False
    if isinstance(value, bool):
        return value
    return value.lower().strip() in _TRUTHY


# ---------------------------------------------------------------------------
# Permission ignore patterns.
#
# getFileReadIgnorePatterns() + normalizePatternsToPath() (src/utils/permissions/filesystem.ts).
# Per docs/SPINE_CONTRACTS.md decision 3, the walking skeleton has no configured permission
# rules, so the deny set is empty. When the permissions layer lands, read `read`/`deny` patterns
# and resolve them relative to `search_dir`.
# ---------------------------------------------------------------------------


def _get_ignore_patterns(
    tool_permission_context: dict[str, Any] | None,
    search_dir: str,  # noqa: ARG001 - kept for parity with normalizePatternsToPath(_, searchDir)
) -> list[str]:
    if not tool_permission_context:
        return []
    # Read-tool deny rules → ignore patterns. Empty in the skeleton.
    deny = tool_permission_context.get("alwaysDenyRules") or {}
    # A full permission configuration resolves these by root relative to search_dir.
    if not deny:
        return []
    return []


# ---------------------------------------------------------------------------
# extractGlobBaseDirectory
# ---------------------------------------------------------------------------

_GLOB_CHARS = re.compile(r"[*?[{]")


def extract_glob_base_directory(pattern: str) -> dict[str, str]:
    """Split ``pattern`` into ``{baseDir, relativePattern}``.

    ``baseDir`` is everything before the first glob metacharacter (``* ? [ {``); the remaining
    relative pattern is what rg's ``--glob`` consumes. Mirrors the TS implementation, including
    the literal-path branch (no glob chars) and the root-directory edge case.
    """
    match = _GLOB_CHARS.search(pattern)

    if match is None:
        # No glob characters — literal path. dirname/basename split.
        return {
            "baseDir": os.path.dirname(pattern),
            "relativePattern": os.path.basename(pattern),
        }

    static_prefix = pattern[: match.start()]

    # Find the last path separator in the static prefix (handle both / and os.sep).
    last_sep_index = max(static_prefix.rfind("/"), static_prefix.rfind(os.sep))

    if last_sep_index == -1:
        # No separator before the glob — pattern is relative to cwd.
        return {"baseDir": "", "relativePattern": pattern}

    base_dir = static_prefix[:last_sep_index]
    relative_pattern = pattern[last_sep_index + 1 :]

    # Root directory patterns (e.g. /*.txt): baseDir empty but separator at index 0 → use '/'.
    if base_dir == "" and last_sep_index == 0:
        base_dir = "/"

    # Windows drive root (C:/*.txt) — 'C:' is relative; we want 'C:\'. POSIX no-op.
    if os.name == "nt" and re.fullmatch(r"[A-Za-z]:", base_dir):
        base_dir = base_dir + os.sep

    return {"baseDir": base_dir, "relativePattern": relative_pattern}


# ---------------------------------------------------------------------------
# ripgrep invocation
# ---------------------------------------------------------------------------


def _rg_files(args: list[str], target: str, abort_signal: AbortSignal | None) -> list[str]:
    """Run ``rg <args> <target>`` and return the trimmed, non-empty path lines.

    Exit code 0 (matches) and 1 (no matches) are both success. Other codes raise. Mirrors the
    success-path handling of TS ``ripGrep`` (the EAGAIN-retry / timeout machinery is omitted in
    the skeleton — see the pure-Python fallback for the rg-less case).
    """
    if abort_signal is not None and abort_signal.aborted:
        return []

    proc = subprocess.run(  # noqa: S603 - args are constructed internally, not shell
        ["rg", *args, target],  # noqa: S607 - resolved via PATH on purpose (no shell hijack)
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode not in (0, 1):
        raise RuntimeError(
            f"ripgrep exited with code {proc.returncode}: {proc.stderr.strip()}"
        )
    return [
        line.rstrip("\r")
        for line in proc.stdout.strip().split("\n")
        if line.strip()
    ]


# ---------------------------------------------------------------------------
# pure-Python fallback (no rg on PATH)
# ---------------------------------------------------------------------------


def _build_spec(patterns: list[str]) -> pathspec.PathSpec:
    """gitwildmatch-flavored PathSpec (rg --glob uses gitignore glob semantics).

    Prefer the non-deprecated ``gitignore`` factory; fall back to ``gitwildmatch`` on older
    pathspec builds that lack it.
    """
    try:
        return pathspec.PathSpec.from_lines("gitignore", patterns)
    except (ValueError, KeyError):
        return pathspec.PathSpec.from_lines("gitwildmatch", patterns)


def _walk_files(
    search_dir: str,
    search_pattern: str,
    ignore_patterns: list[str],
    hidden: bool,
    abort_signal: AbortSignal | None,
) -> list[str]:
    """os.walk + pathspec equivalent of ``rg --files --glob <pat> --sort=modified``.

    Returns paths relative to ``search_dir`` (matching rg's output), sorted oldest-mtime-first.
    """
    base = search_dir or "."
    if not os.path.isdir(base):
        return []

    spec = _build_spec([search_pattern])
    ignore_spec = _build_spec(ignore_patterns) if ignore_patterns else None

    matched: list[tuple[float, str]] = []
    for root, dirs, files in os.walk(base):
        if abort_signal is not None and abort_signal.aborted:
            break
        if not hidden:
            dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            if not hidden and name.startswith("."):
                continue
            abs_path = os.path.join(root, name)
            rel = os.path.relpath(abs_path, base)
            rel_posix = rel.replace(os.sep, "/")
            if not spec.match_file(rel_posix):
                continue
            if ignore_spec is not None and ignore_spec.match_file(rel_posix):
                continue
            try:
                mtime = os.stat(abs_path).st_mtime
            except OSError:
                continue
            matched.append((mtime, rel))

    # --sort=modified is oldest-first (ascending mtime).
    matched.sort(key=lambda item: item[0])
    return [rel for _mtime, rel in matched]


# ---------------------------------------------------------------------------
# glob
# ---------------------------------------------------------------------------


async def glob(
    file_pattern: str,
    cwd: str,
    options: dict[str, int],
    abort_signal: AbortSignal | None = None,
    tool_permission_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """List files matching ``file_pattern`` under ``cwd``.

    ``options`` is ``{'limit': int, 'offset': int}`` (offset defaults to 0). Returns
    ``{'files': list[str], 'truncated': bool}`` with absolute paths, sliced by offset/limit.
    Files come back oldest-modified-first (``--sort=modified``).
    """
    limit = options.get("limit", 0)
    offset = options.get("offset", 0)

    search_dir = cwd
    search_pattern = file_pattern

    # Absolute patterns: rewrite to a search dir + relative pattern (rg --glob needs relative).
    if os.path.isabs(file_pattern):
        extracted = extract_glob_base_directory(file_pattern)
        if extracted["baseDir"]:
            search_dir = extracted["baseDir"]
            search_pattern = extracted["relativePattern"]

    ignore_patterns = _get_ignore_patterns(tool_permission_context, search_dir)

    # Env gates: empty string is treated as unset (TS uses `|| 'true'`).
    no_ignore = _is_env_truthy(os.environ.get("TABVIS_GLOB_NO_IGNORE") or "true")
    hidden = _is_env_truthy(os.environ.get("TABVIS_GLOB_HIDDEN") or "true")

    if shutil.which("rg"):
        args = ["--files", "--glob", search_pattern, "--sort=modified"]
        if no_ignore:
            args.append("--no-ignore")
        if hidden:
            args.append("--hidden")
        for pattern in ignore_patterns:
            args.extend(["--glob", f"!{pattern}"])
        all_paths = _rg_files(args, search_dir, abort_signal)
    else:
        # --no-ignore is always on). no_ignore is accepted for parity but not honored here.
        all_paths = _walk_files(
            search_dir, search_pattern, ignore_patterns, hidden, abort_signal
        )

    # ripgrep returns relative paths — convert to absolute.
    absolute_paths = [
        p if os.path.isabs(p) else os.path.join(search_dir, p) for p in all_paths
    ]

    truncated = len(absolute_paths) > offset + limit
    files = absolute_paths[offset : offset + limit]

    return {"files": files, "truncated": truncated}
