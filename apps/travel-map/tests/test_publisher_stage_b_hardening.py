from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PUBLISHER = ROOT / "deploy/nas/publish-reviewed-image.sh"
PUBLISH_REGISTRY = "ghcr.io/h19h29-design/seoul-education-travel-map"
PUBLISH_TOOL_SEARCH_PATH_ASSIGNMENT = "tool_search_path=$canonical_home/.local/bin:/opt/homebrew/bin:/usr/local/bin:$trusted_path"


def _short_endpoint(tmp_path: Path) -> Path:
    suffix = hashlib.sha256(str(tmp_path).encode("utf-8")).hexdigest()[:16]
    return (
        Path(tempfile.gettempdir()).resolve(strict=True) / f"tm-pub-{suffix}" / "d.sock"
    )


@contextmanager
def _unix_socket(path: Path, mode: int = 0o600) -> Iterator[None]:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    path.chmod(mode)
    try:
        yield
    finally:
        listener.close()
        path.unlink(missing_ok=True)


def _write_named_context_config(
    root: Path,
    endpoint: Path,
    *,
    extra_config: dict[str, object] | None = None,
) -> Path:
    context_name = "release-test"
    context_id = hashlib.sha256(context_name.encode("utf-8")).hexdigest()
    metadata_parent = root / "contexts" / "meta" / context_id
    metadata_parent.mkdir(mode=0o700, parents=True)
    for directory in (root, root / "contexts", root / "contexts/meta", metadata_parent):
        directory.chmod(0o700)

    config: dict[str, object] = {
        "auths": {"ghcr.io": {"auth": "dGVzdA=="}},
        "currentContext": context_name,
    }
    if extra_config:
        config.update(extra_config)
    config_path = root / "config.json"
    config_path.write_text(json.dumps(config) + "\n", encoding="utf-8")
    config_path.chmod(0o600)

    metadata_path = metadata_parent / "meta.json"
    metadata_path.write_text(
        json.dumps(
            {
                "Name": context_name,
                "Metadata": {},
                "Endpoints": {
                    "docker": {
                        "Host": f"unix://{endpoint}",
                        "SkipTLSVerify": False,
                    }
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    metadata_path.chmod(0o600)
    return root


def _write_fake_docker_tools(
    fake_bin: Path,
    *,
    events: Path,
    endpoint: Path,
    mutation: str | None,
) -> tuple[str, str]:
    manifest_media_type = "application/vnd.oci.image.manifest.v1+json"
    image_config = {"os": "linux", "architecture": "amd64"}
    image_config_bytes = (
        json.dumps(image_config, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    image_id = "sha256:" + hashlib.sha256(image_config_bytes).hexdigest()
    manifest = {
        "schemaVersion": 2,
        "mediaType": manifest_media_type,
        "config": {
            "mediaType": "application/vnd.oci.image.config.v1+json",
            "digest": image_id,
            "size": len(image_config_bytes),
        },
        "layers": [
            {
                "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                "digest": "sha256:" + "c" * 64,
                "size": 1024,
            }
        ],
    }
    manifest_bytes = (json.dumps(manifest, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    remote_digest = "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
    descriptor = {
        "digest": remote_digest,
        "mediaType": manifest_media_type,
        "size": len(manifest_bytes),
    }
    source = f"""#!/usr/bin/env python3
import json
import os
import socket
import stat
import sys
from pathlib import Path

source_bin = Path({str(fake_bin)!r})
events = Path({str(events)!r})
endpoint = Path({str(endpoint)!r})
image_id = {image_id!r}
remote_digest = {remote_digest!r}
descriptor = {descriptor!r}
manifest = {manifest!r}
image_config = {image_config!r}
mutation = {mutation!r}
executable = Path(sys.argv[0])
args = sys.argv[1:]
event = {{
    "executable": str(executable),
    "executable_mode": stat.S_IMODE(executable.stat().st_mode),
    "args": args,
    "docker_host": os.environ.get("DOCKER_HOST"),
    "docker_config": os.environ.get("DOCKER_CONFIG"),
    "buildx_config": os.environ.get("BUILDX_CONFIG"),
}}
with events.open("a", encoding="utf-8") as output:
    output.write(json.dumps(event) + "\\n")

if executable.parent == source_bin:
    print("source docker tool executed", file=sys.stderr)
    raise SystemExit(97)

event_count = len(events.read_text(encoding="utf-8").splitlines())
if event_count == 1 and mutation == "socket-rebind":
    endpoint.unlink()
    replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    replacement.bind(str(endpoint))
    endpoint.chmod(0o600)
    replacement.close()
elif event_count == 1 and mutation == "credential-helper":
    config_path = Path(os.environ["DOCKER_CONFIG"]) / "config.json"
    config_path.write_text(
        json.dumps({{"auths": {{"ghcr.io": {{"auth": "dGVzdA=="}}}}, "credsStore": "evil"}}) + "\\n",
        encoding="utf-8",
    )
    config_path.chmod(0o600)

if executable.name == "docker-buildx":
    state = Path(os.environ.get("BUILDX_CONFIG", str(Path(os.environ["DOCKER_CONFIG"]) / "buildx")))
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    (state / "activity").write_text("synthetic builder state")
    args.insert(0, "buildx")

if len(args) == 5 and args[:3] == ["image", "inspect", "--format"]:
    print(f"{{image_id}} linux/amd64")
elif len(args) == 6 and args[:2] == ["image", "ls"]:
    pass
elif len(args) == 3 and args[:2] == ["image", "rm"]:
    pass
elif len(args) == 6 and args[:5] == [
    "buildx",
    "imagetools",
    "inspect",
    "--format",
    "{{{{json .Manifest}}}}",
]:
    print(json.dumps(descriptor, separators=(",", ":")))
elif len(args) == 6 and args[:5] == [
    "buildx",
    "imagetools",
    "inspect",
    "--format",
    "{{{{json .Image}}}}",
]:
    print(json.dumps(image_config, separators=(",", ":")))
elif len(args) == 5 and args[:4] == [
    "buildx",
    "imagetools",
    "inspect",
    "--raw",
]:
    print(json.dumps(manifest, separators=(",", ":")))
else:
    print("unexpected fake docker invocation: " + repr(args), file=sys.stderr)
    raise SystemExit(98)
"""
    docker = fake_bin / "docker"
    docker.write_text(source, encoding="utf-8")
    docker.chmod(0o755)
    shutil.copy2(docker, fake_bin / "docker-buildx")
    (fake_bin / "docker-buildx").chmod(0o755)
    return image_id, remote_digest


def _publisher_fixture(
    tmp_path: Path,
    *,
    endpoint: Path,
    endpoint_setup: str,
    extra_config: dict[str, object] | None = None,
    mutation: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path, str]:
    assert endpoint.parent.name.startswith("tm-pub-")

    fake_bin = tmp_path / "safe-bin"
    fake_bin.mkdir(mode=0o700)
    events = tmp_path / "docker-events.jsonl"
    image_id, remote_digest = _write_fake_docker_tools(
        fake_bin,
        events=events,
        endpoint=endpoint,
        mutation=mutation,
    )

    repository = tmp_path / "repository"
    test_root = repository / "apps/travel-map"
    publisher = test_root / "deploy/nas/publish-reviewed-image.sh"
    publisher.parent.mkdir(parents=True)
    publisher_source = PUBLISHER.read_text(encoding="utf-8")
    replacements = {
        PUBLISH_TOOL_SEARCH_PATH_ASSIGNMENT: f"tool_search_path={fake_bin}:$trusted_path",
    }
    for original, replacement in replacements.items():
        assert publisher_source.count(original) == 1
        publisher_source = publisher_source.replace(original, replacement, 1)
    publisher.write_text(publisher_source, encoding="utf-8")
    publisher.chmod(0o755)
    subprocess.run(["/usr/bin/git", "-C", str(repository), "init", "-q"], check=True)
    subprocess.run(
        [
            "/usr/bin/git",
            "-C",
            str(repository),
            "config",
            "user.name",
            "Publisher Test",
        ],
        check=True,
    )
    subprocess.run(
        [
            "/usr/bin/git",
            "-C",
            str(repository),
            "config",
            "user.email",
            "publisher-test@example.invalid",
        ],
        check=True,
    )
    subprocess.run(
        ["/usr/bin/git", "-C", str(repository), "add", "."],
        check=True,
    )
    subprocess.run(
        [
            "/usr/bin/git",
            "-C",
            str(repository),
            "commit",
            "-qm",
            "reviewed publisher",
        ],
        check=True,
    )
    git_sha = subprocess.run(
        ["/usr/bin/git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    image_tag = f"seoul-education-travel-map:release-gate-{git_sha}"

    record_parent = tmp_path / "approved-record"
    record_parent.mkdir(mode=0o700)
    record_parent.chmod(0o700)
    record = record_parent / "gated-image.record"
    record_payload = (
        f"imageTag={image_tag}\n"
        f"imageId={image_id}\n"
        "platform=linux/amd64\n"
        f"gitSha={git_sha}\n"
    ).encode("ascii")
    record.write_bytes(record_payload)
    record.chmod(0o600)

    docker_config = tmp_path / "protected-docker"
    docker_config.mkdir(mode=0o700)
    _write_named_context_config(
        docker_config,
        endpoint,
        extra_config=extra_config,
    )

    try:
        if endpoint_setup == "regular":
            endpoint.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            endpoint.parent.chmod(0o700)
            endpoint.write_text("not a socket", encoding="utf-8")
            endpoint.chmod(0o600)
            completed = _run_publisher(
                publisher,
                docker_config,
                record,
                image_tag,
                image_id,
                git_sha,
                record_payload,
            )
        elif endpoint_setup == "symlink":
            target = endpoint.with_name("real.sock")
            with _unix_socket(target):
                endpoint.symlink_to(target)
                completed = _run_publisher(
                    publisher,
                    docker_config,
                    record,
                    image_tag,
                    image_id,
                    git_sha,
                    record_payload,
                )
        elif endpoint_setup in {"valid", "group-writable", "world-writable"}:
            mode = {
                "valid": 0o600,
                "group-writable": 0o620,
                "world-writable": 0o602,
            }[endpoint_setup]
            with _unix_socket(endpoint, mode):
                completed = _run_publisher(
                    publisher,
                    docker_config,
                    record,
                    image_tag,
                    image_id,
                    git_sha,
                    record_payload,
                )
        elif endpoint_setup == "writable-ancestor":
            endpoint.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with _unix_socket(endpoint):
                endpoint.parent.chmod(0o770)
                completed = _run_publisher(
                    publisher,
                    docker_config,
                    record,
                    image_tag,
                    image_id,
                    git_sha,
                    record_payload,
                )
        else:
            raise AssertionError(f"unexpected endpoint setup: {endpoint_setup}")
    finally:
        endpoint.unlink(missing_ok=True)
        endpoint.with_name("real.sock").unlink(missing_ok=True)
        endpoint.parent.rmdir()
    return completed, events, remote_digest


def _run_publisher(
    publisher: Path,
    docker_config: Path,
    record: Path,
    image_tag: str,
    image_id: str,
    git_sha: str,
    record_payload: bytes,
) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment.update(
        {
            "PATH": "/nonexistent",
            "DOCKER_CONFIG": str(docker_config),
            "DOCKER_HOST": "tcp://attacker.invalid:2375",
            "DOCKER_CONTEXT": "attacker-context",
            "BUILDKIT_HOST": "tcp://attacker.invalid:1234",
        }
    )
    return subprocess.run(
        [
            "/bin/sh",
            str(publisher),
            str(record.resolve(strict=True)),
            image_tag,
            image_id,
            "linux/amd64",
            git_sha,
            hashlib.sha256(record_payload).hexdigest(),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


@pytest.mark.parametrize(
    "endpoint_setup",
    [
        "regular",
        "symlink",
        "group-writable",
        "world-writable",
        "writable-ancestor",
    ],
)
def test_publisher_rejects_unsafe_endpoint_before_docker_execution(
    tmp_path: Path,
    endpoint_setup: str,
) -> None:
    completed, events, _ = _publisher_fixture(
        tmp_path,
        endpoint=_short_endpoint(tmp_path),
        endpoint_setup=endpoint_setup,
    )

    assert completed.returncode == 2
    assert not events.exists()
    assert "BLOCKED_INVALID_DOCKER_CONFIG" in completed.stderr


@pytest.mark.parametrize(
    ("forbidden_key", "forbidden_value"),
    [
        ("credsStore", "desktop"),
        ("credHelpers", {"ghcr.io": "desktop"}),
        ("cliPluginsExtraDirs", ["/attacker/plugins"]),
    ],
)
def test_publisher_rejects_external_credential_or_plugin_helpers_before_docker(
    tmp_path: Path,
    forbidden_key: str,
    forbidden_value: object,
) -> None:
    endpoint = _short_endpoint(tmp_path)
    completed, events, _ = _publisher_fixture(
        tmp_path,
        endpoint=endpoint,
        endpoint_setup="valid",
        extra_config={forbidden_key: forbidden_value},
    )

    assert completed.returncode == 2
    assert not events.exists()
    assert "BLOCKED_INVALID_DOCKER_CONFIG" in completed.stderr


def test_publisher_derives_endpoint_without_executing_source_tools(
    tmp_path: Path,
) -> None:
    endpoint = _short_endpoint(tmp_path)
    completed, events, remote_digest = _publisher_fixture(
        tmp_path,
        endpoint=endpoint,
        endpoint_setup="valid",
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == f"{PUBLISH_REGISTRY}@{remote_digest}\n"
    observed = [json.loads(line) for line in events.read_text().splitlines()]
    assert observed
    assert all("/trusted-bin/" in event["executable"] for event in observed)
    assert all(event["executable_mode"] == 0o500 for event in observed)
    assert all(event["docker_host"] == f"unix://{endpoint}" for event in observed)
    assert all(
        event["docker_host"] != "tcp://attacker.invalid:2375" for event in observed
    )
    assert all(event["args"][:2] != ["context", "inspect"] for event in observed)
    assert all(event["args"] != ["version"] for event in observed)
    build_events = [
        event for event in observed if Path(event["executable"]).name == "docker-buildx"
    ]
    assert build_events
    for event in build_events:
        state = Path(event["buildx_config"])
        assert state == Path(event["executable"]).parent.parent / "buildx-state"
        assert not state.parent.exists()
    assert all(
        event["buildx_config"] is None
        for event in observed
        if Path(event["executable"]).name == "docker"
    )
    config_root = Path(observed[0]["docker_config"])
    assert {item.name for item in config_root.iterdir()} == {"config.json", "contexts"}


@pytest.mark.parametrize("mutation", ["socket-rebind", "credential-helper"])
def test_publisher_revalidates_config_and_socket_before_every_docker_call(
    tmp_path: Path,
    mutation: str,
) -> None:
    completed, events, _ = _publisher_fixture(
        tmp_path,
        endpoint=_short_endpoint(tmp_path),
        endpoint_setup="valid",
        mutation=mutation,
    )

    assert completed.returncode == 2
    observed = [json.loads(line) for line in events.read_text().splitlines()]
    assert len(observed) == 1
    assert "/trusted-bin/" in observed[0]["executable"]
