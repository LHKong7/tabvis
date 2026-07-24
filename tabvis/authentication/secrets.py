"""Secret value type (design §5.7 / §5.8) — the *only* in-process container for a resolved secret.

The security contract this file enforces is Phase-0 of ``docs/CREDENTIAL_INJECTION_DESIGN.md``:
a resolved account / password / TOTP-seed **MUST NOT** be reachable as a plain ``str`` that could
leak into the model context, a tool argument, a log line, a Pydantic dump, a pickle, or an exception
argument. So :class:`SecretValue` deliberately makes *every* stringification and serialization path
raise, and exposes the bytes only through an explicit, auditable :meth:`borrow_bytes` / :meth:`release`.

Absolute in-memory zeroing is impossible in CPython (bytes are immutable, the GC may copy), so this is
defense-in-depth, not a hardware guarantee — the design is explicit that the real cleanup boundary is
a short-lived Executor worker (Phase 2/3). What this type *does* guarantee is that a secret can never
be *accidentally* rendered: the accidental paths (``str``/``repr``/``json``/``pickle``/format) are all
closed.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


class SecretLeakError(RuntimeError):
    """Raised whenever code tries to stringify, format or serialize a :class:`SecretValue`.

    It is a hard error rather than a redacted placeholder so a leak attempt fails loudly in tests and
    in production instead of silently emitting ``<redacted>`` next to real data.
    """


@runtime_checkable
class SecretValue(Protocol):
    """A resolved secret. Implementations MUST forbid stringification and serialization (design §5.7)."""

    def borrow_bytes(self) -> memoryview:
        """Return a read-only view of the secret bytes for the single act of injection.

        The caller MUST NOT copy the bytes into a ``str``/``bytes`` that outlives the borrow, and MUST
        call :meth:`release` when done.
        """
        ...

    def release(self) -> None:
        """Best-effort overwrite + drop of the underlying buffer. Idempotent."""
        ...


class BufferSecretValue:
    """Mutable-buffer :class:`SecretValue` backed by a ``bytearray`` (design §5.7).

    Stored as a ``bytearray`` (not ``bytes``/``str``) so :meth:`release` can overwrite it in place.
    Once released, :meth:`borrow_bytes` raises so a stale reference can't be reused.
    """

    __slots__ = ("_buf", "_released")

    def __init__(self, raw: bytes | bytearray) -> None:
        # Copy into an owned, overwritable buffer. We intentionally accept bytes only (never str) so a
        # caller cannot smuggle in a long-lived interned ``str`` that we can't overwrite.
        self._buf: bytearray | None = bytearray(raw)
        self._released = False

    # -- the one sanctioned access path --------------------------------------------------------

    def borrow_bytes(self) -> memoryview:
        if self._released or self._buf is None:
            raise SecretLeakError("secret already released")
        return memoryview(self._buf).toreadonly()

    def release(self) -> None:
        if self._buf is not None:
            for i in range(len(self._buf)):
                self._buf[i] = 0
            self._buf = None
        self._released = True

    # -- every accidental-leak path is closed --------------------------------------------------

    def __str__(self) -> str:  # noqa: D105 - contract: never render a secret
        raise SecretLeakError("SecretValue cannot be converted to str")

    def __repr__(self) -> str:  # noqa: D105
        return "<SecretValue redacted>"

    def __format__(self, _spec: str) -> str:  # noqa: D105 - blocks f-strings / str.format
        raise SecretLeakError("SecretValue cannot be formatted")

    def __bytes__(self) -> bytes:  # noqa: D105 - bytes() is a serialization path too
        raise SecretLeakError("SecretValue cannot be converted to bytes")

    def __reduce__(self):  # noqa: ANN204 - blocks pickle / copy / deepcopy
        raise SecretLeakError("SecretValue cannot be pickled")

    def __getstate__(self):  # noqa: ANN204 - belt-and-braces alongside __reduce__
        raise SecretLeakError("SecretValue cannot be serialized")

    def __copy__(self):  # noqa: ANN204
        raise SecretLeakError("SecretValue cannot be copied")

    def __deepcopy__(self, _memo):  # noqa: ANN204
        raise SecretLeakError("SecretValue cannot be copied")

    # Pydantic v2: if a SecretValue is ever placed on a model, refuse to emit it in a dump.
    def __get_pydantic_core_schema__(self, _source, _handler):  # noqa: ANN001, ANN204
        raise SecretLeakError("SecretValue cannot be a Pydantic field")

    def __del__(self) -> None:  # noqa: D105 - last-ditch scrub if release() was skipped
        try:
            self.release()
        except Exception:  # noqa: BLE001 - never raise from a finalizer
            pass


def secret_from_str(value: str) -> BufferSecretValue:
    """Wrap a freshly-resolved plaintext ``str`` into a :class:`BufferSecretValue`.

    This is the ONLY sanctioned bridge from the Secret Provider's ``str`` return into the secret type,
    and it MUST run only inside the trusted Executor domain (design §5.8). The source ``str`` still
    lives in the provider's return buffer until GC — see the module docstring on why zeroing is
    best-effort.
    """
    return BufferSecretValue(value.encode("utf-8"))
