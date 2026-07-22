"""Browser artifacts — the durable record of what an agent did in the browser.

Every browser action the agent takes is captured as an **artifact event** so a run can be audited or
replayed after the fact: where it went, what each page was, what it clicked/typed, and the page's DOM
at that moment. Four kinds, matching the request:

* **navigation** — a goto/back/forward/reload, with the target URL.
* **page**       — a page-level observation (snapshot / wait): the landed URL, title, tab count.
* **interaction**— a click / type / key-press, with the element ref and (optionally redacted) input.
* **DOM content**— the page HTML at the time of the event, stored as a content-addressed blob and
  referenced from the event (identical DOMs across events share one file — free dedup).

Layout (next to ``browser-session.json``, under the per-session dir):

    <config-home>/projects/<sanitized-cwd>/<session-id>/browser-artifacts/
        events.jsonl        # append-only event log, one JSON object per line
        dom/<sha16>.html    # DOM blobs, content-addressed

Writes are **best-effort and off-thread** (``asyncio.to_thread``): recording the trail must never
slow or fail a browser action. Recording is gated by ``TABVIS_BROWSER_ARTIFACTS`` (default on).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import uuid
from typing import Any

from tabvis.browser.session import utc_now
from tabvis.utils.browser_config import (
    get_browser_artifacts_max_dom_bytes,
    is_browser_artifacts_dom_enabled,
    is_browser_artifacts_enabled,
    is_browser_artifacts_include_input,
    is_browser_artifacts_redact_input,
)
from tabvis.utils.debug import log_for_debugging

ARTIFACTS_SUBDIR = "browser-artifacts"
EVENTS_FILENAME = "events.jsonl"
DOM_SUBDIR = "dom"
_MAX_INPUT_CHARS = 500

# A run of 13–19 digits (spaces/dashes allowed between them) — a credit/debit card PAN. Validated
# with the Luhn checksum below so a plain long number (an order id, a phone) is not over-redacted.
_CARD_RE = re.compile(r"(?:\d[ -]?){13,19}")
# A long high-entropy token: 20+ chars from the URL-/JWT-safe alphabet, with at least one digit —
# catches API keys, bearer tokens, JWTs, session ids. Kept deliberately conservative so ordinary
# prose (which lacks a digit and rarely runs 20 unbroken word chars) is left intact.
_TOKEN_RE = re.compile(r"\b(?=[A-Za-z0-9._-]*\d)[A-Za-z0-9._-]{20,}\b")

# Per-session-dir monotonic event counter (single event loop, so a plain dict is race-free enough).
_seq_by_dir: dict[str, int] = {}


def _session_dir_for(session_id: str | None) -> str:
    from tabvis.bootstrap.state import get_original_cwd, get_session_id
    from tabvis.utils.session_storage_portable import get_project_dir

    sid = session_id or str(get_session_id())
    return os.path.join(get_project_dir(get_original_cwd()), sid)


def get_artifacts_dir(session_id: str | None = None) -> str:
    """``<session-dir>/browser-artifacts`` (created lazily by the writer)."""
    return os.path.join(_session_dir_for(session_id), ARTIFACTS_SUBDIR)


def events_path(session_id: str | None = None) -> str:
    return os.path.join(get_artifacts_dir(session_id), EVENTS_FILENAME)


def _next_seq(directory: str) -> int:
    """Monotonic seq for a session's artifacts dir, initialized from any existing log."""
    if directory not in _seq_by_dir:
        n = 0
        path = os.path.join(directory, EVENTS_FILENAME)
        try:
            with open(path, encoding="utf-8") as fh:
                n = sum(1 for _ in fh)
        except OSError:
            n = 0
        _seq_by_dir[directory] = n
    _seq_by_dir[directory] += 1
    return _seq_by_dir[directory]


def _store_dom_sync(directory: str, html: str) -> tuple[str, int]:
    """Write a DOM blob content-addressed by sha256; return (relative ref, byte length).

    Content addressing means an unchanged DOM across successive events maps to the same file, so the
    store never duplicates identical HTML.
    """
    data = html.encode("utf-8", "replace")
    digest = hashlib.sha256(data).hexdigest()[:16]
    ref = os.path.join(DOM_SUBDIR, f"{digest}.html")
    dom_dir = os.path.join(directory, DOM_SUBDIR)
    os.makedirs(dom_dir, exist_ok=True)
    path = os.path.join(directory, ref)
    if not os.path.exists(path):  # content-addressed => skip if already written
        # Unique per writer: PID alone collides across concurrent asyncio.to_thread workers whose
        # captured DOM hashes to the same digest (both would stage to one .tmp.<pid> file).
        tmp = f"{path}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(html)
        os.replace(tmp, path)
    return ref, len(data)


