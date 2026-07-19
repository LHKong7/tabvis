"""File-persistence types + constants

The TS module is plain object ``type`` aliases (no Zod), plus three module constants. The
shapes round-trip to event data / SDK output, so their wire keys are kept verbatim. The TS field
names are already snake_case (``file_id``, ``filename``, ``error``, ``files``, ``failed``), so no
alias mapping is needed here.

Casing: Python identifiers snake_case; UPPER_CASE constants; PascalCase classes.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

# ``TurnStartTime`` is a TS type alias for ``number`` (epoch ms). Python exposes it as a plain
# alias for ``float`` (``fs.lstat().mtimeMs`` and ``Date.now()`` are floating-point ms).
TurnStartTime = float

# Module constants (UPPER_CASE — already upper in the TS source).
DEFAULT_UPLOAD_CONCURRENCY: int = 5
FILE_COUNT_LIMIT: int = 100
OUTPUTS_SUBDIR: str = "outputs"


class PersistedFile(BaseModel):
    """A successfully persisted file (wire keys: ``filename``, ``file_id``)."""

    model_config = ConfigDict(extra="forbid")

    filename: str
    file_id: str


class FailedPersistence(BaseModel):
    """A file that failed to persist (wire keys: ``filename``, ``error``)."""

    model_config = ConfigDict(extra="forbid")

    filename: str
    error: str


class FilesPersistedEventData(BaseModel):
    """Event payload describing the result of a persistence pass.

    Wire keys ``files`` / ``failed`` kept verbatim.
    """

    model_config = ConfigDict(extra="forbid")

    files: list[PersistedFile]
    failed: list[FailedPersistence]


__all__ = [
    "DEFAULT_UPLOAD_CONCURRENCY",
    "FILE_COUNT_LIMIT",
    "OUTPUTS_SUBDIR",
    "FailedPersistence",
    "FilesPersistedEventData",
    "PersistedFile",
    "TurnStartTime",
]
