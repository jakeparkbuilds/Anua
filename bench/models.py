"""Core data model. The Assertion is the unit of work for the whole system."""
from __future__ import annotations
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, Field


class Kind(str, Enum):
    ORACLE = "oracle"            # CAPABILITY: ask the agent, grade its answer vs an oracle
    SELFCLAIM = "selfclaim"      # the agent's claim about its OWN infrastructure.
                                 # We check it directly with an oracle. The agent is
                                 # NEVER called: asking a takedown-risk scorer for the
                                 # TLS status of its own endpoint tests nothing but our
                                 # own confusion, and its confusion scored as failure.
    SCHEMA = "schema"            # contract conformance / reachability / coverage
    CONSISTENCY = "consistency"  # same input twice -> same output
    QUALITY = "quality"          # explainability etc. — classical ML / LLM, never flips pass/fail


class Severity(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"


class Verifiability(str, Enum):
    VERIFIABLE = "VERIFIABLE"        # maps onto an oracle we have -> tests generated
    SCHEMA_ONLY = "SCHEMA_ONLY"      # checkable only as contract/reachability/drift
    UNVERIFIABLE = "UNVERIFIABLE"    # no independent truth exists -> a coverage finding


class Assertion(BaseModel):
    """One test. `expected` is filled by the oracle at runtime unless overridden."""
    id: str
    kind: Kind
    claim: str                       # capability under test, e.g. "tls.chain_valid"
    input: dict[str, Any]            # {"domain": "expired.badssl.com"} or {"url": "https://..."}
    prompt: Optional[str] = None     # what we send the agent; None -> adapter default template
    oracle: Optional[str] = None     # key into oracles.registry.ORACLES
    comparator: str = "eq"           # eq | set_eq | bool | contains | date_close | nonempty
    expected_override: Optional[Any] = None
    severity: Severity = Severity.MEDIUM
    generated: bool = False
    rationale: str = ""

    # filled at runtime
    expected: Optional[Any] = None
    actual: Optional[Any] = None
    raw_response: Optional[str] = None
    passed: Optional[bool] = None
    score: float = 0.0
    evidence: str = ""
    latency_ms: Optional[int] = None
    error: Optional[str] = None


class AgentIdentity(BaseModel):
    host: str
    ans_name: Optional[str] = None
    ans_id: Optional[str] = None
    registry_found: bool = False
    tl_entry_found: bool = False
    tl_server_fingerprint: Optional[str] = None
    live_server_fingerprint: Optional[str] = None
    fingerprint_match: Optional[bool] = None
    identity_cert_uri_san: Optional[str] = None   # TODO(b): validate identity cert
    verified: bool = False
    # VERIFIED | PENDING (TL entry, no sealed cert yet) | MISMATCH | NOT_FOUND | UNVERIFIED
    status: str = "UNVERIFIED"
    notes: list[str] = Field(default_factory=list)


class AgentCards(BaseModel):
    agent_card: dict[str, Any] = Field(default_factory=dict)
    trust_card: dict[str, Any] = Field(default_factory=dict)
    registry_entry: dict[str, Any] = Field(default_factory=dict)   # ANS search hit (registration metadata)
    endpoint: Optional[str] = None
    declared_skills: list[str] = Field(default_factory=list)
    declared_protocols: list[str] = Field(default_factory=list)


class QualityScores(BaseModel):
    readability: Optional[float] = None
    grounding: Optional[float] = None
    actionability: Optional[float] = None
    confidence: str = "low"
    method: str = "classical"


class CapabilityClaim(BaseModel):
    """One discrete thing the agent says it can do, as extracted from its self-descriptions."""
    claim_text: str
    source: str = "description"      # skill | description | trust_card | registry
    # "self"       — about the agent's OWN infrastructure ("exposes an A2A endpoint at X",
    #                "publishes a trust card at Y", "supports DNSSEC"). Verified directly.
    # "capability" — about what the agent DOES for a caller. Only these go through the agent.
    about: str = "capability"
    verifiability: Verifiability = Verifiability.UNVERIFIABLE
    oracle_key: Optional[str] = None
    reason: str = ""


class CoverageReport(BaseModel):
    """How much of what the agent declares can be objectively checked. A first-class result:
    an agent declaring capabilities nobody can verify is itself a finding."""
    claims_found: int = 0
    verifiable: int = 0
    schema_only: int = 0
    unverifiable: int = 0
    claims: list[CapabilityClaim] = Field(default_factory=list)
    coverage_ratio: float = 0.0      # verifiable / claims_found
    self_claims: int = 0             # claims about the agent's own infrastructure
    capability_claims: int = 0       # claims about what it does for a caller
    tests_generated: int = 0
    selfclaim_tests: int = 0
    capability_tests: int = 0
    notes: list[str] = Field(default_factory=list)


class Report(BaseModel):
    agent: str
    target_host: str
    run_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    mode: str = "mock"
    identity: AgentIdentity
    cards: AgentCards
    assertions: list[Assertion]
    quality: QualityScores
    coverage: Optional[CoverageReport] = None
    suite: str = "regression"        # generated | regression | both
    summary: dict[str, Any] = Field(default_factory=dict)
    # None when we graded nothing about the agent's BEHAVIOR. A behavior score computed
    # from zero behavior evidence is not a low score, it is not a score.
    behavior_score: Optional[int] = None
    score_status: str = "OK"         # OK | INSUFFICIENT_COVERAGE
    score_basis: str = ""            # what the number is actually made of
    failures: list[dict[str, Any]] = Field(default_factory=list)
    explanation: str = ""
