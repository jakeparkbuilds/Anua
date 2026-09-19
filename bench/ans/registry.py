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

    def find_by_host(self, host: str) -> Optional[dict[str, Any]]:
        for a in self.search(host, limit=20):
            h = _first(a, "agentHost", "host", "domain", "fqdn", default="")
            name = _first(a, "ansName", "name", default="")
            if host in str(h) or host in str(name):
                return a
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
    def tl_server_fingerprint(entry: dict[str, Any]) -> Optional[str]:
        """agent-trust-discovery README: prod TL carries validServerCerts[] (set) and
        agent-snapshot uses the primary serverCert.fingerprint."""
        fp = _first(entry, "serverCert.fingerprint", "serverCertificate.fingerprint",
                    "certificates.server.fingerprint")
        if fp:
            return _norm_fp(fp)
        certs = _first(entry, "validServerCerts", "serverCerts", default=[])
        if isinstance(certs, list) and certs:
            fp = _first(certs[0], "fingerprint", "sha256", "fingerprintSha256")
            return _norm_fp(fp) if fp else None
        return None

    @staticmethod
    def tl_ans_name(entry: dict[str, Any]) -> Optional[str]:
        return _first(entry, "ansName", "name", "agent.ansName")


def _norm_fp(fp: str) -> str:
    return fp.replace(":", "").replace(" ", "").lower().removeprefix("sha256")
