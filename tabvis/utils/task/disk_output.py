"""Async disk-backed task output.

Encapsulates async disk writes for each task's output. A flat list serves as a write queue
processed by a single drain loop so each chunk is freed immediately after its write completes
(avoiding the memory retention of chained ``.then()`` closures in the TS original).

SECURITY: ``O_NOFOLLOW`` prevents following symlinks when opening task output files. Without it,
an attacker in the sandbox could create symlinks in the tasks directory pointing to arbitrary
files, causing Tabvis on the host to write to those files. ``O_NOFOLLOW`` is not available on
Windows, but the sandbox attack vector is Unix-only.
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

from tabvis.bootstrap.state import get_session_id
from tabvis.utils.errors import get_errno_code
from tabvis.utils.fs_operations import read_file_range, tail_file
from tabvis.utils.log import log_error
from tabvis.utils.permissions.filesystem import get_project_temp_dir

# ``O_NOFOLLOW`` is not defined on Windows; fall back to 0 (no-op) there.
O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)

DEFAULT_MAX_READ_BYTES = 8 * 1024 * 1024  # 8MB

# Disk cap for task output files. Bash execution also uses this value for its file-size watchdog;
# ``DiskTaskOutput`` itself drops chunks after the limit is reached.
MAX_TASK_OUTPUT_BYTES = 5 * 1024 * 1024 * 1024
MAX_TASK_OUTPUT_BYTES_DISPLAY = "5GB"


# The session ID is captured at FIRST CALL, not re-read on every invocation. ``/clear`` calls
# ``regenerate_session_id()``, which would otherwise cause ``ensure_output_dir()`` to create a
# new-session path while existing disk-output objects still hold old-session paths.
_task_output_dir: str | None = None


def get_task_output_dir() -> str:
    """Get the task output directory for this session.

    Uses the project temp directory so reads are auto-allowed by
    ``check_readable_internal_path``. The session ID is included so concurrent sessions in the
    same project don't clobber each other's output files.
    """
    global _task_output_dir
    if _task_output_dir is None:
        _task_output_dir = os.path.join(get_project_temp_dir(), str(get_session_id()), "tasks")
    return _task_output_dir


def _reset_task_output_dir_for_test() -> None:
    """Test helper — clears the memoized dir."""
    global _task_output_dir
    _task_output_dir = None


async def ensure_output_dir() -> None:
    """Ensure the task output directory exists."""
    await asyncio.to_thread(os.makedirs, get_task_output_dir(), exist_ok=True)


def get_task_output_path(task_id: str) -> str:
    """Get the output file path for a task."""
    return os.path.join(get_task_output_dir(), f"{task_id}.output")


# Tracks fire-and-forget tasks (init_task_output, init_task_output_as_symlink, evict_task_output,
# _drain) so tests can drain before teardown. Prevents the async-ENOENT-after-teardown flake
# class: a voided coroutine resumes after the temp dir was nuked -> ENOENT -> unhandled rejection.
_pending_ops: set[Any] = set()


def _track(coro: Any) -> Any:
    """Wrap a coroutine in a tracked task so tests can drain in-flight ops before teardown."""
    task = asyncio.ensure_future(coro)
    _pending_ops.add(task)
    task.add_done_callback(_pending_ops.discard)
    return task


class DiskTaskOutput:
    """Encapsulates async disk writes for a single task's output.

    Uses a flat list as a write queue processed by a single drain loop, so each chunk can be
    freed immediately after its write completes.
    """

    def __init__(self, task_id: str) -> None:
        self._path: str = get_task_output_path(task_id)
        self._queue: list[str] = []
        self._bytes_written: int = 0
        self._capped: bool = False
        # The in-flight drain: a Future resolved when the current drain loop finishes.
        self._flush_future: asyncio.Future[None] | None = None

    def append(self, content: str) -> None:
        if self._capped:
            return
        # ``len(content)`` (code points) undercounts UTF-8 bytes — acceptable for a coarse
        # disk-fill guard; avoids re-scanning every chunk.
        self._bytes_written += len(content)
        if self._bytes_written > MAX_TASK_OUTPUT_BYTES:
            self._capped = True
            self._queue.append(
                f"\n[output truncated: exceeded {MAX_TASK_OUTPUT_BYTES_DISPLAY} disk cap]\n"
            )
        else:
            self._queue.append(content)
        if self._flush_future is None:
            self._flush_future = asyncio.get_event_loop().create_future()
            _track(self._drain())

    async def flush(self) -> None:
        if self._flush_future is not None:
            await asyncio.shield(self._flush_future)

    def cancel(self) -> None:
        self._queue.clear()

    async def _drain_all_chunks(self) -> None:
        while True:
            fd: int | None = None
            try:
                await ensure_output_dir()
                if sys.platform == "win32":
                    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
                else:
                    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | O_NOFOLLOW
                fd = await asyncio.to_thread(os.open, self._path, flags, 0o666)
                while True:
                    await self._write_all_chunks(fd)
                    if len(self._queue) == 0:
                        break
            finally:
                if fd is not None:
                    await asyncio.to_thread(os.close, fd)
            # Another .append() may have arrived while the file was closing — re-check the queue
            # before fully exiting.
            if self._queue:
                continue
            break

    async def _write_all_chunks(self, fd: int) -> None:
        buffer = self._queue_to_buffer()
        await asyncio.to_thread(os.write, fd, buffer)

    def _queue_to_buffer(self) -> bytes:
        """Drain the queue into a single UTF-8 buffer (in-place clear so the list is freed)."""
        queue = self._queue[:]
        self._queue.clear()
        return "".join(queue).encode("utf-8")

    async def _drain(self) -> None:
        try:
            await self._drain_all_chunks()
        except Exception as e:  # noqa: BLE001 - transient fs errors are logged + retried once
            # Retry once for the transient case (queue is intact if open() failed), then log and
            # give up.
            log_error(e)
            if len(self._queue) > 0:
                try:
                    await self._drain_all_chunks()
                except Exception as e2:  # noqa: BLE001
                    log_error(e2)
        finally:
            future = self._flush_future
            self._flush_future = None
            if future is not None and not future.done():
                future.set_result(None)


_outputs: dict[str, DiskTaskOutput] = {}


async def _clear_outputs_for_test() -> None:
    """Test helper — cancel pending writes, await in-flight ops, clear the map.

    Awaits all tracked tasks until the set stabilizes — a settling op may spawn another.
    """
    for output in _outputs.values():
        output.cancel()
    while len(_pending_ops) > 0:
        await asyncio.gather(*list(_pending_ops), return_exceptions=True)
    _outputs.clear()


def _get_or_create_output(task_id: str) -> DiskTaskOutput:
    output = _outputs.get(task_id)
    if output is None:
        output = DiskTaskOutput(task_id)
        _outputs[task_id] = output
    return output


def append_task_output(task_id: str, content: str) -> None:
    """Append output to a task's disk file asynchronously. Creates the file if it doesn't exist."""
    _get_or_create_output(task_id).append(content)


async def flush_task_output(task_id: str) -> None:
    """Wait for all pending writes for a task to complete."""
    output = _outputs.get(task_id)
    if output is not None:
        await output.flush()


def evict_task_output(task_id: str) -> Any:
    """Evict a task's DiskTaskOutput from the in-memory map after flushing.

    Unlike :func:`cleanup_task_output`, this does not delete the output file on disk.
    """

    async def _run() -> None:
        output = _outputs.get(task_id)
        if output is not None:
            await output.flush()
            _outputs.pop(task_id, None)

    return _track(_run())


async def get_task_output_delta(
    task_id: str,
    from_offset: int,
    max_bytes: int = DEFAULT_MAX_READ_BYTES,
) -> dict[str, Any]:
    """Get delta (new content) since last read, reading only from ``from_offset``."""
    try:
        result = await read_file_range(get_task_output_path(task_id), from_offset, max_bytes)
        if not result:
            return {"content": "", "newOffset": from_offset}
        return {
            "content": result.content,
            "newOffset": from_offset + result.bytes_read,
        }
    except Exception as e:  # noqa: BLE001
        code = get_errno_code(e)
        if code == "ENOENT":
            return {"content": "", "newOffset": from_offset}
        log_error(e)
        return {"content": "", "newOffset": from_offset}


async def get_task_output(task_id: str, max_bytes: int = DEFAULT_MAX_READ_BYTES) -> str:
    """Get output for a task, reading the tail of the file (capped at ``max_bytes``)."""
    try:
        result = await tail_file(get_task_output_path(task_id), max_bytes)
        bytes_total = result.bytes_total
        bytes_read = result.bytes_read
        if bytes_total > bytes_read:
            omitted_kb = round((bytes_total - bytes_read) / 1024)
            return f"[{omitted_kb}KB of earlier output omitted]\n{result.content}"
        return result.content
    except Exception as e:  # noqa: BLE001
        code = get_errno_code(e)
        if code == "ENOENT":
            return ""
        log_error(e)
        return ""


async def get_task_output_size(task_id: str) -> int:
    """Get the current size (offset) of a task's output file."""
    try:
        st = await asyncio.to_thread(os.stat, get_task_output_path(task_id))
        return st.st_size
    except Exception as e:  # noqa: BLE001
        code = get_errno_code(e)
        if code == "ENOENT":
            return 0
        log_error(e)
        return 0


