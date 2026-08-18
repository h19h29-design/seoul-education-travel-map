import json
import socket
import subprocess
import sys
import time
from http.client import HTTPConnection
from ipaddress import ip_network
from pathlib import Path

from app.api.common import client_ip
from fastapi import FastAPI
from starlette.requests import Request

_RUNTIME_APP = FastAPI()


@_RUNTIME_APP.get("/client-ip")
async def runtime_client_ip(request: Request) -> dict[str, str]:
    return {"clientIp": client_ip(request, ())}


# Break caught: a forged CF-Connecting-IP reaches rate-limit buckets from an
# untrusted socket peer, or a configured exact Cloudflare peer is ignored.
def test_only_exact_trusted_socket_peer_can_supply_cf_connecting_ip() -> None:
    forged = Request(
        {
            "type": "http",
            "client": ("203.0.113.9", 40123),
            "headers": [(b"cf-connecting-ip", b"1.1.1.1")],
        }
    )
    trusted = Request(
        {
            "type": "http",
            "client": ("127.0.0.1", 40124),
            "headers": [(b"cf-connecting-ip", b"1.1.1.1")],
        }
    )
    cidrs = (ip_network("127.0.0.1/32"),)

    assert client_ip(forged, cidrs) == "203.0.113.9"
    assert client_ip(trusted, cidrs) == "1.1.1.1"


# Break caught: a trusted proxy accepts multiple, private, noncanonical, or
# malformed CF values instead of falling into one bounded invalid bucket.
def test_trusted_proxy_requires_one_canonical_global_cf_connecting_ip() -> None:
    cidrs = (ip_network("127.0.0.1/32"),)
    cases = (
        ((), "trusted-proxy-invalid"),
        (
            ((b"cf-connecting-ip", b"1.1.1.1"), (b"cf-connecting-ip", b"8.8.8.8")),
            "trusted-proxy-invalid",
        ),
        (((b"cf-connecting-ip", b"1.1.1.1, 8.8.8.8"),), "trusted-proxy-invalid"),
        (((b"cf-connecting-ip", b"127.0.0.1"),), "trusted-proxy-invalid"),
        (((b"cf-connecting-ip", b"1.1.1.1 "),), "trusted-proxy-invalid"),
        (((b"cf-connecting-ip", b"1.1.1.1"),), "1.1.1.1"),
    )
    for headers, expected in cases:
        request = Request(
            {
                "type": "http",
                "client": ("127.0.0.1", 40124),
                "headers": headers,
            }
        )
        assert client_ip(request, cidrs) == expected


# Break caught: a broad trusted CIDR permits a different socket peer to forge
# CF-Connecting-IP, despite the deployment boundary permitting exact peers only.
def test_client_ip_never_trusts_wide_proxy_cidrs() -> None:
    request = Request(
        {
            "type": "http",
            "client": ("127.0.0.2", 40124),
            "headers": [(b"cf-connecting-ip", b"1.1.1.1")],
        }
    )

    assert client_ip(request, (ip_network("127.0.0.0/24"),)) == "127.0.0.2"


# Break caught: Uvicorn rewriting the raw ASGI socket peer from Forwarded/XFF
# before the app can enforce its exact trusted-connector boundary.
def test_docker_starts_uvicorn_without_proxy_headers() -> None:
    dockerfile = Path("apps/travel-map/Dockerfile").read_text(encoding="utf-8")
    command = _docker_runtime_command(dockerfile)

    assert command[:2] == ["/bin/sh", "-c"]
    assert command[2].startswith("umask 077; exec uvicorn ")
    assert "--no-proxy-headers" in command[2].split()


# Break caught: a real Uvicorn process rewriting the socket peer from a forged
# Forwarded/X-Forwarded-For header before the app can derive the rate-limit key.
def test_real_uvicorn_process_does_not_rewrite_socket_peer_from_forwarded_headers() -> (
    None
):
    dockerfile = Path("apps/travel-map/Dockerfile").read_text(encoding="utf-8")
    docker_command = _docker_runtime_command(dockerfile)
    port = _free_loopback_port()
    command = [
        sys.executable,
        "-m",
        "uvicorn",
        "tests.security.test_proxy_boundary:_RUNTIME_APP",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--no-access-log",
    ]
    if "--no-proxy-headers" in docker_command[2].split():
        command.append("--no-proxy-headers")
    process = subprocess.Popen(
        command,
        cwd=Path("apps/travel-map"),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        payload = _runtime_client_ip(port)
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    assert payload == {"clientIp": "127.0.0.1"}


def _docker_runtime_command(dockerfile: str) -> list[str]:
    for line in dockerfile.splitlines():
        if line.startswith("CMD ["):
            command = json.loads(line.removeprefix("CMD "))
            if type(command) is list and all(type(value) is str for value in command):
                return command
    raise AssertionError("Dockerfile must declare a JSON-array CMD")


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _runtime_client_ip(port: int) -> dict[str, str]:
    deadline = time.monotonic() + 5.0
    headers = {
        "Forwarded": "for=203.0.113.90",
        "X-Forwarded-For": "203.0.113.91",
    }
    while time.monotonic() < deadline:
        connection = HTTPConnection("127.0.0.1", port, timeout=0.2)
        try:
            connection.request("GET", "/client-ip", headers=headers)
            response = connection.getresponse()
            if response.status == 200:
                payload = json.loads(response.read())
                if type(payload) is dict and type(payload.get("clientIp")) is str:
                    return {"clientIp": payload["clientIp"]}
        except OSError:
            pass
        finally:
            connection.close()
        time.sleep(0.05)
    raise AssertionError("local uvicorn process did not become ready")
