from __future__ import annotations
from typing import Optional
import httpx


def status(domain: str) -> Optional[int]:
    try:
        with httpx.Client(timeout=10, follow_redirects=False, verify=False) as c:
            return c.head(f"https://{domain}/").status_code
    except httpx.HTTPError:
        return None


def https_ok(domain: str) -> bool:
    """Reachable over HTTPS *with* verification. Distinct from tls.chain_valid only in
    that it exercises the HTTP layer too."""
    try:
        with httpx.Client(timeout=10, follow_redirects=True, verify=True) as c:
            c.head(f"https://{domain}/")
            return True
    except httpx.HTTPError:
        return False
