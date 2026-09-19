"""Shared oracle error type.

Lives here (not in dns.py) so tls/http can raise it without importing the DNS module.
dns.py re-exports it, so `from .dns import OracleUnavailable` keeps working.
"""
from __future__ import annotations
import ssl


class OracleUnavailable(Exception):
    """Ground truth could not be computed (timeout/network/resolver).

    NOT an agent failure. The runner marks these assertions ungraded so they are
    excluded from pass rates — our network problem is not their competence problem.
    """


def is_cert_verification_error(exc: BaseException) -> bool:
    """True if a cert verification failure appears anywhere in the cause chain.

    httpx wraps everything in ConnectError, so an expired cert and a dead hostname
    look identical at the top level. Only the former is a verdict about the target.
    """
    seen: set[int] = set()
    e: BaseException | None = exc
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        if isinstance(e, ssl.SSLCertVerificationError):
            return True
        e = e.__cause__ or e.__context__
    return False
