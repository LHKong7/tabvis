"""Filesystem operations facade

Provides the :class:`FsOperations` surface (a subset of Node's ``fs`` with type safety), the
default Node-backed implementation :data:`NODE_FS_OPERATIONS`, the swappable module-level
singleton (:func:`get_fs_implementation` / :func:`set_fs_implementation` /
:func:`set_original_fs_implementation`), the symlink-resolution + permission-path helpers
(:func:`safe_resolve_path`, :func:`is_duplicate_path`,
:func:`resolve_deepest_existing_ancestor_sync`, :func:`get_paths_for_permission_check`), and the
async range/tail/reverse readers.

Each sync filesystem call is wrapped in :func:`tabvis.utils.slow_operations.slow_logging` exactly
as the TS ``using _ = slowLogging`...``` (a no-op in this external build).

Stats / Dirent (per ``docs/SPINE_CONTRACTS.md`` these are plain runtime objects, not wire dicts):
- Node ``fs.Stats`` → :class:`Stats`, a thin wrapper over :class:`os.stat_result` exposing the
  ``is_fifo`` / ``is_socket`` / ``is_character_device`` / ``is_block_device`` / ``is_symbolic_link``
  / ``is_directory`` / ``is_file`` predicates the callers use.
- Node ``fs.Dirent`` → :class:`Dirent`, exposing ``name`` + the same type predicates.
"""

from __future__ import annotations

import asyncio
import os
import os.path as _osp
import stat as _stat
import unicodedata
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any

from tabvis.utils.errors import get_errno_code
from tabvis.utils.slow_operations import slow_logging

# --
# Stats / Dirent wrappers (Node fs.Stats / fs.Dirent surface)


class Stats:
    """Thin wrapper over :class:`os.stat_result` matching the Node ``fs.Stats`` predicate surface
    used by the resolution helpers. Constructed from an ``os.stat``/``os.lstat`` result."""

    __slots__ = ("_st", "size")

    def __init__(self, st: os.stat_result) -> None:
        self._st = st
        self.size = st.st_size

    @property
    def mode(self) -> int:
        return self._st.st_mode

    def is_fifo(self) -> bool:
        return _stat.S_ISFIFO(self._st.st_mode)

    def is_socket(self) -> bool:
        return _stat.S_ISSOCK(self._st.st_mode)

    def is_character_device(self) -> bool:
        return _stat.S_ISCHR(self._st.st_mode)

    def is_block_device(self) -> bool:
        return _stat.S_ISBLK(self._st.st_mode)

    def is_symbolic_link(self) -> bool:
        return _stat.S_ISLNK(self._st.st_mode)

    def is_directory(self) -> bool:
        return _stat.S_ISDIR(self._st.st_mode)

    def is_file(self) -> bool:
        return _stat.S_ISREG(self._st.st_mode)


@dataclass
class Dirent:
    """Node ``fs.Dirent`` surface: an entry name plus its file-type predicates."""

    name: str
    _mode: int

    def is_directory(self) -> bool:
        return _stat.S_ISDIR(self._mode)

    def is_file(self) -> bool:
        return _stat.S_ISREG(self._mode)

    def is_symbolic_link(self) -> bool:
        return _stat.S_ISLNK(self._mode)

    def is_fifo(self) -> bool:
        return _stat.S_ISFIFO(self._mode)

    def is_socket(self) -> bool:
        return _stat.S_ISSOCK(self._mode)

    def is_character_device(self) -> bool:
        return _stat.S_ISCHR(self._mode)

    def is_block_device(self) -> bool:
        return _stat.S_ISBLK(self._mode)


def _dirent_from_scandir(entry: os.DirEntry[str]) -> Dirent:
    try:
        mode = entry.stat(follow_symlinks=False).st_mode
    except OSError:
        mode = 0
    return Dirent(name=entry.name, _mode=mode)


# --
# FsOperations implementation


