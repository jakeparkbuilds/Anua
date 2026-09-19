# ans-bench

**Behavioral benchmarking for ANS-registered AI agents.**

> ANS proves *who* an agent is. It does not prove the agent *does what it says it does*.
> This measures that, and feeds the result back into the Trust Index.

VTHacks 14 — GoDaddy "Best Use of ANS" track.

---

## Table of contents

1. [The one-paragraph pitch](#1-the-one-paragraph-pitch)
2. [Why this gap exists](#2-why-this-gap-exists)
3. [What the system does, step by step](#3-what-the-system-does-step-by-step)
4. [Quick start (5 minutes)](#4-quick-start-5-minutes)
5. [Repo map — what every file is](#5-repo-map--what-every-file-is)
6. [Core concepts you must understand before editing](#6-core-concepts-you-must-understand-before-editing)
7. [The five design rules](#7-the-five-design-rules)
8. [CLI reference](#8-cli-reference)
9. [Report format](#9-report-format)
10. [Going live — exact order](#10-going-live--exact-order)
11. [Where the human edits go](#11-where-the-human-edits-go)
12. [How to add a new capability test](#12-how-to-add-a-new-capability-test)
13. [Build status — what is real vs unproven](#13-build-status--what-is-real-vs-unproven)
14. [What's left to build](#14-whats-left-to-build)
15. [Team split suggestion](#15-team-split-suggestion)
16. [Troubleshooting](#16-troubleshooting)
17. [Suites: generated vs regression](#17-suites-generated-vs-regression)

---

## 1. The one-paragraph pitch

The GoDaddy Trust Index scores agents across five dimensions: **integrity, identity,
solvency, behavior, safety**. In the public reference implementation
(`agentnameservice/agent-trust-discovery`), only integrity and identity have signals.
**Every registered agent scores 0 on `behavior`** — not because agents behave badly, but
because nothing measures it. All eight built-in signals look at certificates, DNS records,
registration age and version churn. *Not one of them calls the agent.*

`ans-bench` is the missing signal producer. It discovers an agent through the ANS registry,
verifies its identity against the transparency log, then **actually calls it and checks
whether its answers are true** — grading against independently computed ground truth rather
than an LLM's opinion, and emitting a `behavior` observation for the Trust Index.

**Status, stated plainly:** discovery, identity verification, calling the agent and scoring
it all work live today — `dnsdoc.webmesh.ai` scores **87** with identity VERIFIED. The
emit side is written but the receiving Go signals are **not yet built**, so the score does
not move the Trust Index yet. See [§13](#13-build-status--what-is-real-vs-unproven).

**Pitch line:** *ANS tells you who you're talking to. We tell you whether they can do what they claim.*

---

## 2. Why this gap exists

Capabilities in ANS are **self-declared at registration**. An agent says "I diagnose TLS
certificates" and nothing ever checks. There are three separate places an agent describes
itself, and nobody compares them:

| Source | Location |
|---|---|
| ANS registration metadata | transparency log entry |
| Agent card | `/.well-known/agent-card.json` |
| Trust card | `/.well-known/ans/trust-card.json` |
| **What it actually does** | **only discoverable by calling it** |

Those four can disagree. We call that **claim drift**, and we test for it.

Identity and competence are different properties. ANS is explicit that it is an identity
layer. This is the layer above it.

---

## 3. What the system does, step by step

Running `python -m bench run` executes six stages (see `bench/pipeline.py`):

```
[1/6] DISCOVER + VERIFY IDENTITY          bench/ans/
      • GET {search_base}/v1/ans/registered-agents?query=<host>
        → find the agent by host (not hardcoded — this is the "discover" verb)
      • GET {transparency_base}/v1/agents/{ansId}
        → pull the sealed TL record
      • live TLS handshake → SHA-256 the peer cert
        → compare to TL-sealed fingerprint (drift check)
      Four distinct outcomes, never collapsed into one boolean:
        VERIFIED   registry + TL + fingerprint match
        PENDING    TL entry exists but no sealed cert yet (validation in flight)
        MISMATCH   sealed fingerprint != live cert  ← the alarm
        NOT_FOUND  no registry entry, or no TL entry for it

[2/6] FETCH SELF-DESCRIPTIONS             bench/agent/card.py
      • /.well-known/agent-card.json  → declared skills, endpoint, protocols
      • /.well-known/ans/trust-card.json → declared functions

[3/6] GENERATE THE TEST SUITE            bench/generator/graph.py
      ★ NOT optional — this is the product. See §17.
      LangGraph StateGraph:
        parse_claims   — read the cards, list every capability the agent claims
        route_claims   — the LLM proposes oracle keys; DETERMINISTIC code then
                         requires each key to exist in ORACLES. A key we cannot
                         compute is dropped and the claim falls through to
                         UNVERIFIABLE. The model suggests; it never asserts.
        generate_tests — concrete inputs (target + prompt + comparator), one
                         shared prompt per target so N tests cost 1 agent call
        validate_tests — reject unknown oracle / bad target / duplicate id;
                         coerce the comparator to the oracle's return type;
                         cap at generator.max_generated
        report_coverage— VERIFIABLE | SCHEMA_ONLY | UNVERIFIABLE, per claim
      Coverage is a first-class result, not a diagnostic. An agent whose claims
      nobody can objectively check is itself the finding.

[4/6] BUILD TRANSPORT                     bench/agent/transport.py
      A2A (JSON-RPC message/send) | MCP (tools/call) | Mock (offline fixtures)

[5/6] RUN ASSERTIONS                      bench/assertions/runner.py
      For each assertion:
        expected = oracle(domain)        ← computed by US, independently
        actual   = extract(agent_response, claim)
        passed   = compare(comparator, expected, actual)

[6/6] BUILD REPORT                        bench/report/
      → out/latest.json  (+ timestamped copy)
      → optionally POST observations to agent-trust-discovery  (--emit)
```

---

## 4. Quick start (5 minutes)

```bash
git clone <this-repo> && cd ans-bench
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python -m bench selftest              # 1. MUST PASS — proves the oracles are honest here
python -m bench oracles               # 2. the 41 oracles the generator may choose from
python -m bench run --suite regression          # 3. offline: mock agent, LIVE oracles
python -m bench run --suite regression --live   # 4. live ANS + live agent, no API key
cat out/latest.json

# 5. the actual product — LangGraph writes the tests for whatever agent you name
echo "ANTHROPIC_API_KEY=sk-..." >> .env
python -m bench run --live                          # dnsdoc
python -m bench run --live --host impact.webmesh.ai # an agent nobody hand-wrote tests for
```

Steps 3 and 4 use `--suite regression` on purpose: they need no API key, so they always work
in a demo. Step 5 is the one that makes the point — see §17.

**Offline run (step 3):** 26 assertions, `behavior_score` around 75, and **2 HIGH-severity
failures**. Those failures are intentional — `fixtures/mock_responses.json` contains a fake
agent with three deliberate lies baked in (it claims `expired.badssl.com` has a valid
certificate). That is the demo: a provable competence failure, caught with zero network
access to the real agent.

**Live run (step 3):** against `dnsdoc.webmesh.ai`, `behavior_score` around 87, 17/19 graded
oracle assertions passed, **0 HIGH-severity failures**, and `identity VERIFIED via ANS` —
the live TLS fingerprint matches the one sealed in the transparency log. The real agent is
honest; the mock is the one that lies. Keep that straight when demoing.

If `selftest` fails on `cloudflare.com`, your Python has no CA trust store — see
[Troubleshooting](#16-troubleshooting). Fix it before trusting any score.

`make` shortcuts exist for all of this — see the `Makefile`.

---

## 5. Repo map — what every file is

```
ans-bench/
├── README.md                    ← you are here
├── Makefile                     make selftest / run / run-live / probe / test
├── requirements.txt
├── config.yaml                  ★ ALL tunable settings. Grep "HUMAN" for unconfirmed values.
├── .env.example                 copy → .env, add ANTHROPIC_API_KEY (only needed for step 3)
│
├── bench/
│   ├── __main__.py              ★ CLI entrypoint. 6 commands: run / selftest / oracles
│   │                              / probe-agent / probe-registry / list
│   ├── llm.py                   ★ The single LLM seam. make_llm() raises LOUDLY when
│   │                              ANTHROPIC_API_KEY is missing; parse_json() tolerates
│   │                              fenced/prefixed model output. Keys come from env only.
│   ├── config.py                Pydantic config loader for config.yaml
│   ├── models.py                ★★ CORE DATA MODEL. Read this first.
│   │                              Assertion, Kind, Severity, AgentIdentity,
│   │                              AgentCards, QualityScores, Report
│   ├── pipeline.py              ★ The 6-stage orchestrator. Read this second.
│   │
│   ├── ans/                     ─── THE ANS LAYER ───
│   │   ├── registry.py          Search API + Transparency Log clients.
│   │   │                        Defensive field extraction (_first) because we have
│   │   │                        not seen a real response yet.
│   │   └── identity.py          Identity verification. Live TLS handshake vs TL-sealed
│   │                            fingerprint = drift check.
│   │                            TODO(b): identity-cert URI SAN validation (needs CA root)
│   │
│   ├── agent/                   ─── TALKING TO THE TARGET AGENT ───
│   │   ├── card.py              Fetches agent-card.json + trust-card.json
│   │   ├── transport.py         A2ATransport / MCPTransport / MockTransport
│   │   │                        MockTransport replays fixtures/mock_responses.json
│   │   └── adapter.py           ★★★ THE FILE YOU WILL EDIT MOST.
│   │                            build_prompt()   — what we send the agent
│   │                            extract()        — pull a claim's value out of the response
│   │                            _from_evidence() — ★ reads the agent's STRUCTURED block.
│   │                                               Preferred path. Prose is reworded every
│   │                                               call, so never grade from `diagnosis`.
│   │                            CLAIM_PATHS      — dotted-path fallback for flat JSON.
│   │
│   ├── oracles/                 ─── GROUND TRUTH (the rigor lives here) ───
│   │   ├── errors.py            ★ OracleUnavailable + is_cert_verification_error().
│   │   │                        The verdict/unavailable boundary — read this first.
│   │   ├── dns.py               A records, MX, DNSSEC, SPF, DMARC via dnspython
│   │   │                        NXDOMAIN/NoAnswer = verdict; everything else raises
│   │   ├── tls.py               Real handshakes: chain_valid, expired, hostname_match,
│   │   │                        not_after, issuer. Only cert-verify failures are verdicts.
│   │   ├── http.py              status code, verified-HTTPS reachability
│   │   ├── packs/               ★★ DOMAIN PACKS — 41 oracles. The generator's vocabulary.
│   │   │   ├── __init__.py      OracleSpec: key, fn, description, input kind (domain|url),
│   │   │   │                    return type, default comparator, known-negative targets.
│   │   │   │                    The description is what the LLM actually reads.
│   │   │   ├── network.py       13 — the original DNS/TLS/HTTP/email oracles, unchanged
│   │   │   ├── web.py           23 — title, meta description, h1 count, canonical, JSON-LD,
│   │   │   │                    Open Graph, viewport, lang, charset, robots.txt,
│   │   │   │                    sitemap.xml, llms.txt, redirects, status, text length.
│   │   │   │                    One cached fetch + stdlib html.parser per URL. No new deps.
│   │   │   └── whois.py         5 — RDAP (RFC 9083): registered, created, expires,
│   │   │                        registrar, nameservers. 404 = verdict; anything else
│   │   │                        is OracleUnavailable, never a failure.
│   │   └── registry.py          PACKS → SPECS → ORACLES (claim → function), LRU-cached.
│   │                            catalog() renders the specs for the generator prompt.
│   │
│   ├── assertions/              ─── TESTS AND GRADING ───
│   │   ├── fixtures.py          The hand-written REGRESSION suite (dnsdoc-specific) plus
│   │   │                        generic_assertions(): agent-agnostic schema/consistency/
│   │   │                        quality tests that ride along with any generated suite.
│   │   ├── drift.py             ★ Three-way claim drift: agent card vs trust card vs
│   │   │                        registry entry (A2A url, version, ansName, functions vs
│   │   │                        skills, protocols, display name). Was vacuous; now real.
│   │   ├── router.py            claim → Kind (oracle > schema > consistency > quality)
│   │   ├── compare.py           comparators: eq / bool / set_eq / set_overlap /
│   │   │                        contains / date_close / nonempty
│   │   ├── quality.py           Tier-3 classical ML: Flesch readability, grounding
│   │   │                        (did it invent values?), actionability. NO LLM.
│   │   └── runner.py            ★ Executes assertions. Response caching lives here.
│   │
│   ├── generator/
│   │   └── graph.py             ★★★ THE LANGGRAPH AGENT — the reason this project exists.
│   │                            parse_claims → route_claims → generate_tests →
│   │                            validate_tests → report_coverage
│   │                            Point it at ANY agent card and it writes that agent's
│   │                            tests. Nodes are closures over an injected llm, so the
│   │                            whole graph is unit-testable offline with a fake.
│   │                            validate_tests() + report_coverage() are pure functions.
│   │
│   └── report/
│       ├── builder.py           Assembles Report, computes behavior_score,
│       │                        sorts failures HIGH → LOW
│       └── emit.py              POSTs observations to agent-trust-discovery's
│                                /v1/internal/observations/import
│
├── fixtures/                    ─── OFFLINE MODE DATA ───
│   ├── mock_registry_search.json    fake ANS search result
│   ├── mock_tl_entry.json           fake transparency log entry
│   ├── mock_agent_card.json         realistic A2A agent card
│   ├── mock_trust_card.json         has an EXTRA function → triggers claim-drift finding
│   └── mock_responses.json      ★ the fake agent's answers, WITH 3 DELIBERATE LIES
│
├── tests/                       57 offline unit tests — pytest -q (no network, no API key)
│   ├── test_compare.py          comparator semantics
│   ├── test_compare_strict.py   ★ unreadable values must never become a verdict
│   ├── test_oracle_unavailable.py ★ the verdict/unavailable boundary, failures injected
│   ├── test_adapter.py          JSON + regex extraction
│   ├── test_quality.py          classical ML scoring
│   ├── test_report.py           behavior_score math, failure ordering
│   └── test_generator_claims.py ★ the whole generator against a FakeLLM: three-way
│                                classification, hallucinated oracle keys falling through
│                                to UNVERIFIABLE, garbage model output never raising,
│                                comparator coercion, coverage math, loud key failure
│
└── out/                         JSON reports land here (gitignored except .gitkeep)
    └── latest.json              most recent run
```

**Reading order for a new teammate:** `models.py` → `pipeline.py` → `oracles/errors.py`
→ `assertions/fixtures.py`
→ `oracles/registry.py` → `agent/adapter.py`. That's about 20 minutes and you'll understand
the whole system.

---

## 6. Core concepts you must understand before editing

### The Assertion is the unit of work

Everything is an `Assertion` (see `bench/models.py`). One test, one claim, one verdict.

```python
Assertion(
    id="tls-expired-chain",
    kind=Kind.ORACLE,                          # how it gets graded
    claim="tls.chain_valid",                   # capability under test
    input={"domain": "expired.badssl.com"},
    oracle="tls.chain_valid",                  # key into ORACLES
    comparator="bool",
    expected_override=False,                   # pin known truth (optional)
    severity=Severity.HIGH,
)
# filled at runtime: expected, actual, passed, score, evidence, latency_ms, error
```

### Four kinds, in order of confidence

| Kind | Graded by | Can it fail the agent? | Count |
|---|---|---|---|
| `ORACLE` | We compute the true answer independently | **Yes** | 20 |
| `SCHEMA` | Contract conformance, reachability, claim drift | **Yes** | 4 |
| `CONSISTENCY` | Same input twice → same answer | **Yes** | 1 |
| `QUALITY` | Classical ML (readability/grounding) | **Never** | 1 |

### The router

`bench/assertions/router.py` decides which kind a claim gets. **Prefer oracle always.**
Fall through only when no independent truth exists. This is the architecture, not a
fallback ladder.

### Oracles vs the agent

This is the whole point. We never ask "does this answer look right?" We compute the right
answer ourselves — with `dnspython` and a real `ssl` handshake — and compare. An agent that
reports `expired.badssl.com` as having a valid chain is **provably** wrong. Not debatable.

`badssl.com` exists precisely for this and gives free unambiguous ground truth.

---

## 7. The five design rules

Say these to judges. They're what separate this from "an LLM grading an LLM."

1. **Oracle first.** Every factual assertion's `expected` is computed independently at
   runtime — real DNS queries, real TLS handshakes against system roots.
2. **An oracle that can't compute truth never penalizes the agent.** `OracleUnavailable`
   → assertion is **skipped** (`passed=None`), excluded from pass rates. Our network
   problem is not their competence problem.
   The boundary is explicit: a **cert verification failure is a verdict** about the target;
   a timeout, dead hostname or refused connection is **not**. `httpx` reports both as
   `ConnectError`, so `is_cert_verification_error()` walks the cause chain to tell them
   apart. The same rule applies to the agent's own probes — if *its* HTTPS check times out,
   that is ungraded too, not a wrong answer.
3. **Quality is classical ML, labelled low-confidence, and structurally cannot flip
   pass/fail.** It contributes 10% to `behavior_score` and nothing else. No LLM judge.
4. **Generated tests are additive and flagged.** Hand-written ones always run. The
   generator can only propose tests that map onto an oracle we already have.
5. **Every failure carries evidence** — which oracle, expected, actual, one line of why.
   The Trust Index reference implementation states pedagogy is its top goal; we match that.
   Corollary: a value we cannot read is reported as unreadable, never coerced. The `bool`
   comparator uses a curated vocabulary and flags anything outside it — an agent saying
   "certificate validation failed" must not be graded as if it had said "valid".

---

## 8. CLI reference

```bash
python -m bench selftest
    Run 9 known-truth checks against badssl.com and friends. If this fails, your
    environment (corporate proxy, DNS filter, custom roots) is lying and NO score
    from this machine can be trusted. ALWAYS RUN FIRST.

python -m bench run [--live] [--suite S] [--emit] [--host H] [--config config.yaml]
    The pipeline. Default is offline (mock agent, live oracles).
    --live          hit the real ANS registry and the real agent
    --host H        benchmark ANY agent, not just dnsdoc. Nothing is hardcoded.
    --suite generated   (DEFAULT) LangGraph reads the target's card and writes the
                        tests. Requires ANTHROPIC_API_KEY — if it is missing this
                        FAILS LOUDLY. It used to skip silently, which is how a
                        hardcoded 26-test suite passed for the real product.
    --suite regression  the hand-written DNS/TLS suite only. No API key, no LLM.
                        Only meaningful against dnsdoc — see §17.
    --suite both        run both, merged into one report
    --no-gen        alias for --suite regression (prints a warning)
    --emit          POST observations to agent-trust-discovery

python -m bench oracles
    Print the oracle catalog: every claim key, its input kind (domain|url), return
    type, default comparator and description. This is verbatim what the generator
    may choose from — a key not in this list can never back a test.

python -m bench probe-registry --live
    Dump the raw ANS search result + transparency log entry.
    RUN THIS BEFORE --live so you can fix field names in bench/ans/registry.py.

python -m bench probe-agent --live [--domain expired.badssl.com]
    Dump the raw agent card, trust card, one raw response, and show what
    extract() pulls out of it for each claim.
    RUN THIS BEFORE --live on a NEW agent, so you can fix extraction in adapter.py.

python -m bench list
    Print all hand-written assertions (the regression suite).
```

---

## 9. Report format

`out/latest.json` — values below are from a real `--live` run:

```jsonc
{
  "agent": "ans://v1.0.6.dnsdoc.webmesh.ai",
  "target_host": "dnsdoc.webmesh.ai",
  "mode": "live",                     // or "mock"
  "identity": {
    "registry_found": true,
    "tl_entry_found": true,
    "tl_server_fingerprint": "…",
    "live_server_fingerprint": "…",
    "fingerprint_match": true,
    "verified": true,
    "notes": []
  },
  "cards": { "declared_skills": [...], "declared_protocols": [...] },
  "assertions": [ /* every assertion with expected/actual/evidence */ ],
  "quality": { "readability": 58.2, "grounding": 0.33, "confidence": "low" },
  "summary": {
    "assertions_run": 26,
    "by_kind": {"oracle": 20, "schema": 4, "consistency": 1, "quality": 1},
    "oracle_pass_rate": 0.9,
    "schema_pass_rate": 1.0,
    "quality_blend": 0.385,
    "high_severity_failures": 0
  },
  "behavior_score": 87,
  "failures": [ /* HIGH severity first */ ],
  "explanation": "17/19 oracle assertions passed; identity VERIFIED via ANS."
}
```

**behavior_score** = `0.70 · oracle_pass_rate + 0.20 · schema_pass_rate + 0.10 · quality_blend`
(weights in `config.yaml`). Ungraded assertions are excluded from the rates — note
`assertions_run` is 26 but only 19 oracle assertions were *graded*; the rest were skipped
because ground truth (or the agent's own probe) was unavailable.

---

## 10. Going live — exact order

Steps 2 and 3 are **done** — the endpoints and field names below are confirmed against the
live services. They are kept here because you repeat them when pointing at a *new* agent.

```bash
# 1. Is this machine honest?
python -m bench selftest

# 2. What does the ANS registry actually return?   [CONFIRMED for dnsdoc]
python -m bench probe-registry --live
#    search_base = https://api.godaddy.com   (registry.ans.godaddy.com does NOT resolve)
#    envelope is {"items": [...]}, host field is `agentHost`
#    TL fingerprint lives at payload.producer.event.attestations.serverCert.fingerprint

# 3. What does the agent actually return?          [CONFIRMED for dnsdoc]
python -m bench probe-agent --live
#    dnsdoc returns {domain, diagnosis, evidence}. Grade from `evidence` (structured),
#    never from `diagnosis` (LLM prose — it is reworded on every call).

# 4. Enable the LangGraph generator. Keys come from env/.env only — never the repo.
cp .env.example .env && echo "ANTHROPIC_API_KEY=sk-..." >> .env
#    bench/llm.py load_dotenv()s this. A missing key is now a HARD ERROR in generated
#    mode, not a silent skip. The silent skip is how a hardcoded suite scored 87.

# 5. The real run — generated suite is the default
python -m bench run --live
python -m bench run --live --host impact.webmesh.ai   # any agent, nothing hardcoded

# 6. Push into the Trust Index (requires agent-trust-discovery running on :8080)
git clone https://github.com/agentnameservice/agent-trust-discovery
cd agent-trust-discovery && make demo      # boots on :8080, no auth
python -m bench run --live --emit
```

---

## 11. Where the human edits go

Grep the codebase for `HUMAN` — every remaining unconfirmed value is marked.

| Priority | File | What to fix |
|---|---|---|
| **1** | `bench/report/emit.py` | Observation payload, once the Go `port.Signal` impls exist. **The only thing between a real score and the Trust Index.** |
| **2** | `bench/ans/identity.py` → `TODO(b)` | Validate the identity cert. The `x5c` chain and the `ans://` URI SAN are already in the trust card; the Merkle proof is already in the TL response. Both currently ignored. |
| **3** | `bench/assertions/runner.py` → `drift.card_vs_trust` | Looks for `trust_card["functions"]`, which the real trust card does not have (it has `endpoints`, `keys`). The test therefore **always passes** — claim drift is currently undetectable. |
| 4 | `bench/agent/adapter.py` → `_from_evidence` | Per-agent response shapes. Confirmed for `dnsdoc`; redo for any new target. |
| 5 | `bench/agent/transport.py` → `MCPTransport` | Untested and wrong: uses the A2A url not `/mcp`, sends the whole prompt as the `domain` arg, no `initialize` handshake, does not parse event streams. |
| 6 | `config.yaml` → `target.endpoint`, `transport.mcp_tool` | If the card doesn't expose `url`, or you use MCP |

---

## 12. How to add a new capability test

Three edits, all small:

```python
# 1. bench/oracles/registry.py — teach us the true answer
ORACLES["dns.caa"] = _dns.caa

# 2. bench/agent/adapter.py — teach us to read the agent's answer.
#    Preferred: a branch in _from_evidence() reading the agent's STRUCTURED block.
#    CLAIM_PATHS is the fallback for agents that return flat JSON.
if claim == "dns.caa":
    return bool(dns.get("CAA")) if dns else None

# 3. bench/assertions/fixtures.py — the test itself
_o("dns-caa-present", "dns.caa", "cloudflare.com", M, "bool"),
```

That's it. The router, runner, comparator, report and score all pick it up automatically.

**Two rules for the new oracle:** it must `raise OracleUnavailable` when it cannot compute
truth (never return `None`, never a default), and it must return a real `bool`/`list`/`int`
— not a prose string the comparator would have to guess at.

---

## 13. Build status — what is real vs unproven

Be honest about this in the demo. Judges penalize overclaiming, not simulation.

| Component | Code | Proven? |
|---|---|---|
| Oracles — network pack (13) | ✅ | ✅ ran live; `selftest` 9/9; verdict/unavailable boundary unit-tested |
| Oracles — web pack (23) | ✅ | ✅ smoke-tested live against `example.com` and `seo.webmesh.ai` |
| Oracles — whois/RDAP pack (5) | ✅ | ✅ live against `badssl.com` (created 2015-04-07, MarkMonitor) |
| Assertions + comparators + scoring | ✅ | ✅ 57 unit tests pass |
| Report + JSON output | ✅ | ✅ |
| Mock pipeline end-to-end | ✅ | ✅ runs clean, finds 2 HIGH failures |
| **ANS registry search** | ✅ | ✅ **live** — discovers `dnsdoc` by host at `api.godaddy.com` |
| **ANS transparency log** | ✅ | ✅ **live** — sealed `serverCert.fingerprint` parsed |
| **TLS fingerprint drift check** | ✅ | ⚠️ **match** proven live; a **mismatch** has never been observed (see below) |
| **A2A transport** | ✅ | ✅ **live** — real responses from `dnsdoc.webmesh.ai` |
| **Live pipeline end-to-end** | ✅ | ✅ `behavior_score=87`, identity VERIFIED, 0 HIGH failures |
| Claim-drift detection | ✅ | ✅ **fixed** — real three-way diff; fires on the mock, clean on 3 live agents |
| Identity: 4 states (VERIFIED/PENDING/MISMATCH/NOT_FOUND) | ✅ | ✅ VERIFIED and NOT_FOUND seen live; MISMATCH still never observed |
| Paywalled/auth-walled agents | ✅ | ✅ **live** — `seo.webmesh.ai` returns HTTP 402 (x402). Left **ungraded**, never scored as incompetence |
| **Generator: writes tests for an arbitrary agent** | ✅ | ✅ **live** — 29 tests for `dnsdoc` (score 80), 28 for `seo`, from the cards alone |
| Self-claim vs capability split | ✅ | ✅ **live** — `impact` went from 26/100 with 9 false HIGH failures to 0 failures |
| Coverage report (VERIFIABLE/SCHEMA_ONLY/UNVERIFIABLE) | ✅ | ✅ **live** on 3 agents: 65% / 61% / 22% |
| INSUFFICIENT_COVERAGE instead of a meaningless number | ✅ | ✅ **live** — fires on `impact` (nothing checkable) and `seo` (paywalled) |
| MCP transport | ⚠️ | ❌ never hit a real agent, and known wrong (see §11) |
| Benchmark server + web UI | ❌ | not started (phase 2/3) |
| Trust Index emit | ✅ | ❌ payload shape is still a guess |
| Identity-cert / Merkle-proof validation | ❌ | stubbed — but the `x5c` chain and proof are already in responses we fetch |
| Go `port.Signal` impls | ❌ | not started |

**On the drift check:** we have proven the *positive* case — the live TLS fingerprint for
`dnsdoc.webmesh.ai` equals the one sealed in the transparency log. We have never seen the
check *fail*, because we do not control any registered agent's certificate. Registering our
own agent would let us rotate a cert and demonstrate drift firing. Say this plainly if asked.

**The bug that mattered most:** the first version of this project had 26 tests hardcoded for
one agent and treated the generator as an optional add-on that skipped silently when no API
key was present. It scored a real agent 87/100 with the generator having never once run. That
is the same self-declaration problem ANS exists to fix, reproduced inside the tool meant to
fix it. The inversion is now the other way round: the generated suite is the default, the
hand-written suite is a named regression fallback, and a missing key is a hard error.

**Honesty note:** two correctness bugs were found and fixed after the first live run, and
both had produced wrong verdicts in *both* directions. Oracles were reporting network
failures as facts about the target, and the `bool` comparator was coercing any unrecognised
string to `True` — so an agent answering "certificate validation failed" was graded as if it
had said "valid". Any score produced before those fixes should be discarded.

---

## 14. What's left to build

Roughly in priority order:

- [x] ~~Run `probe-registry --live` and `probe-agent --live`, fix field names~~
- [x] ~~Verify `selftest` passes on a clean laptop~~ (needs the CA-store fix — see §16)
- [x] ~~Get one real `--live` run producing a real `behavior_score`~~ → **87**
- [ ] **Fork `agent-trust-discovery`, write 3 Go signals under the `behavior` dimension:**
      `capabilityaccuracy`, `capabilitycoverage`, `responseintegrity` ← **do this first now**
- [ ] Add weights for them in `config/default-profile.yaml`
- [ ] Align `emit.py` payload with those signals; demo behavior going 0 → 87
- [ ] Fix claim-drift detection — compare agent-card skills vs trust-card `endpoints` vs
      registry metadata. Currently always passes, so drift is undetectable.
- [ ] Verify the Merkle inclusion proof (TL returns leafHash / leafIndex / path / signed
      root; we ignore all of it). `ans-verify` in the `ans` repo does this — RFC 6962
      `SHA-256(0x00 || payload)`.
- [ ] Identity-cert validation (`TODO(b)` in `ans/identity.py`) — the trust card ships the
      `x5c` chain from GoDaddy's Private ANS Issuing CA with the `ans://` URI SAN embedded
- [ ] Register our own agent with production ANS, so we can rotate a cert and show the
      drift check *failing*
- [ ] Sweep the fleet — 484 agents are discoverable, 30 outside the two bulk domains serve
      an agent card. Legit agents should score well, `rogue-supplier.webmesh.ai` badly.
      Note: the rogue agent fails on *spending-mandate* rules, which our oracles cannot
      check — it needs new oracles, or score it on schema/consistency only.
- [ ] Open live finding: `dns.dnssec` extraction fails on both assertions — the agent never
      reports DNSSEC although the prompt asks for it. Decide: genuine agent coverage gap
      (keep as a failure) or out of scope (skip it)?
- [ ] `--scripted` fallback that replays cached responses, so a wifi failure mid-demo
      doesn't kill the pitch

---

## 15. Team split suggestion

Three roughly independent workstreams with clean interfaces:

| Owner | Scope | Files |
|---|---|---|
| **A — Go / Trust Index** | ← **now the critical path.** Fork `agent-trust-discovery`, run `make demo`, write the 3 behavior signals, wire the import. Nothing else closes the loop. | separate repo + `report/emit.py` |
| **B — ANS crypto depth** | Merkle proof verification, identity-cert `x5c` validation, fix claim drift, register our own agent to demo drift firing | `ans/identity.py`, `ans/registry.py`, `assertions/runner.py` |
| **C — Coverage + demo** | Fleet sweep, more oracles and assertions, `--scripted` mode, rehearsal | `oracles/*`, `assertions/fixtures.py` |

All three are independent — A and B touch different repos, and C only adds tests. ~~Live
integration~~ is done; that work is now folded into B.

---

## 16. Troubleshooting

**`selftest` says `cloudflare.com` chain is NOT valid** ← most common setup failure
Your Python has no CA trust store, so every verified handshake fails and the oracle calls
good sites broken. Happens with python.org builds on macOS, which ship their own OpenSSL and
expect a separate cert step. Either:

```bash
/Applications/Python\ 3.x/Install\ Certificates.command      # the python.org way
# or, inside the venv:
pip install certifi && export SSL_CERT_FILE=$(python -c "import certifi; print(certifi.where())")
```

To make it permanent, append that `export` to `.venv/bin/activate` (use `$VIRTUAL_ENV` for
the path so it survives moving the repo). Note `.venv/` is gitignored, so **every teammate
fixes this on their own machine** — it does not travel with a `git pull`.

**`selftest` says `expired.badssl.com` has a valid chain**
Your network is MITM-ing TLS (corporate proxy, Zscaler, some VPNs). The oracle is lying.
Switch networks or use a personal hotspot. **Do not trust any score until selftest passes.**

**Every oracle assertion fails with "extraction failed"**
`extract()` returned `None` — the agent's response shape isn't handled in `adapter.py`.
Run `python -m bench probe-agent --live`, look at the raw response, and add a branch to
`_from_evidence()` (or fix `CLAIM_PATHS` if the agent returns flat JSON). Expected on first
contact with any new agent.

**`OracleUnavailable: DNS TXT ...: LifetimeTimeout`**
DNS query timed out (we already retry over TCP). The assertion is *skipped*, not failed —
correct behavior. If it's persistent, your resolver is blocking TXT lookups.

**"No endpoint" error on `--live`**
The agent card has no `url` field. Set `target.endpoint` in `config.yaml` manually.

**Generator produces nothing**
`ANTHROPIC_API_KEY` isn't set, or every proposal was rejected by `validate()`. Check
`summary.notes` in the report — the reason is recorded there. The pipeline continues either way.

**`registry_found: false`**
The search API returned nothing for the host. Check `search_base` is `https://api.godaddy.com`
(the older `registry.ans.godaddy.com` does not resolve). Note the search is **fuzzy and
ranked** — a query for `dnsdoc.webmesh.ai` returns ~20 agents with the target well down the
list, so match `agentHost` exactly and follow the `next` page token rather than assuming the
first hit is right. `host=` and `agentHost=` filters return HTTP 400; they are not supported.
Run `probe-registry --live` to see the raw response.

**An assertion says `could not interpret '...' as true/false`**
Extraction returned something the `bool` comparator will not guess at. This is deliberate —
fix the extraction in `adapter.py` so it returns a real boolean. Do **not** widen the
comparator's vocabulary to make it pass.

**Everything is skipped (`passed=None`) rather than graded**
Ground truth could not be computed. Check `summary` and each assertion's `error`: a resolver
outage, no network, or a blocked port will skip the whole oracle tier. That is correct
behaviour, but it means the score is measuring nothing — the pass rates exclude skips.

---

## 17. Suites: generated vs regression

There are two suites and the difference is the whole point of the project.

### `--suite generated` (the default)

The LangGraph agent reads the target's `agent-card.json` and `trust-card.json`, enumerates
the capabilities the agent claims *in the agent's own words*, and writes tests for them. It
knows nothing about DNS, or about dnsdoc, or about any particular agent. Point it at a
booking agent and it writes booking-agent tests — or, more honestly, it tells you it cannot,
and that answer is itself the finding.

The LLM is never a judge and never the last word:

| The LLM does | Deterministic code does |
|---|---|
| lists the claims it reads in the card | nothing — the claims are the agent's, quoted |
| proposes an oracle key per claim | **requires the key to exist in `ORACLES`**, else the claim is UNVERIFIABLE |
| proposes test inputs and prompts | validates the target, coerces the comparator to the oracle's return type, dedupes, caps |
| reads the agent's reply for a value | requires a verbatim quote from the reply, else discards the value |
| — | computes `expected` from the oracle. Always. Never the model. |

A hallucinated oracle key cannot become a passing test. It becomes an UNVERIFIABLE claim
with the invented key named in the reason.

### Two kinds of claim, and never confuse them

An agent's self-description mixes two things that need completely different treatment:

| | SELF claim | CAPABILITY claim |
|---|---|---|
| example | "exposes an A2A endpoint at `https://x`", "publishes a trust card at `Y`", "supports DNSSEC" | "given a domain, reports whether its certificate is expired" |
| about | the agent's own infrastructure | work the agent does on an input we supply |
| how we check it | probe it ourselves with an oracle | call the agent, grade its answer against an oracle |
| is the agent called? | **never** | always |
| `expected` / `actual` | what the card CLAIMS / what we MEASURED | what the oracle computed / what the agent SAID |

Conflating them is not a cosmetic bug. The first version of this asked
`impact.webmesh.ai` — a domain-takedown risk scorer — for the TLS status of
`expired.badssl.com`, because *impact's own card* claims it presents an ANS identity
certificate. It scored **26/100 with nine HIGH-severity failures**, every one of them
measuring nothing but our own category error. After the split it has **zero** failures.

`Kind.SELFCLAIM` assertions never touch the transport, and `validate_tests` rejects any
capability test aimed at the agent's own host. A self-claim may only probe infrastructure
the agent actually owns — its host, its parent zone, or a full URL it publishes itself.

### Coverage is a result, not a diagnostic

Every claim lands in exactly one bucket:

- **VERIFIABLE** — an oracle in `ORACLES` computes the true answer independently. Gradable.
- **SCHEMA_ONLY** — we can check the shape of the answer but not its truth
  ("returns a prioritized list"). Worth testing, worth labelling as weaker evidence.
- **UNVERIFIABLE** — nobody can objectively check it ("provides expert guidance").

`coverage_ratio = verifiable / claims_found`. A low ratio is not a failure of the benchmark,
it is a finding about the agent. `dnsdoc` declaring a single opaque skill called *diagnose*
is exactly the self-declaration problem this project exists to expose: an agent can claim
anything, and until somebody computes the answer independently, nothing in the registry
distinguishes a good one from a confident one.

### When there is no score

`behavior_score` is a *behaviour* score. If not one capability assertion was graded, a
number would be built entirely out of the agent's endpoint being up and its prose being
readable — which is the self-declaration problem again, one level up. So the report says
`INSUFFICIENT_COVERAGE`, `behavior_score` is `null`, and `score_basis` names the cause:

```
impact.webmesh.ai  INSUFFICIENT_COVERAGE
  none of the 15 capability claims this agent makes maps onto an oracle — 12 of 23
  declared claims are unverifiable by anyone. Its 4 self-claims about its own
  infrastructure were checked directly and 4 held.

seo.webmesh.ai     INSUFFICIENT_COVERAGE
  the agent refused to serve us (HTTP 402 payment required), so none of the 29
  capability tests written for it could be graded. Its 3 self-claims held.
```

Those two lines say more about an agent than any number would, and they say it honestly.

### `--suite regression`

The 26 hand-written DNS/TLS/HTTP/email assertions. They are specific to `dnsdoc.webmesh.ai`
and they are kept for exactly two reasons:

1. **They are the control.** They are known-good, human-authored, and they have caught real
   bugs. When the generated suite disagrees with them on the same agent, one of the two is
   wrong and that is worth knowing.
2. **They run with no API key and no LLM**, so `make run-live` always works in a demo.

Do not read a regression score for an agent other than dnsdoc. It will test DNSSEC on a
booking agent and report an honest "I don't do that" as a failure.

`--suite both` runs them together and merges the report.

### Agents that refuse to answer

`seo.webmesh.ai` gates its A2A endpoint behind x402 (USDC on Base) and answers `HTTP 402`.
An agent declining to serve us is **not** a competence verdict. `AgentUnavailable` marks
every oracle assertion `passed=None` (ungraded, excluded from the pass rate) and fails only
`endpoint.reachable`, with the refusal reason as evidence. The same path covers 401/403.
The card is still read and coverage is still reported — you learn what the agent claims and
how much of it is checkable, you just do not learn whether it is telling the truth.
