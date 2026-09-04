from __future__ import annotations

import base64
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
REGISTRY = "ghcr.io/h19h29-design/seoul-education-travel-map"
OCI_INDEX = "application/vnd.oci.image.index.v1+json"
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
OCI_CONFIG = "application/vnd.oci.image.config.v1+json"
OCI_LAYER = "application/vnd.oci.image.layer.v1.tar+gzip"
IN_TOTO_LAYER = "application/vnd.in-toto+json"
TOOL_PATH_ASSIGNMENT = (
    "tool_search_path=$canonical_home/.local/bin:/opt/homebrew/bin:/usr/local/bin:"
    "$trusted_path"
)


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _descriptor(
    payload: bytes,
    media_type: str,
    *,
    mismatch: str | None = None,
) -> dict[str, object]:
    digest = _digest(payload)
    size = len(payload)
    if mismatch == "digest":
        replacement = "0" if digest[-1] != "0" else "1"
        digest = digest[:-1] + replacement
    elif mismatch == "size":
        size += 1
    elif mismatch is not None:
        raise AssertionError(f"unexpected mismatch: {mismatch}")
    return {"digest": digest, "mediaType": media_type, "size": size}


def _manifest(
    config_descriptor: dict[str, object],
    *,
    attestation: bool = False,
) -> bytes:
    return _json_bytes(
        {
            "config": config_descriptor,
            "layers": [
                {
                    "digest": "sha256:" + ("9" if attestation else "8") * 64,
                    "mediaType": IN_TOTO_LAYER if attestation else OCI_LAYER,
                    "size": 512,
                }
            ],
            "mediaType": OCI_MANIFEST,
            "schemaVersion": 2,
        }
    )


def _scenario(target: str | None, mismatch: str | None) -> dict[str, object]:
    runnable_config_value = {"architecture": "amd64", "os": "linux"}
    attestation_config_value = {"architecture": "unknown", "os": "unknown"}
    runnable_config = _json_bytes(runnable_config_value)
    attestation_config = _json_bytes(attestation_config_value)
    runnable_config_output = (
        json.dumps(
            {"os": "linux", "architecture": "amd64"},
            indent=2,
        ).encode("utf-8")
        + b"\n"
    )
    attestation_config_output = (
        json.dumps(
            {"os": "unknown", "architecture": "unknown"},
            indent=2,
        ).encode("utf-8")
        + b"\n"
    )
    expected_image_id = _digest(runnable_config)
    runnable_config_descriptor = _descriptor(
        runnable_config,
        OCI_CONFIG,
        mismatch=mismatch if target in {"classic-config", "runnable-config"} else None,
    )
    attestation_config_descriptor = _descriptor(attestation_config, OCI_CONFIG)
    runnable_manifest = _manifest(runnable_config_descriptor)
    attestation_manifest = _manifest(
        attestation_config_descriptor,
        attestation=True,
    )

    if target in {"classic-root", "classic-config"}:
        root_payload = runnable_manifest
        root_descriptor = _descriptor(
            root_payload,
            OCI_MANIFEST,
            mismatch=mismatch if target == "classic-root" else None,
        )
        image_id = expected_image_id
        raw_by_digest = {str(root_descriptor["digest"]): root_payload.hex()}
        image_by_digest = {
            str(root_descriptor["digest"]): runnable_config_output.hex(),
        }
        expected_network = [
            ["descriptor", "tag"],
            ["raw", str(root_descriptor["digest"])],
        ]
    else:
        runnable_descriptor = {
            **_descriptor(
                runnable_manifest,
                OCI_MANIFEST,
                mismatch=mismatch if target == "runnable-child" else None,
            ),
            "platform": {"architecture": "amd64", "os": "linux"},
        }
        attestation_descriptor = {
            **_descriptor(
                attestation_manifest,
                OCI_MANIFEST,
                mismatch=mismatch if target == "attestation-child" else None,
            ),
            "annotations": {
                "vnd.docker.reference.digest": runnable_descriptor["digest"],
                "vnd.docker.reference.type": "attestation-manifest",
            },
            "platform": {"architecture": "unknown", "os": "unknown"},
        }
        root_payload = _json_bytes(
            {
                "manifests": [runnable_descriptor, attestation_descriptor],
                "mediaType": OCI_INDEX,
                "schemaVersion": 2,
            }
        )
        root_descriptor = _descriptor(
            root_payload,
            OCI_INDEX,
            mismatch=mismatch if target == "index-root" else None,
        )
        image_id = expected_image_id
        raw_by_digest = {
            str(root_descriptor["digest"]): root_payload.hex(),
            str(runnable_descriptor["digest"]): runnable_manifest.hex(),
            str(attestation_descriptor["digest"]): attestation_manifest.hex(),
        }
        image_by_digest = {
            str(runnable_descriptor["digest"]): runnable_config_output.hex(),
            str(attestation_descriptor["digest"]): attestation_config_output.hex(),
        }
        expected_network = [
            ["descriptor", "tag"],
            ["raw", str(root_descriptor["digest"])],
        ]
        if target != "index-root":
            expected_network.append(["raw", str(runnable_descriptor["digest"])])
        if target in {"attestation-child", None}:
            expected_network.append(["image", str(runnable_descriptor["digest"])])
        if target in {"attestation-child", None}:
            expected_network.append(["raw", str(attestation_descriptor["digest"])])
        if target is None:
            expected_network.append(["image", str(attestation_descriptor["digest"])])

    return {
        "expected_network": expected_network,
        "image_by_digest": image_by_digest,
        "image_id": image_id,
        "raw_by_digest": raw_by_digest,
        "root_descriptor": root_descriptor,
    }


