"""Oracle packs: each pack is a set of claims with an independently computable truth.

A pack module exports SPECS: dict[claim_key, OracleSpec]. The merged registry
(bench/oracles/registry.py) exposes ORACLES (key -> callable, unchanged contract) and
SPECS (key -> OracleSpec) so the generator can match an agent's prose claims to keys.

Two rules for every oracle, unchanged from the network pack:
  * raise OracleUnavailable when truth cannot be computed — never return None, never a
    default. Our network problem is not the agent's competence problem.
  * return a real bool / int / str / list — never prose the comparator would have to
    guess at.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass(frozen=True)
class OracleSpec:
    key: str                                  # e.g. "web.h1_count"
    fn: Callable[[str], Any]                  # target -> truth
    description: str                          # one line, machine-matchable
    input: str = "domain"                     # "domain" | "url"
    comparator: str = "bool"                  # default comparator for generated tests
    returns: str = "bool"                     # bool | int | str | list | date
    negatives: tuple[str, ...] = field(default_factory=tuple)  # known-bad targets
    pack: str = ""


def spec(key, fn, description, *, input="domain", comparator="bool", returns="bool",
         negatives=(), pack="") -> OracleSpec:
    return OracleSpec(key, fn, description, input, comparator, returns, tuple(negatives), pack)
