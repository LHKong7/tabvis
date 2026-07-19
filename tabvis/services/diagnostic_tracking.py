"""Diagnostic tracking service.

Tracks IDE diagnostics (errors/warnings) around file edits: captures a baseline
before an edit, then reports the *new* diagnostics that appeared afterward. The
diagnostics come from the connected IDE MCP client over an RPC bridge.

Casing: Python identifiers are snake_case; the diagnostic file/range payloads
that round-trip through the IDE RPC keep their wire keys (``uri``, ``message``,
``severity``, ``range.start.line``, ...).
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any, Literal, TypedDict

from tabvis.agent.mcp.types import MCPServerConnection

from tabvis.utils.errors import TabvisError
from tabvis.utils.ide import call_ide_rpc, get_connected_ide_client
from tabvis.utils.log import log_error
from tabvis.utils.slow_operations import json_parse


class DiagnosticsTrackingError(TabvisError):
    pass


# Severity glyphs (cross/warning/info/star/bullet) defined here directly since
# tabvis.constants.figures carries a different glyph set (non-Windows forms
# fall through to the unicode symbols used by Tabvis's TUI).
_FIGURES_CROSS = "✖"
_FIGURES_WARNING = "⚠"
_FIGURES_INFO = "ℹ"
_FIGURES_STAR = "★"
_FIGURES_BULLET = "●"

MAX_DIAGNOSTICS_SUMMARY_CHARS = 4000


class DiagnosticRangePoint(TypedDict):
    line: int
    character: int


class DiagnosticRange(TypedDict):
    start: DiagnosticRangePoint
    end: DiagnosticRangePoint


class Diagnostic(TypedDict, total=False):
    message: str
    severity: Literal["Error", "Warning", "Info", "Hint"]
    range: DiagnosticRange
    source: str
    code: str


class DiagnosticFile(TypedDict):
    uri: str
    diagnostics: list[Diagnostic]


# Local fallback path-normalization helpers (os.path.normpath + Windows
# case-insensitive lowering), pending equivalent helpers in tabvis.utils.file.
# Rewire callers there once they exist.
def _normalize_path_for_comparison(file_path: str) -> str:
    normalized = os.path.normpath(file_path)
    if sys.platform == "win32":
        normalized = normalized.replace("/", "\\").lower()
    return normalized


def _paths_equal(path1: str, path2: str) -> bool:
    return _normalize_path_for_comparison(path1) == _normalize_path_for_comparison(
        path2
    )


class DiagnosticTrackingService:
    _instance: DiagnosticTrackingService | None = None

    def __init__(self) -> None:
        self._baseline: dict[str, list[Diagnostic]] = {}
        self._initialized = False
        self._mcp_client: MCPServerConnection | None = None
        # Track when files were last processed/fetched.
        self._last_processed_timestamps: dict[str, float] = {}
        # Track which files have received right-file diagnostics and if they've
        # changed: {normalized_path: last_tabvis_fs_right_diagnostics}.
        self._right_file_diagnostics_state: dict[str, list[Diagnostic]] = {}

    @classmethod
    def get_instance(cls) -> DiagnosticTrackingService:
        if cls._instance is None:
            cls._instance = DiagnosticTrackingService()
        return cls._instance

    def initialize(self, mcp_client: MCPServerConnection) -> None:
        if self._initialized:
            return

        self._mcp_client = mcp_client
        self._initialized = True

    async def shutdown(self) -> None:
        self._initialized = False
        self._baseline.clear()
        self._right_file_diagnostics_state.clear()
        self._last_processed_timestamps.clear()

    def reset(self) -> None:
        """Reset tracking state while keeping the service initialized.

        This clears all tracked files and diagnostics.
        """
        self._baseline.clear()
        self._right_file_diagnostics_state.clear()
        self._last_processed_timestamps.clear()

    def _normalize_file_uri(self, file_uri: str) -> str:
        # Remove our protocol prefixes.
        protocol_prefixes = ["file://", "_tabvis_fs_right:", "_tabvis_fs_left:"]

        normalized = file_uri
        for prefix in protocol_prefixes:
            if file_uri.startswith(prefix):
                normalized = file_uri[len(prefix) :]
                break

        # Use shared utility for platform-aware path normalization (handles
        # Windows case-insensitivity and path separators).
        return _normalize_path_for_comparison(normalized)

    async def ensure_file_opened(self, file_uri: str) -> None:
        """Ensure a file is opened in the IDE before processing.

        This is important for language services like diagnostics to work
        properly.
        """
        if (
            not self._initialized
            or not self._mcp_client
            or getattr(self._mcp_client, "type", None) != "connected"
        ):
            return

        try:
            # Call the openFile tool to ensure the file is loaded.
            await call_ide_rpc(
                "openFile",
                {
                    "filePath": file_uri,
                    "preview": False,
                    "startText": "",
                    "endText": "",
                    "selectToEndOfLine": False,
                    "makeFrontmost": False,
                },
                self._mcp_client,
            )
        except Exception as error:  # noqa: BLE001 - log, don't propagate
            log_error(error)

    async def before_file_edited(self, file_path: str) -> None:
        """Capture baseline diagnostics for a specific file before editing.

        This is called before editing a file to ensure we have a baseline to
        compare against.
        """
        if (
            not self._initialized
            or not self._mcp_client
            or getattr(self._mcp_client, "type", None) != "connected"
        ):
            return

        timestamp = time.time() * 1000

        try:
            result = await call_ide_rpc(
                "getDiagnostics",
                {"uri": f"file://{file_path}"},
                self._mcp_client,
            )
            parsed = self._parse_diagnostic_result(result)
            diagnostic_file = parsed[0] if parsed else None
            if diagnostic_file:
                # Compare normalized paths (handles protocol prefixes and
                # Windows case-insensitivity).
                if not _paths_equal(
                    self._normalize_file_uri(file_path),
                    self._normalize_file_uri(diagnostic_file["uri"]),
                ):
                    log_error(
                        DiagnosticsTrackingError(
                            f"Diagnostics file path mismatch: expected {file_path}, "
                            f"got {diagnostic_file['uri']})"
                        )
                    )
                    return

                # Store with normalized path key for consistent Windows lookups.
                normalized_path = self._normalize_file_uri(file_path)
                self._baseline[normalized_path] = diagnostic_file["diagnostics"]
                self._last_processed_timestamps[normalized_path] = timestamp
            else:
                # No diagnostic file returned, store an empty baseline.
                normalized_path = self._normalize_file_uri(file_path)
                self._baseline[normalized_path] = []
                self._last_processed_timestamps[normalized_path] = timestamp
        except Exception:  # noqa: BLE001 - fail silently if IDE lacks diagnostics
            pass

    async def get_new_diagnostics(self) -> list[DiagnosticFile]:
        """Get new diagnostics not in the baseline.

        Processes diagnostics from ``file://``, ``_tabvis_fs_right`` and
        ``_tabvis_fs_`` URIs. Only processes diagnostics for files that have been
        edited.
        """
        if (
            not self._initialized
            or not self._mcp_client
            or getattr(self._mcp_client, "type", None) != "connected"
        ):
            return []

        # Check if we have any files with diagnostic changes.
        all_diagnostic_files: list[DiagnosticFile] = []
        try:
            result = await call_ide_rpc(
                "getDiagnostics",
                {},  # Empty params fetches all diagnostics.
                self._mcp_client,
            )
            all_diagnostic_files = self._parse_diagnostic_result(result)
        except Exception:  # noqa: BLE001 - if fetching all fails, return empty
            return []

        diagnostics_for_file_uris_with_baselines = [
            file
            for file in all_diagnostic_files
            if self._normalize_file_uri(file["uri"]) in self._baseline
            and file["uri"].startswith("file://")
        ]

        diagnostics_for_tabvis_fs_right_uris_with_baselines_map: dict[
            str, DiagnosticFile
        ] = {}
        for file in all_diagnostic_files:
            if (
                self._normalize_file_uri(file["uri"]) in self._baseline
                and file["uri"].startswith("_tabvis_fs_right:")
            ):
                diagnostics_for_tabvis_fs_right_uris_with_baselines_map[
                    self._normalize_file_uri(file["uri"])
                ] = file

        new_diagnostic_files: list[DiagnosticFile] = []

        # Process file:// protocol diagnostics.
        for file in diagnostics_for_file_uris_with_baselines:
            normalized_path = self._normalize_file_uri(file["uri"])
            baseline_diagnostics = self._baseline.get(normalized_path) or []

            # Get the _tabvis_fs_right file if it exists.
            tabvis_fs_right_file = (
                diagnostics_for_tabvis_fs_right_uris_with_baselines_map.get(
                    normalized_path
                )
            )

            # Determine which file to use based on the state of right-file
            # diagnostics.
            file_to_use = file

            if tabvis_fs_right_file:
                previous_right_diagnostics = self._right_file_diagnostics_state.get(
                    normalized_path
                )

                # Use _tabvis_fs_right if:
                # 1. We've never gotten right-file diagnostics for this file
                #    (previous_right_diagnostics is None), OR
                # 2. The right-file diagnostics have just changed.
                if previous_right_diagnostics is None or not (
                    self._are_diagnostic_arrays_equal(
                        previous_right_diagnostics,
                        tabvis_fs_right_file["diagnostics"],
                    )
                ):
                    file_to_use = tabvis_fs_right_file

                # Update our tracking of right-file diagnostics.
                self._right_file_diagnostics_state[normalized_path] = (
                    tabvis_fs_right_file["diagnostics"]
                )

            # Find new diagnostics that aren't in the baseline.
            new_diagnostics = [
                d
                for d in file_to_use["diagnostics"]
                if not any(
                    self._are_diagnostics_equal(d, b) for b in baseline_diagnostics
                )
            ]

            if len(new_diagnostics) > 0:
                new_diagnostic_files.append(
                    {"uri": file["uri"], "diagnostics": new_diagnostics}
                )

            # Update baseline with current diagnostics.
            self._baseline[normalized_path] = file_to_use["diagnostics"]

        return new_diagnostic_files

    def _parse_diagnostic_result(self, result: Any) -> list[DiagnosticFile]:
        if isinstance(result, list):
            text_block = next(
                (block for block in result if block.get("type") == "text"), None
            )
            if text_block is not None and "text" in text_block:
                return json_parse(text_block["text"])
        return []

    def _are_diagnostics_equal(self, a: Diagnostic, b: Diagnostic) -> bool:
        return (
            a.get("message") == b.get("message")
            and a.get("severity") == b.get("severity")
            and a.get("source") == b.get("source")
            and a.get("code") == b.get("code")
            and a["range"]["start"]["line"] == b["range"]["start"]["line"]
            and a["range"]["start"]["character"] == b["range"]["start"]["character"]
            and a["range"]["end"]["line"] == b["range"]["end"]["line"]
            and a["range"]["end"]["character"] == b["range"]["end"]["character"]
        )

    def _are_diagnostic_arrays_equal(
        self, a: list[Diagnostic], b: list[Diagnostic]
    ) -> bool:
        if len(a) != len(b):
            return False

        # Check if every diagnostic in 'a' exists in 'b' and vice versa.
        return all(
            any(self._are_diagnostics_equal(diag_a, diag_b) for diag_b in b)
            for diag_a in a
        ) and all(
            any(self._are_diagnostics_equal(diag_a, diag_b) for diag_a in a)
            for diag_b in b
        )

    async def handle_query_start(
        self, clients: list[MCPServerConnection]
    ) -> None:
        """Handle the start of a new query.

        - Initializes the diagnostic tracker if not already initialized.
        - Resets the tracker if already initialized (for new query loops).
        - Automatically finds the IDE client from the provided clients list.
        """
        # Only proceed if we should query and have clients.
        if not self._initialized:
            # Find the connected IDE client.
            connected_ide_client = get_connected_ide_client(clients)

            if connected_ide_client:
                self.initialize(connected_ide_client)
        else:
            # Reset diagnostic tracking for new query loops.
            self.reset()

    @staticmethod
    def format_diagnostics_summary(files: list[DiagnosticFile]) -> str:
        """Format diagnostics into a human-readable summary string.

        Useful for displaying diagnostics in messages or logs.
        """
        truncation_marker = "…[truncated]"
        parts: list[str] = []
        for file in files:
            filename = file["uri"].split("/")[-1] or file["uri"]
            diagnostic_lines: list[str] = []
            for d in file["diagnostics"]:
                severity_symbol = DiagnosticTrackingService.get_severity_symbol(
                    d["severity"]
                )
                code_suffix = f" [{d['code']}]" if d.get("code") else ""
                source_suffix = f" ({d['source']})" if d.get("source") else ""
                line = d["range"]["start"]["line"] + 1
                char = d["range"]["start"]["character"] + 1
                diagnostic_lines.append(
                    f"  {severity_symbol} [Line {line}:{char}] "
                    f"{d['message']}{code_suffix}{source_suffix}"
                )
            diagnostics = "\n".join(diagnostic_lines)
            parts.append(f"{filename}:\n{diagnostics}")
        result = "\n\n".join(parts)

        if len(result) > MAX_DIAGNOSTICS_SUMMARY_CHARS:
            return (
                result[: MAX_DIAGNOSTICS_SUMMARY_CHARS - len(truncation_marker)]
                + truncation_marker
            )
        return result

    @staticmethod
    def get_severity_symbol(severity: str) -> str:
        """Get the severity symbol for a diagnostic."""
        return {
            "Error": _FIGURES_CROSS,
            "Warning": _FIGURES_WARNING,
            "Info": _FIGURES_INFO,
            "Hint": _FIGURES_STAR,
        }.get(severity, _FIGURES_BULLET)


diagnostic_tracker = DiagnosticTrackingService.get_instance()
