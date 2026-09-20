"""LangGraph test generator — the primary path, not an add-on.

  parse_claims     LLM reads agent card + trust card + registry entry -> discrete claims
  route_claims     LLM proposes an oracle key per claim AND says whether the claim is
                   about the agent's OWN infrastructure or about what it DOES for a
                   caller. Deterministic code REQUIRES every proposed key to exist in
                   ORACLES or the claim falls through to SCHEMA_ONLY / UNVERIFIABLE.
  selfclaim_tests  deterministic, NO LLM: one direct oracle check per self-claim
  generate_tests   LLM writes tests for CAPABILITY claims only, in small batches
  validate_tests   deterministic guardrail, no LLM
  report_coverage  claims_found / verifiable / schema_only / unverifiable + reasons

Two kinds of claim, and conflating them is how a domain-takedown risk scorer got 9 HIGH
failures for not knowing the TLS status of expired.badssl.com:

  SELF CLAIM        "exposes an A2A endpoint at https://x", "publishes a trust card at Y",
                    "supports DNSSEC". This is about the agent's own infrastructure. We
                    go and check it ourselves. The agent is NEVER asked.
  CAPABILITY CLAIM  "reports whether the certificate is expired". This is about what the
                    agent does for a caller, so the only way to test it is to call it.

The LLM proposes; it never decides truth. Every generated test is graded by an oracle.
"""
from __future__ import annotations
import json
import re
from typing import Any, TypedDict
from langgraph.graph import StateGraph, END
from ..models import Assertion, Kind, Severity, AgentCards, CapabilityClaim, CoverageReport, Verifiability
from ..oracles.registry import ORACLES, SPECS, catalog
from ..llm import LLM, parse_json

SUPPORTED_COMPARATORS = ("bool", "eq", "set_eq", "set_overlap", "contains", "date_close", "nonempty", "one_of", "lang_eq")
_DOMAIN = re.compile(r"^(?=.{1,253}$)([a-z0-9-]{1,63}\.)+[a-z]{2,63}$", re.I)
_URL = re.compile(r"^https?://[^\s/]+(/[^\s]*)?$", re.I)
_URL_IN_TEXT = re.compile(r"https?://[^\s,;)\"'<>]+")
_SCHEMA_WORDS = re.compile(r"\b(a2a|mcp|json-?rpc|protocol|streamable|agent card|signed|signature|"
                           r"x402|payment|auth|did:web|jwks|streaming|push ?notification|input ?modes?|output ?modes?)\b", re.I)
# Claims phrased about the agent's own deployment rather than about work it performs.
_SELF_WORDS = re.compile(r"\b(exposes?|publishes?|serves?|hosts?|listens?|presents?|signs?|registered|"
                         r"is (publicly )?(accessible|reachable|available)|endpoint at|available at|"
                         r"logs identity|transparency log|trust card|identity certificate|"
                         r"supports (dnssec|dane|tlsa|spiffe|mutual ?tls))\b", re.I)
# Claims that are a subjective CLASSIFICATION of a page, not a fact about it. "Is this a
# parking page" has no ground truth an oracle can compute: a page with a title and no
# body text may be parked, a stub, or example.com. The router once mapped it to
# web.title and graded the agent HIGH-failed on example.com for saying "not parked".
# Deterministic: a claim matching this is UNVERIFIABLE whatever keys the model proposes.
_SUBJECTIVE_CLASSIFICATION = re.compile(
    r"\b(park(ed|ing)(\s+page|\s+domain)?|placeholder (page|site)|under construction|"
    r"coming soon|(looks|appears) (legitimate|suspicious|abandoned)|abandoned (site|domain))\b", re.I)
