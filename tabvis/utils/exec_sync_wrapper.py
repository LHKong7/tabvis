"""Wrapped synchronous ``execSync``

The TS module wraps Node's ``child_process.execSync`` so an ANT build can time it via the
``slowLogging`` tagged template and surface slow shell-outs on the dev bar. The wrapper does not
change the call semantics: it runs ``command`` through a shell and returns the captured output
(``Buffer`` by default, ``string`` when an encoding is requested).

Behavior notes (per ``docs/SPINE_CONTRACTS.md``):
- ``child_process.execSync`` → :func:`subprocess.run` with ``shell=True`` (Node's ``execSync``
  always runs the command string through ``/bin/sh -c`` on POSIX / ``cmd.exe`` on Windows).
- The TS ``using _ = slowLogging`execSync: ${command.slice(0, 100)}``` tagged template becomes
  ``with slow_logging("execSync: " + command[:100]):`` — a no-op context manager in this
  external build (see :mod:`tabvis.utils.slow_operations`).
- Node's ``execSync`` THROWS on a non-zero exit (the error carries ``stdout``/``stderr``); we
  preserve that by passing ``check=True`` so a non-zero exit raises
  :class:`subprocess.CalledProcessError` (the closest faithful analogue — callers like
  :mod:`tabvis.utils.which` wrap the call in ``try/except``).
- ``options`` mirrors the Node ``ExecSyncOptions`` subset the callers use: ``encoding`` (``None``
  → ``bytes``, else a ``str`` decode), ``cwd``, ``env``, ``input``, ``timeout`` (ms → seconds),
  ``maxBuffer`` and ``stdio`` (a 3-tuple of ``'ignore'``/``'pipe'``/``'inherit'`` markers — only
  the ``stderr`` slot matters here; ``which`` passes ``['ignore','pipe','ignore']`` to silence
  stderr). ``mode``/permission fields are not part of this surface.

.. deprecated:: Prefer async alternatives. Sync exec calls block the event loop.
"""

from __future__ import annotations

import subprocess
from typing import Any

from tabvis.utils.slow_operations import slow_logging

# Node's execSync default; mirrors child_process' 1 MB cap. Kept for parity / overridable.
_DEFAULT_MAX_BUFFER = 1024 * 1024


def _resolve_stdio_redirect(slot: Any) -> Any:
    """Map a Node ``stdio`` slot marker to the subprocess redirect value.

    Node accepts ``'ignore'`` / ``'pipe'`` / ``'inherit'`` (and fds). We only need the cases the
    callers use: ``'ignore'`` → :data:`subprocess.DEVNULL`, ``'inherit'`` → ``None`` (inherit the
    parent's handle), ``'pipe'`` / anything else → :data:`subprocess.PIPE` (captured).
    """
    if slot == "ignore":
        return subprocess.DEVNULL
    if slot == "inherit":
        return None
    return subprocess.PIPE


def exec_sync_deprecated(
    command: str,
    options: dict[str, Any] | None = None,
) -> bytes | str:
    """Wrapped synchronous exec with slow-operation logging.

    Use this instead of calling :func:`subprocess.run` with ``shell=True`` directly so
    performance issues surface on the dev bar.

    Mirrors the TS overloads: with no ``encoding`` the result is ``bytes`` (Node ``Buffer``);
    with ``encoding`` set it is a decoded ``str``. Raises :class:`subprocess.CalledProcessError`
    on a non-zero exit (Node ``execSync`` throws).

    :param command: The shell command string to run.
    :param options: Optional Node-``ExecSyncOptions`` subset — ``encoding``, ``cwd``, ``env``,
        ``input``, ``timeout`` (ms), ``maxBuffer``, ``stdio``.
    """
    with slow_logging(f"execSync: {command[:100]}"):
        opts = options or {}
        encoding = opts.get("encoding")
        stdio = opts.get("stdio")

        # Default stdio: stdin inherits, stdout/stderr are captured (Node execSync returns
        # stdout and pipes stderr to the parent unless told otherwise). Callers that pass an
        # explicit `stdio` array override per-slot.
        stdin_redirect: Any = None
        stdout_redirect: Any = subprocess.PIPE
        stderr_redirect: Any = None
        if isinstance(stdio, (list, tuple)):
            if len(stdio) > 0:
                stdin_redirect = _resolve_stdio_redirect(stdio[0])
            if len(stdio) > 1:
                stdout_redirect = _resolve_stdio_redirect(stdio[1])
            if len(stdio) > 2:
                stderr_redirect = _resolve_stdio_redirect(stdio[2])

        timeout_ms = opts.get("timeout")
        timeout_s = (timeout_ms / 1000) if isinstance(timeout_ms, (int, float)) else None

        input_data = opts.get("input")
        # Node passes `input` as a string/Buffer regardless of encoding; we run in bytes mode and
        # decode the captured output ourselves so the `bytes` (no-encoding) overload is exact.
        if isinstance(input_data, str):
            input_bytes: bytes | None = input_data.encode("utf-8")
        else:
            input_bytes = input_data

        # subprocess.run rejects passing BOTH `stdin` and `input`; when an input payload is given,
        # `input` implies a stdin pipe, so omit the explicit `stdin` redirect.
        run_kwargs: dict[str, Any] = {
            "stdout": stdout_redirect,
            "stderr": stderr_redirect,
            "cwd": opts.get("cwd"),
            "env": opts.get("env"),
            "timeout": timeout_s,
        }
        if input_bytes is not None:
            run_kwargs["input"] = input_bytes
        else:
            run_kwargs["stdin"] = stdin_redirect

        completed = subprocess.run(  # noqa: S602 - shell=True mirrors Node execSync semantics
            command,
            shell=True,
            check=True,
            **run_kwargs,
        )

        stdout: bytes = completed.stdout if completed.stdout is not None else b""
        if encoding is None:
            return stdout
        if encoding in ("buffer", "binary"):
            return stdout
        return stdout.decode(encoding)
