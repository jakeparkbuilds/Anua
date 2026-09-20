"""Transports to call the target agent. A2A (JSON-RPC message/send) and MCP (tools/call).
Mock transport replays fixtures/mock_responses.json so the pipeline runs offline.

HUMAN: the A2A request envelope below follows the A2A 1.0 shape:
  {"jsonrpc":"2.0","id":..,"method":"message/send",
   "params":{"message":{"role":"user","parts":[{"kind":"text","text":...}],"messageId":..}}}
If the live agent rejects it, the error is surfaced in the assertion's `error` field.
Adjust here, nowhere else.
"""
from __future__ import annotations
import json
import time
import uuid
from pathlib import Path
from typing import Any, Protocol
import httpx

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


class Transport(Protocol):
    def send(self, prompt: str) -> tuple[str, int]: ...   # (response_text, latency_ms)


class AgentUnavailable(Exception):
    """The agent did not serve us: a payment or auth wall (permanent for the run), or a
    dead host, timeout, 5xx or 429 (transient — the next call is still attempted). Never a
    competence verdict: oracle tests are left ungraded; `endpoint.reachable` records why.

    This is what turned one dnsdoc run into a 36: seventeen HTTP 502s were graded as
    seventeen wrong answers."""
    def __init__(self, msg: str, permanent: bool = False):
        super().__init__(msg)
        self.permanent = permanent


_RETRY_AFTER_S = 2.0


def _post(client: httpx.Client, url: str, **kw) -> httpx.Response:
    """One POST, one retry for anything transient. A refused connection, a timeout or a
    5xx is the agent being unavailable, not a runner crash and not a wrong answer."""
    last: Exception | None = None
    for attempt in (1, 2):
        try:
            r = client.post(url, **kw)
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            raise AgentUnavailable(f"connection failed: {type(e).__name__}: {str(e)[:120]}") from e
        except httpx.TimeoutException as e:
            last = AgentUnavailable(f"timed out waiting for the agent ({type(e).__name__}, "
                                    f"{client.timeout.read}s read timeout)")
            last.__cause__ = e
        except httpx.HTTPError as e:
            last = AgentUnavailable(f"transport error: {type(e).__name__}: {str(e)[:120]}")
            last.__cause__ = e
        else:
            if r.status_code >= 500 or r.status_code == 429:
                last = AgentUnavailable(f"HTTP {r.status_code} {r.reason_phrase}: the agent could not serve "
                                        f"the request" + (" (rate limited)" if r.status_code == 429 else ""))
            else:
                return r
        if attempt == 1:
            time.sleep(_RETRY_AFTER_S)
    assert last is not None
    raise last


_MAX_BODY = 5_000_000


def _json_body(r: httpx.Response):
    """The agent's reply as JSON. An empty body, HTML, or a multi-megabyte blob is the
    agent failing to answer the protocol — unavailability, never a runner crash."""
    if len(r.content) > _MAX_BODY:
        raise AgentUnavailable(f"reply body is {len(r.content)} bytes; refusing to parse")
    if not r.content.strip():
        raise AgentUnavailable(f"empty reply body (HTTP {r.status_code})")
    try:
        return r.json()
    except ValueError:
        raise AgentUnavailable(f"reply is not JSON (content-type {r.headers.get('content-type', '?')}): "
                               f"{r.text[:60]!r}")


def _check_gate(r: httpx.Response) -> None:
    if r.status_code == 402:
        raise AgentUnavailable(f"HTTP 402 payment required (x402: {r.headers.get('payment-required', '')[:40]}...)", permanent=True)
    if r.status_code in (401, 403):
        raise AgentUnavailable(f"HTTP {r.status_code} {r.reason_phrase}: authentication required", permanent=True)


def _extract_text(result: Any) -> str:
    """Pull text out of an A2A result (message or task) defensively."""
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        # message.parts[].text
        parts = result.get("parts")
        if isinstance(parts, list):
            return "\n".join(p.get("text", "") for p in parts if isinstance(p, dict))
        # task.status.message.parts / task.artifacts[].parts
        for path in (("status", "message", "parts"), ("message", "parts")):
            cur = result
            for k in path:
                cur = cur.get(k) if isinstance(cur, dict) else None
            if isinstance(cur, list):
                return "\n".join(p.get("text", "") for p in cur if isinstance(p, dict))
        arts = result.get("artifacts")
        if isinstance(arts, list):
            out = []
            for a in arts:
                for p in a.get("parts", []):
                    if isinstance(p, dict) and "text" in p:
                        out.append(p["text"])
            if out:
                return "\n".join(out)
        return json.dumps(result)
    return json.dumps(result)


