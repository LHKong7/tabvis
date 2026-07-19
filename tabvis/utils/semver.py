"""Semver comparison utilities

The TS module uses ``Bun.semver`` when available and otherwise falls back to the npm ``semver``
package, **always with ``{ loose: true }``**. There is no ``Bun`` in CPython, so this implementation
implements the npm-``semver``-``loose`` behavior directly. ``packaging.version`` is intentionally
**not** used: it implements PEP 440, whose ordering/range syntax diverges from npm semver in ways
these callers depend on — ``||`` OR ranges, ``^``/``~``/``x`` ranges, hyphen ranges, semver
prerelease ordering (``1.0.0-alpha < 1.0.0``), build-metadata being ignored in comparison, and the
npm "prerelease only satisfies a comparator set that pins the same ``[major,minor,patch]`` with its
own prerelease" rule. So this is a focused stdlib-only slice of the exact surface the tabvis tree
uses (``gt``/``gte``/``lt``/``lte``/``satisfies``/``order``), verified against the bundled npm
``semver`` reference.

Public surface (all snake_case identifiers; functions take/return plain ``str``/``bool``/``int``):

    gt(a, b) -> bool          order(a, b) == 1
    gte(a, b) -> bool         order(a, b) >= 0
    lt(a, b) -> bool          order(a, b) == -1
    lte(a, b) -> bool         order(a, b) <= 0
    satisfies(version, range) -> bool
    order(a, b) -> -1 | 0 | 1     (npm semver.compare, loose)

No wire-key dicts here — this operates on version strings only.

Faithful behavior notes (verified against ``node_modules/semver`` with ``{loose:true}``):
- Loose parse tolerates a leading ``v``/``=`` and whitespace; build metadata (``+...``) is parsed
  but ignored in comparison; prerelease (``-...``) participates in ordering.
- Prerelease ordering: a version *with* a prerelease is *less than* the same version without one;
  identifiers are compared left-to-right, numeric-vs-numeric numerically, numeric < non-numeric,
  and a shorter prerelease prefix is lower (``1.0.0-alpha < 1.0.0-alpha.1``).
- ``satisfies`` prerelease rule: a prerelease ``version`` only satisfies a comparator-set if some
  comparator in that set has the identical ``[major,minor,patch]`` tuple AND itself carries a
  prerelease tag. (This is the npm "no surprise prereleases" behavior.)

Only the subset of npm range grammar exercised by the tabvis tree is implemented (``||``,
AND-by-whitespace, ``^`` ``~`` ``>`` ``>=`` ``<`` ``<=`` ``=`` comparators, ``x``/``*`` wildcards,
``A - B`` hyphen ranges, empty range = ``*``). Exotic forms (e.g. ``1.2.3 - 2`` with partial bounds
beyond what's tested) follow the same npm rules but are only lightly covered.
"""

from __future__ import annotations

import re
from functools import cmp_to_key

# Loose semver parse: optional leading v/=, then a FULL M.m.p with optional -pre and +build.
# npm ``{loose:true}`` only tolerates a leading ``v``/``=`` and whitespace — a *concrete* version
# must still carry all three of major.minor.patch. ``compare('1', ...)`` / ``new SemVer('1')``
# throw "Invalid Version" under loose (verified against node_modules/semver). Partial versions are
# only valid inside *ranges* (handled separately by ``_parse_partial``), never as a SemVer.
_LOOSE_RE = re.compile(
    r"^[v=\s]*"
    r"(\d+)"  # major (required)
    r"\.(\d+)"  # minor (required — loose does NOT allow it missing)
    r"\.(\d+)"  # patch (required — loose does NOT allow it missing)
    r"(?:-([0-9A-Za-z.-]+))?"  # prerelease
    r"(?:\+([0-9A-Za-z.-]+))?"  # build (ignored in comparison)
    r"\s*$"
)


class _SemVer:
    """A parsed (loose) semantic version: major/minor/patch + prerelease identifiers."""

    __slots__ = ("major", "minor", "patch", "prerelease")

    def __init__(self, major: int, minor: int, patch: int, prerelease: list[str | int]) -> None:
        self.major = major
        self.minor = minor
        self.patch = patch
        self.prerelease = prerelease

    @property
    def tuple3(self) -> tuple[int, int, int]:
        return (self.major, self.minor, self.patch)


