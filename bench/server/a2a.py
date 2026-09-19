"""The A2A endpoint. This is what makes the benchmarker a PARTICIPANT in ANS rather
than an observer: https://anuabot.vip/a2a is the endpoint we registered, and any agent
can send it the same JSON-RPC envelope our own A2ATransport sends.

One turn, no streaming: parse a host or ans:// name from the text, run (or replay) the
benchmark, answer with a readable text part.
"""
from __future__ import annotations
import uuid
from typing import Any
from ..models import Report
from .runs import RunStore, parse_agent
from . import cards

_PARSE_ERROR, _INVALID_REQUEST, _METHOD_NOT_FOUND, _INVALID_PARAMS, _INTERNAL = -32700, -32600, -32601, -32602, -32603


def _text_of(params: dict) -> str:
    msg = params.get("message") or {}
    parts = msg.get("parts") or []
    return "\n".join(str(p.get("text", "")) for p in parts if isinstance(p, dict) and p.get("text"))


def _err(id_: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def summary_text(r: Report, cached: bool, public_url: str) -> str:
    ident = r.identity
    when = r.run_at.strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"{cards.NAME} — {r.target_host} (run {when}{', cached' if cached else ''})", ""]
    why = {"VERIFIED": "registry hit, transparency-log entry, live TLS fingerprint = sealed",
           "PENDING": "transparency-log entry present, certificate not yet sealed",
           "MISMATCH": "live TLS fingerprint differs from the sealed one — DRIFT",
           "NOT_FOUND": "no registry hit and no transparency-log entry"}.get(ident.status, "; ".join(ident.notes)[:120])
    lines.append(f"IDENTITY  {ident.status}  {ident.ans_name or ''}  — {why}")
    if r.score_status == "OK":
        lines.append(f"BEHAVIOR  {r.behavior_score}/100  — {r.score_basis}")
    else:
        lines.append(f"BEHAVIOR  {r.score_status}  — {r.score_basis}")
    if r.coverage:
        c = r.coverage
        lines.append(f"COVERAGE  {c.verifiable}/{c.claims_found} declared claims verifiable ({c.coverage_ratio:.0%}); "
                     f"{c.unverifiable} unverifiable, {c.schema_only} schema-only; "
                     f"{c.capability_tests} capability tests + {c.selfclaim_tests} self-claim checks")
    else:
        lines.append(f"COVERAGE  regression suite ({len(r.assertions)} hand-written tests) — declared claims not read")
    if r.failures:
        lines += ["", "TOP FINDINGS"]
        for f in r.failures[:5]:
            tgt = next(iter((f.get("input") or {}).values()), "")
            if f.get("error"):                      # a refusal or runner error: the evidence says what happened
                lines.append(f"  {f['severity']:<6} {f['oracle'] or f['claim']} — {str(f.get('evidence'))[:110]}")
                continue
            said = ("its answer contained no value for this" if f.get("actual") is None
                    else f"agent said {str(f['actual'])[:60]!r}")
            lines.append(f"  {f['severity']:<6} {f['oracle'] or f['claim']} @ {tgt} — expected {f['expected']!r}, {said}")
        if len(r.failures) > 5:
            lines.append(f"  … {len(r.failures) - 5} more")
    else:
        lines += ["", "No failed assertions."]
    lines += ["", f"Full report: {public_url}/api/benchmark/{r.target_host}"]
    return "\n".join(lines)


def handle(body: Any, store: RunStore, public_url: str) -> dict:
    if not isinstance(body, dict):
        return _err(None, _INVALID_REQUEST, "expected a JSON-RPC 2.0 object")
    id_ = body.get("id")
    if body.get("jsonrpc") != "2.0":
        return _err(id_, _INVALID_REQUEST, "jsonrpc must be '2.0'")
    method = body.get("method")
    if method not in ("message/send", "message/stream"):
        return _err(id_, _METHOD_NOT_FOUND, f"method {method!r} not supported; use message/send")
    params = body.get("params") or {}
    text = _text_of(params) if isinstance(params, dict) else ""
    host = parse_agent(text)
    if not host:
        return _err(id_, _INVALID_PARAMS, "tell me which agent to benchmark: a host such as dnsdoc.webmesh.ai "
                                          "or an ans:// name, e.g. 'Benchmark ans://v1.0.6.dnsdoc.webmesh.ai'")
    suite = "regression" if "regression" in text.lower() else "generated"
    force = "fresh" in text.lower() or "force" in text.lower()
    try:
        report, cached = store.wait_report(host, suite, force)
    except RuntimeError as e:
        return _err(id_, _INTERNAL, f"benchmark of {host} failed: {e}")
    return {"jsonrpc": "2.0", "id": id_, "result": {
        "kind": "message", "role": "agent", "messageId": str(uuid.uuid4()),
        "parts": [{"kind": "text", "text": summary_text(report, cached, public_url)}],
        "metadata": {"host": host, "suite": suite, "cached": cached,
                     "behavior_score": report.behavior_score, "score_status": report.score_status,
                     "identity_status": report.identity.status,
                     "report_url": f"{public_url}/api/benchmark/{host}?suite={suite}"},
    }}
