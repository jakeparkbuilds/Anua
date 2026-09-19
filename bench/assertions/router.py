"""Route a capability claim to an assertion kind. Prefer oracle; fall through only when
no independent truth exists. Used by the generator to classify proposed tests, and as
a guard so nothing is silently downgraded."""
from __future__ import annotations
from ..models import Kind
from ..oracles.registry import has_oracle

DETERMINISTIC_PREFIXES = ("dns.", "tls.", "http.", "email.")


def route(claim: str, has_schema: bool = False) -> Kind:
    if has_oracle(claim):
        return Kind.ORACLE
    if claim.startswith("card.") or claim.startswith("drift.") or claim.startswith("endpoint."):
        return Kind.SCHEMA
    if has_schema:
        return Kind.SCHEMA
    if claim.startswith(DETERMINISTIC_PREFIXES):
        return Kind.CONSISTENCY
    return Kind.QUALITY
