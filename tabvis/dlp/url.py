"""URL scrubbing for DLP egress (design §11.2).

Removes the parts of a URL that can carry a secret before it leaves the trusted domain: userinfo
(``user:pass@``), the fragment, and query values except a narrow allowlist of public structural
parameters. Path is preserved. Anything unparseable returns a safe redaction rather than the
original string.

Dropping every query value made public data APIs unusable: a browser snapshot of a USGS request lost
``format=geojson`` and its time window, so the Agent could not verify which catalog it had read.
Search terms and unknown parameters remain redacted; only bounded dates, numeric pagination/
coordinates, and documented output/sort selectors survive.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_PUBLIC_QUERY_KEYS = frozenset(
    {
        "abstracts",
        "catalog",
        "contributor",
        "endtime",
        "eventid",
        "format",
        "includeallmagnitudes",
        "includeallorigins",
        "latitude",
        "limit",
        "maxdepth",
        "maxlatitude",
        "maxlongitude",
        "maxmagnitude",
        "maxradius",
        "maxradiuskm",
        "mindepth",
        "minlatitude",
        "minlongitude",
        "minmagnitude",
        "offset",
        "order",
        "orderby",
        "page",
        "producttype",
        "size",
        "sort",
        "start",
        "starttime",
    }
)
_ENUM_QUERY_VALUES = {
    "format": re.compile(r"(?:geojson|json|xml|text|csv|quakeml|kml|map|count)", re.IGNORECASE),
    "orderby": re.compile(r"(?:time|time-asc|magnitude|magnitude-asc)", re.IGNORECASE),
    "order": re.compile(r"(?:asc|ascending|desc|descending)", re.IGNORECASE),
    "sort": re.compile(r"(?:ascending|descending|relevance|submitted_date)", re.IGNORECASE),
    "includeallmagnitudes": re.compile(r"(?:true|false|0|1)", re.IGNORECASE),
    "includeallorigins": re.compile(r"(?:true|false|0|1)", re.IGNORECASE),
}
_TIME_QUERY_KEYS = frozenset({"starttime", "endtime"})
_INTEGER_QUERY_KEYS = frozenset({"limit", "offset", "page", "size", "start"})
_NUMBER_QUERY_KEYS = frozenset(
    {
        "latitude",
        "maxdepth",
        "maxlatitude",
        "maxlongitude",
        "maxmagnitude",
        "maxradius",
        "maxradiuskm",
        "mindepth",
        "minlatitude",
        "minlongitude",
        "minmagnitude",
    }
)
_IDENTIFIER_QUERY_KEYS = frozenset({"catalog", "contributor", "eventid", "producttype"})
_ISO_DATE_TIME = re.compile(r"\d{4}-\d{2}-\d{2}(?:[Tt][0-2]\d:[0-5]\d(?::[0-5]\d(?:\.\d{1,6})?)?(?:Z|[+-][0-2]\d:[0-5]\d)?)?")
_INTEGER = re.compile(r"\d{1,9}")
_NUMBER = re.compile(r"[+-]?(?:\d{1,6}(?:\.\d{1,8})?|\.\d{1,8})")
_PUBLIC_IDENTIFIER = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_ARXIV_DATE_KEY = re.compile(r"date-(date_type|filter_by|from|from_date|to|to_date|year)", re.IGNORECASE)
_ARXIV_TERM_KEY = re.compile(r"terms-\d+-(field|operator)", re.IGNORECASE)
_ARXIV_CLASSIFICATION_KEY = re.compile(r"classification-[a-z0-9_-]+", re.IGNORECASE)
_ARXIV_DATE_TYPE = re.compile(r"(?:submitted_date|last_updated_date)", re.IGNORECASE)
_ARXIV_DATE_FILTER = re.compile(r"(?:all_dates|past_12|specific_year|date_range)", re.IGNORECASE)
_ARXIV_FIELD = re.compile(
    r"(?:all|title|abstract|author|comment|journal_ref|acm_class|msc_class|"
    r"report_num|paper_id|doi|orcid|license)",
    re.IGNORECASE,
)
_ARXIV_OPERATOR = re.compile(r"(?:and|or|andnot)", re.IGNORECASE)
_ARXIV_CLASSIFICATION_VALUE = re.compile(r"(?:include|exclude|true|false|on|yes|no|0|1)", re.IGNORECASE)


def _clean_query_value(key: str, value: str) -> str:
    normalized = key.lower()
    date_match = _ARXIV_DATE_KEY.fullmatch(normalized)
    term_match = _ARXIV_TERM_KEY.fullmatch(normalized)
    classification_match = _ARXIV_CLASSIFICATION_KEY.fullmatch(normalized)
    if (
        normalized not in _PUBLIC_QUERY_KEYS
        and date_match is None
        and term_match is None
        and classification_match is None
    ):
        return ""
    enum_pattern = _ENUM_QUERY_VALUES.get(normalized)
    if enum_pattern is not None:
        return value if enum_pattern.fullmatch(value) is not None else ""
    if normalized in _TIME_QUERY_KEYS:
        return value if _ISO_DATE_TIME.fullmatch(value) is not None else ""
    if normalized in _INTEGER_QUERY_KEYS:
        return value if _INTEGER.fullmatch(value) is not None else ""
    if normalized in _NUMBER_QUERY_KEYS:
        return value if _NUMBER.fullmatch(value) is not None else ""
    if normalized in _IDENTIFIER_QUERY_KEYS:
        return value if _PUBLIC_IDENTIFIER.fullmatch(value) is not None else ""
    if date_match is not None:
        part = date_match.group(1).lower()
        if part == "date_type":
            valid = _ARXIV_DATE_TYPE.fullmatch(value)
        elif part == "filter_by":
            valid = _ARXIV_DATE_FILTER.fullmatch(value)
        elif part == "year":
            valid = re.fullmatch(r"\d{4}", value)
        else:
            valid = re.fullmatch(r"\d{4}(?:-\d{2}(?:-\d{2})?)?", value)
        return value if valid is not None else ""
    if term_match is not None:
        pattern = _ARXIV_FIELD if term_match.group(1).lower() == "field" else _ARXIV_OPERATOR
        return value if pattern.fullmatch(value) is not None else ""
    if classification_match is not None:
        return value if _ARXIV_CLASSIFICATION_VALUE.fullmatch(value) is not None else ""
    # ``abstracts`` is a boolean display toggle on arXiv.
    if normalized == "abstracts":
        return value if re.fullmatch(r"(?:show|hide|true|false|0|1)", value, re.IGNORECASE) else ""
    return ""


def clean_url(url: str) -> str:
    """Strip credentials/fragment and retain only bounded public query parameters."""
    if not url:
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        return "[redacted-url]"
    # rebuild netloc without userinfo
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    # Keep query keys for audit structure. Values survive only for the narrow public allowlist.
    if parts.query:
        pairs = [
            (key, _clean_query_value(key, value))
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
        ]
        query = urlencode(pairs)
    else:
        query = ""
    return urlunsplit((parts.scheme, host, parts.path, query, ""))  # fragment dropped
