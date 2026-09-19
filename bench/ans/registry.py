"""ANS discovery: public Search API + Transparency Log.

Both endpoints are documented as unauthenticated GETs (agent-trust-discovery README),
rate-limited at 100 req / 60s.

  GET {search_base}/v1/ans/registered-agents?query=...
  GET {transparency_base}/v1/agents/{ansId}

HUMAN: the exact JSON field names in the responses are not confirmed. The extractors
below are defensive (try several plausible keys). Adjust after your first --live call:
    python -m bench probe-registry --host dnsdoc.webmesh.ai --live
"""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any, Optional
import httpx

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


def _get(url: str, timeout: int, params: dict | None = None) -> dict[str, Any]:
    with httpx.Client(timeout=timeout, follow_redirects=True) as c:
        r = c.get(url, params=params)
        r.raise_for_status()
        return r.json()


def _first(d: dict, *keys: str, default=None):
    """Return first present key (supports dotted paths)."""
    for k in keys:
        cur: Any = d
        ok = True
        for part in k.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                ok = False
                break
        if ok and cur not in (None, ""):
            return cur
    return default


class Registry:
    def __init__(self, search_base: str, transparency_base: str, timeout_s: int, live: bool):
        self.search_base = search_base.rstrip("/")
        self.tl_base = transparency_base.rstrip("/")
        self.timeout = timeout_s
        self.live = live

    # ---- search -----------------------------------------------------------
    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        if not self.live:
            return json.loads((FIXTURES / "mock_registry_search.json").read_text())
        data = _get(f"{self.search_base}/v1/ans/registered-agents", self.timeout,
                    params={"query": query, "pageSize": limit})
        # plausible envelope shapes
        return _first(data, "agents", "items", "results", "data", default=data if isinstance(data, list) else [])

    def find_by_host(self, host: str, display_name: str = "") -> Optional[dict[str, Any]]:
        """The search is fuzzy and ranked: `query=seo.webmesh.ai` returns 50 agents and
        misses seo itself, `query=seo` finds it. Try the host, then its first label, then
        the card's display name. Exact `agentHost` match only."""
        tried: set[str] = set()
        for q in (host, host.split(".")[0], display_name):
            q = (q or "").strip()
            if not q or q.lower() in tried:
                continue
            tried.add(q.lower())
            for a in self.search(q, limit=50):
                if str(_first(a, "agentHost", "host", default="")).lower() == host.lower():
                    return a
            if not self.live:
                break
        return None

    # ---- transparency log -------------------------------------------------
    def tl_entry(self, ans_id: str) -> Optional[dict[str, Any]]:
        if not self.live:
            return json.loads((FIXTURES / "mock_tl_entry.json").read_text())
        try:
            return _get(f"{self.tl_base}/v1/agents/{ans_id}", self.timeout)
        except httpx.HTTPError as e:
            return None


    @staticmethod
    def tl_server_fingerprint(entry):
        att = _first(entry, "payload.producer.event.attestations", default={}) or {}
        fp = _first(att, "serverCert.fingerprint")
        if not fp:
            certs = att.get("validServerCerts") or []
            fp = certs[0].get("fingerprint") if certs else None
        return _norm_fp(fp) if fp else None

    @staticmethod
    def tl_ans_name(entry):
        return _first(entry, "payload.producer.event.ansName", "ansName")

def _norm_fp(fp: str) -> str:
    return fp.replace(":", "").replace(" ", "").lower().removeprefix("sha256")
