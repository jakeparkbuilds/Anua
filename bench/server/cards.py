"""Our own agent card and trust card.

These are written to be READ BY OUR OWN PIPELINE. A vague card is exactly what we
criticise in dnsdoc, so every sentence is a discrete, checkable claim, and the
self-claims (endpoint, published cards, health check) are ones this process satisfies
the moment it is up. What we cannot yet claim we do not: we do not hold the issued
certificate chain, so `keys` is empty and the registration state is stated as it is,
never padded.

Everything host-specific comes from the environment so the same code serves
localhost and anuabot.vip.
"""
from __future__ import annotations
import os

PUBLIC_URL = os.getenv("BENCH_PUBLIC_URL", "https://anuabot.vip").rstrip("/")
HOST = PUBLIC_URL.split("://", 1)[-1].split("/", 1)[0]
VERSION = "1.0.0"
ANS_NAME = os.getenv("BENCH_ANS_NAME", f"ans://v{VERSION}.{HOST}")
ANS_AGENT_ID = os.getenv("BENCH_ANS_AGENT_ID", "9f99e3fd-1dfb-4aa7-a31b-60480bcf72c1")
ANS_STATUS = os.getenv("BENCH_ANS_STATUS", "ACTIVE")
NAME = "Anua Benchmarker"
SKILL_ID = "benchmark_agent"

A2A_URL = f"{PUBLIC_URL}/a2a"
CARD_URL = f"{PUBLIC_URL}/.well-known/agent-card.json"
TRUST_URL = f"{PUBLIC_URL}/.well-known/ans/trust-card.json"
HEALTH_URL = f"{PUBLIC_URL}/health"

DESCRIPTION = (
    "Benchmarks ANS-registered AI agents. Given an agent's host or ans:// name, discovers the "
    "agent in the GoDaddy ANS registry, fetches its transparency-log entry, and compares the "
    "sealed server-certificate fingerprint to a live TLS handshake. Reads the agent's own A2A "
    "agent card and ANS trust card, extracts each declared capability claim, and writes tests "
    "for every claim an independent oracle can grade (DNS, TLS, HTTP, email, RDAP and web-page "
    "facts). Expected values are computed by oracles, never by a language model. Claims an "
    "agent makes about its own infrastructure are checked directly without calling the agent. "
    "Returns a behavior score with per-assertion evidence, or withholds the score as "
    "INSUFFICIENT_COVERAGE when no capability test could be graded, and reports how many of "
    "the agent's declared claims were verifiable at all. "
    f"Exposes an A2A JSON-RPC endpoint at {A2A_URL}. Publishes its agent card at {CARD_URL} "
    f"and its ANS trust card at {TRUST_URL}. Serves a health check at {HEALTH_URL}."
)

SKILL = {
    "id": SKILL_ID,
    "name": "Benchmark an ANS-registered agent",
    "description": (
        "Given an ANS-registered agent (a host such as dnsdoc.webmesh.ai, or an ans:// name), "
        "verifies its identity against the ANS transparency log and measures whether its "
        "declared capabilities actually work. Returns the identity status (VERIFIED, PENDING, "
        "MISMATCH or NOT_FOUND), a behavior score with per-assertion evidence or "
        "INSUFFICIENT_COVERAGE with the reason, a coverage report splitting declared claims "
        "into verifiable, schema-only and unverifiable, and the highest-severity failures. "
        "Results are cached per agent and the response states the run timestamp."
    ),
    "tags": ["benchmark", "ans", "a2a", "identity", "verification", "trust", "coverage"],
    "examples": [
        "Benchmark dnsdoc.webmesh.ai",
        "Benchmark ans://v1.0.6.dnsdoc.webmesh.ai",
        "How does impact.webmesh.ai score?",
        "Run the regression suite against dnsdoc.webmesh.ai",
    ],
    "inputModes": ["text/plain"],
    "outputModes": ["text/plain"],
}


def agent_card() -> dict:
    return {
        "name": NAME,
        "description": DESCRIPTION,
        "url": A2A_URL,
        "version": VERSION,
        "protocolVersion": "1.0",
        "provider": {"organization": "Anua", "url": PUBLIC_URL},
        "documentationUrl": PUBLIC_URL,
        "supportedInterfaces": [{"url": A2A_URL, "protocolBinding": "jsonrpc", "protocolVersion": "1.0"}],
        "protocols": ["a2a"],
        "capabilities": {"streaming": False, "pushNotifications": False, "extendedAgentCard": False},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [SKILL],
        "x-identity": {
            "ans": {"uri": ANS_NAME, "agentId": ANS_AGENT_ID, "status": ANS_STATUS},
            "trustCard": TRUST_URL,
        },
    }


def trust_card() -> dict:
    return {
        "ansName": ANS_NAME,
        "agentDisplayName": NAME,
        "version": VERSION,
        "agentHost": HOST,
        "agentId": ANS_AGENT_ID,
        "endpoints": [{"protocol": "A2A", "agentUrl": A2A_URL, "metaDataUrl": CARD_URL}],
        # No identity certificate has been issued yet, so there is no x5c chain to publish.
        # An empty list is the truth; a copied chain would be the thing we exist to catch.
        "keys": [],
        "registration": {"status": ANS_STATUS, "submitted": "2026-09-19",
                         "note": "ACTIVE in ANS with a sealed certificate; we do not hold its chain, so keys[] stays empty until we do"},
    }