# Oracles whose TRUE value is the bad news. An agent never claims its own cert is expired.
_NEGATIVE_SENSE = frozenset({"tls.expired", "web.robots_disallow_all"})
# A self-claim asserts a thing is TRUE or PRESENT. For an int or a date there is no
# claimed value to compare against, so we do not invent one.
_SELF_GRADABLE_RETURNS = frozenset({"bool", "list", "str"})
# A COUNT oracle only covers a claim that is about a count. "Checks redirects" is not a
# promise to report how many hops; dnsdoc reports the redirect target and was failed for
# never stating a number. Same for h1: "reports heading structure" is not "counts h1s".
_INT_NEEDS = {"web.redirect_count": re.compile(r"\b(count|number|how many|hops|chain length)\b", re.I),
              "web.h1_count": re.compile(r"\b(count|number|how many|multiple|more than one|exactly one|single)\b", re.I)}
_BATCH = 3                      # capability claims per generate_tests call

# Capability tests fire real DNS, TLS and HTTP requests at real hosts. The model chooses
# the targets, so the set it may choose from is bounded to hosts that exist for exactly
# this purpose or are large, public and documented: a benchmark must never look like a
# scan of someone's infrastructure. Anything else is rejected with a note.
_TARGET_ALLOW = frozenset({
    "badssl.com", "example.com", "example.net", "example.org", "example.edu", "iana.org",
    "cloudflare.com", "google.com", "github.com", "wikipedia.org", "mozilla.org", "isc.org",
    "internic.net", "ietf.org", "w3.org", "letsencrypt.org", "httpbin.org",
    "httpstat.us", "neverssl.com", "invalid",
})


def target_allowed(host: str) -> bool:
    h = (host or "").lower().rstrip(".")
    return any(h == d or h.endswith("." + d) for d in _TARGET_ALLOW)


class GenState(TypedDict, total=False):
    cards: dict
    host: str
    existing_ids: list[str]
    max_n: int
    claims: list[dict]
    proposals: list[dict]
    selfclaims: list[dict]
    assertions: list[dict]
    coverage: dict
    notes: list[str]


# ---- prompts ----------------------------------------------------------------
_PARSE_SYS = """You extract capability claims from an AI agent's self-descriptions.
A claim is one discrete, checkable-sounding thing the agent says it does or reports, e.g.
"reports whether the TLS certificate is expired", "reports the page's canonical link",
"scores a domain 1-10 for disruption". Split compound sentences into separate claims.
Include protocol/identity claims (A2A endpoint, MCP tool, signed card) as their own claims.
Do not invent capabilities that are not stated. 5 to 25 claims.

Return STRICT JSON: a list of {"claim_text": "...", "source": "skill"|"description"|"trust_card"|"registry"}.
JSON only."""

_ROUTE_SYS = """You match an AI agent's claims to oracle keys. An oracle independently computes
the true value of something about a domain or URL.

The agent under test is hosted at {host}.

FIRST, classify what each claim is ABOUT. This matters more than anything else you do here:

  "self"       The claim is about the agent's OWN deployment: its endpoint, its own URLs,
               its own certificate, its own DNS, its own trust card, its own registration.
               Example: "exposes an A2A endpoint at https://{host}", "publishes a trust
               card at https://{host}/.well-known/ans/trust-card.json", "supports DNSSEC".
               We verify these by probing {host} ourselves. The agent is never asked.

  "capability" The claim is about work the agent performs on an input a CALLER supplies.
               Example: "given a URL, reports whether it has a meta description",
               "scores a domain 1-10 for disruption". Only these are worth asking about.

If the claim describes infrastructure rather than work, it is "self" — even when it sounds
technical and even when an oracle could check it.

Oracle catalog (key, input kind, return type, meaning):
{catalog}

For EACH claim return one object:
  {{"claim_text": <verbatim>, "about": "self"|"capability",
   "oracle_keys": [<0-3 catalog keys whose meaning the claim directly covers>],
   "self_target": "<for about=self only: the exact domain or URL to probe; must belong to
                   {host} or be a URL the agent publishes>",
   "kind": "oracle"|"schema"|"none", "reason": "<one line>"}}
Rules: only use keys that appear in the catalog, exactly as written. Use "schema" for claims
about protocols, message formats, auth, payment or signatures (checkable by contract, not by
an oracle). Use "none" with a reason when the claim is subjective, a score the agent invents,
or depends on data nobody can independently compute (press coverage, "what breaks",
recommendations, rewriting text), or is a subjective classification of a page ("is this a
parking page", "does it look abandoned"). Be strict: a claim about recommendations is NOT
covered by an oracle that measures the underlying fact, and a classification is NOT covered
by an oracle that measures one signal it might be based on.
Return STRICT JSON list. JSON only."""

