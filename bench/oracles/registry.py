"""ORACLES: claim -> function(target) -> ground truth, merged from the domain packs in
bench/oracles/packs/. SPECS carries the description / input kind / default comparator the
generator uses to match an agent's prose claims to keys.

Adding a claim = one `spec(...)` entry in a pack. Results are cached per (claim, target)
for the run."""
from __future__ import annotations
from functools import lru_cache
from typing import Any, Callable
from .errors import OracleUnavailable  # re-export
from .packs import OracleSpec
from .packs import network as _network, web as _web, whois as _whois

PACKS: dict[str, dict[str, OracleSpec]] = {
    "network": _network.SPECS,
    "web": _web.SPECS,
    "whois": _whois.SPECS,
}

SPECS: dict[str, OracleSpec] = {k: s for pack in PACKS.values() for k, s in pack.items()}
ORACLES: dict[str, Callable[[str], Any]] = {k: s.fn for k, s in SPECS.items()}


@lru_cache(maxsize=1024)
def compute(claim: str, target: str) -> Any:
    if claim not in ORACLES:
        raise KeyError(f"no oracle for claim {claim!r}")
    return ORACLES[claim](target)


def has_oracle(claim: str) -> bool:
    return claim in ORACLES


def catalog() -> list[dict[str, Any]]:
    """Machine-readable oracle list for the generator prompt."""
    return [{"key": s.key, "pack": s.pack, "input": s.input, "returns": s.returns,
             "comparator": s.comparator, "description": s.description,
             "known_negative_targets": list(s.negatives)} for s in SPECS.values()]
