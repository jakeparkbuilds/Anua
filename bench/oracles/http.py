from __future__ import annotations
import httpx

from .errors import OracleUnavailable, is_cert_verification_error


def status(domain: str) -> int:
    """HTTPS status code. Raises OracleUnavailable if we never got a response — an
    unreachable host has no status code, and inventing one would penalise the agent."""
    try:
        with httpx.Client(timeout=10, follow_redirects=False, verify=False) as c:
            return c.head(f"https://{domain}/").status_code
    except httpx.HTTPError as e:
        raise OracleUnavailable(f"HTTP {domain}: {type(e).__name__}: {e}") from e


def https_ok(domain: str) -> bool:
    """Reachable over HTTPS *with* verification. Distinct from tls.chain_valid only in
    that it exercises the HTTP layer too.

    A cert verification failure is a verdict (False). Anything else — DNS failure,
    timeout, refused connection — means we could not test it, so raise.
    """
    try:
        with httpx.Client(timeout=10, follow_redirects=True, verify=True) as c:
            c.head(f"https://{domain}/")
            return True
    except httpx.HTTPError as e:
        if is_cert_verification_error(e):
            return False
        raise OracleUnavailable(f"HTTPS {domain}: {type(e).__name__}: {e}") from e
