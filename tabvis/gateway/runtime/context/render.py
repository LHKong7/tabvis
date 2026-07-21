"""Render a Context Pack into a system-prompt section (design §11 → model call path).

Turns the deterministic pack's system sections into a single labeled block that can be appended to the
model's system prompt. Two guards:

* **No secret material** — ``secret_ref`` sections carry only a reference and are dropped here (design
  §11.7); the pack never held the value anyway.
* **No duplication** — sections the base system prompt already emits (project instructions, memory,
  safety, agent definition) are excluded by default, so only the Context Runtime's *situational*
  additions (workspace/Git, browser, todos, channel identity) reach the model. This is the interim
  overlap boundary until the base prompt assembly is fully migrated onto the pack.

The block is prefixed with the pack id and a short digest so what the model saw is traceable back to an
``explain`` report.
"""

from __future__ import annotations

from tabvis.gateway.runtime.context.pack import SECRET_REF, ContextPack

# The base system prompt already emits these; excluding them avoids double-inclusion.
DEFAULT_EXCLUDED = frozenset({"project_instructions", "memory", "safety", "agent"})


def render_system_context(
    pack: ContextPack, *, exclude_providers: frozenset[str] = DEFAULT_EXCLUDED
) -> str | None:
    """A system-prompt block from the pack's situational sections, or None if there is nothing to add."""
    blocks: list[str] = []
    for section in pack.system_sections:
        if section["provider_id"] in exclude_providers:
            continue
        if section["sensitivity"] == SECRET_REF:
            continue
        content = (section.get("content") or "").strip()
        if not content:
            continue
        blocks.append(f"## {section['title']}\n{content}")

    if not blocks:
        return None
    header = f"# Situational context (pack {pack.context_pack_id}, digest {pack.digest[:12]})"
    return "\n\n".join([header, *blocks])