class FsOperations:
    """Simplified filesystem operations interface based on Node's ``fs`` module.

    Provides a subset of commonly used sync (and a few async) operations. Allows abstraction for
    alternative implementations (e.g. mock, virtual) by swapping the module-level singleton.
    """

    def cwd(self) -> str:
        return os.getcwd()

    def exists_sync(self, fs_path: str) -> bool:
        with slow_logging(f"fs.existsSync({fs_path})"):
            return _osp.exists(fs_path)

    async def stat(self, fs_path: str) -> Stats:
        return await asyncio.to_thread(lambda: Stats(os.stat(fs_path)))

    async def readdir(self, fs_path: str) -> list[Dirent]:
        def _read() -> list[Dirent]:
            with os.scandir(fs_path) as it:
                return [_dirent_from_scandir(e) for e in it]

        return await asyncio.to_thread(_read)

    async def unlink(self, fs_path: str) -> None:
        await asyncio.to_thread(os.unlink, fs_path)

    async def rmdir(self, fs_path: str) -> None:
        await asyncio.to_thread(os.rmdir, fs_path)

    async def rm(self, fs_path: str, options: dict[str, Any] | None = None) -> None:
        options = options or {}
        recursive = bool(options.get("recursive"))
        force = bool(options.get("force"))

        def _rm() -> None:
            import shutil

            if recursive:
                if force and not _osp.lexists(fs_path):
                    return
                if _osp.isdir(fs_path) and not _osp.islink(fs_path):
                    shutil.rmtree(fs_path, ignore_errors=force)
                else:
                    try:
                        os.unlink(fs_path)
                    except FileNotFoundError:
                        if not force:
                            raise
            else:
                try:
                    os.unlink(fs_path)
                except FileNotFoundError:
                    if not force:
                        raise

        await asyncio.to_thread(_rm)

    async def mkdir(self, dir_path: str, options: dict[str, Any] | None = None) -> None:
        options = options or {}

        def _mkdir() -> None:
            mode = options.get("mode")
            try:
                if mode is not None:
                    os.makedirs(dir_path, mode=mode, exist_ok=False)
                else:
                    os.makedirs(dir_path, exist_ok=False)
            except OSError as e:
                # Bun/Windows: recursive throws EEXIST on read-only-attribute dirs. The dir
                # exists; ignore. (tabvis-agent-core/tabvis#30924)
                if get_errno_code(e) != "EEXIST":
                    raise

        await asyncio.to_thread(_mkdir)

    async def read_file(self, fs_path: str, options: dict[str, Any]) -> str:
        encoding = options["encoding"]

        def _read() -> str:
            with open(fs_path, encoding=encoding) as f:
                return f.read()

        return await asyncio.to_thread(_read)

    async def rename(self, old_path: str, new_path: str) -> None:
        await asyncio.to_thread(os.rename, old_path, new_path)

    def stat_sync(self, fs_path: str) -> Stats:
        with slow_logging(f"fs.statSync({fs_path})"):
            return Stats(os.stat(fs_path))

    def lstat_sync(self, fs_path: str) -> Stats:
        with slow_logging(f"fs.lstatSync({fs_path})"):
            return Stats(os.lstat(fs_path))

    def read_file_sync(self, fs_path: str, options: dict[str, Any]) -> str:
        with slow_logging(f"fs.readFileSync({fs_path})"):
            with open(fs_path, encoding=options["encoding"]) as f:
                return f.read()

    def read_file_bytes_sync(self, fs_path: str) -> bytes:
        with slow_logging(f"fs.readFileBytesSync({fs_path})"):
            with open(fs_path, "rb") as f:
                return f.read()

    def read_sync(self, fs_path: str, options: dict[str, Any]) -> dict[str, Any]:
        length = options["length"]
        with slow_logging(f"fs.readSync({fs_path}, {length} bytes)"):
            fd: int | None = None
            try:
                fd = os.open(fs_path, os.O_RDONLY)
                data = os.pread(fd, length, 0)
                return {"buffer": data, "bytes_read": len(data)}
            finally:
                if fd is not None:
                    os.close(fd)

    def append_file_sync(
        self, path: str, data: str, options: dict[str, Any] | None = None
    ) -> None:
        with slow_logging(f"fs.appendFileSync({path}, {len(data)} chars)"):
            mode = (options or {}).get("mode")
            payload = data.encode("utf-8")
            # For new files with explicit mode, use exclusive create ('ax' / O_EXCL) to avoid a
            # TOCTOU race. Fall back to normal append if the file already exists.
            if mode is not None:
                try:
                    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND, mode)
                    try:
                        os.write(fd, payload)
                    finally:
                        os.close(fd)
                    return
                except OSError as e:
                    if get_errno_code(e) != "EEXIST":
                        raise
                    # File exists — fall through to normal append.
            with open(path, "ab") as f:
                f.write(payload)

    def copy_file_sync(self, src: str, dest: str) -> None:
        with slow_logging(f"fs.copyFileSync({src} → {dest})"):
            import shutil

            shutil.copyfile(src, dest)

    def unlink_sync(self, path: str) -> None:
        with slow_logging(f"fs.unlinkSync({path})"):
            os.unlink(path)

    def rename_sync(self, old_path: str, new_path: str) -> None:
        with slow_logging(f"fs.renameSync({old_path} → {new_path})"):
            os.rename(old_path, new_path)

    def link_sync(self, target: str, path: str) -> None:
        with slow_logging(f"fs.linkSync({target} → {path})"):
            os.link(target, path)

    def symlink_sync(self, target: str, path: str, link_type: str | None = None) -> None:
        with slow_logging(f"fs.symlinkSync({target} → {path})"):
            os.symlink(target, path, target_is_directory=(link_type == "dir"))

    def readlink_sync(self, path: str) -> str:
        with slow_logging(f"fs.readlinkSync({path})"):
            return os.readlink(path)

    def realpath_sync(self, path: str) -> str:
        with slow_logging(f"fs.realpathSync({path})"):
            return unicodedata.normalize("NFC", os.path.realpath(path, strict=True))

    def mkdir_sync(self, dir_path: str, options: dict[str, Any] | None = None) -> None:
        with slow_logging(f"fs.mkdirSync({dir_path})"):
            mode = (options or {}).get("mode")
            try:
                if mode is not None:
                    os.makedirs(dir_path, mode=mode, exist_ok=False)
                else:
                    os.makedirs(dir_path, exist_ok=False)
            except OSError as e:
                # Bun/Windows EEXIST-on-read-only-dir tolerance (see mkdir above).
                if get_errno_code(e) != "EEXIST":
                    raise

    def readdir_sync(self, dir_path: str) -> list[Dirent]:
        with slow_logging(f"fs.readdirSync({dir_path})"):
            with os.scandir(dir_path) as it:
                return [_dirent_from_scandir(e) for e in it]

    def readdir_string_sync(self, dir_path: str) -> list[str]:
        with slow_logging(f"fs.readdirStringSync({dir_path})"):
            return os.listdir(dir_path)

    def is_dir_empty_sync(self, dir_path: str) -> bool:
        with slow_logging(f"fs.isDirEmptySync({dir_path})"):
            files = self.readdir_sync(dir_path)
            return len(files) == 0

    def rmdir_sync(self, dir_path: str) -> None:
        with slow_logging(f"fs.rmdirSync({dir_path})"):
            os.rmdir(dir_path)

    def rm_sync(self, path: str, options: dict[str, Any] | None = None) -> None:
        with slow_logging(f"fs.rmSync({path})"):
            import shutil

            options = options or {}
            recursive = bool(options.get("recursive"))
            force = bool(options.get("force"))
            if recursive:
                if force and not _osp.lexists(path):
                    return
                if _osp.isdir(path) and not _osp.islink(path):
                    shutil.rmtree(path, ignore_errors=force)
                else:
                    try:
                        os.unlink(path)
                    except FileNotFoundError:
                        if not force:
                            raise
            else:
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    if not force:
                        raise

    def create_write_stream(self, path: str) -> Any:
        # Node returns a WriteStream; the closest faithful Python analogue is an opened binary
        # file handle in write mode (caller writes/closes it).
        return open(path, "wb")  # noqa: SIM115 - caller owns the handle's lifecycle

    async def read_file_bytes(self, fs_path: str, max_bytes: int | None = None) -> bytes:
        def _read() -> bytes:
            if max_bytes is None:
                with open(fs_path, "rb") as f:
                    return f.read()
            fd = os.open(fs_path, os.O_RDONLY)
            try:
                size = os.fstat(fd).st_size
                read_size = min(size, max_bytes)
                return os.pread(fd, read_size, 0)
            finally:
                os.close(fd)

        return await asyncio.to_thread(_read)


