"""Channel Framework (design §4).

A Channel turns an external conversation (Web console, Slack, Feishu, a webhook) into normalized
inbound messages, and turns gateway outbound deliveries back into that channel's format. The Agent
Runtime contains **no per-channel logic** (design §4.1): every channel funnels through the same
inbound pipeline (dedupe → bind → message event → Run) and the same delivery path (idempotent receipt
+ event).

Layout:

* ``channels/core`` — the contract, capability vocabulary, account/binding stores, the inbound
  pipeline, and delivery.
* ``channels/web`` — the ``WebChannel`` that wraps today's HTTP/console behavior (implemented first,
  design §4.8).
* ``channels/plugins`` — external channels; ``example_webhook`` is the proof channel that exercises
  signature verification and webhook-retry idempotency.
"""

from __future__ import annotations
