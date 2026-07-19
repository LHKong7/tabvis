"""Agent swarms / teammate feature gate

The single centralized runtime check for agent-teams/teammate features. Checked everywhere
teammates are referenced (prompts, code, tool ``isEnabled``, UI, etc.).

Gating (faithful to the TS):
  - Ant builds (``USER_TYPE === 'ant'``): always enabled.
  - External builds require BOTH:
      1. Opt-in via the ``TABVIS_EXPERIMENTAL_AGENT_TEAMS`` env var OR the ``--agent-teams`` flag.
      2. The ``tengu_amber_flint`` GrowthBook gate (killswitch, default ``True``).

Behavior notes:
- ``isAgentTeamsFlagSet`` reads ``process.argv`` directly to avoid import cycles with
  ``bootstrap/state`` — mirrored here with :data:`sys.argv`.
- ``isEnvTruthy`` → :func:`tabvis.utils.env_utils.is_env_truthy` (the REAL existing module).
- The killswitch gate: the headless path has no growthbook client, so the cached value is the
  supplied default (``True`` for the killswitch → not tripped), folded to the constant below.
"""

from __future__ import annotations

import os
import sys
from typing import TypeVar

from tabvis.utils.env_utils import is_env_truthy

_T = TypeVar("_T")


def _is_agent_teams_flag_set() -> bool:
    """Whether ``--agent-teams`` was passed on the CLI (reads ``sys.argv`` to avoid import cycles)."""
    return "--agent-teams" in sys.argv


def is_agent_swarms_enabled() -> bool:
    """Centralized runtime gate for agent teams/teammate features.

    Ant builds: always on. External builds: opt-in (env or flag) AND the killswitch gate.
    """

    # External: require opt-in via env var or --agent-teams flag.
    if (
        not is_env_truthy(os.environ.get("TABVIS_EXPERIMENTAL_AGENT_TEAMS"))
        and not _is_agent_teams_flag_set()
    ):
        return False

    return True
