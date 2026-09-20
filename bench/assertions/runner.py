"""Execute assertions. Oracle expected values are computed here (cached), the agent is
called once per distinct input, and comparators grade the result.

Three outcomes, and the difference is the whole design:
  PASS / FAIL   the agent stated a value and the oracle agreed / disagreed
  SKIPPED       WE could not do our part — no ground truth, no reader, a runner bug, a
                control target that is no longer what the test assumes. passed=None.
                Never counted in any denominator.
  NO VERDICT    the AGENT did not serve us (paywall, dead host, timeout, 5xx). Also
                passed=None. Only `endpoint.reachable` records it as a failure.

A wrong PASS is worse than a wrong FAIL, and both are worse than an honest SKIPPED.
"""
from __future__ import annotations
from typing import Any
from ..models import Assertion, Kind, AgentCards
from ..oracles.registry import compute, OracleUnavailable
from ..agent import adapter
from ..agent.adapter import ExtractionUnavailable
from ..agent.transport import AgentUnavailable
from .compare import compare, Ungradable

# What the agent was asked to state, in the words a reader would look for. Used only to
# make a "did not state it" verdict specific: "the answer never mentions DNSSEC".
_TOPIC = {
    "dns.dnssec": ("dnssec",), "dns.mx": ("mx", "mail exchange"), "dns.a_record": ("a record", "ipv4", "address"),
    "dns.resolves": ("resolv", "nxdomain"), "tls.chain_valid": ("certificate", "tls", "ssl"),
    "tls.expired": ("expir",), "tls.hostname_match": ("hostname",), "tls.not_after": ("expir", "not after", "valid until"),
    "tls.issuer": ("issuer", "issued by"), "http.status": ("status",), "http.https_ok": ("https",),
    "email.spf": ("spf",), "email.dmarc": ("dmarc",), "whois.nameservers": ("nameserver", "name server", " ns "),
    "whois.registrar": ("registrar",), "whois.created": ("creat", "registered on"), "whois.expires": ("expir",),
    "whois.registered": ("register",), "web.redirect_count": ("redirect",), "web.status": ("status",),
    "web.final_url": ("final url", "redirect", "lands on"), "web.h1_count": ("h1", "heading"),
}