def _parse(version: str) -> _SemVer | None:
    """Loose-parse a version string into a :class:`_SemVer`, or ``None`` if unparseable."""
    if version is None:
        return None
    m = _LOOSE_RE.match(version)
    if not m:
        return None
    major = int(m.group(1))
    minor = int(m.group(2)) if m.group(2) is not None else 0
    patch = int(m.group(3)) if m.group(3) is not None else 0
    prerelease = _split_prerelease(m.group(4)) if m.group(4) else []
    return _SemVer(major, minor, patch, prerelease)


def _split_prerelease(pre: str) -> list[str | int]:
    """Split a prerelease string into dot-separated identifiers (numeric → int)."""
    parts: list[str | int] = []
    for ident in pre.split("."):
        if ident.isdigit():
            # Numeric identifier (npm strips leading zeros via Number()).
            parts.append(int(ident))
        else:
            parts.append(ident)
    return parts


def _compare_identifiers(a: str | int, b: str | int) -> int:
    """Compare two prerelease identifiers per npm rules (numeric < alphanumeric)."""
    a_num = isinstance(a, int)
    b_num = isinstance(b, int)
    if a_num and b_num:
        return -1 if a < b else (1 if a > b else 0)
    if a_num and not b_num:
        # Numeric identifiers always have lower precedence than non-numeric.
        return -1
    if not a_num and b_num:
        return 1
    # Both strings: lexical ASCII comparison.
    sa, sb = str(a), str(b)
    return -1 if sa < sb else (1 if sa > sb else 0)


def _compare_prerelease(a: list[str | int], b: list[str | int]) -> int:
    """Compare prerelease identifier lists. A non-empty list is < an empty list (release)."""
    # A version WITH a prerelease is lower than the same version WITHOUT one.
    if a and not b:
        return -1
    if not a and b:
        return 1
    if not a and not b:
        return 0
    i = 0
    while True:
        if i >= len(a) and i >= len(b):
            return 0
        if i >= len(a):  # a is a proper prefix of b → a is lower
            return -1
        if i >= len(b):  # b is a proper prefix of a → a is higher
            return 1
        cmp = _compare_identifiers(a[i], b[i])
        if cmp != 0:
            return cmp
        i += 1


def _compare(a: _SemVer, b: _SemVer) -> int:
    """Full version comparison (build metadata ignored), returning -1/0/1."""
    for x, y in ((a.major, b.major), (a.minor, b.minor), (a.patch, b.patch)):
        if x != y:
            return -1 if x < y else 1
    return _compare_prerelease(a.prerelease, b.prerelease)


def order(a: str, b: str) -> int:
    """npm ``semver.compare`` (loose): ``-1`` if ``a < b``, ``0`` if equal, ``1`` if ``a > b``.

    Raises:
        ValueError: if either version is not parseable (mirrors npm throwing on invalid input).
    """
    pa = _parse(a)
    pb = _parse(b)
    if pa is None:
        raise ValueError(f"Invalid Version: {a}")
    if pb is None:
        raise ValueError(f"Invalid Version: {b}")
    return _compare(pa, pb)


def gt(a: str, b: str) -> bool:
    """Whether ``a`` is greater than ``b`` (loose)."""
    return order(a, b) == 1


def gte(a: str, b: str) -> bool:
    """Whether ``a`` is greater than or equal to ``b`` (loose)."""
    return order(a, b) >= 0


def lt(a: str, b: str) -> bool:
    """Whether ``a`` is less than ``b`` (loose)."""
    return order(a, b) == -1


def lte(a: str, b: str) -> bool:
    """Whether ``a`` is less than or equal to ``b`` (loose)."""
    return order(a, b) <= 0


# --- Range / satisfies ------------------------------------------------------------------------
#
# npm range grammar (subset): a range is a set of comparator-sets joined by ``||`` (OR). Each
# comparator-set is a whitespace-joined list of comparators (AND). A comparator is an operator
# (``>`` ``>=`` ``<`` ``<=`` ``=``) plus a (possibly partial / x-wildcard) version. Sugar forms
# ``^`` / ``~`` / hyphen ranges / x-ranges expand to comparator-sets.