def _append_event_sync(directory: str, event: dict[str, Any]) -> None:
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, EVENTS_FILENAME)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, default=str) + "\n")


def _luhn_ok(digits: str) -> bool:
    """Luhn (mod-10) checksum — the check every real card number satisfies."""
    total, alt = 0, False
    for ch in reversed(digits):
        d = ord(ch) - 48
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def _looks_sensitive(text: str) -> bool:
    """Whether ``text`` looks like a secret that must never be persisted, whatever the config.

    Catches the two classes the design calls out that we *can* recognise from the value alone: a
    card number (13–19 digits passing Luhn) and a long high-entropy token / API key. Password fields
    have no tell in the value, so those are covered by the default (redact-unless-opted-in) posture
    rather than here.
    """
    for m in _CARD_RE.finditer(text):
        digits = re.sub(r"[ -]", "", m.group())
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            return True
    return bool(_TOKEN_RE.search(text))


def _redact_interaction(interaction: dict[str, Any]) -> dict[str, Any]:
    """Reduce typed text to just its length unless input inclusion is explicitly opted into.

    Default posture (secure): the artifact keeps ``text_len`` and drops the text — keystrokes
    routinely carry credentials. ``TABVIS_BROWSER_ARTIFACTS_INCLUDE_INPUT=1`` opts into saving the
    (truncated) text, *except* when it looks like a card number / token, which is stripped
    unconditionally. ``TABVIS_BROWSER_ARTIFACTS_REDACT_INPUT=1`` forces redaction even when inclusion
    is on.
    """
    if "text" not in interaction:
        return interaction
    text = interaction.get("text")
    text_len = len(text) if isinstance(text, str) else 0

    include = is_browser_artifacts_include_input() and not is_browser_artifacts_redact_input()
    if include and isinstance(text, str) and not _looks_sensitive(text):
        out = {**interaction, "text_len": text_len}
        if len(text) > _MAX_INPUT_CHARS:
            out["text"] = text[:_MAX_INPUT_CHARS]
            out["text_truncated"] = True
        return out

    out = {k: v for k, v in interaction.items() if k != "text"}
    out["text_redacted"] = True
    out["text_len"] = text_len
    if include:  # inclusion was on but the value tripped the sensitive-content guard
        out["text_redacted_reason"] = "sensitive"
    return out


def _workspace_id_for(agent_id: str | None) -> str | None:
    """The agent's workspace id (WS-4), best-effort. None if there is no workspace yet."""
    if not agent_id:
        return None
    try:
        from tabvis.browser.workspace import get_workspace_for_agent

        record = get_workspace_for_agent(agent_id)
        return record.workspace_id if record is not None else None
    except Exception:  # noqa: BLE001
        return None


async def record_browser_artifact(event: dict[str, Any], data: dict[str, Any]) -> None:
    """Record one browser action as an artifact. Best-effort — never fails/slows the action.

    ``event`` carries the action-specific fields (type/action/url/interaction) from the tool; ``data``
    is the BrowserService observation (url/title/tab_count/…). The two are merged, the DOM is captured
    (if enabled and a live browser is around), and the row is appended to ``events.jsonl`` off-thread.
    """
    if not is_browser_artifacts_enabled():
        return
    try:
        from tabvis.browser.manager import current_agent_id

        agent_id = current_agent_id()
        directory = get_artifacts_dir()
        record: dict[str, Any] = {
            "seq": _next_seq(directory),
            "ts": utc_now(),
            "agent_id": agent_id,
            "workspace_id": _workspace_id_for(agent_id),  # WS-4: key the artifact to its workspace
            "type": event.get("type", "page"),
            "action": event.get("action"),
            # Page metadata — from the observation the action produced.
            "url": event.get("url") or data.get("url"),
            "title": data.get("title"),
            "tab_count": data.get("tab_count"),
        }
        if data.get("waited_out") is not None:
            record["waited_out"] = data["waited_out"]
        interaction = event.get("interaction")
        if interaction:
            record["interaction"] = _redact_interaction(interaction)

        # DOM content: capture the live page HTML and store it content-addressed.
        if is_browser_artifacts_dom_enabled():
            html = await _capture_dom()
            if html:
                ref, nbytes = await asyncio.to_thread(_store_dom_sync, directory, html)
                record["dom_ref"] = ref
                record["dom_bytes"] = nbytes

        await asyncio.to_thread(_append_event_sync, directory, record)

        # PERS-4: index the event in the SQLite metadata store. Best-effort — the JSONL log above
        # stays the source of truth, so a DB hiccup never affects the trail.
        try:
            from tabvis.bootstrap.state import get_session_id
            from tabvis.browser.persistence import db

            await asyncio.to_thread(
                db.insert_artifact, str(get_session_id()), record.get("agent_id"), record
            )
        except Exception as e:  # noqa: BLE001
            log_for_debugging(f"[ARTIFACTS] failed to index artifact in sqlite: {e}")
    except Exception as e:  # noqa: BLE001 - recording the trail must never break a browser action
        log_for_debugging(f"[ARTIFACTS] failed to record browser artifact: {e}")


