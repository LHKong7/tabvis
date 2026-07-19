"""Portable synchronous shell-exec with defaults

Provides :func:`exec_sync_with_defaults_deprecated`, a forgiving synchronous shell-out that never
throws on a non-zero exit and returns the trimmed stdout (or ``None`` when there is no output / on
any failure). The TS module uses ``execa`` with ``{ shell: true, reject: false }``; we map that to
:func:`subprocess.run` with ``shell=True`` and no ``check`` (so a non-zero exit is swallowed).

Behavior notes (per ``docs/SPINE_CONTRACTS.md``):
- ``execa(command, { shell: true, reject: false, env: process.env, cwd: getCwd(),
  maxBuffer: 1_000_000, timeout, stdio, input })`` → :func:`subprocess.run`. ``reject: false`` →
  no ``check=True``; ``shell: true`` → ``shell=True``; ``env: process.env`` → :data:`os.environ`;
  ``cwd: getCwd()`` → :func:`tabvis.utils.cwd.get_cwd`.
- The overloads collapse to one Python signature: the second arg is EITHER an options dict OR an
  abort-signal-like object (old call shape). We detect an :class:`~tabvis.utils.abort.AbortSignal`
  (anything exposing ``throw_if_aborted`` / ``aborted``) and route it to ``options.abort_signal``,
  matching the TS ``optionsOrAbortSignal instanceof AbortSignal`` branch.
- ``abortSignal?.throwIfAborted()`` is honored before the slow-logging window opens.
- ``slowLogging`exec: ${command.slice(0, 200)}``` → ``with slow_logging("exec: " + command[:200]):``
  (a no-op in this external build).
- Default ``stdio`` is ``['ignore', 'pipe', 'pipe']`` (stdin ignored, stdout + stderr captured).
- Return contract: ``None`` when stdout is empty/whitespace OR on ANY exception (timeout, spawn
  failure, abort) — the TS ``try { … } catch { return null }`` swallows everything.

.. deprecated:: Use async exec directly. Sync exec blocks the event loop.
"""

from __future__ import annotations

import os
import subprocess
from typing import Any

from tabvis.utils.cwd import get_cwd
from tabvis.utils.slow_operations import slow_logging

_MS_IN_SECOND = 1000
_SECONDS_IN_MINUTE = 60
_DEFAULT_TIMEOUT_MS = 10 * _SECONDS_IN_MINUTE * _MS_IN_SECOND
_MAX_BUFFER = 1_000_000


def _is_abort_signal(value: Any) -> bool:
    """Heuristic for the old ``execSyncWithDefaults_DEPRECATED(command, abortSignal, timeout)``
    call shape: a non-dict object exposing the AbortSignal surface (``aborted`` /
    ``throw_if_aborted``)."""
    if value is None or isinstance(value, dict):
        return False
    return hasattr(value, "throw_if_aborted") or hasattr(value, "aborted")


def _resolve_stdio_redirect(slot: Any) -> Any:
    if slot == "ignore":
        return subprocess.DEVNULL
    if slot == "inherit":
        return None
    return subprocess.PIPE


def exec_sync_with_defaults_deprecated(
    command: str,
    options_or_abort_signal: Any = None,
    timeout: int = _DEFAULT_TIMEOUT_MS,
) -> str | None:
    """Run ``command`` through a shell with forgiving defaults; never raises.

    Returns the trimmed stdout, or ``None`` when stdout is empty / on any error.

    :param command: Shell command string.
    :param options_or_abort_signal: Either an options dict (``abort_signal``, ``timeout``,
        ``input``, ``stdio``) or an abort-signal object (old signature, paired with ``timeout``).
    :param timeout: Timeout in ms used only when the second arg is an abort signal.
    """
    options: dict[str, Any]
    if options_or_abort_signal is None:
        options = {}
    elif _is_abort_signal(options_or_abort_signal):
        options = {"abort_signal": options_or_abort_signal, "timeout": timeout}
    else:
        options = dict(options_or_abort_signal)

    abort_signal = options.get("abort_signal")
    final_timeout = options.get("timeout")
    if not isinstance(final_timeout, (int, float)):
        final_timeout = _DEFAULT_TIMEOUT_MS
    input_data = options.get("input")
    stdio = options.get("stdio", ["ignore", "pipe", "pipe"])

    # abortSignal?.throwIfAborted()
    if abort_signal is not None:
        throw_if_aborted = getattr(abort_signal, "throw_if_aborted", None)
        if callable(throw_if_aborted):
            throw_if_aborted()

    with slow_logging(f"exec: {command[:200]}"):
        try:
            stdin_redirect = _resolve_stdio_redirect(stdio[0]) if len(stdio) > 0 else subprocess.DEVNULL
            stdout_redirect = _resolve_stdio_redirect(stdio[1]) if len(stdio) > 1 else subprocess.PIPE
            stderr_redirect = _resolve_stdio_redirect(stdio[2]) if len(stdio) > 2 else subprocess.PIPE

            input_bytes: bytes | None
            if isinstance(input_data, str):
                input_bytes = input_data.encode("utf-8")
            elif isinstance(input_data, bytes):
                input_bytes = input_data
            else:
                input_bytes = None

            # subprocess.run rejects passing BOTH `stdin` and `input`; when an input payload is
            # given, `input` implies a stdin pipe, so omit the explicit `stdin` redirect.
            run_kwargs: dict[str, Any] = {
                "stdout": stdout_redirect,
                "stderr": stderr_redirect,
                "cwd": get_cwd(),
                "env": dict(os.environ),
                "timeout": final_timeout / _MS_IN_SECOND,
            }
            if input_bytes is not None:
                run_kwargs["input"] = input_bytes
            else:
                run_kwargs["stdin"] = stdin_redirect

            completed = subprocess.run(  # noqa: S602 - shell=True mirrors execa { shell: true }
                command,
                shell=True,
                check=False,  # reject: false — don't throw on non-zero exit codes
                **run_kwargs,
            )

            stdout_bytes: bytes = completed.stdout if completed.stdout is not None else b""
            stdout = stdout_bytes.decode("utf-8", errors="replace")
            if not stdout:
                return None
            trimmed = stdout.strip()
            return trimmed or None
        except Exception:  # noqa: BLE001 - TS `catch { return null }` swallows everything
            return None