class _Comparator:
    """A single comparator: an operator plus a concrete :class:`_SemVer` bound (or ``*``)."""

    __slots__ = ("op", "ver", "any")

    def __init__(self, op: str, ver: _SemVer | None, any_: bool = False) -> None:
        self.op = op  # one of '<', '<=', '>', '>=', '='
        self.ver = ver
        self.any = any_  # True ⇒ matches everything (the '*' comparator)

    def test(self, v: _SemVer) -> bool:
        if self.any:
            return True
        assert self.ver is not None
        cmp = _compare(v, self.ver)
        if self.op == "=":
            return cmp == 0
        if self.op == ">":
            return cmp > 0
        if self.op == ">=":
            return cmp >= 0
        if self.op == "<":
            return cmp < 0
        if self.op == "<=":
            return cmp <= 0
        return False


_X = object()  # sentinel for an 'x'/'*'/missing version part


def _parse_partial(version: str) -> tuple[object, object, object, str | None]:
    """Parse a (possibly partial / x-wildcard) version into (major, minor, patch, prerelease).

    Each of major/minor/patch is either an ``int`` or the :data:`_X` wildcard sentinel.
    """
    v = version.strip()
    v = re.sub(r"^[v=]+", "", v).strip()
    if v == "" or v in ("*", "x", "X"):
        return (_X, _X, _X, None)
    m = re.match(
        r"^(\d+|x|X|\*)"
        r"(?:\.(\d+|x|X|\*))?"
        r"(?:\.(\d+|x|X|\*))?"
        r"(?:-([0-9A-Za-z.-]+))?"
        r"(?:\+[0-9A-Za-z.-]+)?$",
        v,
    )
    if not m:
        # Unparseable partial → treat as full wildcard (lenient, matches npm loose tolerance).
        return (_X, _X, _X, None)

    def part(g: str | None) -> object:
        if g is None or g in ("x", "X", "*"):
            return _X
        return int(g)

    return (part(m.group(1)), part(m.group(2)), part(m.group(3)), m.group(4))


def _make_ver(major: int, minor: int, patch: int, pre: str | None = None) -> _SemVer:
    return _SemVer(major, minor, patch, _split_prerelease(pre) if pre else [])


def _expand_caret(major: object, minor: object, patch: object, pre: str | None) -> list[_Comparator]:
    """Expand a ``^`` range into a ``>=lower <upper`` comparator pair (npm semantics)."""
    if major is _X:
        return [_Comparator("", None, any_=True)]
    maj = int(major)  # type: ignore[arg-type]
    if minor is _X:
        # ^1  →  >=1.0.0 <2.0.0
        return [
            _Comparator(">=", _make_ver(maj, 0, 0)),
            _Comparator("<", _make_ver(maj + 1, 0, 0)),
        ]
    mnr = int(minor)  # type: ignore[arg-type]
    if patch is _X:
        if maj == 0:
            # ^0.2  →  >=0.2.0 <0.3.0
            return [
                _Comparator(">=", _make_ver(0, mnr, 0)),
                _Comparator("<", _make_ver(0, mnr + 1, 0)),
            ]
        # ^1.2  →  >=1.2.0 <2.0.0
        return [
            _Comparator(">=", _make_ver(maj, mnr, 0)),
            _Comparator("<", _make_ver(maj + 1, 0, 0)),
        ]
    pat = int(patch)  # type: ignore[arg-type]
    lower = _make_ver(maj, mnr, pat, pre)
    if maj != 0:
        upper = _make_ver(maj + 1, 0, 0)
    elif mnr != 0:
        upper = _make_ver(0, mnr + 1, 0)
    else:
        upper = _make_ver(0, 0, pat + 1)
    return [_Comparator(">=", lower), _Comparator("<", upper)]