_GEN_SYS = """You design benchmark tests for an AI agent. Each test asks the agent about one
target (a domain or URL) and grades ONE fact the agent reports against an oracle that
computes the truth independently. You choose the target and write the prompt we send.

These are CAPABILITY tests: the target is an input WE supply to the agent, a third-party
domain or page. Never use the agent's own host as a target — that is verified separately.

Oracle keys you may use (with meaning, input kind, comparator, and known negative targets):
{catalog}

Agent facts: name {name}; input modes {input_modes}; the agent's own example prompts:
{examples}

Write 2-4 tests per claim below. Prefer NEGATIVE and EDGE cases the agent could get wrong:
for network oracles use badssl.com subdomains (expired., self-signed., wrong.host.,
untrusted-root., revoked.), NXDOMAIN, domains with no MX; for web oracles use pages with
no <h1>, no meta description, no robots.txt / sitemap, 404 pages, redirects (e.g.
https://example.com/ has a title and h1 but NO meta description, canonical, JSON-LD, OG,
robots.txt or sitemap; https://example.com/does-not-exist is a 404). Targets must be real,
public and stable, and MUST be under one of these hosts (anything else is discarded):
{allowed}. Use the same target for several tests where sensible.

Return STRICT JSON list of:
  {{"id": "kebab-case-unique", "claim": "<oracle key>", "target": "<domain or full URL per the
   oracle's input kind>", "comparator": "<the oracle's comparator>", "severity": "HIGH"|"MEDIUM"|"LOW",
   "rationale": "<one line>", "prompt": "<the exact request to send the agent, phrased like its
   examples, naming the target, and explicitly asking it to state the facts under test with
   concrete values (say: respond in JSON if you can)>"}}
Do not reuse these ids: {existing}. At most {n} tests in this reply. Keep each field short.
JSON only, no prose before or after."""


def _trim_cards(cards: dict) -> dict:
    ac, tc, re_ = cards.get("agent_card", {}) or {}, dict(cards.get("trust_card", {}) or {}), cards.get("registry_entry", {}) or {}
    tc.pop("keys", None); tc.pop("transparencyReceipt", None)
    caps = ac.get("capabilities", {}) or {}
    return {
        "agent_card": {k: ac.get(k) for k in ("name", "description", "url", "version", "protocolVersion",
                                              "skills", "supportedInterfaces", "defaultInputModes", "defaultOutputModes",
                                              "securitySchemes", "x-identity") if ac.get(k) is not None}
                      | ({"capabilities": {**{k: v for k, v in caps.items() if k != "extensions"},
                                           "extensions": [{"uri": e.get("uri"), "description": e.get("description"),
                                                           "params": e.get("params")} for e in caps.get("extensions", []) or []]}}
                         if caps else {}),
        "trust_card": tc,
        "registry_entry": {k: re_.get(k) for k in ("ansName", "agentDisplayName", "agentDescription", "agentVersion",
                                                    "endpoints", "lifecycle") if re_.get(k) is not None},
    }


def _examples(ac: dict) -> list[str]:
    out = []
    for s in ac.get("skills", []) or []:
        out.extend(s.get("examples", []) or [])
    return out[:8]


