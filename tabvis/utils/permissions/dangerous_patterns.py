"""Dangerous shell-tool allow-rule prefixes.

An allow rule like ``Bash(python:*)`` or ``PowerShell(node:*)`` lets the model run arbitrary
code via that interpreter. These pattern lists feed the ``is_dangerous_{bash,powershell}_
permission`` predicates (in ``permissionSetup.ts`` — not yet implemented). The matcher in each
predicate handles the rule-shape variants (exact, ``:*``, trailing ``*``, `` *``, `` -…*``);
PS-specific cmdlet strings live with the PowerShell predicate.

The ant-only tail of :data:`DANGEROUS_BASH_PATTERNS` is gated on ``USER_TYPE === 'ant'`` exactly
as in the TS (an empirical-risk call grounded in ant sandbox dotfile data, not a universal
judgment). Evaluated at import time, matching the TS module-init evaluation of
``process.env.USER_TYPE``.

Casing: ``DANGEROUS_BASH_PATTERNS`` / ``CROSS_PLATFORM_CODE_EXEC`` are UPPER_CASE module
constants; pattern strings are matcher inputs kept verbatim.
"""

from __future__ import annotations


# Cross-platform code-execution entry points present on both Unix and Windows.
# Shared to prevent the two lists drifting apart on interpreter additions.
CROSS_PLATFORM_CODE_EXEC: tuple[str, ...] = (
    # Interpreters
    "python",
    "python3",
    "python2",
    "node",
    "deno",
    "tsx",
    "ruby",
    "perl",
    "php",
    "lua",
    # Package runners
    "npx",
    "bunx",
    "npm run",
    "yarn run",
    "pnpm run",
    "bun run",
    # Shells reachable from both (Git Bash / WSL on Windows, native on Unix)
    "bash",
    "sh",
    # Remote arbitrary-command wrapper (native OpenSSH on Win10+)
    "ssh",
)

# Provider internal: tabvis-only tools plus general tools that ant sandbox dotfile data shows are
# commonly over-allowlisted as broad prefixes. These stay tabvis-only — external users don't have
# coo, and the rest are an empirical-risk call grounded in ant sandbox data, not a universal
# "this tool is unsafe" judgment. PS may want these once it has usage data.
_ANT_ONLY_BASH_PATTERNS: tuple[str, ...] = (
    "fa run",
    # Cluster code launcher — arbitrary code on the cluster
    "coo",
    # Network/exfil: gh gist create --public, gh api arbitrary HTTP, curl/wget POST. gh api needs
    # its own entry — the matcher is exact-shape, not prefix, so pattern 'gh' alone does not catch
    # rule 'gh api:*' (same reason 'npm run' is separate from 'npm').
    "gh",
    "gh api",
    "curl",
    "wget",
    # git config core.sshCommand / hooks install = arbitrary code
    "git",
    # Cluster resource writes
    "kubectl",
)

DANGEROUS_BASH_PATTERNS: tuple[str, ...] = (
    *CROSS_PLATFORM_CODE_EXEC,
    "zsh",
    "fish",
    "eval",
    "exec",
    "env",
    "xargs",
    "sudo",
    *(),
)