def _expand_tilde(major: object, minor: object, patch: object, pre: str | None) -> list[_Comparator]:
    """Expand a ``~`` range into a ``>=lower <upper`` comparator pair (npm semantics)."""
    if major is _X:
        return [_Comparator("", None, any_=True)]
    maj = int(major)  # type: ignore[arg-type]
    if minor is _X:
        # ~1  →  >=1.0.0 <2.0.0
        return [
            _Comparator(">=", _make_ver(maj, 0, 0)),
            _Comparator("<", _make_ver(maj + 1, 0, 0)),
        ]
    mnr = int(minor)  # type: ignore[arg-type]
    if patch is _X:
        # ~1.2  →  >=1.2.0 <1.3.0
        return [
            _Comparator(">=", _make_ver(maj, mnr, 0)),
            _Comparator("<", _make_ver(maj, mnr + 1, 0)),
        ]
    pat = int(patch)  # type: ignore[arg-type]
    # ~1.2.3  →  >=1.2.3 <1.3.0
    return [
        _Comparator(">=", _make_ver(maj, mnr, pat, pre)),
        _Comparator("<", _make_ver(maj, mnr + 1, 0)),
    ]


def _expand_xrange(
    op: str, major: object, minor: object, patch: object, pre: str | None
) -> list[_Comparator]:
    """Expand a plain (possibly x-wildcard) version with an operator into comparators."""
    if major is _X:
        # '*' / 'x' / '' → any (for '=', '>=', '<='); '<x'/'>x' are degenerate but rare.
        if op in ("", "=", ">=", "<="):
            return [_Comparator("", None, any_=True)]
        # '>x' matches nothing-above-everything; npm treats '>*' as '<0.0.0' (match none). Keep it
        # simple: an explicit-comparator with a wildcard version → match none.
        return [_Comparator("<", _make_ver(0, 0, 0))]

    maj = int(major)  # type: ignore[arg-type]

    if minor is _X:
        # e.g. '1' / '1.x'
        if op in ("", "="):
            return [
                _Comparator(">=", _make_ver(maj, 0, 0)),
                _Comparator("<", _make_ver(maj + 1, 0, 0)),
            ]
        if op == ">":  # >1.x → >=2.0.0
            return [_Comparator(">=", _make_ver(maj + 1, 0, 0))]
        if op == ">=":  # >=1.x → >=1.0.0
            return [_Comparator(">=", _make_ver(maj, 0, 0))]
        if op == "<":  # <1.x → <1.0.0
            return [_Comparator("<", _make_ver(maj, 0, 0))]
        if op == "<=":  # <=1.x → <2.0.0
            return [_Comparator("<", _make_ver(maj + 1, 0, 0))]

    mnr = int(minor)  # type: ignore[arg-type]

    if patch is _X:
        # e.g. '1.2' / '1.2.x'
        if op in ("", "="):
            return [
                _Comparator(">=", _make_ver(maj, mnr, 0)),
                _Comparator("<", _make_ver(maj, mnr + 1, 0)),
            ]
        if op == ">":  # >1.2.x → >=1.3.0
            return [_Comparator(">=", _make_ver(maj, mnr + 1, 0))]
        if op == ">=":  # >=1.2.x → >=1.2.0
            return [_Comparator(">=", _make_ver(maj, mnr, 0))]
        if op == "<":  # <1.2.x → <1.2.0
            return [_Comparator("<", _make_ver(maj, mnr, 0))]
        if op == "<=":  # <=1.2.x → <1.3.0
            return [_Comparator("<", _make_ver(maj, mnr + 1, 0))]

    pat = int(patch)  # type: ignore[arg-type]
    target_op = op if op else "="
    return [_Comparator(target_op, _make_ver(maj, mnr, pat, pre))]


def _parse_comparator(token: str) -> list[_Comparator]:
    """Parse a single range token (one whitespace-delimited piece) into comparators."""
    token = token.strip()
    if token == "" or token in ("*", "x", "X"):
        return [_Comparator("", None, any_=True)]
    if token.startswith("^"):
        maj, mnr, pat, pre = _parse_partial(token[1:])
        return _expand_caret(maj, mnr, pat, pre)
    if token.startswith("~>"):  # '~>' is treated like '~'
        maj, mnr, pat, pre = _parse_partial(token[2:])
        return _expand_tilde(maj, mnr, pat, pre)
    if token.startswith("~"):
        maj, mnr, pat, pre = _parse_partial(token[1:])
        return _expand_tilde(maj, mnr, pat, pre)
    m = re.match(r"^(>=|<=|>|<|=)?\s*(.*)$", token)
    op = m.group(1) or "" if m else ""
    rest = m.group(2) if m else token
    maj, mnr, pat, pre = _parse_partial(rest)
    return _expand_xrange(op, maj, mnr, pat, pre)