NODE_FS_OPERATIONS: FsOperations = FsOperations()

# The currently active filesystem implementation.
_active_fs: FsOperations = NODE_FS_OPERATIONS


def set_fs_implementation(implementation: FsOperations) -> None:
    """Override the filesystem implementation. Does not automatically update cwd."""
    global _active_fs
    _active_fs = implementation


def get_fs_implementation() -> FsOperations:
    """Return the currently active filesystem implementation."""
    return _active_fs


def set_original_fs_implementation() -> None:
    """Reset the filesystem implementation to the default Node-backed one. Does not update cwd."""
    global _active_fs
    _active_fs = NODE_FS_OPERATIONS


# --
# Symlink resolution + permission-path helpers


def safe_resolve_path(fs: FsOperations, file_path: str) -> dict[str, Any]:
    """Safely resolve a file path, handling symlinks and errors gracefully.

    - If the file doesn't exist, returns the original path (allows file creation).
    - If symlink resolution fails (broken symlink, permission denied, circular links), returns
      the original path and marks it as not a symlink.

    Returns ``{"resolved_path", "is_symlink", "is_canonical"}``.
    """
    # Block UNC paths before any filesystem access (prevents DNS/SMB on Windows).
    if file_path.startswith("//") or file_path.startswith("\\\\"):
        return {"resolved_path": file_path, "is_symlink": False, "is_canonical": False}

    try:
        # Check for special file types (FIFOs, sockets, devices) before realpath — realpath can
        # block on FIFOs waiting for a writer. If the file doesn't exist, lstat raises ENOENT
        # which the except below handles (returns original path → allows file creation).
        stats = fs.lstat_sync(file_path)
        if (
            stats.is_fifo()
            or stats.is_socket()
            or stats.is_character_device()
            or stats.is_block_device()
        ):
            return {"resolved_path": file_path, "is_symlink": False, "is_canonical": False}

        resolved_path = fs.realpath_sync(file_path)
        return {
            "resolved_path": resolved_path,
            "is_symlink": resolved_path != file_path,
            # realpath returned: resolved_path is canonical (all symlinks resolved). Callers
            # can skip further symlink resolution.
            "is_canonical": True,
        }
    except OSError:
        # lstat/realpath failed (ENOENT, broken symlink, EACCES, ELOOP, …) → return the
        # original path so operations can proceed.
        return {"resolved_path": file_path, "is_symlink": False, "is_canonical": False}


