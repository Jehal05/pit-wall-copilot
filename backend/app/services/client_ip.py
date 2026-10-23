"""Trusted-proxy-aware client IP extraction — AWS hackathon-final pass,
P1-10.

Every rate limiter, audit-log line, and pairing-attempt counter in this
codebase previously keyed on `request.client.host` directly. Behind an
Application Load Balancer (the deployed stack's own entry point — see
infrastructure/template.yaml's `BackendLoadBalancer`), that value is
ALWAYS the ALB's own internal ENI address, never the originating client's
IP: the ALB terminates the TCP connection itself and forwards the
original client's address only in the `X-Forwarded-For` header. The
practical effect of the pre-fix code: every distinct real-world client
behind the ALB was rate-limited, audited, and pairing-attempt-throttled
as if it were the SAME single identity (the ALB) — one abusive client
exhausts the shared budget for every other legitimate client on the same
deployment, and the audit trail cannot distinguish one caller from
another.

`X-Forwarded-For` cannot simply be trusted unconditionally either: it is
an ordinary HTTP request header, and any client can set an arbitrary
value in it, trivially forging a claimed source IP unless the server
verifies who is ALLOWED to set it — the immediate TCP peer this
connection actually came from. This module resolves the client IP by:

  1. If `TRUSTED_PROXY_CIDRS` is unset/empty (the default -- matches
     local dev, tests, and any deployment that has not explicitly opted
     in), `X-Forwarded-For` is never trusted at all, and the immediate
     TCP peer (`request.client.host`) is used directly -- identical to
     this codebase's behavior before this pass.
  2. If it IS configured (the deployed stack sets it to the VPC CIDR the
     ALB's ENIs live in -- see infrastructure/template.yaml), and the
     immediate TCP peer falls within one of those CIDRs, the LEFTMOST
     entry of `X-Forwarded-For` is trusted as the originating client
     (this is correct for exactly the "N clients -> one ALB -> this
     backend" topology this stack deploys: the ALB appends the true
     client IP as the first hop of that header on every request, and
     this backend has no OTHER proxy in front of it that could have
     prepended a forged entry ahead of the ALB's own).
  3. If the immediate TCP peer is NOT a trusted proxy, `X-Forwarded-For`
     is ignored entirely, regardless of its content -- a request that
     didn't arrive through the ALB gets no benefit from claiming
     otherwise via a forged header.
"""

from __future__ import annotations

import ipaddress
from functools import lru_cache

from fastapi import Request

from app.config import Settings


@lru_cache(maxsize=1)
def _parse_trusted_cidrs(raw: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    networks = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            networks.append(ipaddress.ip_network(chunk, strict=False))
        except ValueError:
            continue  # an invalid entry is skipped, not fatal -- fail toward "not trusted", not toward a crash
    return tuple(networks)


def _is_trusted_proxy(peer_ip: str, trusted_cidrs: tuple) -> bool:
    if not trusted_cidrs:
        return False
    try:
        parsed_peer = ipaddress.ip_address(peer_ip)
    except ValueError:
        return False
    return any(parsed_peer in network for network in trusted_cidrs)


def get_client_ip(request: Request, settings: Settings) -> str:
    """The one function every rate limiter / audit log / pairing
    attempt counter in this codebase should call instead of reading
    `request.client.host` directly (see this module's docstring for
    why). Never raises -- returns "unknown" for a request with no
    transport-level peer at all (the same fallback every prior direct
    `request.client.host if request.client else "unknown"` call site
    used)."""
    peer_ip = request.client.host if request.client else None
    trusted_cidrs = _parse_trusted_cidrs(settings.trusted_proxy_cidrs)

    if peer_ip is not None and _is_trusted_proxy(peer_ip, trusted_cidrs):
        forwarded_for = request.headers.get("x-forwarded-for")
        if forwarded_for:
            first_hop = forwarded_for.split(",")[0].strip()
            if first_hop:
                return first_hop

    return peer_ip or "unknown"
