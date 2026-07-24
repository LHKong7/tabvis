"""DLP Gateway (docs/CREDENTIAL_INJECTION_DESIGN.md §11).

Phase 0 delivers the secret **canary** core (``canary.py``): an irreversible, per-process-keyed
fingerprint registry plus the value/substring scan that egress points fail closed on. The unified
Gateway that routes *every* outbound surface (model requests, transcripts, artifacts, logs, telemetry,
API) through this scan is Phase 5 (design §11.1).
"""

from __future__ import annotations

from tabvis.dlp import canary

__all__ = ["canary"]
