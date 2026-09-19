"""Assemble the Report and compute behavior_score.

behavior_score = 0.70 * oracle_pass_rate + 0.20 * schema_pass_rate + 0.10 * quality
(weights from config). Quality contributes only to this blend — it never flips a verdict.
HIGH-severity oracle failures are listed first in `failures`.
"""
from __future__ import annotations
from typing import Any
from ..models import Report, Assertion, Kind, AgentIdentity, AgentCards, QualityScores, Severity, CoverageReport

_SEV_ORDER = {Severity.HIGH: 0, Severity.MEDIUM: 1, Severity.LOW: 2, Severity.INFO: 3}


def _rate(items: list[Assertion]) -> float | None:
    graded = [a for a in items if a.passed is not None]
    return round(sum(1 for a in graded if a.passed) / len(graded), 3) if graded else None


def build(agent_label: str, host: str, mode: str, identity: AgentIdentity, cards: AgentCards,
          assertions: list[Assertion], weights: dict[str, float], notes: list[str],
          coverage: CoverageReport | None = None, suite: str = "regression") -> Report:
    by_kind = {k: [a for a in assertions if a.kind == k] for k in Kind}
    oracle_rate = _rate(by_kind[Kind.ORACLE] + by_kind[Kind.CONSISTENCY])
    schema_rate = _rate(by_kind[Kind.SCHEMA])
    q_items = by_kind[Kind.QUALITY]
    quality = QualityScores(**q_items[0].actual) if q_items and isinstance(q_items[0].actual, dict) else QualityScores()
    q_score = (q_items[0].score / 100.0) if q_items else 0.0

    w = weights
    parts = []
    if oracle_rate is not None: parts.append((w.get("oracle", .7), oracle_rate))
    if schema_rate is not None: parts.append((w.get("schema", .2), schema_rate))
    parts.append((w.get("quality", .1), q_score))
    behavior = round(100 * sum(wt * v for wt, v in parts) / sum(wt for wt, _ in parts)) if parts else 0

    failures = sorted(
        [a for a in assertions if a.passed is False],
        key=lambda a: (_SEV_ORDER[a.severity], a.id))
    failure_rows: list[dict[str, Any]] = [{
        "id": a.id, "kind": a.kind.value, "claim": a.claim, "input": a.input,
        "expected": a.expected, "actual": a.actual, "severity": a.severity.value,
        "oracle": a.oracle, "evidence": a.evidence, "generated": a.generated, "error": a.error,
    } for a in failures]

    high = sum(1 for a in failures if a.severity == Severity.HIGH)
    n_or = len([a for a in by_kind[Kind.ORACLE] if a.passed is not None])
    p_or = sum(1 for a in by_kind[Kind.ORACLE] if a.passed)
    status = "VERIFIED" if identity.verified else identity.status
    ident_word = {"VERIFIED": "VERIFIED", "PENDING": "validation PENDING", "MISMATCH": "fingerprint MISMATCH",
                  "NOT_FOUND": "NOT FOUND"}.get(status, "NOT verified")
    expl = (f"{p_or}/{n_or} oracle assertions passed"
            + (f"; {high} HIGH-severity failure(s)" if high else "")
            + f"; identity {ident_word} via ANS"
            + (f"; {coverage.verifiable}/{coverage.claims_found} declared claims verifiable"
               + (f" ({coverage.unverifiable} unverifiable)" if coverage.unverifiable else "") if coverage else "")
            + (f"; {len([a for a in assertions if a.generated])} generated test(s)" if any(a.generated for a in assertions) else "")
            + ".")

    return Report(
        agent=identity.ans_name or agent_label, target_host=host, mode=mode,
        identity=identity, cards=cards, assertions=assertions, quality=quality,
        coverage=coverage, suite=suite,
        summary={
            "assertions_run": len(assertions),
            "by_kind": {k.value: len(v) for k, v in by_kind.items()},
            "oracle_pass_rate": oracle_rate, "schema_pass_rate": schema_rate,
            "quality_blend": round(q_score, 3), "high_severity_failures": high,
            "generated_count": len([a for a in assertions if a.generated]),
            "coverage_ratio": coverage.coverage_ratio if coverage else None,
            "claims_found": coverage.claims_found if coverage else None,
            "unverifiable_claims": coverage.unverifiable if coverage else None,
            "identity_status": status,
            "notes": notes,
        },
        behavior_score=behavior, failures=failure_rows, explanation=expl,
    )