def is_duplicate_path(fs: FsOperations, file_path: str, loaded_paths: set[str]) -> bool:
    """Return ``True`` if ``file_path`` resolves to a path already in ``loaded_paths`` (and
    should be skipped); otherwise add the resolved path and return ``False``."""
    resolved_path = safe_resolve_path(fs, file_path)["resolved_path"]
    if resolved_path in loaded_paths:
        return True
    loaded_paths.add(resolved_path)
    return False


def resolve_deepest_existing_ancestor_sync(
    fs: FsOperations, absolute_path: str
) -> str | None:
    """Resolve the deepest existing ancestor of a path via realpath, walking up until it
    succeeds. Detects dangling symlinks via lstat and resolves them via readlink.

    Returns the resolved absolute path with non-existent tail segments rejoined, or ``None`` if
    no symlink was found in any existing ancestor.
    """
    dir_path = absolute_path
    segments: list[str] = []
    # Walk up using lstat (cheap) to find the first existing component. lstat does not follow
    # symlinks, so dangling symlinks are detected here. Only call realpath once at the end.
    while dir_path != _osp.dirname(dir_path):
        try:
            st = fs.lstat_sync(dir_path)
        except OSError:
            # lstat failed: truly non-existent. Walk up.
            segments.insert(0, _osp.basename(dir_path))
            dir_path = _osp.dirname(dir_path)
            continue

        if st.is_symbolic_link():
            # Found a symlink (live or dangling). Try realpath first (resolves chained
            # symlinks); fall back to readlink for dangling symlinks.
            try:
                resolved = fs.realpath_sync(dir_path)
                return resolved if not segments else _osp.join(resolved, *segments)
            except OSError:
                target = fs.readlink_sync(dir_path)
                abs_target = (
                    target
                    if _osp.isabs(target)
                    else _osp.abspath(_osp.join(_osp.dirname(dir_path), target))
                )
                return abs_target if not segments else _osp.join(abs_target, *segments)

        # Existing non-symlink component. One realpath resolves any symlinks in its ancestors.
        # If none, return None (no symlink).
        try:
            resolved = fs.realpath_sync(dir_path)
            if resolved != dir_path:
                return resolved if not segments else _osp.join(resolved, *segments)
        except OSError:
            # realpath can still fail (e.g. EACCES in ancestors). Return None — we can't
            # resolve, and the logical path is already in the caller's set.
            pass
        return None
    return None


