"""Unicode figure/symbol constants.

A leaf constants module: a flat set of single-glyph string constants used across the UI
(status indicators, effort levels, MCP/review/issue markers, blockquote rules). The only
runtime branch is :data:`BLACK_CIRCLE`, which picks a vertically-aligned glyph on macOS and
falls back to the more widely-supported ``●`` elsewhere (Windows/Linux) by checking
``env.platform == "darwin"``.

Casing: ``tabvis/constants/*`` is naming-lint-exempt; the glyph constants keep UPPER_CASE names.
"""

from __future__ import annotations

from tabvis.utils.env import env

# The former is better vertically aligned, but isn't usually supported on Windows/Linux
BLACK_CIRCLE = "⏺" if env.platform == "darwin" else "●"  # ⏺ / ●
BULLET_OPERATOR = "∙"  # ∙
TEARDROP_ASTERISK = "✻"  # ✻
UP_ARROW = "↑"  # ↑ - used for opus 1m merge notice
DOWN_ARROW = "↓"  # ↓ - used for scroll hint
EFFORT_LOW = "○"  # ○ - effort level: low
EFFORT_MEDIUM = "◐"  # ◐ - effort level: medium
EFFORT_HIGH = "●"  # ● - effort level: high
EFFORT_MAX = "◉"  # ◉ - effort level: max (Opus 4.6 only)

# Media/trigger status indicators
PLAY_ICON = "▶"  # ▶
PAUSE_ICON = "⏸"  # ⏸

# MCP subscription indicators
REFRESH_ARROW = "↻"  # ↻ - used for resource update indicator
CHANNEL_ARROW = "←"  # ← - inbound channel message indicator

# Review status indicators (ultrareview diamond states)
DIAMOND_OPEN = "◇"  # ◇ - running
DIAMOND_FILLED = "◆"  # ◆ - completed/failed
REFERENCE_MARK = "※"  # ※ - komejirushi, away-summary recap marker

# Issue flag indicator
FLAG_ICON = "⚑"  # ⚑ - used for issue flag banner

# Blockquote indicator
BLOCKQUOTE_BAR = "▎"  # ▎ - left one-quarter block, used as blockquote line prefix
HEAVY_HORIZONTAL = "━"  # ━ - heavy box-drawing horizontal
