"""ripgrep invocation + pure-Python fallback

The TS tree shells out to ripgrep (``rg``) for both content search (``GrepTool``) and file
listing (``GlobTool`` / ``glob.ts``), buffering stdout and splitting it into a ``string[]`` of
lines. ``ripGrep(args, target, abortSignal)`` is the buffered entry point both tools use.

This Python implementation keeps that surface but renames it ``rip_grep(args, target)`` (snake_case, per
the naming conventions; the ``AbortSignal`` is optional here). Per ``SPINE_CONTRACTS.md`` decision
#2 ("ripgrep: shell out to system ``rg`` when present; otherwise a pure-Python fallback walker.
No bundled rg."):

* When ``rg`` is resolvable on ``PATH`` (and ``USE_BUILTIN_RIPGREP`` is not forcing it off in a
  way we can honor — we have no bundled binary, so an explicit falsy value is the *only* reason
  to prefer system rg, and absence of rg means we fall back), we exec ``rg`` and return its
  trimmed, ``\\r``-stripped, blank-filtered stdout lines (exit 0 = matches, 1 = no matches).
* Otherwise a pure-Python walker (:func:`_python_fallback`) interprets the subset of ripgrep
  flags that ``GrepTool``/``glob.ts`` emit: ``--files``, ``--glob``/``-g``, ``--hidden``,
  ``--no-ignore``/``--no-ignore-parent``, ``--sort=modified``, ``-l``/``--files-with-matches``,
  ``-c``/``--count``, ``-n``, ``-i``, ``-C``/``-A``/``-B`` context, ``-e``, ``--type``, and a
  trailing literal pattern.

Faithful behaviors preserved from the TS ``ripGrep`` result handler:
* output is ``stdout.trim().split('\\n')`` mapped through ``line.replace(/\\r$/, '')`` then
  ``.filter(Boolean)`` (drop empty lines);
* exit code 1 ("no matches") resolves to ``[]``;
* ENOENT/EACCES/EPERM (and a missing ``rg`` binary, when no fallback applies) surface as errors;
* a timeout that produced *no* lines raises :class:`RipgrepTimeoutError`.

The embedded/builtin vendored-rg modes, codesign-on-darwin, EAGAIN single-thread retry, first-use
availability telemetry, and the streaming/file-count helpers (ripGrepStream / countFilesRoundedRg)
are not supported in this build — only the buffered ``rip_grep`` used by Grep + Glob is provided.
"""

from __future__ import annotations

import asyncio
import functools
import os
import re
import shutil
from pathlib import Path

from tabvis.utils.debug import log_for_debugging
from tabvis.utils.log import log_error

# 20MB; large monorepos can have 200k+ files (parity with TS MAX_BUFFER_SIZE).
MAX_BUFFER_SIZE = 20_000_000


class RipgrepTimeoutError(Exception):
    """Raised when an ``rg`` search times out without producing any complete results.

    Mirrors the TS ``RipgrepTimeoutError``: callers can distinguish a genuine timeout from
    an empty "no matches" result. ``partial_results`` carries any lines salvaged before the
    kill (with the last, possibly-torn, line dropped).
    """

    def __init__(self, message: str, partial_results: list[str]) -> None:
        super().__init__(message)
        self.partial_results = partial_results


def _is_env_defined_falsy(value: str | None) -> bool:
    """``True`` only for an explicit 0/false/no/off."""
    if value is None or value == "":
        return False
    return value.strip().lower() in ("0", "false", "no", "off")


def _is_env_truthy(value: str | None) -> bool:
    """``True`` for 1/true/yes/on."""
    if not value:
        return False
    return value.strip().lower() in ("1", "true", "yes", "on")


@functools.lru_cache(maxsize=1)
def _resolve_system_rg() -> str | None:
    """Resolve ``rg`` on PATH (TS ``findExecutable('rg', [])`` -> ``whichSync``).

    SECURITY (parity with TS): we resolve by name so the OS does PATH resolution; we never
    exec a relative ``./rg`` discovered in the cwd.
    """
    return shutil.which("rg")


