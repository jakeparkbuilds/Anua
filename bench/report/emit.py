"""Optional: POST observations into agent-trust-discovery's import contract so the
`behavior` dimension moves off 0.

HUMAN: the exact observation schema is defined by the signals you register on the Go side
(docs/extending-signal-sources.md). The payload below is a reasonable guess for three
custom signals; adjust field names once you've written the Go `port.Signal` impls:
   capabilityaccuracy, capabilitycoverage, responseintegrity
"""
from __future__ import annotations
from datetime import datetime, timezone
import httpx
from ..models import Report


def observations(report: Report) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    s = report.summary
    return {
        "observations": [
            {"agentId": report.identity.ans_id, "signal": "capabilityaccuracy", "observedAt": now,
             "value": {"passed": int(round((s.get("oracle_pass_rate") or 0) * s["by_kind"].get("oracle", 0))),
                       "total": s["by_kind"].get("oracle", 0)},
             "explanation": report.explanation},
            {"agentId": report.identity.ans_id, "signal": "capabilitycoverage", "observedAt": now,
             "value": {"declared": len(report.cards.declared_skills),
                       "drift": any(f["claim"] == "drift.card_vs_trust" for f in report.failures)},
             "explanation": "declared skills vs trust-card functions"},
            {"agentId": report.identity.ans_id, "signal": "responseintegrity", "observedAt": now,
             "value": {"schemaPassRate": s.get("schema_pass_rate")},
             "explanation": "reachability, protocol declaration, card consistency"},
        ]
    }


def emit(report: Report, url: str, admin_key: str = "", timeout: int = 15) -> tuple[int, str]:
    headers = {"Content-Type": "application/json"}
    if admin_key:
        headers["X-Admin-Key"] = admin_key   # HUMAN: confirm header name in runtime.yaml docs
    with httpx.Client(timeout=timeout) as c:
        r = c.post(url, json=observations(report), headers=headers)
    return r.status_code, r.text[:500]
