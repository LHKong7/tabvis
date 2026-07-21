"""Deterministic budget policy (design §11.5).

The policy, made concrete:

* **Required sections are reserved first** and never dropped — system safety, the current user message,
  and the tool schemas the Run needs (a provider marks these ``required``).
* **Optional sections are ranked**, then greedily included while they fit. The ranking encodes the rest
  of §11.5 through provider-assigned priority/freshness: recent transcript outranks compact summaries,
  which outrank older raw messages, which outrank optional extras.
* **Sections are atomic** — included whole or dropped whole — so structured JSON is never truncated into
  invalid content (design §11.5).

Every drop carries a reason, which is what makes the budget explainable (design §15 Phase 5). The
function is pure and order-stable: identical input yields identical inclusion.
"""

from __future__ import annotations

from dataclasses import dataclass

from tabvis.gateway.runtime.context.pack import ContextSection


@dataclass
class BudgetDecision:
    included: list[ContextSection]      # in original provider order
    dropped: list[tuple[ContextSection, str]]
    reserved_tokens: int
    total_tokens: int
    over_budget: bool                    # required alone exceeded the budget


def plan(sections: list[ContextSection], max_tokens: int) -> BudgetDecision:
    order = {id(s): i for i, s in enumerate(sections)}  # stable original position
    required = [s for s in sections if s.required]
    optional = [s for s in sections if not s.required]

    reserved = sum(s.tokens() for s in required)
    remaining = max_tokens - reserved
    over_budget = reserved > max_tokens

    # Rank optional: priority desc, freshness desc, then original order for a total, stable order.
    ranked = sorted(optional, key=lambda s: (-s.priority, -s.freshness, order[id(s)]))

    chosen: list[ContextSection] = []
    dropped: list[tuple[ContextSection, str]] = []
    running = 0
    for s in ranked:
        cost = s.tokens()
        if running + cost <= remaining:
            chosen.append(s)
            running += cost
        else:
            dropped.append((s, "budget"))

    included = sorted([*required, *chosen], key=lambda s: order[id(s)])
    total = reserved + running
    return BudgetDecision(included=included, dropped=dropped, reserved_tokens=reserved,
                          total_tokens=total, over_budget=over_budget)