def _hash_file_sync(path: str) -> tuple[str | None, int | None]:
    """``(sha256_hex, size_bytes)`` of a file on disk, or ``(None, None)`` if unreadable."""
    try:
        h = hashlib.sha256()
        size = 0
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
                size += len(chunk)
        return h.hexdigest(), size
    except OSError:
        return None, None


async def record_download_artifact(
    *,
    action: str,
    url: str | None,
    path: str | None,
    filename: str | None = None,
    policy_effect: str | None = None,
    policy_rule_id: str | None = None,
    quarantined: bool = False,
    extra: dict[str, Any] | None = None,
) -> None:
    """Record a ``type=download`` artifact event — the audit link for a fetched file (issue #5).

    Stores only a *reference* to the file (``path_ref``), its ``sha256`` and ``size_bytes``, plus the
    source URL and the policy decision that let it through — never the file's bytes (those stay in
    the workspace / quarantine). This closes the audit chain: a download is now correlated with the
    navigation, click and permission decision around it, exactly like the other artifact kinds.

    ``action`` is one of ``explicit_download`` / ``click_download`` / ``pdf_navigation``. Best-effort:
    a failure here never breaks a download.
    """
    if not is_browser_artifacts_enabled():
        return
    try:
        from tabvis.browser.manager import current_agent_id

        agent_id = current_agent_id()
        directory = get_artifacts_dir()
        sha256 = size_bytes = None
        if path:
            sha256, size_bytes = await asyncio.to_thread(_hash_file_sync, path)
        record: dict[str, Any] = {
            "seq": _next_seq(directory),
            "ts": utc_now(),
            "agent_id": agent_id,
            "workspace_id": _workspace_id_for(agent_id),
            "type": "download",
            "action": action,
            "url": url,
            "filename": filename or (os.path.basename(path) if path else None),
            "path_ref": path,
            "sha256": sha256,
            "size_bytes": size_bytes,
            "policy_effect": policy_effect,
            "policy_rule_id": policy_rule_id,
            "quarantined": quarantined,
        }
        if extra:
            record.update(extra)
        await asyncio.to_thread(_append_event_sync, directory, record)
        try:
            from tabvis.bootstrap.state import get_session_id
            from tabvis.browser.persistence import db

            await asyncio.to_thread(
                db.insert_artifact, str(get_session_id()), record.get("agent_id"), record
            )
        except Exception as e:  # noqa: BLE001
            log_for_debugging(f"[ARTIFACTS] failed to index download artifact in sqlite: {e}")
    except Exception as e:  # noqa: BLE001 - recording the trail must never break a download
        log_for_debugging(f"[ARTIFACTS] failed to record download artifact: {e}")


async def _capture_dom() -> str:
    """The current page's DOM via the live BrowserService, or "" if none / capture fails."""
    from tabvis.browser.manager import get_browser_service

    service = get_browser_service()
    if service is None or not service.is_alive():
        return ""
    try:
        return await service.capture_dom(max_bytes=get_browser_artifacts_max_dom_bytes())
    except Exception as e:  # noqa: BLE001
        log_for_debugging(f"[ARTIFACTS] DOM capture failed: {e}")
        return ""


# --------------------------------------------------------------------------- read side


def load_artifacts(session_id: str | None = None) -> list[dict[str, Any]]:
    """Every recorded artifact event for a session (oldest first). [] if none."""
    path = events_path(session_id)
    out: list[dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except OSError:
        return []
    return out


def read_dom(dom_ref: str, session_id: str | None = None) -> str | None:
    """The stored DOM blob referenced by an event's ``dom_ref`` (relative path), or None."""
    if not dom_ref:
        return None
    # Guard against a path escaping the artifacts dir.
    directory = get_artifacts_dir(session_id)
    path = os.path.normpath(os.path.join(directory, dom_ref))
    if not path.startswith(os.path.join(directory, DOM_SUBDIR) + os.sep):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def artifacts_summary(session_id: str | None = None) -> dict[str, Any]:
    """A compact roll-up for a session's artifacts (counts by type + last url)."""
    events = load_artifacts(session_id)
    by_type: dict[str, int] = {}
    for e in events:
        by_type[e.get("type", "?")] = by_type.get(e.get("type", "?"), 0) + 1
    return {
        "count": len(events),
        "by_type": by_type,
        "last_url": events[-1].get("url") if events else None,
    }
