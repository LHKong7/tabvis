"""Context Runtime — deterministic Context Pack assembly (design §11).

Today tabvis assembles model context across system prompts, project instructions, transcript, memory,
tools, MCP resources, and browser observations, with ownership spread across the stack (design §11.1).
The Context Runtime makes that assembly **deterministic and inspectable**: an ordered set of providers
each contributes labeled sections, a deterministic budget decides what fits, and the result is an
immutable :class:`ContextPack` with a content ``digest`` and full ``provenance`` — so identical sources
produce an identical digest and every include/drop decision is explainable (design §15 Phase 5).

The runtime is a pure function of its :class:`ContextRequest`: providers read from the request's source
snapshot, never from ambient mutable state, which is what makes the digest reproducible.
"""

from __future__ import annotations
