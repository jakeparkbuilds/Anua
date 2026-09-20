"""Assemble the Report and compute behavior_score.

behavior_score = 0.70 * oracle + 0.25 * selfclaim + 0.20 * schema + 0.10 * quality,
normalised over whichever pools actually produced a graded result. Quality contributes
only to this blend — it never flips a verdict. HIGH-severity failures are listed first.

Scoring policy (renormalised blend):
  * A pool (capability = oracle+consistency, self-claim, schema, quality) is PRESENT when
    it has at least one graded assertion (passed is not None; quality is present when it
    ran). Absent pools are excluded from the denominator — never counted as zero.
  * score = 100 * Σ(weight_p * rate_p) / Σ(weight_p) over PRESENT pools only.
  * A score is emitted when MIN_GRADED (3) or more assertions were graded in total. Below
    that the run is genuinely empty: score_status INSUFFICIENT_COVERAGE, score None.
  * score_confidence says what the number rests on: `high` when graded capability tests
    outnumber everything else, `medium` when it rests mostly on self-claims and schema,
    `low` when only three or four assertions were graded, `none` when there is no score.
The coverage figures beside the score carry the caveat; the score itself is always the
same arithmetic, and scripts/recompute.py redoes it by hand from the raw assertions.
"""
from __future__ import annotations
from typing import Any
from ..models import Report, Assertion, Kind, AgentIdentity, AgentCards, QualityScores, Severity, CoverageReport

_SEV_ORDER = {Severity.HIGH: 0, Severity.MEDIUM: 1, Severity.LOW: 2, Severity.INFO: 3}
MIN_GRADED = 3        # fewer graded assertions than this and there is no score at all


def _rate(items: list[Assertion]) -> float | None:
    graded = [a for a in items if a.passed is not None]
    return round(sum(1 for a in graded if a.passed) / len(graded), 3) if graded else None


def _graded(items: list[Assertion]) -> tuple[int, int]:
    g = [a for a in items if a.passed is not None]
    return sum(1 for a in g if a.passed), len(g)


def _why_no_behaviour(assertions: list[Assertion], coverage: CoverageReport | None) -> str:
    """Name the actual cause, so the reader knows whether to blame the agent or us."""
    behaviour = [a for a in assertions if a.kind in (Kind.ORACLE, Kind.CONSISTENCY)]
    refused = [a for a in behaviour if a.error and "agent unavailable" in a.error]
    if refused:
        why = refused[0].error.split("agent unavailable: ", 1)[-1].split(" (x402")[0]
        return (f"the agent refused to serve us ({why}), so none of the {len(behaviour)} "
                f"capability test(s) written for it could be graded")
    if behaviour:
        return (f"all {len(behaviour)} capability test(s) were left ungraded because ground "
                f"truth or the agent's answer was unavailable")
    if coverage and coverage.claims_found and not coverage.capability_tests:
        if coverage.capability_claims == 0:
            return ("every claim this agent makes is about its own infrastructure; it "
                    "declares no capability we can exercise")
        return (f"none of the {coverage.capability_claims} capability claim(s) this agent "
                f"makes maps onto an oracle — {coverage.unverifiable} of "
                f"{coverage.claims_found} declared claims are unverifiable by anyone")
    if coverage and not coverage.claims_found:
        return "no claims could be read from the agent's self-descriptions"
    return "no capability tests were run"