def get_paths_for_permission_check(input_path: str) -> list[str]:
    """Return all paths to check for permissions: the original path, every intermediate symlink
    target in the chain, and the final resolved path.

    Important for security: a deny rule for ``/etc/passwd`` should block access even if the file
    is actually at ``/private/etc/passwd`` (as on macOS).
    """
    # Expand tilde defensively (defense in depth for permission checking).
    path = input_path
    home = unicodedata.normalize("NFC", _osp.expanduser("~"))
    if path == "~":
        path = home
    elif path.startswith("~/"):
        path = _osp.join(home, path[2:])

    path_set: set[str] = set()
    path_order: list[str] = []

    def _add(p: str) -> None:
        if p not in path_set:
            path_set.add(p)
            path_order.append(p)

    fs_impl = get_fs_implementation()

    # Always check the original path.
    _add(path)

    # Block UNC paths before any filesystem access.
    if path.startswith("//") or path.startswith("\\\\"):
        return path_order

    # Follow the symlink chain, collecting ALL intermediate targets. Handles cases like
    # test.txt -> /etc/passwd -> /private/etc/passwd (we want all three).
    try:
        current_path = path
        visited: set[str] = set()
        max_depth = 40  # Prevent runaway loops; matches typical SYMLOOP_MAX.

        for _depth in range(max_depth):
            if current_path in visited:
                break
            visited.add(current_path)

            if not fs_impl.exists_sync(current_path):
                # Path doesn't exist (new file case). exists follows symlinks, so this is also
                # reached for DANGLING symlinks. Resolve symlinks in the path and its ancestors
                # so permission checks see the real destination.
                if current_path == path:
                    resolved = resolve_deepest_existing_ancestor_sync(fs_impl, path)
                    if resolved is not None:
                        _add(resolved)
                break

            stats = fs_impl.lstat_sync(current_path)

            # Skip special file types that can cause issues.
            if (
                stats.is_fifo()
                or stats.is_socket()
                or stats.is_character_device()
                or stats.is_block_device()
            ):
                break

            if not stats.is_symbolic_link():
                break

            # Get the immediate symlink target.
            target = fs_impl.readlink_sync(current_path)

            # If target is relative, resolve it relative to the symlink's directory.
            absolute_target = (
                target
                if _osp.isabs(target)
                else _osp.abspath(_osp.join(_osp.dirname(current_path), target))
            )

            _add(absolute_target)
            current_path = absolute_target
    except OSError:
        # If anything fails during chain traversal, continue with what we have.
        pass

    # Also add the final resolved path via realpath for completeness (remaining symlinks in dir
    # components).
    resolved_info = safe_resolve_path(fs_impl, path)
    if resolved_info["is_symlink"] and resolved_info["resolved_path"] != path:
        _add(resolved_info["resolved_path"])

    return path_order