def _parse_hyphen(part: str) -> list[_Comparator] | None:
    """Parse an ``A - B`` hyphen range into comparators, or ``None`` if not a hyphen range."""
    m = re.match(r"^\s*(.+?)\s+-\s+(.+?)\s*$", part)
    if not m:
        return None
    lo_maj, lo_mnr, lo_pat, lo_pre = _parse_partial(m.group(1))
    hi_maj, hi_mnr, hi_pat, hi_pre = _parse_partial(m.group(2))

    comps: list[_Comparator] = []
    # Lower bound.
    if lo_maj is _X:
        pass  # no lower bound
    else:
        lo = _make_ver(
            int(lo_maj),  # type: ignore[arg-type]
            int(lo_mnr) if lo_mnr is not _X else 0,  # type: ignore[arg-type]
            int(lo_pat) if lo_pat is not _X else 0,  # type: ignore[arg-type]
            lo_pre,
        )
        comps.append(_Comparator(">=", lo))
    # Upper bound (partial high bound becomes a '<' on the next increment).
    if hi_maj is _X:
        pass  # no upper bound
    elif hi_mnr is _X:
        comps.append(_Comparator("<", _make_ver(int(hi_maj) + 1, 0, 0)))  # type: ignore[arg-type]
    elif hi_pat is _X:
        comps.append(
            _Comparator("<", _make_ver(int(hi_maj), int(hi_mnr) + 1, 0))  # type: ignore[arg-type]
        )
    else:
        comps.append(
            _Comparator(
                "<=",
                _make_ver(
                    int(hi_maj),  # type: ignore[arg-type]
                    int(hi_mnr),  # type: ignore[arg-type]
                    int(hi_pat),  # type: ignore[arg-type]
                    hi_pre,
                ),
            )
        )
    if not comps:
        return [_Comparator("", None, any_=True)]
    return comps


def _parse_comparator_set(part: str) -> list[_Comparator]:
    """Parse one comparator-set (the AND-group between ``||``)."""
    part = part.strip()
    if part == "":
        return [_Comparator("", None, any_=True)]
    hyphen = _parse_hyphen(part)
    if hyphen is not None:
        return hyphen
    comps: list[_Comparator] = []
    for token in re.split(r"\s+", part):
        if token == "":
            continue
        comps.extend(_parse_comparator(token))
    if not comps:
        return [_Comparator("", None, any_=True)]
    return comps


def _parse_range(range_str: str) -> list[list[_Comparator]]:
    """Parse a full range into a list of comparator-sets (OR of ANDs)."""
    if range_str.strip() == "":
        return [[_Comparator("", None, any_=True)]]
    sets: list[list[_Comparator]] = []
    for part in range_str.split("||"):
        sets.append(_parse_comparator_set(part))
    return sets


def _set_allows_prerelease(comp_set: list[_Comparator], v: _SemVer) -> bool:
    """npm prerelease rule: ``v`` (a prerelease) may only match a set that pins ``v``'s tuple3."""
    for c in comp_set:
        if c.any or c.ver is None:
            continue
        if c.ver.prerelease and c.ver.tuple3 == v.tuple3:
            return True
    return False


def satisfies(version: str, range: str) -> bool:
    """Whether ``version`` satisfies ``range`` (npm semver, loose). Returns ``False`` if invalid."""
    v = _parse(version)
    if v is None:
        return False
    try:
        sets = _parse_range(range)
    except Exception:  # noqa: BLE001 - malformed range → no match (npm throws; we mirror loosely)
        return False

    for comp_set in sets:
        # Every comparator in the set must pass (AND).
        if not all(c.test(v) for c in comp_set):
            continue
        # Prerelease guard: a prerelease version must be explicitly allowed by this set.
        if v.prerelease and not _set_allows_prerelease(comp_set, v):
            continue
        return True
    return False


# Stable sort helper exposed for parity (not in the TS surface, but handy + cheap to keep here).
def sort_versions(versions: list[str]) -> list[str]:
    """Sort version strings ascending by :func:`order` (invalid versions raise)."""
    return sorted(versions, key=cmp_to_key(order))
