from ipaddress import IPv4Network, IPv6Network, ip_address

from fastapi import HTTPException, Request

from app.dependencies import AppDependencies


def dependencies_for(request: Request) -> AppDependencies:
    dependencies = getattr(request.app.state, "dependencies", None)
    if type(dependencies) is not AppDependencies:
        raise HTTPException(status_code=503, detail="SERVICE_UNAVAILABLE")
    return dependencies


def client_ip(
    request: Request,
    trusted_proxy_cidrs: tuple[IPv4Network | IPv6Network, ...] = (),
) -> str:
    """Derive a bounded rate-limit key without trusting client-supplied headers."""

    if request.client is None:
        return "unknown"
    try:
        peer = ip_address(request.client.host)
    except ValueError:
        return "unknown"
    if not any(
        peer == network.network_address and network.prefixlen == network.max_prefixlen
        for network in trusted_proxy_cidrs
    ):
        return peer.compressed
    values = request.headers.getlist("cf-connecting-ip")
    if len(values) != 1:
        return "trusted-proxy-invalid"
    try:
        candidate = ip_address(values[0])
    except ValueError:
        return "trusted-proxy-invalid"
    if not candidate.is_global or candidate.compressed != values[0]:
        return "trusted-proxy-invalid"
    return candidate.compressed
