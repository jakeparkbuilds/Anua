"""Comparators. Return (passed, evidence).

Rule: never guess. A value we cannot interpret is reported as such and fails — it is
never coerced into a verdict. Silently reading "certificate validation failed" as True
would hand the agent a pass (or a fail) it did not earn, which is the one thing this
benchmark cannot afford.
"""
from __future__ import annotations
from datetime import date
from typing import Any

# Curated vocabulary. Anything outside it is UNKNOWN, not True.
_TRUE_WORDS = frozenset((
    "true", "yes", "y", "valid", "ok", "okay", "enabled", "present", "pass", "passed",
    "success", "successful", "reachable", "trusted", "signed", "found", "1", "on",
))
_FALSE_WORDS = frozenset((
    "false", "no", "n", "invalid", "disabled", "absent", "missing", "none", "null",
    "fail", "failed", "failure", "error", "unreachable", "untrusted", "unsigned",
    "not found", "not present", "not valid", "expired", "0", "off",
))

UNKNOWN = object()   # sentinel: could not be interpreted as a boolean


def _b(v: Any) -> Any:
    """Coerce to bool, or UNKNOWN. Never falls back to Python truthiness for strings."""
    if isinstance(v, bool):
        return v
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip().strip(".,;:!?\"'").lower()
        if s in _TRUE_WORDS:
            return True
        if s in _FALSE_WORDS:
            return False
        return UNKNOWN
    if isinstance(v, int) and not isinstance(v, bool):
        # 0/1 are unambiguous; any other number (200, 443) is not a boolean.
        return bool(v) if v in (0, 1) else UNKNOWN
    if isinstance(v, (list, tuple, set, dict)):
        # Empty collection = absent, non-empty = present. Well-defined for record lookups.
        return bool(v)
    return UNKNOWN


def _num(v: Any) -> Any:
    """Numeric value of v, or UNKNOWN. Lets 200 and "200" compare equal."""
    if isinstance(v, bool):
        return UNKNOWN
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        try:
            return int(v.strip())
        except ValueError:
            try:
                return float(v.strip())
            except ValueError:
                return UNKNOWN
    return UNKNOWN


def _eq(expected: Any, actual: Any) -> bool:
    """Equality that tolerates representation differences but not type confusion."""
    if expected == actual:
        return True
    e_num, a_num = _num(expected), _num(actual)
    if e_num is not UNKNOWN and a_num is not UNKNOWN:
        return e_num == a_num
    if isinstance(expected, str) and isinstance(actual, str):
        return expected.strip().lower() == actual.strip().lower()
    return False


def compare(comparator: str, expected: Any, actual: Any) -> tuple[bool, str]:
    if actual is None:
        return False, "agent response did not contain a value for this claim (extraction failed)"
    if comparator == "bool":
        e, a = _b(expected), _b(actual)
        if a is UNKNOWN:
            return False, (f"could not interpret {actual!r} as true/false — "
                           f"extraction returned an unrecognised value (expected {_b(expected)})")
        if e is UNKNOWN or e is None:
            return False, f"oracle value {expected!r} is not a usable boolean"
        return e == a, f"expected {e}, agent said {a}"
    if comparator == "eq":
        return _eq(expected, actual), f"expected {expected!r}, agent said {actual!r}"
    if comparator == "set_eq":
        e, a = set(map(str, expected or [])), set(map(str, actual or []))
        return e == a, f"expected {sorted(e)}, agent said {sorted(a)}"
    if comparator == "set_overlap":
        e, a = set(map(str, expected or [])), set(map(str, actual or []))
        ok = bool(e & a) if e else (not a)
        return ok, f"overlap {sorted(e & a)} of expected {sorted(e)} / agent {sorted(a)}"
    if comparator == "contains":
        return str(expected).lower() in str(actual).lower(), f"looked for {expected!r} in {str(actual)[:80]!r}"
    if comparator == "date_close":
        try:
            e, a = date.fromisoformat(str(expected)[:10]), date.fromisoformat(str(actual)[:10])
            return abs((e - a).days) <= 1, f"expected {e}, agent said {a}"
        except ValueError:
            return False, f"unparseable date: expected {expected!r}, actual {actual!r}"
    if comparator == "nonempty":
        return bool(actual), f"{'non-empty' if actual else 'EMPTY'} response"
    return False, f"unknown comparator {comparator}"