class Runner:
    def __init__(self, transport, cards: AgentCards, extractor=None):
        self.t = transport
        self.cards = cards
        self.extractor = extractor            # adapter.LLMExtractor or None
        self._resp_cache: dict[str, tuple[str, int]] = {}
        self._claims_by_prompt: dict[str, list[tuple[str, str]]] = {}
        self._blocked: str | None = None      # set once the agent refuses us for good (402/401/403)

    def plan(self, assertions: list[Assertion]) -> None:
        """Group oracle claims by prompt so LLM extraction is one call per response."""
        for a in assertions:
            if a.kind in (Kind.ORACLE, Kind.CONSISTENCY):
                self._claims_by_prompt.setdefault(adapter.build_prompt(a), []).append((a.claim, adapter.target_of(a)))

    # ---- reading the agent's answer ----------------------------------------------
    def _extract(self, raw: str, a: Assertion) -> tuple[Any, str]:
        """(value, how). how ∈ {"json", "llm", "regex", "none"}. Raises
        ExtractionUnavailable when OUR reader failed — that is a skip, not a fail."""
        v = adapter.extract(raw, a.claim, allow_regex=False)
        if v is not None:
            return v, "json"
        if self.extractor is not None:
            self.extractor.prime(raw, self._claims_by_prompt.get(adapter.build_prompt(a), []))
            v = self.extractor.extract(raw, a.claim, adapter.target_of(a))
            return v, ("llm" if v is not None else "none")
        v = adapter.extract(raw, a.claim, allow_regex=True)
        return v, ("regex" if v is not None else "none")

    def _not_stated(self, raw: str, a: Assertion) -> str:
        """Why a None value is a FAIL here: the agent was asked, in a prompt naming the
        target and the fact, and its answer does not state it. Say whether it even
        mentions the topic — 'never mentions DNSSEC' is the finding a judge can check."""
        words = _TOPIC.get(a.claim) or (a.claim.split(".", 1)[-1].replace("_", " "),)
        low = f" {raw.lower()} "
        mentioned = [w.strip() for w in words if w in low]
        what = a.claim.split(".", 1)[-1].replace("_", " ")
        if mentioned:
            return (f"asked for {what}; the answer mentions '{mentioned[0]}' but states no value for it "
                    f"(no verbatim statement a reader could quote)")
        return f"asked for {what}; the answer never mentions it"

    def _ask(self, a: Assertion, force_fresh: bool = False) -> tuple[str, int]:
        prompt = adapter.build_prompt(a)
        if not force_fresh and prompt in self._resp_cache:
            return self._resp_cache[prompt]
        if self._blocked:
            raise AgentUnavailable(self._blocked, permanent=True)   # don't hammer a paywall 30 times
        try:
            raw, ms = self.t.send(prompt)
        except AgentUnavailable as e:
            if e.permanent:
                self._blocked = str(e)
            raise
        self._resp_cache[prompt] = (raw, ms)
        return raw, ms

    # ---- dispatch ------------------------------------------------------------------
    def run(self, a: Assertion) -> Assertion:
        try:
            if a.kind == Kind.ORACLE:
                return self._oracle(a)
            if a.kind == Kind.SELFCLAIM:
                return self._selfclaim(a)
            if a.kind == Kind.SCHEMA:
                return self._schema(a)
            if a.kind == Kind.CONSISTENCY:
                return self._consistency(a)
            if a.kind == Kind.QUALITY:
                return self._quality(a)
            return self._skip(a, f"unknown assertion kind {a.kind}")
        except OracleUnavailable as e:
            # We couldn't compute ground truth. That is OUR problem, not the agent's.
            return self._skip(a, f"oracle unavailable: {e}")
        except ExtractionUnavailable as e:
            # Our reader broke. The agent may have answered perfectly.
            return self._skip(a, f"reader unavailable: {e}")
        except Ungradable as e:
            return self._skip(a, f"ungradable: {e}")
        except AgentUnavailable as e:
            # The agent would not / could not serve us. We could not test competence, so
            # no verdict — except reachability, which is exactly what failed.
            a.error = f"agent unavailable: {e}"
            a.score = 0.0
            if a.kind == Kind.SCHEMA and a.claim == "endpoint.reachable":
                a.passed = False
                a.evidence = f"endpoint refused the call: {e}"
            else:
                a.passed = None
                a.evidence = f"NO VERDICT — {a.error}"
        except Exception as e:  # never let one assertion kill the run — and never blame the agent for our bug
            return self._skip(a, f"runner error (ours): {type(e).__name__}: {str(e)[:160]}")
        return a

    @staticmethod
    def _skip(a: Assertion, why: str) -> Assertion:
        a.error = why
        a.passed = None
        a.score = 0.0
        a.evidence = f"SKIPPED — {why}"
        return a

    # ---- kinds ---------------------------------------------------------------------
    def _selfclaim(self, a: Assertion) -> Assertion:
        """A claim about the agent's OWN infrastructure — "exposes an endpoint at X",
        "publishes a trust card at Y", "supports DNSSEC".

        We go and look. The agent is never called, and deliberately so: asking a
        domain-takedown risk scorer about the TLS state of its own endpoint measures
        nothing except whether it happens to also be a TLS tool, and its puzzlement
        was being recorded as nine HIGH-severity competence failures.

        Note the direction is the reverse of a capability test. Here `expected` is what
        the agent CLAIMS about itself and `actual` is what we measured to be TRUE."""
        target = adapter.target_of(a)
        a.actual = compute(a.oracle, target)
        a.expected = a.expected_override
        if a.comparator == "nonempty":
            a.passed, detail = compare("nonempty", None, a.actual)
        else:
            a.passed, detail = compare(a.comparator, a.expected, a.actual)
        a.score = 100.0 if a.passed else 0.0
        claimed = "present/non-empty" if a.comparator == "nonempty" else repr(a.expected)
        a.evidence = (f"[{a.oracle}] SELF-CLAIM (agent not called): card claims {claimed} for "
                      f"{target}; we measured {a.actual!r} — {detail}")
        return a

    def _expected(self, a: Assertion, target: str) -> Any:
        """Ground truth. Always computed live. A hand-written test may carry a pinned
        expectation for a known-negative control (expired.badssl.com is expired); if the
        live oracle disagrees, the control has changed and the test is void — SKIPPED,
        never graded against a stale assumption."""
        live = compute(a.oracle, target)
        if live is None:
            # Belt and braces: oracles raise rather than return None, but never grade
            # the agent against an unknown truth.
            raise OracleUnavailable(f"oracle {a.oracle} returned no value for {target}")
        if a.expected_override is not None and a.expected_override != live:
            raise Ungradable(f"control target changed: test assumes {a.expected_override!r} for {target}, "
                             f"the oracle now measures {live!r}")
        return live

    def _oracle(self, a: Assertion) -> Assertion:
        target = adapter.target_of(a)
        a.expected = self._expected(a, target)
        raw, ms = self._ask(a)
        a.raw_response, a.latency_ms = raw, ms
        a.actual, how = self._extract(raw, a)

        if a.claim == "http.https_ok" and adapter.https_timed_out(raw):
            a.passed = None
            a.score = 0.0
            a.evidence = f"[{a.oracle}] NO VERDICT — agent HTTPS probe timed out"
            return a

        if a.actual is None:
            a.passed, a.score = False, 0.0
            a.evidence = f"[{a.oracle}] {self._not_stated(raw, a)}; oracle says {a.expected!r}"
            return a
        a.passed, a.evidence = compare(a.comparator, a.expected, a.actual)
        a.score = 100.0 if a.passed else 0.0
        tag = {"json": "", "llm": " (read by LLM reader, verbatim-quoted)", "regex": " (read by pattern match; no LLM reader available)"}[how]
        a.evidence = f"[{a.oracle}] {a.evidence}{tag}"
        return a

    def _schema(self, a: Assertion) -> Assertion:
        c = a.claim
        if c == "endpoint.reachable":
            raw, ms = self._ask(a)
            a.raw_response, a.latency_ms = raw, ms
            a.actual = raw
            a.passed, a.evidence = compare("nonempty", None, raw)
        elif c == "card.skills_nonempty":
            if getattr(self.cards, "card_error", None):
                return self._skip(a, f"agent card not readable ({self.cards.card_error}); skills unknown, not absent")
            a.actual = bool(self.cards.declared_skills)
            a.expected = True
            a.passed, a.evidence = compare("bool", True, a.actual)
            a.evidence += f"; declared skills: {self.cards.declared_skills}"
        elif c == "drift.card_vs_trust":
            from .drift import divergences, sources_present
            srcs = sources_present(self.cards.agent_card, self.cards.trust_card, self.cards.registry_entry)
            if len(srcs) < 2:
                return self._skip(a, f"only {srcs} available; nothing to compare against")
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
            return self._skip(a, f"unknown schema claim {c}")
        a.score = 100.0 if a.passed else 0.0
        return a

    def _consistency(self, a: Assertion) -> Assertion:
        r1, ms1 = self._ask(a, force_fresh=True)
        r2, ms2 = self._ask(a, force_fresh=True)
        (v1, _), (v2, _) = self._extract(r1, a), self._extract(r2, a)
        a.expected, a.actual = v1, v2
        a.raw_response, a.latency_ms = r2, (ms1 + ms2) // 2
        if v1 is None or v2 is None:
            return self._skip(a, "consistency needs a readable value from both answers; "
                                 + ("neither" if v1 is None and v2 is None else "one") + " could be read")
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
