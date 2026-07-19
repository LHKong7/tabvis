"""Observation events — the ``RuntimeEvent`` envelope and semantic-observation vocabulary (OBS-1).

This is the *data-only* seam for the observation pipeline described in ``design.md`` (§"Event Model"
and §"Observation Normalizer"). Nothing publishes or consumes these yet — a later step stands up the
in-process Event Bus (OBS-2), turns the post-action hook into a producer (OBS-3), and adds the
Observation Normalizer (OBS-4). This module just defines the immutable envelope every future event
rides in, plus the source and type constants, so the rest of the pipeline can be built against a
stable shape.

The envelope carries id / type / timestamp / agent_id / workspace_id / identity_id / session_id /
execution_id / source / payload / schema_version, all in snake_case. ``payload`` is a plain dict
here (Python has no generics at runtime); the envelope is frozen so an event, once emitted, is
never mutated in place.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from tabvis.browser.session import utc_now

# Bumped when the envelope's shape changes, so persisted/streamed events stay interpretable.
SCHEMA_VERSION = 1

# The adapter an event originated from (``design.md`` RuntimeEvent.source). "runtime" is the daemon
# itself; the rest are the Runtime Adapters. Only "runtime" and "playwright" are wired today.
EventSource = Literal["runtime", "playwright", "cdp", "extension", "filesystem"]
EVENT_SOURCES: tuple[EventSource, ...] = (
    "runtime",
    "playwright",
    "cdp",
    "extension",
    "filesystem",
)


class RawEventType:
    """Raw event types — what a producer emits *before* normalization.

    These line up with the mechanical artifact kinds already recorded today
    (``tabvis.browser.artifacts``): navigation / page / interaction / DOM. ``ACTION_PERFORMED``
    is the coarse "a browser action just completed" event the post-action hook will publish (OBS-3).
    """

    ACTION_PERFORMED = "action.performed"
    NAVIGATION = "navigation"
    PAGE = "page"
    INTERACTION = "interaction"
    DOM = "dom"


class ObservationType:
    """Semantic observation types — what the Normalizer maps raw events *into* (``design.md`` §3).

    These are the domain-level facts an agent should consume ("a page loaded", "an artifact was
    downloaded") rather than raw DOM/Playwright detail. The Normalizer that produces them is OBS-4;
    the vocabulary is fixed here so producers and sinks agree on the strings.
    """

    PAGE_LOADED = "page.loaded"
    ARTIFACT_DOWNLOADED = "artifact.downloaded"
    SEARCH_PERFORMED = "search.performed"
    ELEMENT_INTERACTED = "element.interacted"
    NAVIGATION_PERFORMED = "navigation.performed"
    DIALOG_OPENED = "dialog.opened"
    CONSOLE_ERROR = "console.error"


def new_event_id() -> str:
    """A short, unique event id (``ev_…``), used when a producer does not supply one."""
    return f"ev_{uuid.uuid4().hex[:16]}"


@dataclass(frozen=True)
class RuntimeEvent:
    """The immutable event envelope — the unit that flows over the (future) Event Bus.

    Required: ``type`` (a ``RawEventType`` / ``ObservationType`` string) and ``source``. Everything
    else is optional context that correlates the event to a run: the ids exist so a downstream sink
    can key an event to its agent / workspace / identity / session / execution without re-deriving
    them. ``payload`` carries the type-specific body.
    """

    type: str
    source: EventSource
    payload: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=new_event_id)
    timestamp: str = field(default_factory=utc_now)
    agent_id: str | None = None
    workspace_id: str | None = None
    identity_id: str | None = None
    session_id: str | None = None
    execution_id: str | None = None
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        """A JSON-safe dict view (for persistence / SSE)."""
        return asdict(self)
