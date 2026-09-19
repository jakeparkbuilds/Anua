"""Core data model. The Assertion is the unit of work for the whole system."""
from __future__ import annotations
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, Field


class Kind(str, Enum):
    ORACLE = "oracle"            # independently computable ground truth
    SCHEMA = "schema"            # contract conformance / reachability / coverage
    CONSISTENCY = "consistency"  # same input twice -> same output
    QUALITY = "quality"          # explainability etc. — classical ML / LLM, never flips pass/fail


class Severity(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"


class Assertion(BaseModel):
    """One test. `expected` is filled by the oracle at runtime unless overridden."""
    id: str
    kind: Kind
    claim: str                       # capability under test, e.g. "tls.chain_valid"
    input: dict[str, Any]            # e.g. {"domain": "expired.badssl.com"}
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
    notes: list[str] = Field(default_factory=list)


class AgentCards(BaseModel):
    agent_card: dict[str, Any] = Field(default_factory=dict)
    trust_card: dict[str, Any] = Field(default_factory=dict)
    endpoint: Optional[str] = None
    declared_skills: list[str] = Field(default_factory=list)
    declared_protocols: list[str] = Field(default_factory=list)


class QualityScores(BaseModel):
    readability: Optional[float] = None
    grounding: Optional[float] = None
    actionability: Optional[float] = None
    confidence: str = "low"
    method: str = "classical"


class Report(BaseModel):
    agent: str
    target_host: str
    run_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    mode: str = "mock"
    identity: AgentIdentity
    cards: AgentCards
    assertions: list[Assertion]
    quality: QualityScores
    summary: dict[str, Any] = Field(default_factory=dict)
    behavior_score: int = 0
    failures: list[dict[str, Any]] = Field(default_factory=list)
    explanation: str = ""
