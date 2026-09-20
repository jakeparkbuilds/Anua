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
        try:
            return r.json()
        except ValueError as e:
            raise httpx.HTTPError(f"{url}: body is not JSON") from e


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
        self.last_error: str | None = None

    # ---- search -----------------------------------------------------------
    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        if not self.live:
            return json.loads((FIXTURES / "mock_registry_search.json").read_text())
        try:
            data = _get(f"{self.search_base}/v1/ans/registered-agents", self.timeout,
                        params={"query": query, "pageSize": limit})
        except httpx.HTTPError as e:
            # The registry being down is not "this agent is not registered". Callers
            # treat None-ish as not found; the identity notes carry the reason.
            self.last_error = f"registry search failed: {type(e).__name__}: {str(e)[:100]}"
            return []
        # plausible envelope shapes
        found = _first(data, "agents", "items", "results", "data", default=data if isinstance(data, list) else [])
        return [a for a in found if isinstance(a, dict)] if isinstance(found, list) else []

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
            hits = [a for a in self.search(q, limit=50)
                    if str(_first(a, "agentHost", "host", default="")).lower() == host.lower()]
            if hits:
                # Several registrations for one host (versions): prefer ACTIVE, then the
                # highest version, deterministically — never "whichever came first".
                def rank(a):
                    ver = str(_first(a, "agentVersion", default="")).lstrip("v")
                    parts = tuple(int(x) if x.isdigit() else 0 for x in ver.split("."))
                    return (str(_first(a, "lifecycle.status", default="")).upper() == "ACTIVE", parts)
                return max(hits, key=rank)
            if not self.live:
                break
        return None

    # ---- transparency log -------------------------------------------------
    def tl_entry(self, ans_id: str) -> Optional[dict[str, Any]]:
        if not self.live:
            return json.loads((FIXTURES / "mock_tl_entry.json").read_text())
        try:
            d = _get(f"{self.tl_base}/v1/agents/{ans_id}", self.timeout)
        except httpx.HTTPError:
            return None
        return d if isinstance(d, dict) else None


    @staticmethod
    def tl_server_fingerprint(entry):
        att = _first(entry, "payload.producer.event.attestations", default={}) or {}
        fp = _first(att, "serverCert.fingerprint")
        if not fp:
            certs = [c for c in (att.get("validServerCerts") or []) if isinstance(c, dict)] if isinstance(att, dict) else []
            fp = certs[0].get("fingerprint") if certs else None
        return _norm_fp(fp) if fp else None

    @staticmethod
    def tl_ans_name(entry):
        return _first(entry, "payload.producer.event.ansName", "ansName")

def _norm_fp(fp: str) -> str:
    return fp.replace(":", "").replace(" ", "").lower().removeprefix("sha256")