class A2ATransport:
    def __init__(self, endpoint: str, method: str = "message/send", timeout: int = 30):
        self.endpoint, self.method, self.timeout = endpoint, method, timeout

    def send(self, prompt: str) -> tuple[str, int]:
        body = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": self.method,
            "params": {"message": {"role": "user", "messageId": str(uuid.uuid4()),
                                   "parts": [{"kind": "text", "text": prompt}]}},
        }
        t0 = time.perf_counter()
        with httpx.Client(timeout=self.timeout) as c:
            r = _post(c, self.endpoint, json=body)
        ms = int((time.perf_counter() - t0) * 1000)
        _check_gate(r)
        r.raise_for_status()
        data = _json_body(r)
        if isinstance(data, dict) and "error" in data:
            raise AgentUnavailable(f"A2A error reply: {str(data['error'])[:120]}")
        return _extract_text(data.get("result", data) if isinstance(data, dict) else data), ms


class MCPTransport:
    """Minimal streamable-HTTP MCP tools/call. HUMAN: set transport.mcp_tool in config
    and confirm the argument name the tool expects (assumed 'domain')."""
    def __init__(self, endpoint: str, tool: str, timeout: int = 30, arg_name: str = "domain"):
        self.endpoint, self.tool, self.timeout, self.arg_name = endpoint, tool, timeout, arg_name

    def send(self, prompt: str) -> tuple[str, int]:
        body = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "tools/call",
                "params": {"name": self.tool, "arguments": {self.arg_name: prompt}}}
        t0 = time.perf_counter()
        with httpx.Client(timeout=self.timeout, headers={"Accept": "application/json, text/event-stream"}) as c:
            r = _post(c, self.endpoint, json=body)
        ms = int((time.perf_counter() - t0) * 1000)
        _check_gate(r)
        r.raise_for_status()
        data = _json_body(r)
        if not isinstance(data, dict):
            raise AgentUnavailable("MCP reply is not a JSON object")
        if "error" in data:
            raise AgentUnavailable(f"MCP error reply: {str(data['error'])[:120]}")
        content = data.get("result", {}).get("content", [])
        return "\n".join(c.get("text", "") for c in content if isinstance(c, dict)), ms


class NoEndpointTransport:
    """We do not know where to send anything. Every call is the agent being unavailable
    (permanently, for this run) so competence stays ungraded and `endpoint.reachable`
    records why — instead of the whole run dying with a traceback."""
    def __init__(self, why: str):
        self.why = why

    def send(self, prompt: str) -> tuple[str, int]:
        raise AgentUnavailable(self.why, permanent=True)


class MockTransport:
    """Replays canned responses keyed by domain. Some are DELIBERATELY WRONG so the
    offline demo shows a real failure (e.g. calls expired.badssl.com valid)."""
    def __init__(self):
        self.responses = json.loads((FIXTURES / "mock_responses.json").read_text())

    def send(self, prompt: str) -> tuple[str, int]:
        for domain, resp in self.responses.items():
            if domain in prompt:
                return json.dumps(resp), 42
        return json.dumps({"error": "mock has no fixture for this input"}), 5


def build(cfg, cards) -> Transport:
    if not cfg.live:
        return MockTransport()
    endpoint = cfg.target.endpoint or cards.endpoint
    if not endpoint:
        return NoEndpointTransport("no endpoint is known for this agent: no agent card `url` and no "
                                   "registered endpoint")
    if cfg.target.transport == "mcp":
        if not cfg.transport.mcp_tool:
            raise RuntimeError("transport=mcp requires transport.mcp_tool in config.yaml")
        return MCPTransport(endpoint, cfg.transport.mcp_tool, cfg.target.timeout_s)
    return A2ATransport(endpoint, cfg.transport.a2a_method, cfg.target.timeout_s)
