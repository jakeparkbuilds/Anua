"""Execute assertions. Oracle expected values are computed here (cached), the agent is
called once per distinct input, and comparators grade the result."""
from __future__ import annotations
import time
from typing import Any
from ..models import Assertion, Kind, AgentCards
from ..oracles.registry import compute, OracleUnavailable
from ..agent import adapter
from ..agent.transport import AgentUnavailable
from .compare import compare


class Runner:
    def __init__(self, transport, cards: AgentCards, extractor=None):
        self.t = transport
        self.cards = cards
        self.extractor = extractor            # adapter.LLMExtractor or None (regression suite)
        self._resp_cache: dict[str, tuple[str, int]] = {}
        self._claims_by_prompt: dict[str, list[tuple[str, str]]] = {}
        self._blocked: str | None = None      # set once the agent refuses us (402/401/403)

    def plan(self, assertions: list[Assertion]) -> None:
        """Group oracle claims by prompt so LLM extraction is one call per response."""
        for a in assertions:
            if a.kind in (Kind.ORACLE, Kind.CONSISTENCY):
                self._claims_by_prompt.setdefault(adapter.build_prompt(a), []).append((a.claim, adapter.target_of(a)))

    def _extract(self, raw: str, a: Assertion):
        v = adapter.extract(raw, a.claim)
        if v is None and self.extractor is not None:
            self.extractor.prime(raw, self._claims_by_prompt.get(adapter.build_prompt(a), []))
            v = self.extractor.extract(raw, a.claim, adapter.target_of(a))
        return v

    def _ask(self, a: Assertion, force_fresh: bool = False) -> tuple[str, int]:
        prompt = adapter.build_prompt(a)
        if not force_fresh and prompt in self._resp_cache:
            return self._resp_cache[prompt]
        if self._blocked:
            raise AgentUnavailable(self._blocked)   # don't hammer a paywall 30 times
        try:
            raw, ms = self.t.send(prompt)
        except AgentUnavailable as e:
            self._blocked = str(e)
            raise
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
        except AgentUnavailable as e:
            # The agent would not serve us (payment / auth wall). We could not test
            # competence, so no verdict — except reachability, which is exactly what failed.
            a.error = f"agent unavailable: {e}"
            a.score = 0.0
            if a.kind == Kind.SCHEMA and a.claim == "endpoint.reachable":
                a.passed = False
                a.evidence = f"endpoint refused the call: {e}"
            else:
                a.passed = None
                a.evidence = f"NO VERDICT — {a.error}"
        except Exception as e:  # never let one assertion kill the run
            a.error = f"{type(e).__name__}: {e}"
            a.passed = False
            a.score = 0.0
            a.evidence = f"runner error: {a.error}"
        return a

    # ----------------------------------------------------------------------
    def _oracle(self, a: Assertion) -> Assertion:
        target = adapter.target_of(a)
        a.expected = a.expected_override if a.expected_override is not None else compute(a.oracle, target)
        if a.expected is None:
            # Belt and braces: oracles raise OracleUnavailable rather than returning None,
            # but if one ever does, never grade the agent against an unknown truth.
            raise OracleUnavailable(f"oracle {a.oracle} returned no value for {target}")
        raw, ms = self._ask(a)
        a.raw_response, a.latency_ms = raw, ms
        a.actual = self._extract(raw, a)

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
            from .drift import divergences, sources_present
            srcs = sources_present(self.cards.agent_card, self.cards.trust_card, self.cards.registry_entry)
            if len(srcs) < 2:
                a.passed, a.evidence = None, f"only {srcs} available; nothing to compare against"
                a.score = 0.0
                return a
            diffs = divergences(self.cards.agent_card, self.cards.trust_card, self.cards.registry_entry)
            a.expected, a.actual = [], diffs
            a.passed = not diffs
            a.evidence = (f"{' / '.join(srcs)} agree on url, version, ansName, functions, protocols, name" if not diffs
                          else "DRIFT: " + "; ".join(diffs))
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
        v1, v2 = self._extract(r1, a), self._extract(r2, a)
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
        target = adapter.target_of(a)
        domain = target.split("://", 1)[-1].split("/", 1)[0]
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


def run_all(assertions: list[Assertion], transport, cards: AgentCards, extractor=None,
            on_result=None) -> list[Assertion]:
    r = Runner(transport, cards, extractor)
    r.plan(assertions)
    out = []
    for a in assertions:
        out.append(r.run(a))
        if on_result:
            on_result(out[-1])
    return out