def ripgrep_command() -> tuple[str | None, list[str]]:
    """Return ``(rg_path_or_None, base_args)`` describing how to invoke ripgrep.

    Return the ripgrep config.
    supports: system ``rg`` (when present) or ``None`` (no binary -> pure-Python fallback).
    No bundled/embedded binary ships with the Python implementation.
    """
    system_rg = _resolve_system_rg()
    # ``USE_BUILTIN_RIPGREP`` explicitly falsy means "prefer system rg". We have no builtin
    # binary, so we always prefer system rg when it exists regardless; the env var is honored
    # only insofar as it can never force a (nonexistent) builtin.
    _ = _is_env_defined_falsy(os.environ.get("USE_BUILTIN_RIPGREP"))
    if system_rg is not None:
        return system_rg, []
    return None, []


def _platform_default_timeout_ms() -> int:
    """20s default; TABVIS_GLOB_TIMEOUT_SECONDS overrides (WSL's 60s default is not detected here)."""
    parsed = 0
    raw = os.environ.get("TABVIS_GLOB_TIMEOUT_SECONDS") or ""
    try:
        parsed = int(raw)
    except ValueError:
        parsed = 0
    if parsed > 0:
        return parsed * 1000
    return 20_000


def _split_lines(stdout: str) -> list[str]:
    """``stdout.trim().split('\\n').map(strip \\r).filter(Boolean)`` — the TS success transform."""
    trimmed = stdout.strip()
    if not trimmed:
        return []
    return [line[:-1] if line.endswith("\r") else line for line in trimmed.split("\n") if line]


async def _run_system_rg(rg_path: str, full_args: list[str], timeout_ms: int) -> list[str]:
    """Exec system ``rg`` and return result lines (async, non-blocking)."""
    proc = await asyncio.create_subprocess_exec(
        rg_path,
        *full_args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    timed_out = False
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_ms / 1000
        )
    except TimeoutError:
        timed_out = True
        proc.kill()
        stdout_b, stderr_b = await proc.communicate()

    stdout = (stdout_b or b"")[:MAX_BUFFER_SIZE].decode("utf-8", "replace")
    stderr = (stderr_b or b"")[:MAX_BUFFER_SIZE].decode("utf-8", "replace")
    code = proc.returncode

    if not timed_out and code in (0, 1):
        # 0 = matches found, 1 = no matches (both success).
        return _split_lines(stdout)

    # Salvage partial output; drop a possibly-torn final line on timeout.
    lines = _split_lines(stdout)
    if timed_out and lines:
        lines = lines[:-1]

    log_for_debugging(
        f"rg error (timed_out={timed_out}, code={code}, stderr: {stderr}), {len(lines)} results"
    )

    if timed_out and not lines:
        secs = _platform_default_timeout_ms() // 1000
        raise RipgrepTimeoutError(
            f"Ripgrep search timed out after {secs} seconds. The search may have matched files "
            "but did not complete in time. Try searching a more specific path or pattern.",
            lines,
        )
    return lines


async def rip_grep(args: list[str], target: str) -> list[str]:
    """Run ripgrep over ``target`` with ``args`` and return matching lines.

        ``glob.ts``. ``target`` is appended as the final positional argument (ripgrep hangs in
    non-interactive mode without a path). Returns the trimmed, ``\\r``-stripped, blank-filtered
    stdout lines; "no matches" (rg exit 1) is ``[]``.

    When system ``rg`` is unavailable, falls back to a pure-Python walker that interprets the
    flag subset Grep/Glob emit.
    """
    rg_path, base_args = ripgrep_command()
    full_args = [*base_args, *args, target]

    if rg_path is not None:
        try:
            return await _run_system_rg(rg_path, full_args, _platform_default_timeout_ms())
        except RipgrepTimeoutError:
            raise
        except FileNotFoundError as err:
            # rg vanished between resolution and exec — fall through to the walker.
            log_for_debugging(f"rg exec failed ({err}); using pure-Python fallback")
        except OSError as err:
            log_error(err)
            raise

    # Pure-Python fallback (no rg binary available).
    return await asyncio.to_thread(_python_fallback, args, target)


# --------------------------------------------------------------------------------------------
# Pure-Python fallback walker
# --------------------------------------------------------------------------------------------

_DEFAULT_IGNORE_DIRS = {".git", "node_modules", ".hg", ".svn"}


