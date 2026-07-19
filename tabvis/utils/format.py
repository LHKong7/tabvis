"""Pure display formatters

Leaf-safe number / byte / duration / relative-time / list formatting helpers (no Ink). The
width-aware truncation helpers stayed in ``src/utils/truncate.ts`` (→ :mod:`tabvis.utils.truncate`)
and are re-exported here for back-compat, exactly as the TS module re-exports them.

Casing: Python identifiers are snake_case. These functions only ever return ``str`` (display
text), so there are no wire-key dicts to preserve; the literal suffixes (``KB``/``MB``/``GB``,
``d``/``h``/``m``/``s``, ``·`` separators) are protocol text kept verbatim.

Fidelity notes (verified against the TS oracle's JS runtime):

* ``Number.prototype.toFixed(n)`` rounds half-away-from-zero on a decimal value; Python's
  built-in ``round`` is banker's rounding, so :func:`_to_fixed` uses :class:`decimal.Decimal`
  with ``ROUND_HALF_UP`` to match.
* ``Intl.NumberFormat('en-US', {notation:'compact', …})`` is reproduced by :func:`_compact_format`
  (the CLDR "short" compact ladder K/M/B/T with up to one fraction digit). This is a faithful
  stdlib slice — CPython has no ICU/``Intl`` — covering the magnitudes the callers actually pass
  (token counts, ``formatNumber``/``formatTokens``).
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

from tabvis.utils.intl import get_relative_time_format, get_time_zone

# Back-compat: truncate helpers moved to ./truncate.py (needs string-width). Mirror the TS
# re-export so callers importing them from ``format`` keep working.
from tabvis.utils.truncate import (  # noqa: F401  (re-exported for back-compat)
    truncate,
    truncate_path_middle,
    truncate_start_to_width,
    truncate_to_width,
    truncate_to_width_no_ellipsis,
    wrap_text,
)

__all__ = [
    "format_duration",
    "format_file_size",
    "format_log_metadata",
    "format_number",
    "format_relative_time",
    "format_relative_time_ago",
    "format_reset_text",
    "format_reset_time",
    "format_seconds_short",
    "format_tokens",
    # re-exported truncation helpers
    "truncate",
    "truncate_path_middle",
    "truncate_start_to_width",
    "truncate_to_width",
    "truncate_to_width_no_ellipsis",
    "wrap_text",
]


def _to_fixed(value: float, digits: int) -> str:
    """Format a number with fixed precision using half-away-from-zero rounding."""
    quant = Decimal(1).scaleb(-digits)  # 10**-digits
    rounded = Decimal(repr(value)).quantize(quant, rounding=ROUND_HALF_UP)
    return f"{rounded:.{digits}f}"


def _strip_trailing_zero_decimal(text: str) -> str:
    """Drop a trailing ``.0`` (JS ``.replace(/\\.0$/, '')``)."""
    return text[:-2] if text.endswith(".0") else text


def format_file_size(size_in_bytes: float) -> str:
    """Format a byte count to a human-readable string (KB, MB, GB).

    Example: ``format_file_size(1536)`` → ``"1.5KB"``.
    """
    kb = size_in_bytes / 1024
    if kb < 1:
        return f"{size_in_bytes} bytes"
    if kb < 1024:
        return f"{_strip_trailing_zero_decimal(_to_fixed(kb, 1))}KB"
    mb = kb / 1024
    if mb < 1024:
        return f"{_strip_trailing_zero_decimal(_to_fixed(mb, 1))}MB"
    gb = mb / 1024
    return f"{_strip_trailing_zero_decimal(_to_fixed(gb, 1))}GB"


def format_seconds_short(ms: float) -> str:
    """Format milliseconds as seconds with 1 decimal place (``1234`` → ``"1.2s"``).

    Unlike :func:`format_duration`, always keeps the decimal — use for sub-minute timings
    where the fractional second is meaningful (TTFT, hook durations, etc.).
    """
    return f"{_to_fixed(ms / 1000, 1)}s"


def format_duration(
    ms: float,
    *,
    hide_trailing_zeros: bool = False,
    most_significant_only: bool = False,
) -> str:
    """Format a duration in milliseconds as a compact ``d/h/m/s`` string.

    Mirrors the TS ``formatDuration(ms, {hideTrailingZeros?, mostSignificantOnly?})``.
    """
    if ms < 60000:
        # Special case for 0.
        if ms == 0:
            return "0s"
        # For durations < 1s, show 1 decimal place (e.g., 0.5s).
        if ms < 1:
            s = _to_fixed(ms / 1000, 1)
            return f"{s}s"
        s = str(int(ms // 1000))
        return f"{s}s"

    days = int(ms // 86400000)
    hours = int((ms % 86400000) // 3600000)
    minutes = int((ms % 3600000) // 60000)
    # JS ``Math.round`` is round-half-UP (towards +Infinity), not Python's banker's rounding.
    seconds = math.floor((ms % 60000) / 1000 + 0.5)

    # Handle rounding carry-over (e.g., 59.5s rounds to 60s).
    if seconds == 60:
        seconds = 0
        minutes += 1
    if minutes == 60:
        minutes = 0
        hours += 1
    if hours == 24:
        hours = 0
        days += 1

    hide = hide_trailing_zeros

    if most_significant_only:
        if days > 0:
            return f"{days}d"
        if hours > 0:
            return f"{hours}h"
        if minutes > 0:
            return f"{minutes}m"
        return f"{seconds}s"

    if days > 0:
        if hide and hours == 0 and minutes == 0:
            return f"{days}d"
        if hide and minutes == 0:
            return f"{days}d {hours}h"
        return f"{days}d {hours}h {minutes}m"
    if hours > 0:
        if hide and minutes == 0 and seconds == 0:
            return f"{hours}h"
        if hide and seconds == 0:
            return f"{hours}h {minutes}m"
        return f"{hours}h {minutes}m {seconds}s"
    if minutes > 0:
        if hide and seconds == 0:
            return f"{minutes}m"
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


# CLDR "short" compact ladder used by ``Intl.NumberFormat`` for en-US, smallest divisor first
# (so a rounding overflow at one tier — e.g. 999.95K → 1000.0 — can promote to the next tier).
_COMPACT_DIVISORS: list[tuple[int, str]] = [
    (1_000, "K"),
    (1_000_000, "M"),
    (1_000_000_000, "B"),
    (1_000_000_000_000, "T"),
]


def _compact_format(number: float, min_fraction_digits: int) -> str:
    """Reproduce ``Intl.NumberFormat('en-US', {notation:'compact', maximumFractionDigits:1, …})``.

    ``minimumFractionDigits`` is ``min_fraction_digits``; ``maximumFractionDigits`` is fixed at 1
    (the only configuration the callers use). Magnitudes below 1000 are formatted with no suffix.
    Handles the CLDR rounding-promotion case (e.g. ``999950`` → ``"1.0M"``, not ``"1000.0K"``).
    """
    sign = "-" if number < 0 else ""
    magnitude = abs(number)

    # Natural tier: the largest divisor that fits. Index -1 means "below 1000" (no suffix).
    tier = -1
    for idx, (divisor, _suffix) in enumerate(_COMPACT_DIVISORS):
        if magnitude >= divisor:
            tier = idx
        else:
            break

    if tier < 0:
        # Below 1000: plain number, up to 1 fraction digit, honoring the minimum.
        return f"{sign}{_format_fraction(magnitude, min_fraction_digits)}"

    # Rounding-promotion: if scaling+rounding overflows to 1000.0, bump to the next tier (e.g.
    # 999950 → 0.99995M → "1.0M"). The top tier (T) keeps a >=1000 reading verbatim ("1000.0t").
    divisor = _COMPACT_DIVISORS[tier][0]
    rounded = Decimal(repr(magnitude / divisor)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    if rounded >= Decimal(1000) and tier + 1 < len(_COMPACT_DIVISORS):
        tier += 1
        divisor = _COMPACT_DIVISORS[tier][0]

    suffix = _COMPACT_DIVISORS[tier][1]
    return f"{sign}{_format_fraction(magnitude / divisor, min_fraction_digits)}{suffix}"


def _format_fraction(value: float, min_fraction_digits: int) -> str:
    """Round to <=1 fraction digit (half-up), then trim to honor ``min_fraction_digits``."""
    rounded = Decimal(repr(value)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    if min_fraction_digits >= 1:
        # Keep exactly one fraction digit (maximumFractionDigits is 1).
        return f"{rounded:.1f}"
    # minimumFractionDigits == 0: drop a trailing ``.0``.
    if rounded == rounded.to_integral_value():
        return str(int(rounded))
    return f"{rounded:.1f}"


def format_number(number: float) -> str:
    """Format ``number`` compactly and lowercased (``1321`` → ``"1.3k"``, ``900`` → ``"900"``)."""
    # Only use minimumFractionDigits for numbers that will be shown in compact notation.
    should_use_consistent_decimals = number >= 1000
    min_fraction = 1 if should_use_consistent_decimals else 0
    return _compact_format(number, min_fraction).lower()


def format_tokens(count: float) -> str:
    """Format a token count compactly, dropping a redundant ``.0`` (``1000`` → ``"1k"``)."""
    return format_number(count).replace(".0", "")


# Time intervals with custom short units (mirrors the TS ``intervals`` table).
_INTERVALS: list[tuple[str, int, str]] = [
    ("year", 31536000, "y"),
    ("month", 2592000, "mo"),
    ("week", 604800, "w"),
    ("day", 86400, "d"),
    ("hour", 3600, "h"),
    ("minute", 60, "m"),
    ("second", 1, "s"),
]


def _trunc(value: float) -> int:
    """JS ``Math.trunc`` — truncate towards zero."""
    return int(value)


def format_relative_time(
    date: datetime,
    *,
    style: str = "narrow",
    numeric: str = "always",
    now: datetime | None = None,
) -> str:
    """Format ``date`` relative to ``now`` (``"in 5m"`` / ``"3h ago"`` for narrow style)."""
    now = now if now is not None else datetime.now(UTC)
    diff_in_ms = _epoch_ms(date) - _epoch_ms(now)
    # Math.trunc towards zero for both positive and negative values.
    diff_in_seconds = _trunc(diff_in_ms / 1000)

    for unit, interval_seconds, short_unit in _INTERVALS:
        if abs(diff_in_seconds) >= interval_seconds:
            value = _trunc(diff_in_seconds / interval_seconds)
            if style == "narrow":
                if diff_in_seconds < 0:
                    return f"{abs(value)}{short_unit} ago"
                return f"in {value}{short_unit}"
            # For days and longer, use long style regardless of the style parameter.
            return get_relative_time_format("long", numeric).format(value, unit)

    # For values less than 1 second.
    if style == "narrow":
        return "0s ago" if diff_in_seconds <= 0 else "in 0s"
    return get_relative_time_format(style, numeric).format(0, "second")


def format_relative_time_ago(
    date: datetime,
    *,
    style: str = "narrow",
    numeric: str = "always",
    now: datetime | None = None,
) -> str:
    """Format ``date`` as elapsed time. Future dates fall back to plain relative time."""
    now = now if now is not None else datetime.now(UTC)
    if _epoch_ms(date) > _epoch_ms(now):
        # For future dates, just return the relative time without "ago".
        return format_relative_time(date, style=style, numeric=numeric, now=now)
    # For past dates, force numeric: 'always' to ensure we get "X units ago".
    return format_relative_time(date, style=style, numeric="always", now=now)


def _epoch_ms(date: datetime) -> float:
    """Epoch milliseconds (parity with JS ``Date.prototype.getTime``)."""
    if date.tzinfo is None:
        date = date.replace(tzinfo=UTC)
    return date.timestamp() * 1000


def format_log_metadata(log: dict) -> str:
    """Format log metadata for display (time, size or message count, branch, tag, PR).

    ``log`` is a plain dict with wire keys ``modified`` (``datetime``), ``messageCount`` (int),
    optional ``fileSize``/``gitBranch``/``tag``/``agentSetting``/``prNumber``/``prRepository``.
    """
    file_size = log.get("fileSize")
    size_or_count = (
        format_file_size(file_size)
        if file_size is not None
        else f"{log['messageCount']} messages"
    )
    parts: list[str] = [format_relative_time_ago(log["modified"], style="short")]
    git_branch = log.get("gitBranch")
    if git_branch:
        parts.append(git_branch)
    parts.append(size_or_count)

    tag = log.get("tag")
    if tag:
        parts.append(f"#{tag}")
    agent_setting = log.get("agentSetting")
    if agent_setting:
        parts.append(f"@{agent_setting}")
    pr_number = log.get("prNumber")
    if pr_number:
        pr_repository = log.get("prRepository")
        parts.append(f"{pr_repository}#{pr_number}" if pr_repository else f"#{pr_number}")
    return " · ".join(parts)


_MONTHS = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]


def _format_hour12(hour24: int) -> tuple[int, str]:
    """Return ``(hour12, ampm)`` for a 0-23 hour (matches ``toLocaleTimeString`` hour12)."""
    ampm = "am" if hour24 < 12 else "pm"
    hour12 = hour24 % 12
    if hour12 == 0:
        hour12 = 12
    return hour12, ampm


def format_reset_time(
    timestamp_in_seconds: int | None,
    show_timezone: bool = False,
    show_time: bool = True,
) -> str | None:
    """Format a reset timestamp (epoch seconds) for display, or ``None`` when falsy.

    Within 24h shows just the time; beyond 24h prepends the date (and the year when it differs
    from the current year). Mirrors the TS ``toLocaleString``/``toLocaleTimeString`` 'en-US' shape
    (no space before am/pm, lowercased).
    """
    if not timestamp_in_seconds:
        return None

    date = datetime.fromtimestamp(timestamp_in_seconds, tz=_local_tz()).astimezone(_local_tz())
    now = datetime.now(_local_tz())
    minutes = date.minute

    hours_until_reset = (_epoch_ms(date) - _epoch_ms(now)) / (1000 * 60 * 60)
    tz_suffix = f" ({get_time_zone()})" if show_timezone else ""

    if hours_until_reset > 24:
        # Date (+ optional time) for resets more than a day away.
        date_part = f"{_MONTHS[date.month - 1]} {date.day}"
        if date.year != now.year:
            date_part = f"{date_part}, {date.year}"
        if show_time:
            hour12, ampm = _format_hour12(date.hour)
            if minutes == 0:
                time_part = f"{hour12}{ampm}"
            else:
                time_part = f"{hour12}:{minutes:02d}{ampm}"
            return f"{date_part}, {time_part}{tz_suffix}"
        return f"{date_part}{tz_suffix}"

    # Within 24 hours: just the time.
    hour12, ampm = _format_hour12(date.hour)
    if minutes == 0:
        time_string = f"{hour12}{ampm}"
    else:
        time_string = f"{hour12}:{minutes:02d}{ampm}"
    return f"{time_string}{tz_suffix}"


def format_reset_text(
    resets_at: str,
    show_timezone: bool = False,
    show_time: bool = True,
) -> str:
    """Parse an ISO timestamp string and format it via :func:`format_reset_time`."""
    dt = datetime.fromisoformat(resets_at.replace("Z", "+00:00"))
    return f"{format_reset_time(int(_epoch_ms(dt) // 1000), show_timezone, show_time)}"


def _local_tz():
    """Local timezone (parity with JS ``Date`` rendering in the host timezone)."""
    return datetime.now().astimezone().tzinfo