def _short_socket_path(tmp_path: Path) -> Path:
    suffix = hashlib.sha256(str(tmp_path).encode("utf-8")).hexdigest()[:16]
    root = Path(tempfile.gettempdir()).resolve(strict=True) / f"tm-desc-{suffix}"
    return root / "d.sock"


@contextmanager
def _unix_socket(path: Path) -> Iterator[None]:
    path.parent.mkdir(mode=0o700, parents=True)
    path.parent.chmod(0o700)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    path.chmod(0o600)
    try:
        yield
    finally:
        listener.close()
        path.unlink(missing_ok=True)
        path.parent.rmdir()


def _write_docker_config(root: Path, socket_path: Path) -> None:
    name = "release-test"
    context_id = hashlib.sha256(name.encode("utf-8")).hexdigest()
    metadata_parent = root / "contexts" / "meta" / context_id
    metadata_parent.mkdir(mode=0o700, parents=True)
    for directory in (root, root / "contexts", root / "contexts/meta", metadata_parent):
        directory.chmod(0o700)
    config = root / "config.json"
    config.write_text(
        json.dumps(
            {
                "auths": {
                    "ghcr.io": {
                        "auth": base64.b64encode(
                            f"fixture:{socket_path}".encode()
                        ).decode("ascii")
                    }
                },
                "currentContext": name,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    config.chmod(0o600)
    metadata = metadata_parent / "meta.json"
    metadata.write_text(
        json.dumps(
            {
                "Endpoints": {
                    "docker": {
                        "Host": f"unix://{socket_path}",
                        "SkipTLSVerify": False,
                    }
                },
                "Metadata": {},
                "Name": name,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    metadata.chmod(0o600)


def _write_fake_docker_tools(
    fake_bin: Path,
    scenario_path: Path,
    events_path: Path,
) -> None:
    source = f"""#!/usr/bin/env python3
import json
import os
import stat
import sys
from pathlib import Path

source_bin = Path({str(fake_bin)!r})
scenario = json.loads(Path({str(scenario_path)!r}).read_text(encoding="utf-8"))
events = Path({str(events_path)!r})
executable = Path(sys.argv[0])
args = sys.argv[1:]
if executable.parent == source_bin:
    print("source tool execution blocked", file=sys.stderr)
    raise SystemExit(97)
if executable.name == "docker-buildx":
    args.insert(0, "buildx")
with events.open("a", encoding="utf-8") as output:
    output.write(json.dumps({{"args": args, "host": os.environ.get("DOCKER_HOST")}}) + "\\n")

def emit_hex(value):
    sys.stdout.buffer.write(bytes.fromhex(value))

if len(args) == 5 and args[:3] == ["image", "inspect", "--format"]:
    print(f"{{scenario['image_id']}} linux/amd64")
elif len(args) == 6 and args[:2] == ["image", "ls"]:
    pass
elif len(args) == 3 and args[:2] == ["image", "rm"]:
    pass
elif len(args) == 3 and args[0] in {{"tag", "push"}}:
    print("unexpected mutating registry operation", file=sys.stderr)
    raise SystemExit(96)
elif len(args) == 6 and args[1:5] == [
    "imagetools", "inspect", "--format", "{{{{json .Manifest}}}}"
]:
    print(json.dumps(scenario["root_descriptor"], separators=(",", ":")))
elif len(args) == 5 and args[1:4] == ["imagetools", "inspect", "--raw"]:
    digest = args[4].partition("@")[2]
    emit_hex(scenario["raw_by_digest"][digest])
elif len(args) == 6 and args[1:5] == [
    "imagetools", "inspect", "--format", "{{{{json .Image}}}}"
]:
    digest = args[5].partition("@")[2]
    emit_hex(scenario["image_by_digest"][digest])
else:
    print("unexpected invocation", file=sys.stderr)
    raise SystemExit(98)
"""
    docker = fake_bin / "docker"
    docker.write_text(source, encoding="utf-8")
    docker.chmod(0o755)
    shutil.copy2(docker, fake_bin / "docker-buildx")
    (fake_bin / "docker-buildx").chmod(0o755)


def _network_events(events_path: Path, tagged: str) -> list[list[str]]:
    result: list[list[str]] = []
    for line in events_path.read_text(encoding="utf-8").splitlines():
        args = json.loads(line)["args"]
        if args[:2] == ["buildx", "imagetools"]:
            if args[3:5] == ["--format", "{{json .Manifest}}"]:
                reference = args[5]
                result.append(
                    ["descriptor", "tag" if reference == tagged else "immutable"]
                )
            elif args[3] == "--raw":
                result.append(["raw", args[4].partition("@")[2]])
            elif args[3:5] == ["--format", "{{json .Image}}"]:
                result.append(["image", args[5].partition("@")[2]])
        elif args[:1] == ["push"]:
            result.append(["push", args[1]])
        elif (
            args[:2] == ["image", "rm"]
            and len(args) == 3
            and args[2].startswith("seoul-education-travel-map:release-gate-")
        ):
            result.append(["release-tag-rm", args[2]])
    return result


def _run_publisher(
    tmp_path: Path,
    *,
    target: str | None,
    mismatch: str | None,
    tool_parent: Path | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[list[str]], tuple[str, ...]]:
    scenario = _scenario(target, mismatch)
    fake_bin = (tool_parent or tmp_path) / "safe-bin"
    fake_bin.mkdir(mode=0o700)
    scenario_path = tmp_path / "scenario.json"
    scenario_path.write_text(json.dumps(scenario), encoding="utf-8")
    events_path = tmp_path / "events.jsonl"
    _write_fake_docker_tools(fake_bin, scenario_path, events_path)

    repository = tmp_path / "repository"
    publisher = repository / "apps/travel-map/deploy/nas/publish-reviewed-image.sh"
    publisher.parent.mkdir(parents=True)
    source = PUBLISHER.read_text(encoding="utf-8")
    replacements = {TOOL_PATH_ASSIGNMENT: f"tool_search_path={fake_bin}:$trusted_path"}
    for original, replacement in replacements.items():
        assert source.count(original) == 1
        source = source.replace(original, replacement, 1)
    publisher.write_text(source, encoding="utf-8")
    publisher.chmod(0o755)

    subprocess.run(["/usr/bin/git", "-C", str(repository), "init", "-q"], check=True)
    subprocess.run(
        [
            "/usr/bin/git",
            "-C",
            str(repository),
            "config",
            "user.name",
            "Descriptor Test",
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
            "descriptor-test@example.invalid",
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
    image_id = str(scenario["image_id"])
    image_tag = f"seoul-education-travel-map:release-gate-{git_sha}"
    image_id_hex = image_id.removeprefix("sha256:")
    tagged = f"{REGISTRY}:{git_sha}-sha256-{image_id_hex}"
    record_parent = tmp_path / "operator-input-must-not-echo"
    record_parent.mkdir(mode=0o700)
    record = record_parent / "gated-image.record"
    record_payload = (
        f"imageTag={image_tag}\n"
        f"imageId={image_id}\n"
        "platform=linux/amd64\n"
        f"gitSha={git_sha}\n"
    ).encode("ascii")
    record.write_bytes(record_payload)
    record.chmod(0o600)

    socket_path = _short_socket_path(tmp_path)
    docker_config = tmp_path / "protected-docker"
    docker_config.mkdir(mode=0o700)
    environment = dict(os.environ)
    environment.update(
        {
            "BUILDKIT_HOST": "tcp://attacker.invalid:1234",
            "DOCKER_CONFIG": str(docker_config),
            "DOCKER_CONTEXT": "attacker-context",
            "DOCKER_HOST": "tcp://attacker.invalid:2375",
            "PATH": "/nonexistent",
        }
    )
    with _unix_socket(socket_path):
        _write_docker_config(docker_config, socket_path)
        completed = subprocess.run(
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
    inputs = (str(record), image_tag, image_id, git_sha)
    network_events = (
        _network_events(events_path, tagged) if events_path.exists() else []
    )
    return completed, network_events, inputs


@pytest.mark.parametrize(
    ("target", "mismatch"),
    [
        ("classic-root", "digest"),
        ("classic-root", "size"),
        ("index-root", "digest"),
        ("index-root", "size"),
        ("runnable-child", "digest"),
        ("runnable-child", "size"),
        ("attestation-child", "digest"),
        ("attestation-child", "size"),
        ("classic-config", "digest"),
        ("runnable-config", "digest"),
    ],
)
def test_publisher_rejects_raw_bytes_that_do_not_match_their_descriptor(
    tmp_path: Path,
    target: str,
    mismatch: str,
) -> None:
    completed, network_events, inputs = _run_publisher(
        tmp_path,
        target=target,
        mismatch=mismatch,
    )
    expected = _scenario(target, mismatch)["expected_network"]

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert "BLOCKED_REMOTE_IMAGE_MISMATCH" in completed.stderr
    assert "Traceback" not in completed.stderr
    assert all(value not in completed.stderr for value in inputs)
    assert network_events == expected
    assert not any(event[0] == "push" for event in network_events)


def test_publisher_accepts_a_fully_descriptor_bound_index(
    tmp_path: Path,
) -> None:
    completed, network_events, _ = _run_publisher(
        tmp_path,
        target=None,
        mismatch=None,
    )
    scenario = _scenario(None, None)
    assert all(
        b"\n" in bytes.fromhex(payload)
        for payload in scenario["image_by_digest"].values()
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == f"{REGISTRY}@{scenario['root_descriptor']['digest']}\n"
    assert network_events == [
        *scenario["expected_network"],
        ["descriptor", "immutable"],
        ["descriptor", "tag"],
    ]
    assert "Traceback" not in completed.stderr


# Break caught: Linux CI keeps pytest's private tool directory below sticky /tmp.
def test_publisher_accepts_private_tools_below_sticky_tmp(tmp_path: Path) -> None:
    with tempfile.TemporaryDirectory(
        prefix="tm-desc-tools-",
        dir=Path("/tmp").resolve(strict=True),
    ) as tool_parent:
        completed, network_events, _ = _run_publisher(
            tmp_path,
            target=None,
            mismatch=None,
            tool_parent=Path(tool_parent),
        )

    assert completed.returncode == 0, completed.stderr
    assert network_events[-1][0] == "descriptor"
