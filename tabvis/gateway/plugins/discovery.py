"""Plugin discovery (design §8.4).

Discovery yields :class:`PluginCandidate`s from two sources: built-in adapters registered in-process
(design §8.7) and, optionally, ``manifest.json`` files under a plugins directory. A file-discovered
candidate carries no factory — it can be validated but not started until an entrypoint resolver is
wired, which is deliberate: third-party installation is the last migration step (design §8.7).
"""

from __future__ import annotations

import json
import os

from tabvis.gateway.plugins.contract import PluginCandidate
from tabvis.gateway.plugins.manifest import PluginManifest
from tabvis.utils.debug import log_for_debugging

MANIFEST_FILENAME = "manifest.json"


def discover_directory(root: str) -> list[PluginCandidate]:
    """Scan ``root`` for ``<plugin>/manifest.json`` files (design §8.4). Bad manifests are skipped."""
    candidates: list[PluginCandidate] = []
    if not os.path.isdir(root):
        return candidates
    for entry in sorted(os.listdir(root)):
        path = os.path.join(root, entry, MANIFEST_FILENAME)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                manifest = PluginManifest.from_dict(json.load(fh))
            candidates.append(PluginCandidate(manifest=manifest, source=path, factory=None))
        except Exception as e:  # noqa: BLE001 - a bad manifest must not break discovery
            log_for_debugging(f"[PLUGIN] skipped unreadable manifest {path}: {e}")
    return candidates
