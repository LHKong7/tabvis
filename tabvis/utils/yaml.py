"""YAML parsing wrapper

The TS module uses ``Bun.YAML.parse`` (the built-in, zero-cost parser) when running
under Bun and lazily ``require('yaml')`` otherwise so native Bun builds never load
the ~270KB ``yaml`` npm parser. In Python there is no Bun fast path; the single
backend is PyYAML (already a project dependency), so this collapses to one branch.

The ``yaml`` npm package (and Bun's parser) follow YAML 1.2 / JSON-superset
semantics: a scalar document parses to its scalar value, a mapping to an object,
a sequence to a list, and the empty document to ``null``. PyYAML's ``safe_load``
matches this closely for the frontmatter use-case this wraps (the lone caller,
``frontmatterParser.ts``, treats the result as ``FrontmatterData | null``).

Faithful-behavior notes:
- The TS function is named ``parseYaml`` and returns ``unknown``; the Python
  identifier is snake_case (``parse_yaml``) per the implementation convention. Parsed
  mapping keys are kept verbatim (no key-casing transform) — frontmatter keys are
  wire data consumed downstream.
- ``safe_load`` is used (not ``load``) to avoid arbitrary-object construction; the
  npm ``yaml`` parser likewise does not instantiate host objects from tags by
  default. An empty / whitespace-only / comment-only document yields ``None``,
  matching JS ``null``.
"""

from __future__ import annotations

from typing import Any

import yaml  # PyYAML (stdlib name resolves to the installed package, not tabvis.utils.yaml)


def parse_yaml(input: str) -> Any:
    """Parse a YAML ``input`` string into the corresponding Python value.

    Parity with the TS ``parseYaml`` wrapper (``yaml.parse`` / ``Bun.YAML.parse``):
    scalars → scalar, mapping → ``dict``, sequence → ``list``, empty document →
    ``None``.
    """
    return yaml.safe_load(input)