async def cleanup_task_output(task_id: str) -> None:
    """Clean up a task's output file and write queue."""
    output = _outputs.get(task_id)
    if output is not None:
        output.cancel()
        _outputs.pop(task_id, None)

    try:
        await asyncio.to_thread(os.unlink, get_task_output_path(task_id))
    except Exception as e:  # noqa: BLE001
        code = get_errno_code(e)
        if code == "ENOENT":
            return
        log_error(e)


def init_task_output(task_id: str) -> Any:
    """Initialize output file for a new task — creates an empty file so the path exists."""

    async def _run() -> str:
        await ensure_output_dir()
        output_path = get_task_output_path(task_id)
        # SECURITY: O_NOFOLLOW prevents symlink-following attacks. O_EXCL ensures we create a new
        # file and fail if something already exists at this path.
        if sys.platform == "win32":
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        else:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | O_NOFOLLOW
        fd = await asyncio.to_thread(os.open, output_path, flags, 0o666)
        await asyncio.to_thread(os.close, fd)
        return output_path

    return _track(_run())


def init_task_output_as_symlink(task_id: str, target_path: str) -> Any:
    """Initialize the output file as a symlink to another file (e.g., agent transcript).

    Tries to create the symlink first; if a file already exists, removes it and retries.
    """

    async def _run() -> str:
        try:
            await ensure_output_dir()
            output_path = get_task_output_path(task_id)

            try:
                await asyncio.to_thread(os.symlink, target_path, output_path)
            except OSError:
                await asyncio.to_thread(os.unlink, output_path)
                await asyncio.to_thread(os.symlink, target_path, output_path)

            return output_path
        except Exception as error:  # noqa: BLE001
            log_error(error)
            return await init_task_output(task_id)

    return _track(_run())
