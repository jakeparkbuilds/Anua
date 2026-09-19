"""Execute assertions. Oracle expected values are computed here (cached), the agent is
called once per distinct input, and comparators grade the result."""
from __future__ import annotations
import time
from typing import Any
from ..models import Assertion, Kind, AgentCards
from ..oracles.registry import compute, OracleUnavailable
from ..agent import adapter
from .compare import compare


class Runner:
    def __init__(self, transport, cards: AgentCards):
        self.t = transport
        self.cards = cards
        self._resp_cache: dict[str, tuple[str, int]] = {}

    def _ask(self, a: Assertion, force_fresh: bool = False) -> tuple[str, int]:
        prompt = adapter.build_prompt(a)
        if not force_fresh and prompt in self._resp_cache:
            return self._resp_cache[prompt]
        raw, ms = self.t.send(prompt)
        self._resp_cache[prompt] = (raw, ms)
        return raw, ms

    def run(self, a: Assertion) -> Assertion:
        try:
            if a.kind == Kind.ORACLE:
                return self._oracle(a)
            if a.kind == Kind.SCHEMA:
                return self._schema(a)
            if a.kind == Kind.CONSISTENCY:
                return self._consistency(a)
            if a.kind == Kind.QUALITY:
                return self._quality(a)
        except OracleUnavailable as e:
            # We couldn't compute ground truth. That is OUR problem, not the agent's:
            # mark ungraded so it never counts against the score.
            a.error = f"oracle unavailable: {e}"
            a.passed = None
            a.score = 0.0
            a.evidence = f"SKIPPED — {a.error}"
        except Exception as e:  # never let one assertion kill the run
            a.error = f"{type(e).__name__}: {e}"
            a.passed = False
            a.score = 0.0
            a.evidence = f"runner error: {a.error}"
        return a

    # ----------------------------------------------------------------------
    def _oracle(self, a: Assertion) -> Assertion:
        domain = a.input["domain"]
        a.expected = a.expected_override if a.expected_override is not None else compute(a.oracle, domain)
        raw, ms = self._ask(a)
        a.raw_response, a.latency_ms = raw, ms
        a.actual = adapter.extract(raw, a.claim)

        if a.claim == "http.https_ok" and adapter.https_timed_out(raw):
            a.passed = None
            a.score = 0.0
            a.evidence = f"[{a.oracle}] NO VERDICT — agent HTTPS probe timed out"
            return a

        a.passed, a.evidence = compare(a.comparator, a.expected, a.actual)
        a.score = 100.0 if a.passed else 0.0
        a.evidence = f"[{a.oracle}] {a.evidence}"
        return a

    def _schema(self, a: Assertion) -> Assertion:
        c = a.claim
        if c == "endpoint.reachable":
            raw, ms = self._ask(a)
            a.raw_response, a.latency_ms = raw, ms
            a.actual = raw
            a.passed, a.evidence = compare("nonempty", None, raw)
        elif c == "card.skills_nonempty":
            a.actual = bool(self.cards.declared_skills)
            a.expected = True
            a.passed, a.evidence = compare("bool", True, a.actual)
            a.evidence += f"; declared skills: {self.cards.declared_skills}"
        elif c == "drift.card_vs_trust":
            card_fns = set(self.cards.declared_skills)
            trust_fns = set(map(str, self.cards.trust_card.get("functions", []) or []))
            a.expected, a.actual = sorted(card_fns), sorted(trust_fns)
            if not trust_fns:
                a.passed, a.evidence = True, "trust card lists no functions; nothing to compare"
            else:
                a.passed, a.evidence = compare("set_eq", card_fns, trust_fns)
                only_trust, only_card = trust_fns - card_fns, card_fns - trust_fns
                if only_trust:  a.evidence += f"; DRIFT: in trust card but not agent card: {sorted(only_trust)}"
                if only_card:   a.evidence += f"; DRIFT: in agent card but not trust card: {sorted(only_card)}"
        elif c == "card.protocol_declared":
            a.actual = a.input["protocol"] in self.cards.declared_protocols
            a.expected = True
            a.passed, a.evidence = compare("bool", True, a.actual)
            a.evidence += f"; declared: {self.cards.declared_protocols}"
        else:
            a.passed, a.evidence = False, f"unknown schema claim {c}"
        a.score = 100.0 if a.passed else 0.0
        return a

    def _consistency(self, a: Assertion) -> Assertion:
        r1, ms1 = self._ask(a, force_fresh=True)
        r2, ms2 = self._ask(a, force_fresh=True)
        v1, v2 = adapter.extract(r1, a.claim), adapter.extract(r2, a.claim)
        a.expected, a.actual = v1, v2
        a.raw_response, a.latency_ms = r2, (ms1 + ms2) // 2
        a.passed, a.evidence = compare("eq", v1, v2)
        a.evidence = f"[repeat call] {a.evidence}"
        a.score = 100.0 if a.passed else 0.0
        return a

    def _quality(self, a: Assertion) -> Assertion:
        from . import quality
        raw, ms = self._ask(a)
        a.raw_response, a.latency_ms = raw, ms
        domain = a.input["domain"]
        known = []
        for claim in ("tls.not_after", "tls.issuer", "dns.a_record"):
            try:
                v = compute(claim, domain)
                known.extend(v if isinstance(v, list) else [v])
            except Exception:
                pass
        q = quality.score(raw, known)
        a.actual = q.model_dump()
        a.passed = None                       # quality never passes/fails
        a.score = round((q.readability or 0) * 0.4 + (q.grounding or 0) * 100 * 0.4 + (q.actionability or 0) * 100 * 0.2, 1)
        a.evidence = f"[classical ML, low confidence] readability={q.readability} grounding={q.grounding} actionability={q.actionability}"
        return a


def run_all(assertions: list[Assertion], transport, cards: AgentCards) -> list[Assertion]:
    r = Runner(transport, cards)
    return [r.run(a) for a in assertions]
