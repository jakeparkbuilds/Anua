from __future__ import annotations
import httpx

from .errors import OracleUnavailable, is_cert_verification_error


def status(domain: str) -> list[int]:
    """The HTTPS status codes an honest agent could report for https://domain/: the first
    response's status and, if that is a redirect, the final status after following it.
    One agent reports the 301, another the 200 it lands on; both checked the domain. The
    comparator is one_of. Raises OracleUnavailable if we never got a response."""
    try:
        with httpx.Client(timeout=10, follow_redirects=False, verify=False) as c:
            first = c.head(f"https://{domain}/")
        codes = [first.status_code]
        if first.is_redirect:
            with httpx.Client(timeout=10, follow_redirects=True, verify=False) as c:
                final = c.head(f"https://{domain}/").status_code
            if final not in codes:
                codes.append(final)
        return codes
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
