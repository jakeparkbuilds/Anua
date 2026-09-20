"""Comparators. Return (passed, evidence).

Rule: never guess. A value we cannot interpret is reported as such and fails — it is
never coerced into a verdict. Silently reading "certificate validation failed" as True
would hand the agent a pass (or a fail) it did not earn, which is the one thing this
benchmark cannot afford.
"""
from __future__ import annotations
import re
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


class Ungradable(Exception):
    """The comparison cannot be made honestly — OUR limitation (an oracle value the
    comparator cannot use, a value we could not interpret, an unknown comparator). The
    runner turns this into SKIPPED. It is never a verdict about the agent."""


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


def _name(v: Any) -> str:
    """Canonical form of a hostname-like set member. A trailing dot is DNS FQDN notation,
    not a different name: an agent answering ['ns1.example.net.'] against an oracle that
    says ['ns1.example.net'] was being scored zero overlap, a HIGH failure it never made."""
    return str(v).strip().rstrip(".").lower()


def _members(v: Any) -> list:
    """A set-comparator operand as a list. A scalar string is ONE member (or a few, if the
    agent wrote a comma-separated list) — never a sequence of characters, which is what
    set(map(str, "ns1.example.com")) silently was."""
    if v is None:
        return []
    if isinstance(v, str):
        import re
        return [x for x in re.split(r"[,\s;]+", v.strip()) if x]
    if isinstance(v, (list, tuple, set, frozenset)):
        return list(v)
    if isinstance(v, dict):
        return list(v.keys())
    return [v]


_CORP = frozenset({"inc", "inc.", "llc", "ltd", "ltd.", "limited", "corp", "corp.", "co", "co.", "gmbh",
                   "sa", "s.a.", "the", "of", "and", "&", "ca", "authority", "certificate", "trust"})


def _contains(needle: str, hay: str) -> bool:
    """Oracle string inside the agent's string, tolerant of what does not change meaning:
    a URL's trailing slash, corporate suffixes ('MarkMonitor Inc.' vs 'MarkMonitor'),
    and the agent giving a shorter but still specific form. Never a one-token match on a
    stop word."""
    if needle.startswith(("http://", "https://")):
        return needle.rstrip("/") in hay.rstrip("/")
    if needle in hay:
        return True
    if len(hay) >= 4 and hay in needle:
        return True                                          # agent said the shorter, specific form
    toks = [t for t in re.split(r"[\s,]+", needle) if t and t not in _CORP]
    return bool(toks) and all(t in hay for t in toks)


def _lang(v: Any) -> str:
    return str(v or "").strip().lower().replace("_", "-").split("-")[0]


_DATE_FORMATS = ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d", "%d %b %Y", "%b %d %Y", "%b %d, %Y", "%B %d, %Y",
                 "%d %B %Y", "%b %d %H:%M:%S %Y", "%b %d %H:%M:%S %Y %Z", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y")


def _date(v: Any) -> date | None:
    """Parse the common ways a certificate or registration date gets written."""
    if isinstance(v, date):
        return v
    if v is None:
        return None
    t = " ".join(str(v).strip().split())
    try:
        return date.fromisoformat(t[:10])
    except ValueError:
        pass
    from datetime import datetime
    for f in _DATE_FORMATS:
        try:
            return datetime.strptime(t, f).date()
        except ValueError:
            continue
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", t)
    return date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


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
        return False, "agent's answer does not state a value for this (nothing to grade against the oracle)"
    if comparator == "bool":
        e, a = _b(expected), _b(actual)
        if e is UNKNOWN or e is None:
            raise Ungradable(f"oracle value {expected!r} is not a usable boolean")
        if a is UNKNOWN:
            raise Ungradable(f"could not interpret the agent's value {str(actual)[:60]!r} as true/false")
        return e == a, f"expected {e}, agent said {a}"
    if comparator == "eq":
        return _eq(expected, actual), f"expected {expected!r}, agent said {actual!r}"
    if comparator == "set_eq":
        e, a = set(map(_name, _members(expected))), set(map(_name, _members(actual)))
        return e == a, f"expected {sorted(e)}, agent said {sorted(a)}"
    if comparator == "set_overlap":
        # Not "any one element matches": at least one match AND at least half of what the
        # agent listed must be real. Three nameservers with one right one is not a pass.
        e, a = set(map(_name, _members(expected))), set(map(_name, _members(actual)))
        both = e & a
        if not e:
            return (not a), f"expected none, agent said {sorted(a)}"
        ok = bool(both) and 2 * len(both) >= len(a)
        return ok, (f"{len(both)} of the agent's {len(a)} value(s) are in the oracle's {len(e)}: "
                    f"agent {sorted(a)} vs oracle {sorted(e)}" + ("" if ok else " (need ≥1 match and ≥½ of the agent's list correct)"))
    if comparator == "contains":
        needle = str(expected).strip().lower()
        if not needle:
            raise Ungradable("oracle value is empty: 'contains' cannot grade absence (use a *_present oracle)")
        return _contains(needle, str(actual).strip().lower()), f"looked for {expected!r} in {str(actual)[:80]!r}"
    if comparator == "one_of":
        opts = _members(expected)
        return any(_eq(o, actual) for o in opts), f"expected one of {opts!r}, agent said {actual!r}"
    if comparator == "lang_eq":
        e, a = _lang(expected), _lang(actual)
        if not e:
            raise Ungradable("oracle found no <html lang>; cannot grade absence with lang_eq")
        return e == a, f"expected language {e!r}, agent said {a!r}"
    if comparator == "date_close":
        e, a = _date(expected), _date(actual)
        if e is None:
            raise Ungradable(f"oracle date {expected!r} is unparseable")
        if a is None:
            raise Ungradable(f"the agent's date {str(actual)[:40]!r} is in a format we could not parse")
        return abs((e - a).days) <= 1, f"expected {e}, agent said {a}"
    if comparator == "nonempty":
        return bool(actual), f"{'non-empty' if actual else 'EMPTY'} response"
    raise Ungradable(f"unknown comparator {comparator}")