# --
# Async range / tail / reverse readers


@dataclass
class ReadFileRangeResult:
    content: str
    bytes_read: int
    bytes_total: int


async def read_file_range(
    path: str, offset: int, max_bytes: int
) -> ReadFileRangeResult | None:
    """Read up to ``max_bytes`` from a file starting at ``offset``. Returns ``None`` if the file
    is smaller than (or equal to) the offset."""

    def _read() -> ReadFileRangeResult | None:
        fd = os.open(path, os.O_RDONLY)
        try:
            size = os.fstat(fd).st_size
            if size <= offset:
                return None
            bytes_to_read = min(size - offset, max_bytes)
            chunks: list[bytes] = []
            total_read = 0
            while total_read < bytes_to_read:
                chunk = os.pread(fd, bytes_to_read - total_read, offset + total_read)
                if not chunk:
                    break
                chunks.append(chunk)
                total_read += len(chunk)
            data = b"".join(chunks)
            return ReadFileRangeResult(
                content=data.decode("utf-8", errors="replace"),
                bytes_read=total_read,
                bytes_total=size,
            )
        finally:
            os.close(fd)

    return await asyncio.to_thread(_read)


async def tail_file(path: str, max_bytes: int) -> ReadFileRangeResult:
    """Read the last ``max_bytes`` of a file. Returns the whole file if it's smaller."""

    def _read() -> ReadFileRangeResult:
        fd = os.open(path, os.O_RDONLY)
        try:
            size = os.fstat(fd).st_size
            if size == 0:
                return ReadFileRangeResult(content="", bytes_read=0, bytes_total=0)
            offset = max(0, size - max_bytes)
            bytes_to_read = size - offset
            chunks: list[bytes] = []
            total_read = 0
            while total_read < bytes_to_read:
                chunk = os.pread(fd, bytes_to_read - total_read, offset + total_read)
                if not chunk:
                    break
                chunks.append(chunk)
                total_read += len(chunk)
            data = b"".join(chunks)
            return ReadFileRangeResult(
                content=data.decode("utf-8", errors="replace"),
                bytes_read=total_read,
                bytes_total=size,
            )
        finally:
            os.close(fd)

    return await asyncio.to_thread(_read)


async def read_lines_reverse(path: str) -> AsyncGenerator[str, None]:
    """Async generator that yields lines from a file in reverse order, reading backwards in
    chunks to avoid loading the entire file into memory.

    Raw bytes are carried across chunk boundaries so multi-byte UTF-8 sequences split by the 4KB
    boundary are not corrupted (decoding per-chunk would turn a split sequence into U+FFFD on
    both sides).
    """
    chunk_size = 1024 * 4
    fd = await asyncio.to_thread(os.open, path, os.O_RDONLY)
    try:
        size = (await asyncio.to_thread(os.fstat, fd)).st_size
        position = size
        remainder = b""

        while position > 0:
            current_chunk_size = min(chunk_size, position)
            position -= current_chunk_size

            chunk = await asyncio.to_thread(os.pread, fd, current_chunk_size, position)
            combined = chunk[:current_chunk_size] + remainder

            first_newline = combined.find(b"\n")
            if first_newline == -1:
                remainder = combined
                continue

            remainder = combined[:first_newline]
            lines = combined[first_newline + 1 :].decode("utf-8", errors="replace").split("\n")

            for i in range(len(lines) - 1, -1, -1):
                line = lines[i]
                if line:
                    yield line

        if len(remainder) > 0:
            yield remainder.decode("utf-8", errors="replace")
    finally:
        await asyncio.to_thread(os.close, fd)
