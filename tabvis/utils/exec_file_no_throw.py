"""Async exec wrappers that never throw

These wrappers over ``node:child_process`` (via ``execa`` in TS) ease error handling and
cross-platform compatibility. :func:`exec_file_no_throw` and :func:`exec_file_no_throw_with_cwd`
always RESOLVE (never raise): on a non-zero exit / spawn failure they return a result dict
``{stdout, stderr, code, error?}`` rather than throwing.

Re-exports :func:`tabvis.utils.exec_file_no_throw_portable.exec_sync_with_defaults_deprecated`
(the TS ``export { execSyncWithDefaults_DEPRECATED } from './execFileNoThrowPortable.js'``).

Behavior notes (per ``docs/SPINE_CONTRACTS.md``):
- ``execa(file, args, { reject: false, maxBuffer, signal, timeout, cwd, env, shell, stdin, input })``
  → :func:`asyncio.create_subprocess_exec` (or ``_shell`` when ``shell`` is set). ``reject: false``
  is intrinsic to ``create_subprocess_*`` (a non-zero exit is just a returncode, not an
  exception); we replicate the execa result shape (``failed`` ⇔ non-zero exit or signal) and the
  ``preserveOutputOnError`` branch.
- Return dict keys (``stdout``/``stderr``/``code``/``error``) are kept verbatim — they round-trip
  to callers and (transitively) into diagnostics, so they are wire keys.
- ``getErrorMessage`` priority preserved: ``shortMessage`` (synthesized here as execa's
  "Command failed with exit code N: <file> <args>" / "...was killed with SIG..."), then
  ``signal`` name, then the numeric exit code.
- ``abortSignal`` → kill the child when the signal fires (execa ``signal`` option).
- ``input`` is written to stdin (``stdin='pipe'``); a non-``input`` ``stdin`` of ``'ignore'`` /
  ``'inherit'`` / ``'pipe'`` maps to DEVNULL / inherit / PIPE.
- The outer ``.catch`` (spawn-level failure) → ``{stdout:'', stderr:'', code:1}`` after
  :func:`tabvis.utils.log.log_error`.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal as _signal
from typing import Any

from tabvis.utils.cwd import get_cwd
from tabvis.utils.exec_file_no_throw_portable import (
    exec_sync_with_defaults_deprecated as exec_sync_with_defaults_deprecated,
)
from tabvis.utils.log import log_error

_MS_IN_SECOND = 1000
_SECONDS_IN_MINUTE = 60
_DEFAULT_TIMEOUT_MS = 10 * _SECONDS_IN_MINUTE * _MS_IN_SECOND
_DEFAULT_MAX_BUFFER = 1_000_000


async def exec_file_no_throw(
    file: str,
    args: list[str],
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """execFile that always resolves. Thin entry over :func:`exec_file_no_throw_with_cwd`.

    ``options`` (Node ``ExecFileOptions`` subset): ``abort_signal``, ``timeout`` (ms),
    ``preserve_output_on_error`` (default ``True``), ``use_cwd`` (default ``True`` — when ``False``
    no cwd is passed, breaking the ``get_cwd → PersistentShell`` init cycle), ``env``, ``stdin``,
    ``input``.
    """
    if options is None:
        options = {
            "timeout": _DEFAULT_TIMEOUT_MS,
            "preserve_output_on_error": True,
            "use_cwd": True,
        }
    use_cwd = options.get("use_cwd", True)
    return await exec_file_no_throw_with_cwd(
        file,
        args,
        {
            "abort_signal": options.get("abort_signal"),
            "timeout": options.get("timeout"),
            "preserve_output_on_error": options.get("preserve_output_on_error"),
            "cwd": get_cwd() if use_cwd else None,
            "env": options.get("env"),
            "stdin": options.get("stdin"),
            "input": options.get("input"),
        },
    )


def _get_error_message(short_message: str | None, sig: str | None, error_code: int) -> str:
    """Extract a human-readable error message from a (synthesized) execa result.

    Priority: ``shortMessage`` (already includes signal info), then the ``signal`` name, then the
    numeric exit code.
    """
    if short_message:
        return short_message
    if isinstance(sig, str):
        return sig
    return str(error_code)


def _resolve_stdin(stdin: Any, has_input: bool) -> Any:
    if has_input:
        return asyncio.subprocess.PIPE
    if stdin == "ignore":
        return asyncio.subprocess.DEVNULL
    if stdin == "inherit":
        return None
    if stdin == "pipe":
        return asyncio.subprocess.PIPE
    # execa default when no stdin/input is given: stdin is inherited/ignored. Node's execFile
    # defaults to a pipe with no writer; DEVNULL is the safe non-blocking analogue.
    return asyncio.subprocess.DEVNULL


async def exec_file_no_throw_with_cwd(
    file: str,
    args: list[str],
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """execFile, but always resolves (never throws).

    Returns ``{"stdout": str, "stderr": str, "code": int, "error"?: str}``.
    """
    if options is None:
        options = {
            "timeout": _DEFAULT_TIMEOUT_MS,
            "preserve_output_on_error": True,
            "max_buffer": _DEFAULT_MAX_BUFFER,
        }

    abort_signal = options.get("abort_signal")
    final_timeout = options.get("timeout")
    if not isinstance(final_timeout, (int, float)):
        final_timeout = _DEFAULT_TIMEOUT_MS
    preserve_output = options.get("preserve_output_on_error")
    if preserve_output is None:
        preserve_output = True
    final_cwd = options.get("cwd")
    final_env = options.get("env")
    shell = options.get("shell")
    final_stdin = options.get("stdin")
    final_input = options.get("input")

    input_bytes: bytes | None
    if isinstance(final_input, str):
        input_bytes = final_input.encode("utf-8")
    elif isinstance(final_input, bytes):
        input_bytes = final_input
    else:
        input_bytes = None

    stdin_redirect = _resolve_stdin(final_stdin, input_bytes is not None)

    try:
        if shell:
            # execa({ shell }) runs the assembled command line through a shell.
            command_line = " ".join([file, *args]) if not isinstance(shell, str) else f"{file} {' '.join(args)}"
            proc = await asyncio.create_subprocess_shell(
                command_line,
                stdin=stdin_redirect,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=final_cwd,
                env=final_env if final_env is not None else dict(os.environ),
            )
        else:
            proc = await asyncio.create_subprocess_exec(
                file,
                *args,
                stdin=stdin_redirect,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=final_cwd,
                env=final_env if final_env is not None else dict(os.environ),
            )
    except (OSError, ValueError) as error:
        # Spawn-level failure (ENOENT, bad args, …) → mirror the execa `.catch` branch.
        log_error(error)
        return {"stdout": "", "stderr": "", "code": 1}

    # Wire the abort signal to kill the child (execa `signal` option).
    abort_cb_registered = False
    if abort_signal is not None and hasattr(abort_signal, "add_event_listener"):
        def _on_abort() -> None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()

        if getattr(abort_signal, "aborted", False):
            _on_abort()
        else:
            abort_signal.add_event_listener("abort", _on_abort)
            abort_cb_registered = True

    timed_out = False
    signal_name: str | None = None
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(input=input_bytes),
            timeout=final_timeout / _MS_IN_SECOND,
        )
    except TimeoutError:
        timed_out = True
        signal_name = "SIGTERM"
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        stdout_b, stderr_b = await proc.communicate()
    except Exception as error:  # noqa: BLE001 - mirror execa outer `.catch`
        log_error(error)
        return {"stdout": "", "stderr": "", "code": 1}

    _ = abort_cb_registered  # listener fires for its side effect (kill); nothing to unregister

    stdout = (stdout_b or b"").decode("utf-8", errors="replace")
    stderr = (stderr_b or b"").decode("utf-8", errors="replace")
    return_code = proc.returncode if proc.returncode is not None else 1

    # execa marks `failed` on non-zero exit OR a terminating signal. A negative returncode from
    # asyncio means the child was killed by signal -N.
    killed_by_signal = return_code < 0
    if killed_by_signal and signal_name is None:
        with contextlib.suppress(ValueError):
            signal_name = _signal.Signals(-return_code).name
    failed = return_code != 0 or timed_out or killed_by_signal

    if not failed:
        return {"stdout": stdout, "stderr": stderr, "code": 0}

    if not preserve_output:
        exit_code = return_code if return_code > 0 else 1
        return {"stdout": "", "stderr": "", "code": exit_code}

    error_code = return_code if return_code > 0 else 1
    # Synthesize execa's `shortMessage` so getErrorMessage's priority order is exercised.
    cmd_str = " ".join([file, *args])
    if timed_out:
        short_message = f"Command timed out after {final_timeout}ms: {cmd_str}"
    elif killed_by_signal and signal_name:
        short_message = f"Command was killed with {signal_name}: {cmd_str}"
    else:
        short_message = f"Command failed with exit code {error_code}: {cmd_str}"

    return {
        "stdout": stdout or "",
        "stderr": stderr or "",
        "code": error_code,
        "error": _get_error_message(short_message, signal_name, error_code),
    }