class _ParsedArgs:
    __slots__ = (
        "files_mode",
        "globs",
        "neg_globs",
        "hidden",
        "no_ignore",
        "sort_modified",
        "list_files",
        "count",
        "line_numbers",
        "ignore_case",
        "ctx_before",
        "ctx_after",
        "pattern",
    )

    def __init__(self) -> None:
        self.files_mode = False
        self.globs: list[str] = []
        self.neg_globs: list[str] = []
        self.hidden = False
        self.no_ignore = False
        self.sort_modified = False
        self.list_files = False
        self.count = False
        self.line_numbers = False
        self.ignore_case = False
        self.ctx_before = 0
        self.ctx_after = 0
        self.pattern: str | None = None


def _parse_args(args: list[str]) -> _ParsedArgs:
    """Interpret the ripgrep flag subset Grep/Glob emit. Unknown flags are ignored."""
    p = _ParsedArgs()
    i = 0
    n = len(args)
    while i < n:
        a = args[i]
        if a == "--files":
            p.files_mode = True
        elif a in ("--glob", "-g") and i + 1 < n:
            i += 1
            _add_glob(p, args[i])
        elif a.startswith("--glob="):
            _add_glob(p, a[len("--glob=") :])
        elif a == "--hidden":
            p.hidden = True
        elif a in ("--no-ignore", "--no-ignore-parent"):
            p.no_ignore = True
        elif a == "--sort=modified" or a == "--sort" and i + 1 < n and args[i + 1] == "modified":
            p.sort_modified = True
            if a == "--sort":
                i += 1
        elif a in ("-l", "--files-with-matches"):
            p.list_files = True
        elif a in ("-c", "--count"):
            p.count = True
        elif a == "-n":
            p.line_numbers = True
        elif a in ("-i", "--ignore-case"):
            p.ignore_case = True
        elif a in ("-C", "--context") and i + 1 < n:
            i += 1
            p.ctx_before = p.ctx_after = _safe_int(args[i])
        elif a in ("-B", "--before-context") and i + 1 < n:
            i += 1
            p.ctx_before = _safe_int(args[i])
        elif a in ("-A", "--after-context") and i + 1 < n:
            i += 1
            p.ctx_after = _safe_int(args[i])
        elif a in ("-e", "--regexp") and i + 1 < n:
            i += 1
            p.pattern = args[i]
        elif a == "--type" and i + 1 < n:
            i += 1  # type filter not modeled in fallback
        elif a in ("--max-columns", "-M", "--max-count", "-m") and i + 1 < n:
            i += 1  # value-taking flag; value not modeled in fallback (must skip it,
            # else the value is misread as the trailing literal pattern)
        elif a.startswith(("--max-columns=", "--max-count=")):
            pass
        elif a in ("-U", "--multiline", "--multiline-dotall"):
            pass  # multiline not modeled in fallback (single-line match only)
        elif a.startswith("-"):
            pass  # unknown flag — ignore
        elif p.pattern is None:
            p.pattern = a
        i += 1
    return p


def _add_glob(p: _ParsedArgs, glob: str) -> None:
    if glob.startswith("!"):
        p.neg_globs.append(glob[1:])
    else:
        p.globs.append(glob)


def _safe_int(s: str) -> int:
    try:
        return int(s)
    except ValueError:
        return 0


def _rg_glob_to_regex(glob: str) -> re.Pattern[str]:
    """Translate a ripgrep ``--glob`` pattern into a regex matched against a relative path.

    Supports ``*`` (no ``/``), ``**`` (any incl ``/``), ``?``, ``[...]`` classes, and ``{a,b}``
    alternation. A leading ``**/`` is also allowed to match at the root (so ``**/*.py`` matches
    ``a.py``). A bare ``*.py`` matches at any depth (ripgrep semantics for unanchored globs).
    """
    anchored = glob.startswith("/")
    g = glob[1:] if anchored else glob
    out: list[str] = []
    i = 0
    length = len(g)
    while i < length:
        c = g[i]
        if c == "*":
            if i + 1 < length and g[i + 1] == "*":
                i += 1
                # consume optional trailing slash of **/
                if i + 1 < length and g[i + 1] == "/":
                    i += 1
                    out.append("(?:.*/)?")
                else:
                    out.append(".*")
            else:
                out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            j = i + 1
            if j < length and g[j] in ("!", "^"):
                j += 1
            if j < length and g[j] == "]":
                j += 1
            while j < length and g[j] != "]":
                j += 1
            klass = g[i + 1 : j]
            if klass.startswith("!"):
                klass = "^" + klass[1:]
            out.append("[" + klass + "]")
            i = j
        elif c == "{":
            j = g.find("}", i)
            if j == -1:
                out.append(re.escape(c))
            else:
                alts = g[i + 1 : j].split(",")
                out.append("(?:" + "|".join(re.escape(a) for a in alts) + ")")
                i = j
        else:
            out.append(re.escape(c))
        i += 1
    body = "".join(out)
    if anchored:
        pattern = "^" + body + "$"
    elif "/" in g:
        pattern = "^" + body + "$"
    else:
        # bare filename glob: match at any depth.
        pattern = "(?:^|/)" + body + "$"
    return re.compile(pattern)