def build(agent_label: str, host: str, mode: str, identity: AgentIdentity, cards: AgentCards,
          assertions: list[Assertion], weights: dict[str, float], notes: list[str],
          coverage: CoverageReport | None = None, suite: str = "regression") -> Report:
    by_kind = {k: [a for a in assertions if a.kind == k] for k in Kind}
    behaviour_items = by_kind[Kind.ORACLE] + by_kind[Kind.CONSISTENCY]
    oracle_rate = _rate(behaviour_items)
    self_rate = _rate(by_kind[Kind.SELFCLAIM])
    schema_rate = _rate(by_kind[Kind.SCHEMA])
    q_items = by_kind[Kind.QUALITY]
    quality = QualityScores(**q_items[0].actual) if q_items and isinstance(q_items[0].actual, dict) else QualityScores()
    q_score = (q_items[0].score / 100.0) if q_items else 0.0

    w = weights
    parts = []
    if oracle_rate is not None: parts.append((w.get("oracle", .7), oracle_rate))
    if self_rate is not None: parts.append((w.get("selfclaim", .25), self_rate))
    if schema_rate is not None: parts.append((w.get("schema", .2), schema_rate))
    if q_items:
        # Only when a quality assertion actually ran. Counting an absent quality item as 0
        # capped every agent without one at 87.5 for nothing.
        parts.append((w.get("quality", .1), q_score))

    p_beh, n_beh = _graded(behaviour_items)
    p_self, n_self = _graded(by_kind[Kind.SELFCLAIM])
    p_sch, n_sch = _graded(by_kind[Kind.SCHEMA])
    n_graded = n_beh + n_self + n_sch          # quality never has a verdict, so it is not "graded"
    pools_txt = ((f"{p_beh}/{n_beh} capability" if n_beh else "no graded capability test")
                 + (f", {p_self}/{n_self} self-claim" if n_self else "")
                 + (f", {p_sch}/{n_sch} schema" if n_sch else "")
                 + (f", quality {q_score:.2f}" if q_items else ""))
    if n_graded < MIN_GRADED:
        behavior, score_status = None, "INSUFFICIENT_COVERAGE"
        score_basis = (f"only {n_graded} assertion(s) graded (fewer than {MIN_GRADED}): "
                       + _why_no_behaviour(assertions, coverage))
        if n_self:
            score_basis += (f". Its {n_self} self-claim(s) about its own infrastructure were "
                            f"checked directly and {p_self} held")
    else:
        # Renormalised: Σ(w·rate) / Σ(w) over the pools that are PRESENT (see module doc).
        # A pool with nothing graded is simply not in either sum.
        num = sum(wt * v for wt, v in parts)
        den = sum(wt for wt, _ in parts)
        behavior = round(100 * num / den)
        score_status = "OK"
        weights_txt = " + ".join(f"{wt:.2f}×{v:.3f}" for wt, v in parts)
        score_basis = (f"{pools_txt}; score = 100 × ({weights_txt}) / {den:.2f}")
        if n_beh == 0:
            score_basis += "; " + _why_no_behaviour(assertions, coverage)

    # What the number rests on. 83 off 30 capability tests and 83 off four checks of the
    # agent's own DNS are not the same claim, and the report says which it is.
    if behavior is None:
        confidence = "none"
    elif n_graded <= 4:
        confidence = "low"
    elif n_beh > n_graded - n_beh:
        confidence = "high"
    else:
        confidence = "medium"
    if behavior is not None:
        score_basis += (f"; evidence: {n_graded} graded assertion(s), {n_beh} of them capability tests"
                        f" — {confidence} confidence")

    failures = sorted(
        [a for a in assertions if a.passed is False],
        key=lambda a: (_SEV_ORDER[a.severity], a.id))
    failure_rows: list[dict[str, Any]] = [{
        "id": a.id, "kind": a.kind.value, "claim": a.claim, "input": a.input,
        "expected": a.expected, "actual": a.actual, "severity": a.severity.value,
        "oracle": a.oracle, "evidence": a.evidence, "generated": a.generated, "error": a.error,
    } for a in failures]

    high = sum(1 for a in failures if a.severity == Severity.HIGH)
    status = "VERIFIED" if identity.verified else identity.status
    ident_word = {"VERIFIED": "VERIFIED", "PENDING": "validation PENDING", "MISMATCH": "fingerprint MISMATCH",
                  "ROTATED": "cert ROTATED since the ANS seal (attestation stale)",
                  "NOT_FOUND": "NOT FOUND"}.get(status, "NOT verified")
    expl = ((f"{p_beh}/{n_beh} capability assertions passed" if n_beh
             else ("NO capability assertions graded — behavior score withheld" if behavior is None
                   else "NO capability assertions graded — score rests on self-claims and schema"))
            + (f"; {p_self}/{n_self} self-claim(s) verified directly" if n_self else "")
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
            "oracle_pass_rate": oracle_rate, "selfclaim_pass_rate": self_rate,
            "schema_pass_rate": schema_rate,
            "capability_assertions_graded": n_beh, "selfclaim_assertions_graded": n_self,
            "graded_total": n_graded,
            "score_confidence": confidence, "evidence_n": n_beh,
            "skipped": sum(1 for a in assertions if a.passed is None and a.kind != Kind.QUALITY),
            "quality_blend": round(q_score, 3), "high_severity_failures": high,
            "generated_count": len([a for a in assertions if a.generated]),
            "coverage_ratio": coverage.coverage_ratio if coverage else None,
            "claims_found": coverage.claims_found if coverage else None,
            "unverifiable_claims": coverage.unverifiable if coverage else None,
            "identity_status": status,
            "score_status": score_status,
            "notes": notes,
        },
        behavior_score=behavior, score_status=score_status, score_basis=score_basis,
        failures=failure_rows, explanation=expl,
    )
