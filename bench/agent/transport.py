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
            r = c.post(self.endpoint, json=body)
        ms = int((time.perf_counter() - t0) * 1000)
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            raise RuntimeError(f"A2A error: {data['error']}")
        return _extract_text(data.get("result", data)), ms


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
            r = c.post(self.endpoint, json=body)
        ms = int((time.perf_counter() - t0) * 1000)
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            raise RuntimeError(f"MCP error: {data['error']}")
        content = data.get("result", {}).get("content", [])
        return "\n".join(c.get("text", "") for c in content if isinstance(c, dict)), ms


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
        raise RuntimeError("No endpoint: set target.endpoint in config.yaml or ensure agent card has `url`")
    if cfg.target.transport == "mcp":
        if not cfg.transport.mcp_tool:
            raise RuntimeError("transport=mcp requires transport.mcp_tool in config.yaml")
        return MCPTransport(endpoint, cfg.transport.mcp_tool, cfg.target.timeout_s)
    return A2ATransport(endpoint, cfg.transport.a2a_method, cfg.target.timeout_s)
