"""Shell provider contract

The TS module is purely structural: a ``SHELL_TYPES`` const tuple, the ``ShellType`` literal,
the ``DEFAULT_HOOK_SHELL`` default, and the ``ShellProvider`` object-shape ``type`` (the bash /
powershell providers implement it). There is no runtime logic here.

Implementation mapping:
- ``SHELL_TYPES`` (a ``readonly [...] as const`` tuple) → an UPPER_CASE Python ``tuple`` constant.
- ``ShellType`` (``(typeof SHELL_TYPES)[number]``) → a :data:`typing.Literal`.
- ``DEFAULT_HOOK_SHELL`` → an UPPER_CASE constant.
- ``ShellProvider`` (a TS object ``type`` with methods) → a :class:`typing.Protocol` so the bash /
  powershell providers structurally satisfy it. ``async`` methods + ``Promise<...>`` returns map to
  ``async def`` returning the awaited value.

Casing: Python identifiers snake_case; class/protocol PascalCase; the SHELL_TYPES / DEFAULT_HOOK_SHELL
constants UPPER_CASE. The provider's ``buildExecCommand`` returns a result dict whose keys
(``commandString`` / ``cwdFilePath``) round-trip into the shell-spawn layer, so they are kept verbatim
as wire keys.
"""

from __future__ import annotations

from typing import Literal, Protocol, TypedDict

# ``['bash', 'powershell'] as const`` → an immutable tuple of the supported shell types.
SHELL_TYPES: tuple[str, ...] = ("bash", "powershell")

# ``(typeof SHELL_TYPES)[number]`` → the union of the literal members.
ShellType = Literal["bash", "powershell"]

# The default shell used by hooks when none is configured.
DEFAULT_HOOK_SHELL: ShellType = "bash"


class BuildExecCommandResult(TypedDict):
    """Result of :meth:`ShellProvider.build_exec_command`.

    Wire keys kept verbatim (camelCase) — this dict is consumed by the shell-spawn layer.
    """

    commandString: str
    cwdFilePath: str


class BuildExecCommandOpts(TypedDict, total=False):
    """Options accepted by :meth:`ShellProvider.build_exec_command`.

    ``id`` and ``useSandbox`` are required at the call site; ``sandboxTmpDir`` is optional.
    Wire keys kept verbatim (camelCase) to mirror the TS ``opts`` object.
    """

    id: int | str
    sandboxTmpDir: str
    useSandbox: bool


class ShellProvider(Protocol):
    """Structural contract every shell provider implements (TS ``ShellProvider`` type).

    Attributes:
        type: the shell type (``'bash'`` | ``'powershell'``).
        shell_path: absolute path to the shell executable.
        detached: whether the child is spawned detached.
    """

    type: ShellType
    shell_path: str
    detached: bool

    async def build_exec_command(
        self,
        command: str,
        opts: BuildExecCommandOpts,
    ) -> BuildExecCommandResult:
        """Build the full command string including all shell-specific setup.

        For bash: source snapshot, session env, disable extglob, eval-wrap, pwd tracking.
        """
        ...

    def get_spawn_args(self, command_string: str) -> list[str]:
        """Shell args for spawn (e.g., ``['-c', '-l', cmd]`` for bash)."""
        ...

    async def get_environment_overrides(self, command: str) -> dict[str, str]:
        """Extra env vars for this shell type.

        May perform async initialization (e.g., tmux socket setup for bash).
        """
        ...
