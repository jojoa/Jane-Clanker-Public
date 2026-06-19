from __future__ import annotations

import socket
from collections.abc import Iterable


_TRANSIENT_NETWORK_TEXT = (
    "temporary failure in name resolution",
    "could not resolve host",
    "name or service not known",
    "nodename nor servname provided",
    "no address associated with hostname",
    "cannot connect to host",
    "could not connect to server",
    "failed to connect to",
    "network is unreachable",
    "connection timed out",
    "operation timed out",
    "timed out",
    "connection refused",
    "connection reset by peer",
    "server disconnected",
    "bad record mac",
    "temporary failure resolving",
)
_TRANSIENT_EXCEPTION_CLASS_NAMES = {
    "ClientConnectorDNSError",
    "ClientConnectorError",
}


def _walkExceptionChain(exc: BaseException | None) -> Iterable[BaseException]:
    seen: set[int] = set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def isLikelyTransientNetworkError(exc: BaseException | None) -> bool:
    for current in _walkExceptionChain(exc):
        if isinstance(current, socket.gaierror):
            return True
        if current.__class__.__name__ in _TRANSIENT_EXCEPTION_CLASS_NAMES:
            return True
        text = str(current).lower()
        if any(pattern in text for pattern in _TRANSIENT_NETWORK_TEXT):
            return True
    return False


def textLooksLikeTransientNetworkError(value: object) -> bool:
    text = str(value or "").lower()
    return any(pattern in text for pattern in _TRANSIENT_NETWORK_TEXT)
