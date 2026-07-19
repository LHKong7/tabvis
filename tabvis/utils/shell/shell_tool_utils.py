"""Shell-tool name utilities

Exposes the shell tool names and the runtime gate for the PowerShell tool.

Implementation notes (per ``docs/SPINE_CONTRACTS.md`` + the FLAT ``tabvis/tools`` architecture):
- ``BASH_TOOL_NAME`` is imported from the flat :mod:`tabvis.agent.tools.bash_tool` (the TS
  ``tools/BashTool/toolName`` collapsed into the single module).
- ``POWERSHELL_TOOL_NAME`` is a small constant (value ``'PowerShell'``) inlined directly here.
- ``isEnvDefinedFalsy`` / ``isEnvTruthy`` → :func:`tabvis.utils.env_utils.is_env_defined_falsy` /
  :func:`~tabvis.utils.env_utils.is_env_truthy`; ``getPlatform`` → :func:`tabvis.utils.platform.get_platform`.
- ``process.env.X`` → ``os.environ.get('X')``.
"""

from __future__ import annotations


from tabvis.agent.tools.bash_tool import BASH_TOOL_NAME

POWERSHELL_TOOL_NAME = "PowerShell"

SHELL_TOOL_NAMES: list[str] = [BASH_TOOL_NAME, POWERSHELL_TOOL_NAME]