def own_hosts(cards: dict, host: str) -> set[str]:
    """Hosts we may probe directly when checking a SELF claim: the agent's own host, its
    parent domains (DNSSEC lives on the zone apex, not the label), and any host the agent
    names in its own cards (its transparency-log URL, for instance)."""
    host = (host or "").lower().strip(".")
    hosts = {host} if _DOMAIN.match(host) else set()
    labels = host.split(".")
    for i in range(1, max(len(labels) - 1, 1)):
        parent = ".".join(labels[i:])
        if _DOMAIN.match(parent):
            hosts.add(parent)
    for m in _URL_IN_TEXT.finditer(json.dumps(cards)):
        h = m.group(0).split("://", 1)[-1].split("/", 1)[0].split(":")[0].lower()
        if _DOMAIN.match(h):
            hosts.add(h)
    return hosts


def _host_of(target: str) -> str:
    return target.lower().split("://", 1)[-1].split("/", 1)[0].split(":")[0]


# ---- nodes (closures over the llm) ------------------------------------------
def build_graph(llm: LLM, on_event=None):
    emit = on_event or (lambda name, payload: None)

    def parse_claims(st: GenState) -> GenState:
        notes = list(st.get("notes", []))
        try:
            raw = llm.complete(_PARSE_SYS, "Agent self-descriptions:\n" + json.dumps(_trim_cards(st["cards"]), indent=1)[:14000])
            props = parse_json(raw, "list")
        except Exception as e:
            notes.append(f"parse_claims failed: {type(e).__name__}: {e}")
            props = []
        claims, seen = [], set()
        for p in props if isinstance(props, list) else []:
            if not isinstance(p, dict) or not str(p.get("claim_text", "")).strip():
                continue
            txt = " ".join(str(p["claim_text"]).split())
            if txt.lower() in seen:
                continue
            seen.add(txt.lower())
            src = str(p.get("source", "description")).lower()
            claims.append(CapabilityClaim(claim_text=txt, source=src if src in ("skill", "description", "trust_card", "registry") else "description").model_dump())
        if not claims:
            notes.append("no capability claims extracted from the agent's self-descriptions")
        emit("claims", {"claims_found": len(claims)})
        return {**st, "claims": claims, "notes": notes}

    def route_claims(st: GenState) -> GenState:
        claims = [CapabilityClaim(**c) for c in st.get("claims", [])]
        notes = list(st.get("notes", []))
        host = st.get("host", "")
        mine = own_hosts(st.get("cards", {}), host)
        routed: dict[str, dict] = {}
        if claims:
            cat = "\n".join(f"- {c['key']} [{c['input']} -> {c['returns']}]: {c['description']}" for c in catalog())
            try:
                raw = llm.complete(_ROUTE_SYS.format(catalog=cat, host=host or "the agent's host"),
                                   "Claims:\n" + json.dumps([{"claim_text": c.claim_text} for c in claims], indent=1))
                for r in parse_json(raw, "list"):
                    if isinstance(r, dict) and r.get("claim_text"):
                        routed[" ".join(str(r["claim_text"]).split()).lower()] = r
            except Exception as e:
                notes.append(f"route_claims failed: {type(e).__name__}: {e}")
        selfclaims: list[dict] = []
        for c in claims:
            r = routed.get(c.claim_text.lower(), {})
            keys = [k for k in (r.get("oracle_keys") or ([r["oracle_key"]] if r.get("oracle_key") else [])) if k in ORACLES]
            keys = [k for k in keys if k not in _INT_NEEDS or _INT_NEEDS[k].search(c.claim_text)]
            kind = str(r.get("kind", "")).lower()

            # --- self vs capability. Deterministic evidence outranks the model: a claim
            # that names the agent's own host IS about the agent's own host.
            named = [t for t in _URL_IN_TEXT.findall(c.claim_text)] + \
                    [w.strip(".,;:()") for w in c.claim_text.split() if _DOMAIN.match(w.strip(".,;:()"))]
            named_mine = [t for t in named if _host_of(t) in mine]
            if named_mine:
                c.about = "self"
            elif str(r.get("about", "")).lower() == "self":
                c.about = "self"
            elif not named and _SELF_WORDS.search(c.claim_text):
                c.about = "self"
            else:
                c.about = "capability"

            subjective = _SUBJECTIVE_CLASSIFICATION.search(c.claim_text)
            if subjective:
                # A proxy oracle here is worse than none: web.title says nothing about
                # whether a page is parked, and grading against it manufactures failures.
                c.verifiability, c.oracle_key = Verifiability.UNVERIFIABLE, None
                c.reason = (f"'{subjective.group(0)}' is a subjective classification, not a fact: "
                            "no oracle computes it and any single signal (title, text length) "
                            "is only a proxy")
            elif keys:                                                # deterministic: key MUST exist
                c.verifiability, c.oracle_key = Verifiability.VERIFIABLE, ",".join(keys)
                c.reason = r.get("reason", "") or f"oracle {keys[0]}"
            elif kind == "schema" or _SCHEMA_WORDS.search(c.claim_text):
                c.verifiability = Verifiability.SCHEMA_ONLY
                c.reason = r.get("reason", "") or "contract/protocol claim: checkable by schema, not by an oracle"
            else:
                bad = [k for k in (r.get("oracle_keys") or []) if k not in ORACLES]
                c.verifiability = Verifiability.UNVERIFIABLE
                c.reason = r.get("reason", "") or ("proposed oracle does not exist: " + ", ".join(bad) if bad else "no independent oracle computes this")

            if c.about == "self" and c.verifiability == Verifiability.VERIFIABLE:
                # Deterministic evidence first: the claim text carries the FULL url
                # ("...at https://transparency.ans.godaddy.com/v1/agents/<id>"), while the
                # model tends to hand back just the origin, which then 404s and reads as
                # the agent having lied about its own transparency-log entry.
                tgt = (named_mine[0] if named_mine else str(r.get("self_target") or "").strip()) or host
                # No model-supplied expectation: a self-claim asserts the thing is TRUE, and
                # the only way an "expected: false" could arrive here is from text in the
                # card steering the router. That would turn a dead endpoint into a pass.
                selfclaims.append({"claim_text": c.claim_text, "oracle_keys": keys, "target": tgt})
        emit("routed", {"claims_found": len(claims),
                        "self": sum(1 for c in claims if c.about == "self"),
                        "capability": sum(1 for c in claims if c.about != "self"),
                        "verifiable": sum(1 for c in claims if c.verifiability == Verifiability.VERIFIABLE),
                        "unverifiable": sum(1 for c in claims if c.verifiability == Verifiability.UNVERIFIABLE)})
        return {**st, "claims": [c.model_dump() for c in claims], "selfclaims": selfclaims, "notes": notes}

    def generate_tests(st: GenState) -> GenState:
        """CAPABILITY claims only. Batched: one big reply gets truncated and the whole
        array is lost, so we ask for a few claims at a time."""
        claims = [CapabilityClaim(**c) for c in st.get("claims", [])
                  if c.get("verifiability") == "VERIFIABLE" and c.get("about") != "self"]
        notes = list(st.get("notes", []))
        if not claims:
            notes.append("no VERIFIABLE capability claims: no agent-facing tests to generate")
            emit("tests", {"written": 0, "batch": 0, "batches": 0})
            return {**st, "proposals": [], "notes": notes}
        ac = st["cards"].get("agent_card", {}) or {}
        max_n = int(st.get("max_n", 30))
        existing = list(st["existing_ids"])
        props: list[dict] = []
        batches = [claims[i:i + _BATCH] for i in range(0, len(claims), _BATCH)]
        per_batch = max(2, -(-max_n // max(len(batches), 1)))
        for bi, batch in enumerate(batches):
            if len(props) >= max_n:
                break
            keys = sorted({k for c in batch for k in c.oracle_key.split(",")})
            cat = "\n".join(f"- {k} [{SPECS[k].input}, comparator {SPECS[k].comparator}]: {SPECS[k].description}"
                            + (f" | negatives: {', '.join(SPECS[k].negatives)}" if SPECS[k].negatives else "") for k in keys)
            sys_ = _GEN_SYS.format(catalog=cat, name=ac.get("name", "?"),
                                   input_modes=ac.get("defaultInputModes", ["text/plain"]),
                                   examples=json.dumps(_examples(ac)),
                                   allowed=", ".join(sorted(_TARGET_ALLOW)),
                                   existing=", ".join(existing[-40:]) or "(none)", n=per_batch)
            human = "Claims to test:\n" + json.dumps(
                [{"claim_text": c.claim_text, "oracle_keys": c.oracle_key.split(",")} for c in batch], indent=1)
            try:
                got = parse_json(llm.complete(sys_, human), "list")
            except Exception as e:
                notes.append(f"generate_tests batch {bi + 1}/{len(batches)} failed: {type(e).__name__}: {e}")
                continue
            got = [g for g in got if isinstance(g, dict)] if isinstance(got, list) else []
            props.extend(got)
            existing.extend(str(g.get("id", "")) for g in got)
            emit("tests", {"written": len(props), "batch": bi + 1, "batches": len(batches)})
        if not props:
            notes.append("generate_tests produced no usable proposals")
        return {**st, "proposals": props, "notes": notes}

    g = StateGraph(GenState)
    g.add_node("parse_claims", parse_claims)
    g.add_node("route_claims", route_claims)
    g.add_node("generate_tests", generate_tests)
    g.add_node("validate_tests", validate_tests)
    g.add_node("report_coverage", report_coverage)
    g.set_entry_point("parse_claims")
    g.add_edge("parse_claims", "route_claims")
    g.add_edge("route_claims", "generate_tests")
    g.add_edge("generate_tests", "validate_tests")
    g.add_edge("validate_tests", "report_coverage")
    g.add_edge("report_coverage", END)
    return g.compile()


# ---- deterministic nodes (no LLM; unit-tested offline) ----------------------
def _slug(s: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", str(s).lower())).strip("-")[:48]


def _normalise_target(target: str, want: str) -> dict | None:
    """Coerce a target to the oracle's input kind, or None if it is not usable."""
    target = (target or "").strip()
    if want == "url":
        if _DOMAIN.match(target):
            target = f"https://{target.lower()}/"
        return {"url": target} if _URL.match(target) else None
    host = _host_of(target)
    return {"domain": host} if _DOMAIN.match(host) else None


def selfclaim_assertions(st: GenState) -> tuple[list[Assertion], list[str]]:
    """Build the direct checks for the agent's claims about its OWN infrastructure.

    No LLM, no agent call: the oracle computes what is actually true at the agent's own
    host, and we compare it to what the agent's card says. `expected` is the CLAIM,
    `actual` is REALITY — the opposite way round from a capability test, where expected
    is reality and actual is what the agent told us."""
    host = st.get("host", "")
    mine = own_hosts(st.get("cards", {}), host)
    seen = set(st.get("existing_ids", []))
    out: list[Assertion] = []
    notes: list[str] = []
    skipped: dict[str, int] = {}
    for sc in st.get("selfclaims", []) or []:
        for key in sc.get("oracle_keys") or []:
            # A self-claim about a published URL asks "is something serving here?", not
            # "is this a retrievable HTML document?". An MCP endpoint answering 406 to an
            # HTML GET is doing its job.
            if key == "web.fetchable" and "web.endpoint_live" in SPECS:
                key = "web.endpoint_live"
            sp = SPECS.get(key)
            if sp is None:
                continue
            if sp.returns not in _SELF_GRADABLE_RETURNS or sp.comparator == "one_of":
                # one_of oracles (http.status) return a non-empty list for ANY live host:
                # "nonempty" would pass a self-claim without checking anything.
                skipped[f"{sp.returns}-valued oracle has no claimed value to check"] = \
                    skipped.get(f"{sp.returns}-valued oracle has no claimed value to check", 0) + 1
                continue
            # A self-claim may only probe the agent's own infrastructure. This is the guard
            # that stops a "negative control" against badssl.com becoming a self-check: if
            # the proposed target is not ours, fall back to the agent's own host, and if
            # even that will not normalise, drop the check rather than probe a stranger.
            inp = _normalise_target(sc.get("target") or host, sp.input)
            if inp is None or _host_of(next(iter(inp.values()))) not in mine:
                inp = _normalise_target(host, sp.input)
            if inp is None or _host_of(next(iter(inp.values()))) not in mine:
                skipped["target is not the agent's own infrastructure"] = \
                    skipped.get("target is not the agent's own infrastructure", 0) + 1
                continue
            tgt_val = next(iter(inp.values()))
            if _host_of(tgt_val) != _host_of(host) and not tgt_val.split("://", 1)[-1][len(_host_of(tgt_val)):].strip("/"):
                # A bare third-party ORIGIN is the model paraphrasing a URL it did not
                # copy: "logs identity to a transparency log" became
                # https://transparency.ans.godaddy.com, whose root 404s, which then reads
                # as the agent lying about its own transparency entry. Probing someone
                # else's API root tests nothing about this agent. Drop it.
                skipped["third-party origin with no path — nothing about this agent to check"] = \
                    skipped.get("third-party origin with no path — nothing about this agent to check", 0) + 1
                continue
            path = tgt_val.split("://", 1)[-1][len(_host_of(tgt_val)):].strip("/")
            aid = _slug(f"self-{key}-{_host_of(tgt_val)}" + (f"-{path}" if path else ""))
            if aid in seen:
                continue
            seen.add(aid)
            if sp.returns == "bool":
                expected, comparator = key not in _NEGATIVE_SENSE, "bool"
            else:
                expected, comparator = None, "nonempty"
            out.append(Assertion(
                id=aid, kind=Kind.SELFCLAIM, claim=key, input=inp, oracle=key,
                comparator=comparator, expected_override=expected, severity=Severity.MEDIUM,
                generated=True, rationale=f"agent's own claim: {sc['claim_text'][:140]}"))
    if skipped:
        notes.append("selfclaim_tests skipped: " + ", ".join(f"{n} ({why})" for why, n in skipped.items()))
    return out, notes


def validate_tests(st: GenState) -> GenState:
    seen = set(st.get("existing_ids", []))
    notes = list(st.get("notes", []))
    rejected: dict[str, int] = {}
    host = st.get("host", "")
    mine = own_hosts(st.get("cards", {}), host)

    selfs, snotes = selfclaim_assertions(st)
    notes.extend(snotes)
    out: list[dict] = [a.model_dump() for a in selfs]
    seen.update(a.id for a in selfs)

    prompt_by_target: dict[str, str] = {}
    for p in st.get("proposals", []) or []:
        if not isinstance(p, dict):
            continue
        claim, aid = str(p.get("claim", "")).strip(), _slug(p.get("id", ""))
        target = str(p.get("target") or p.get("domain") or p.get("url") or "").strip()
        sp = SPECS.get(claim)
        if sp is None:
            rejected["unknown oracle"] = rejected.get("unknown oracle", 0) + 1; continue
        if not aid or aid in seen:
            rejected["duplicate/empty id"] = rejected.get("duplicate/empty id", 0) + 1; continue
        inp = _normalise_target(target, sp.input)
        if inp is None:
            rejected["invalid target"] = rejected.get("invalid target", 0) + 1; continue
        target = next(iter(inp.values()))
        if _host_of(target) in mine:
            # Asking the agent about itself is a self-claim, and it is already checked
            # directly above. Sending it here would grade it on answering a question it
            # was never built to answer.
            rejected["capability test aimed at the agent's own host"] = \
                rejected.get("capability test aimed at the agent's own host", 0) + 1; continue
        if not target_allowed(_host_of(target)):
            rejected["target outside the allowed benchmark host set"] = \
                rejected.get("target outside the allowed benchmark host set", 0) + 1; continue
        comparator = p.get("comparator") if p.get("comparator") in SUPPORTED_COMPARATORS else sp.comparator
        if comparator == "bool" and sp.returns != "bool":
            comparator = sp.comparator                     # never grade an int/str as a bool
        sev = Severity(p["severity"]) if p.get("severity") in ("HIGH", "MEDIUM", "LOW") else Severity.MEDIUM
        prompt = " ".join(str(p.get("prompt") or "").split())
        if not prompt or target.split("://", 1)[-1].rstrip("/") not in prompt:
            prompt = f"{prompt} Target: {target}. State the facts you observe with concrete values; respond in JSON if you can.".strip()
        prompt = prompt_by_target.setdefault(target, prompt)   # one prompt per target -> one agent call
        seen.add(aid)
        out.append(Assertion(id=aid, kind=Kind.ORACLE, claim=claim, input=inp, prompt=prompt, oracle=claim,
                             comparator=comparator, severity=sev, generated=True,
                             rationale=str(p.get("rationale", ""))[:200]).model_dump())
        if len(out) - len(selfs) >= int(st.get("max_n", 30)):
            notes.append(f"generated capability tests capped at {st.get('max_n', 30)}")
            break
    if rejected:
        notes.append("validate_tests rejected: " + ", ".join(f"{n} {why}" for why, n in rejected.items()))
    return {**st, "assertions": out, "notes": notes}


def report_coverage(st: GenState) -> GenState:
    claims = [CapabilityClaim(**c) for c in st.get("claims", [])]
    asserts = st.get("assertions", []) or []
    n = len(claims)
    v = sum(1 for c in claims if c.verifiability == Verifiability.VERIFIABLE)
    s = sum(1 for c in claims if c.verifiability == Verifiability.SCHEMA_ONLY)
    u = n - v - s
    n_self = sum(1 for a in asserts if a.get("kind") == Kind.SELFCLAIM.value)
    cov = CoverageReport(claims_found=n, verifiable=v, schema_only=s, unverifiable=u, claims=claims,
                         coverage_ratio=round(v / n, 3) if n else 0.0,
                         self_claims=sum(1 for c in claims if c.about == "self"),
                         capability_claims=sum(1 for c in claims if c.about != "self"),
                         tests_generated=len(asserts), selfclaim_tests=n_self,
                         capability_tests=len(asserts) - n_self, notes=list(st.get("notes", [])))
    return {**st, "coverage": cov.model_dump()}


# ---- entry point -------------------------------------------------------------
def generate(cards: AgentCards, existing: list[Assertion], model: str, max_n: int,
             llm: LLM | None = None, host: str = "",
             on_event=None) -> tuple[list[Assertion], CoverageReport, list[str]]:
    """Returns (generated assertions, coverage report, notes). Raises if no LLM can be
    built — a missing key must be loud, never a silent skip."""
    if llm is None:
        from ..llm import make_llm
        llm = make_llm(model)
    graph = build_graph(llm, on_event=on_event)
    st = graph.invoke({"cards": {"agent_card": cards.agent_card, "trust_card": cards.trust_card,
                                 "registry_entry": cards.registry_entry},
                       "host": host or (cards.endpoint or "").split("://", 1)[-1].split("/", 1)[0],
                       "existing_ids": [a.id for a in existing], "max_n": max_n, "notes": []})
    cov = CoverageReport(**st["coverage"])
    return [Assertion(**d) for d in st.get("assertions", [])], cov, st.get("notes", [])
