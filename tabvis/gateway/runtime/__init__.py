"""Gateway runtime — the Run aggregate and its orchestration (design §7).

The design's central split (§7.1): a durable **Agent** owns identity and configuration; an immutable
**Run** is one prompt-to-terminal execution. Today ``AgentRecord`` conflates the two and overwrites
prompt/timing/outcome on reuse. These modules introduce the Run as its own aggregate so history is
preserved — continuing one Agent yields two queryable Runs (design §15 Phase 1 acceptance).
"""

from __future__ import annotations