def _iter_files(root: Path, parsed: _ParsedArgs) -> list[tuple[Path, str]]:
    """Walk ``root`` honoring hidden/no_ignore flags; return ``(abs_path, rel_posix)`` pairs."""
    results: list[tuple[Path, str]] = []
    if root.is_file():
        return [(root, root.name)]
    for dirpath, dirnames, filenames in os.walk(root):
        # prune directories in-place
        kept = []
        for d in dirnames:
            if not parsed.hidden and d.startswith("."):
                continue
            if not parsed.no_ignore and d in _DEFAULT_IGNORE_DIRS:
                continue
            kept.append(d)
        dirnames[:] = kept
        for f in filenames:
            if not parsed.hidden and f.startswith("."):
                continue
            abs_path = Path(dirpath) / f
            rel = abs_path.relative_to(root).as_posix()
            results.append((abs_path, rel))
    return results


def _matches_globs(rel: str, parsed: _ParsedArgs) -> bool:
    for neg in parsed.neg_globs:
        if _rg_glob_to_regex(neg).search(rel):
            return False
    if not parsed.globs:
        return True
    return any(_rg_glob_to_regex(g).search(rel) for g in parsed.globs)


def _python_fallback(args: list[str], target: str) -> list[str]:
    """Pure-Python emulation of the ripgrep features Grep/Glob rely on.

    Returns lines in the same shape as system rg would (filtered, ``\\r``-free). Best-effort:
    not a complete ripgrep reimplementation, but faithful for the flag set the two callers emit.
    """
    parsed = _parse_args(args)
    root = Path(target)
    if not root.exists():
        return []

    entries = _iter_files(root, parsed)

    if parsed.sort_modified:
        def _mtime(item: tuple[Path, str]) -> float:
            try:
                return item[0].stat().st_mtime
            except OSError:
                return 0.0

        entries.sort(key=_mtime)

    # --files: list file paths. System rg prints each path joined onto the target
    # argument the callers pass (always ABSOLUTE: expand_path(path) or get_cwd()), so the
    # fallback must emit absolute paths too — both Grep and Glob consume rg output as
    # absolute (to_relative_path + os.stat) and would mishandle relative paths.
    if parsed.files_mode:
        out = [abs_path.as_posix() for abs_path, rel in entries if _matches_globs(rel, parsed)]
        return [line for line in out if line]

    if parsed.pattern is None:
        return []

    flags = re.IGNORECASE if parsed.ignore_case else 0
    try:
        regex = re.compile(parsed.pattern, flags)
    except re.error:
        regex = re.compile(re.escape(parsed.pattern), flags)

    lines_out: list[str] = []
    for abs_path, rel in entries:
        if not _matches_globs(rel, parsed):
            continue
        try:
            text = abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        file_lines = text.split("\n")
        matched_idx = [idx for idx, ln in enumerate(file_lines) if regex.search(ln)]
        if not matched_idx:
            continue

        # Emit absolute paths to match system rg (see _files-mode note above).
        path_str = abs_path.as_posix()
        if parsed.list_files:
            lines_out.append(path_str)
            continue
        if parsed.count:
            lines_out.append(f"{path_str}:{len(matched_idx)}")
            continue

        # content mode (optionally with context + line numbers)
        emit: set[int] = set()
        for idx in matched_idx:
            lo = max(0, idx - parsed.ctx_before)
            hi = min(len(file_lines) - 1, idx + parsed.ctx_after)
            emit.update(range(lo, hi + 1))
        for idx in sorted(emit):
            ln = file_lines[idx]
            if parsed.line_numbers:
                lines_out.append(f"{path_str}:{idx + 1}:{ln}")
            else:
                lines_out.append(f"{path_str}:{ln}")

    return [line for line in lines_out if line]
