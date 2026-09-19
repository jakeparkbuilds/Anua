"""Comparators. Return (passed, evidence)."""
from __future__ import annotations
from datetime import date
from typing import Any


def _b(v: Any):
    if isinstance(v, bool): return v
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "yes", "valid", "ok", "enabled", "present"): return True
        if s in ("false", "no", "invalid", "disabled", "absent", "missing", "none"): return False
    if v is None: return None
    return bool(v)


def compare(comparator: str, expected: Any, actual: Any) -> tuple[bool, str]:
    if actual is None:
        return False, "agent response did not contain a value for this claim (extraction failed)"
    if comparator == "bool":
        e, a = _b(expected), _b(actual)
        return e == a, f"expected {e}, agent said {a}"
    if comparator == "eq":
        return expected == actual, f"expected {expected!r}, agent said {actual!r}"
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
