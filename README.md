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
      verified = registry_found AND tl_found AND fingerprint_match

[2/6] FETCH SELF-DESCRIPTIONS             bench/agent/card.py
      • /.well-known/agent-card.json  → declared skills, endpoint, protocols
      • /.well-known/ans/trust-card.json → declared functions

[3/6] GENERATE EXTRA TESTS (optional)     bench/generator/graph.py
      LangGraph: read_card → propose → validate → END
      An LLM proposes additional test inputs; validate() throws out anything
      we cannot objectively grade. Survivors are flagged generated=true.

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

python -m bench selftest      # 1. MUST PASS — proves the oracles are honest here
python -m bench run --no-gen  # 2. offline run: mock agent, LIVE oracles
python -m bench run --live --no-gen   # 3. the real thing: live ANS + live agent
cat out/latest.json
```

**Offline run (step 2):** 26 assertions, `behavior_score` around 75, and **2 HIGH-severity
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
│   ├── __main__.py              ★ CLI entrypoint. 5 commands: run / selftest / probe-agent
│   │                              / probe-registry / list
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
│   │   └── registry.py          ORACLES dict: claim → function. LRU-cached per run.
│   │
│   ├── assertions/              ─── TESTS AND GRADING ───
│   │   ├── fixtures.py          ★ THE 26 HAND-WRITTEN ASSERTIONS. Add tests here.
│   │   ├── router.py            claim → Kind (oracle > schema > consistency > quality)
│   │   ├── compare.py           comparators: eq / bool / set_eq / set_overlap /
│   │   │                        contains / date_close / nonempty
│   │   ├── quality.py           Tier-3 classical ML: Flesch readability, grounding
│   │   │                        (did it invent values?), actionability. NO LLM.
│   │   └── runner.py            ★ Executes assertions. Response caching lives here.
│   │
│   ├── generator/
│   │   └── graph.py             ★ LANGGRAPH AGENT.
│   │                            StateGraph: read_card → propose → validate → END
│   │                            propose(): ChatAnthropic reads the card, proposes tests
│   │                            validate(): rejects anything ungradable. The guardrail.
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
├── tests/                       50 offline unit tests — pytest -q
│   ├── test_compare.py          comparator semantics
│   ├── test_compare_strict.py   ★ unreadable values must never become a verdict
│   ├── test_oracle_unavailable.py ★ the verdict/unavailable boundary, failures injected
│   ├── test_adapter.py          JSON + regex extraction
│   ├── test_quality.py          classical ML scoring
│   ├── test_report.py           behavior_score math, failure ordering
│   └── test_generator_validate.py   LangGraph validation guardrail
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

python -m bench run [--live] [--emit] [--no-gen] [--host H] [--config config.yaml]
    The pipeline. Default is offline (mock agent, live oracles).
    --live    hit the real ANS registry and the real agent
    --no-gen  skip the LangGraph generator (no API key needed)
    --emit    POST observations to agent-trust-discovery

python -m bench probe-registry --live
    Dump the raw ANS search result + transparency log entry.
    RUN THIS BEFORE --live so you can fix field names in bench/ans/registry.py.

python -m bench probe-agent --live [--domain expired.badssl.com]
    Dump the raw agent card, trust card, one raw response, and show what
    extract() pulls out of it for each claim.
    RUN THIS BEFORE --live on a NEW agent, so you can fix extraction in adapter.py.

python -m bench list
    Print all hand-written assertions.
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

# 4. Enable the LangGraph generator
cp .env.example .env && echo "ANTHROPIC_API_KEY=sk-..." >> .env
export $(cat .env | xargs)

# 5. The real run
python -m bench run --live

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
| Oracles (DNS/TLS/HTTP) | ✅ | ✅ ran live; `selftest` passes; verdict/unavailable boundary unit-tested |
| Assertions + comparators + scoring | ✅ | ✅ 50 unit tests pass |
| Report + JSON output | ✅ | ✅ |
| Mock pipeline end-to-end | ✅ | ✅ runs clean, finds 2 HIGH failures |
| **ANS registry search** | ✅ | ✅ **live** — discovers `dnsdoc` by host at `api.godaddy.com` |
| **ANS transparency log** | ✅ | ✅ **live** — sealed `serverCert.fingerprint` parsed |
| **TLS fingerprint drift check** | ✅ | ⚠️ **match** proven live; a **mismatch** has never been observed (see below) |
| **A2A transport** | ✅ | ✅ **live** — real responses from `dnsdoc.webmesh.ai` |
| **Live pipeline end-to-end** | ✅ | ✅ `behavior_score=87`, identity VERIFIED, 0 HIGH failures |
| Claim-drift detection | ✅ | ❌ **vacuous** — looks for a `functions` key the real trust card lacks |
| LangGraph graph structure + validation | ✅ | ⚠️ validation tested; LLM call still untested (no key used yet) |
| MCP transport | ⚠️ | ❌ never hit a real agent, and known wrong (see §11) |
| Trust Index emit | ✅ | ❌ payload shape is still a guess |
| Identity-cert / Merkle-proof validation | ❌ | stubbed — but the `x5c` chain and proof are already in responses we fetch |
| Go `port.Signal` impls | ❌ | not started |

**On the drift check:** we have proven the *positive* case — the live TLS fingerprint for
`dnsdoc.webmesh.ai` equals the one sealed in the transparency log. We have never seen the
check *fail*, because we do not control any registered agent's certificate. Registering our
own agent would let us rotate a cert and demonstrate drift firing. Say this plainly if asked.

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
