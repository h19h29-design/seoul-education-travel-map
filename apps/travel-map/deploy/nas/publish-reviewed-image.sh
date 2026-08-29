#!/bin/sh
set -eu

umask 077

blocked() {
    printf '%s\n' "$1" >&2
    exit 2
}

remove_private_directory() {
    directory=$1
    [ -n "$directory" ] || return 0
    [ -d "$directory" ] && [ ! -L "$directory" ] || return 1
    /usr/bin/find "$directory" -mindepth 1 -depth -delete >/dev/null 2>&1 \
        && /bin/rmdir "$directory"
}

validate_private_directory() {
    /usr/bin/python3 -I -S - "$1" <<'PY'
import os
import json
import stat
import sys
from pathlib import Path

try:
    details = Path(sys.argv[1]).lstat()
    if (
        not stat.S_ISDIR(details.st_mode)
        or stat.S_IMODE(details.st_mode) != 0o700
        or details.st_uid != os.getuid()
    ):
        raise ValueError
except (OSError, ValueError):
    raise SystemExit(2) from None
PY
}

[ "$#" -eq 6 ] || {
    printf '%s\n' 'usage: publish-reviewed-image.sh RECORD EXPECTED_IMAGE_TAG EXPECTED_IMAGE_ID EXPECTED_PLATFORM EXPECTED_GIT_SHA EXPECTED_RECORD_SHA256' >&2
    exit 64
}

record=$1
expected_image_tag=$2
expected_image_id=$3
nas_platform=$4
git_sha=$5
expected_record_sha256=$6
registry=ghcr.io/h19h29-design/seoul-education-travel-map

[ "${#git_sha}" -eq 40 ] || blocked 'BLOCKED_INVALID_PUBLISH_INPUT'
case "$git_sha" in *[!0-9a-f]*) blocked 'BLOCKED_INVALID_PUBLISH_INPUT' ;; esac
expected_tag=seoul-education-travel-map:release-gate-$git_sha
[ "$expected_image_tag" = "$expected_tag" ] \
    || blocked 'BLOCKED_INVALID_PUBLISH_INPUT'
case "$expected_image_id" in
    sha256:????????????????????????????????????????????????????????????????) ;;
    *) blocked 'BLOCKED_INVALID_PUBLISH_INPUT' ;;
esac
case "${expected_image_id#sha256:}" in
    *[!0-9a-f]*) blocked 'BLOCKED_INVALID_PUBLISH_INPUT' ;;
esac
[ "${#expected_record_sha256}" -eq 64 ] \
    || blocked 'BLOCKED_INVALID_PUBLISH_INPUT'
case "$expected_record_sha256" in
    *[!0-9a-f]*) blocked 'BLOCKED_INVALID_PUBLISH_INPUT' ;;
esac
case "$nas_platform" in
    linux/amd64|linux/arm64) ;;
    *) blocked 'BLOCKED_INVALID_PUBLISH_INPUT' ;;
esac

validate_approved_record() {
    /usr/bin/python3 -I -S - \
    "$record" "$expected_image_tag" "$expected_image_id" \
    "$nas_platform" "$git_sha" "$expected_record_sha256" <<'PY'
import hashlib
import os
import stat
import sys
from pathlib import Path

record_path = Path(sys.argv[1])
image_tag, image_id, platform, git_sha, expected_hash = sys.argv[2:]
try:
    parent = record_path.parent
    parent_details = parent.lstat()
    if (
        record_path.name != "gated-image.record"
        or not record_path.is_absolute()
        or parent.resolve(strict=True) != parent
        or parent.is_symlink()
        or not stat.S_ISDIR(parent_details.st_mode)
        or stat.S_IMODE(parent_details.st_mode) != 0o700
        or parent_details.st_uid != os.getuid()
        or {entry.name for entry in parent.iterdir()} != {record_path.name}
    ):
        raise ValueError
    for ancestor in parent.parents:
        ancestor_details = ancestor.stat()
        shared_write = ancestor_details.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        if (
            not stat.S_ISDIR(ancestor_details.st_mode)
            or ancestor_details.st_uid not in {0, os.getuid()}
            or (shared_write and not ancestor_details.st_mode & stat.S_ISVTX)
        ):
            raise ValueError
    descriptor = os.open(
        record_path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        details = os.fstat(descriptor)
        payload = os.read(descriptor, 4097)
    finally:
        os.close(descriptor)
    path_details = record_path.lstat()
    expected_payload = (
        f"imageTag={image_tag}\n"
        f"imageId={image_id}\n"
        f"platform={platform}\n"
        f"gitSha={git_sha}\n"
    ).encode("ascii")
    if (
        not stat.S_ISREG(details.st_mode)
        or stat.S_IMODE(details.st_mode) != 0o600
        or details.st_uid != os.getuid()
        or details.st_nlink != 1
        or (details.st_dev, details.st_ino)
        != (path_details.st_dev, path_details.st_ino)
        or payload != expected_payload
        or hashlib.sha256(payload).hexdigest() != expected_hash
        or {entry.name for entry in parent.iterdir()} != {record_path.name}
    ):
        raise ValueError
except (OSError, UnicodeError, ValueError):
    raise SystemExit(2) from None
PY
}

validate_approved_record || blocked 'BLOCKED_INVALID_GATE_ATTESTATION'

case "$0" in
    /*) invoked_publisher_script=$0 ;;
    *) invoked_publisher_script=$PWD/$0 ;;
esac
invoked_publisher_directory=${invoked_publisher_script%/*}
invoked_publisher_name=${invoked_publisher_script##*/}
invoked_publisher_directory=$(CDPATH= cd -- "$invoked_publisher_directory" \
    && /bin/pwd -P) || blocked 'BLOCKED_INVALID_PUBLISH_CONTEXT'
invoked_publisher_script=$invoked_publisher_directory/$invoked_publisher_name

validate_publish_launcher() {
    /usr/bin/env -i \
        HOME=/var/empty PATH=/usr/bin:/bin TMPDIR=/tmp \
        GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 \
        GIT_NO_REPLACE_OBJECTS=1 GIT_OPTIONAL_LOCKS=0 \
        /usr/bin/python3 -I -S - "$@" <<'PY'
import hashlib
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

RELATIVE = Path("apps/travel-map/deploy/nas/publish-reviewed-image.sh")
GIT = "/usr/bin/git"


def git(repository, *arguments):
    return subprocess.run(
        [
            GIT,
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.untrackedCache=false",
            "-C",
            str(repository),
            *arguments,
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env={
            "HOME": "/var/empty",
            "PATH": "/usr/bin:/bin",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_LITERAL_PATHSPECS": "1",
        },
    ).stdout


def read_regular(path, expected_mode):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        details = os.fstat(descriptor)
        payload = bytearray()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            payload.extend(chunk)
    finally:
        os.close(descriptor)
    path_details = path.lstat()
    if (
        path.is_symlink()
        or not stat.S_ISREG(details.st_mode)
        or stat.S_IMODE(details.st_mode) != expected_mode
        or details.st_uid not in {0, os.getuid()}
        or details.st_nlink != 1
        or (details.st_dev, details.st_ino)
        != (path_details.st_dev, path_details.st_ino)
    ):
        raise ValueError
    return bytes(payload)


def approved_blob(repository, expected_sha):
    if (
        re.fullmatch(r"[0-9a-f]{40}", expected_sha) is None
        or repository.resolve(strict=True) != repository
        or repository.is_symlink()
        or not repository.is_dir()
        or git(repository, "rev-parse", "--show-toplevel").decode().strip()
        != str(repository)
        or git(repository, "rev-parse", "HEAD").decode().strip()
        != expected_sha
        or git(
            repository,
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=normal",
        )
        != b""
    ):
        raise ValueError
    tracked = [
        entry
        for entry in git(repository, "ls-files", "-v", "-z").split(b"\0")
        if entry
    ]
    if not tracked or any(not entry.startswith(b"H ") for entry in tracked):
        raise ValueError
    tree_entry = git(
        repository,
        "ls-tree",
        "-z",
        expected_sha,
        "--",
        RELATIVE.as_posix(),
    )
    match = re.fullmatch(
        rb"100755 blob ([0-9a-f]{40})\t"
        + re.escape(RELATIVE.as_posix().encode("ascii"))
        + rb"\0",
        tree_entry,
    )
    if match is None:
        raise ValueError
    payload = git(repository, "cat-file", "blob", match.group(1).decode("ascii"))
    source = repository / RELATIVE
    if read_regular(source, 0o755) != payload:
        raise ValueError
    return payload


try:
    action, repository_raw, launcher_raw, expected_sha, expected_hash = sys.argv[1:]
    repository = Path(repository_raw)
    launcher = Path(launcher_raw)
    payload = approved_blob(repository, expected_sha)
    payload_hash = hashlib.sha256(payload).hexdigest()
    if expected_hash and payload_hash != expected_hash:
        raise ValueError
    launcher_root = launcher.parent
    tmp_root = Path("/tmp").resolve(strict=True)
    root_details = launcher_root.lstat()
    if (
        launcher_root.parent != tmp_root
        or not launcher_root.name.startswith("travel-map-publish-launcher.")
        or launcher.name != "publish-reviewed-image.sh"
        or launcher_root.resolve(strict=True) != launcher_root
        or launcher_root.is_symlink()
        or not stat.S_ISDIR(root_details.st_mode)
        or stat.S_IMODE(root_details.st_mode) != 0o700
        or root_details.st_uid != os.getuid()
    ):
        raise ValueError
    if action == "create":
        if any(launcher_root.iterdir()) or expected_hash:
            raise ValueError
        descriptor = os.open(
            launcher,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o500,
        )
        try:
            os.fchmod(descriptor, 0o500)
            with os.fdopen(descriptor, "wb", closefd=False) as output:
                output.write(payload)
                output.flush()
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        root_descriptor = os.open(
            launcher_root,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(root_descriptor)
        finally:
            os.close(root_descriptor)
    elif action == "verify":
        if {entry.name for entry in launcher_root.iterdir()} != {launcher.name}:
            raise ValueError
        if read_regular(launcher, 0o500) != payload:
            raise ValueError
    else:
        raise ValueError
except (OSError, UnicodeError, ValueError, subprocess.SubprocessError):
    raise SystemExit(2) from None
print(hashlib.sha256(payload).hexdigest())
PY
}

clean_environment_marker=${TRAVEL_MAP_PUBLISH_CLEAN_ENVIRONMENT:-}
private_launcher_marker=${TRAVEL_MAP_PUBLISH_PRIVATE_LAUNCHER:-}
launcher_root=
cleanup_outer_launcher() {
    [ -n "$launcher_root" ] || return 0
    remove_private_directory "$launcher_root"
}
arm_private_launcher_cleanup() {
    launcher_root=${TRAVEL_MAP_PUBLISH_LAUNCHER_ROOT:-}
    /usr/bin/python3 -I -S - \
        "$launcher_root" "$invoked_publisher_script" <<'PY' \
        || blocked 'BLOCKED_INVALID_PUBLISH_CONTEXT'
import os
import stat
import sys
from pathlib import Path

try:
    root = Path(sys.argv[1])
    launcher = Path(sys.argv[2])
    tmp_root = Path("/tmp").resolve(strict=True)
    root_details = root.lstat()
    launcher_details = launcher.lstat()
    if (
        root.parent != tmp_root
        or not root.name.startswith("travel-map-publish-launcher.")
        or root.resolve(strict=True) != root
        or root.is_symlink()
        or not stat.S_ISDIR(root_details.st_mode)
        or stat.S_IMODE(root_details.st_mode) != 0o700
        or root_details.st_uid != os.getuid()
        or launcher != root / "publish-reviewed-image.sh"
        or launcher.is_symlink()
        or not stat.S_ISREG(launcher_details.st_mode)
        or stat.S_IMODE(launcher_details.st_mode) != 0o500
        or launcher_details.st_uid != os.getuid()
        or {entry.name for entry in root.iterdir()} != {launcher.name}
    ):
        raise ValueError
except (OSError, ValueError):
    raise SystemExit(2) from None
PY
    trap 'cleanup_outer_launcher' EXIT
    trap 'exit 2' HUP INT TERM
}
load_verified_private_launcher() {
    publisher_repository=${TRAVEL_MAP_PUBLISH_REPOSITORY:-}
    launcher_root=${TRAVEL_MAP_PUBLISH_LAUNCHER_ROOT:-}
    publisher_launcher_hash=${TRAVEL_MAP_PUBLISH_LAUNCHER_SHA256:-}
    [ "$invoked_publisher_script" \
        = "$launcher_root/publish-reviewed-image.sh" ] \
        || blocked 'BLOCKED_INVALID_PUBLISH_CONTEXT'
    [ "$(validate_publish_launcher verify \
        "$publisher_repository" "$invoked_publisher_script" \
        "$git_sha" "$publisher_launcher_hash")" \
        = "$publisher_launcher_hash" ] \
        || blocked 'BLOCKED_INVALID_PUBLISH_CONTEXT'
    publisher_script=$invoked_publisher_script
}
case "$clean_environment_marker" in
    '')
        case "$private_launcher_marker" in
            '')
                publisher_repository=$(CDPATH= cd -- \
                    "$invoked_publisher_directory/../../../.." \
                    && /bin/pwd -P) \
                    || blocked 'BLOCKED_INVALID_PUBLISH_CONTEXT'
                normalized_launcher_tmp=$(/usr/bin/python3 -I -S - <<'PY'
import os
import stat
from pathlib import Path

try:
    root = Path("/tmp").resolve(strict=True)
    details = root.lstat()
    if (
        not stat.S_ISDIR(details.st_mode)
        or details.st_uid not in {0, os.getuid()}
        or not details.st_mode & stat.S_ISVTX
    ):
        raise ValueError
except (OSError, ValueError):
    raise SystemExit(2) from None
print(root)
PY
                ) || blocked 'BLOCKED_INVALID_PUBLISH_CONTEXT'
                launcher_root=$(/usr/bin/mktemp -d \
                    "$normalized_launcher_tmp/travel-map-publish-launcher.XXXXXX") \
                    || blocked 'BLOCKED_INVALID_PUBLISH_CONTEXT'
                /bin/chmod 0700 "$launcher_root" \
                    || blocked 'BLOCKED_INVALID_PUBLISH_CONTEXT'
                trap 'cleanup_outer_launcher' EXIT
                trap 'exit 2' HUP INT TERM
                publisher_repository=$(CDPATH= cd -- "$publisher_repository" \
                    && /bin/pwd -P) \
                    || blocked 'BLOCKED_INVALID_PUBLISH_CONTEXT'
                publisher_script=$launcher_root/publish-reviewed-image.sh
                publisher_launcher_hash=$(validate_publish_launcher create \
                    "$publisher_repository" "$publisher_script" \
                    "$git_sha" '') \
                    || blocked 'BLOCKED_INVALID_PUBLISH_CONTEXT'
                case "$publisher_launcher_hash" in
                    ????????????????????????????????????????????????????????????????) ;;
                    *) blocked 'BLOCKED_INVALID_PUBLISH_CONTEXT' ;;
                esac
                case "$publisher_launcher_hash" in
                    *[!0-9a-f]*) blocked 'BLOCKED_INVALID_PUBLISH_CONTEXT' ;;
                esac
                exec /usr/bin/env -i \
                    HOME=/var/empty PATH=/usr/bin:/bin TMPDIR=/tmp \
                    DOCKER_CONFIG="${DOCKER_CONFIG:-}" \
                    TRAVEL_MAP_PUBLISH_PRIVATE_LAUNCHER=1 \
                    TRAVEL_MAP_PUBLISH_REPOSITORY="$publisher_repository" \
                    TRAVEL_MAP_PUBLISH_LAUNCHER_ROOT="$launcher_root" \
                    TRAVEL_MAP_PUBLISH_LAUNCHER_SHA256="$publisher_launcher_hash" \
                    "$publisher_script" "$@"
                ;;
            1)
                arm_private_launcher_cleanup
                load_verified_private_launcher
                ;;
            *) blocked 'BLOCKED_INVALID_PUBLISH_CONTEXT' ;;
        esac
        ;;
    1)
        [ "$private_launcher_marker" = 1 ] \
            || blocked 'BLOCKED_INVALID_PUBLISH_CONTEXT'
        load_verified_private_launcher
        ;;
    *) blocked 'BLOCKED_UNSAFE_RELEASE_ENVIRONMENT' ;;
esac

bootstrap_python() {
    /usr/bin/env -i \
        HOME=/var/empty \
        PATH=/usr/bin:/bin \
        TMPDIR=/tmp \
        /usr/bin/python3 -I -S "$@"
}

trusted_path=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin

resolve_publish_tool() {
    bootstrap_python - "$tool_search_path" "$1" <<'PY'
import os
import re
import shutil
import stat
import sys
from pathlib import Path

try:
    configured = shutil.which(sys.argv[2], path=sys.argv[1])
    if configured is None:
        raise ValueError
    resolved = Path(configured).resolve(strict=True)
    details = resolved.stat()
    if (
        not stat.S_ISREG(details.st_mode)
        or not os.access(resolved, os.X_OK)
        or details.st_uid not in {0, os.getuid()}
        or details.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise ValueError
    for parent in resolved.parents:
        parent_details = parent.stat()
        if (
            not stat.S_ISDIR(parent_details.st_mode)
            or parent_details.st_uid not in {0, os.getuid()}
            or parent_details.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise ValueError
except (OSError, ValueError):
    raise SystemExit(2) from None
print(resolved)
PY
}
capture_publish_tool_identity() {
    bootstrap_python - "$1" <<'PY'
import hashlib
import os
import stat
import sys
from pathlib import Path

try:
    path = Path(sys.argv[1])
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        details = os.fstat(descriptor)
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            payload = source.read()
    finally:
        os.close(descriptor)
    path_details = path.lstat()
    if (
        path.resolve(strict=True) != path
        or path.is_symlink()
        or not stat.S_ISREG(details.st_mode)
        or details.st_uid not in {0, os.getuid()}
        or details.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or (details.st_dev, details.st_ino)
        != (path_details.st_dev, path_details.st_ino)
    ):
        raise ValueError
except (OSError, ValueError):
    raise SystemExit(2) from None
print(
    ":".join(
        (
            str(details.st_uid),
            str(stat.S_IMODE(details.st_mode)),
            str(details.st_dev),
            str(details.st_ino),
            str(details.st_size),
            hashlib.sha256(payload).hexdigest(),
        )
    )
)
PY
}
case "$clean_environment_marker" in
    '')
        canonical_home=$(bootstrap_python - <<'PY'
import os
import pwd
import stat
from pathlib import Path

try:
    configured = Path(pwd.getpwuid(os.getuid()).pw_dir)
    resolved = configured.resolve(strict=True)
    details = resolved.stat()
    if (
        not configured.is_absolute()
        or configured != resolved
        or not stat.S_ISDIR(details.st_mode)
        or details.st_uid != os.getuid()
        or details.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise ValueError
except (KeyError, OSError, ValueError):
    raise SystemExit(2) from None
print(resolved)
PY
        ) || blocked 'BLOCKED_UNSAFE_RELEASE_ENVIRONMENT'
        tool_search_path=$canonical_home/.local/bin:/opt/homebrew/bin:/usr/local/bin:$trusted_path
        docker_tool=$(resolve_publish_tool docker) \
            || blocked 'BLOCKED_UNSAFE_RELEASE_ENVIRONMENT'
        buildx_tool=$(resolve_publish_tool docker-buildx) \
            || blocked 'BLOCKED_UNSAFE_RELEASE_ENVIRONMENT'
        docker_tool_identity=$(capture_publish_tool_identity "$docker_tool") \
            || blocked 'BLOCKED_UNSAFE_RELEASE_ENVIRONMENT'
        buildx_tool_identity=$(capture_publish_tool_identity "$buildx_tool") \
            || blocked 'BLOCKED_UNSAFE_RELEASE_ENVIRONMENT'
        ;;
    1)
        docker_tool=${TRAVEL_MAP_PUBLISH_SOURCE_DOCKER_TOOL:-}
        buildx_tool=${TRAVEL_MAP_PUBLISH_SOURCE_BUILDX_TOOL:-}
        docker_tool_identity=${TRAVEL_MAP_PUBLISH_DOCKER_IDENTITY:-}
        buildx_tool_identity=${TRAVEL_MAP_PUBLISH_BUILDX_IDENTITY:-}
        [ -n "$docker_tool" ] && [ -n "$buildx_tool" ] \
            && [ -n "$docker_tool_identity" ] \
            && [ -n "$buildx_tool_identity" ] \
            || blocked 'BLOCKED_UNSAFE_RELEASE_ENVIRONMENT'
        ;;
    *) blocked 'BLOCKED_UNSAFE_RELEASE_ENVIRONMENT' ;;
esac

PATH=$trusted_path
export PATH

run_docker() {
    verify_publish_tools || return 1
    DOCKER_CONFIG=$publisher_docker_config DOCKER_HOST=$publisher_docker_host \
        "$docker_tool" "$@"
}

run_buildx() {
    verify_publish_tools || return 1
    DOCKER_CONFIG=$publisher_docker_config DOCKER_HOST=$publisher_docker_host \
        "$buildx_tool" "$@"
}

normalized_tmp_root=$(bootstrap_python - /tmp <<'PY'
import os
import stat
import sys
from pathlib import Path

try:
    configured = Path(sys.argv[1])
    resolved = configured.resolve(strict=True)
    details = resolved.stat()
    shared_write = details.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    if (
        not configured.is_absolute()
        or not stat.S_ISDIR(details.st_mode)
        or details.st_uid not in {0, os.getuid()}
        or (shared_write and not details.st_mode & stat.S_ISVTX)
    ):
        raise ValueError
except (OSError, ValueError):
    raise SystemExit(2) from None
print(resolved)
PY
) || blocked 'BLOCKED_UNSAFE_RELEASE_ENVIRONMENT'

docker_config=${DOCKER_CONFIG:-}
validate_publisher_docker_authority() {
    bootstrap_python - "$1" "$2" "$3" <<'PY'
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path


def strict_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError
        value[key] = item
    return value


def read_protected_file(path):
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        details = os.fstat(descriptor)
        payload = bytearray()
        while len(payload) <= 65_536:
            chunk = os.read(descriptor, 65_537 - len(payload))
            if not chunk:
                break
            payload.extend(chunk)
    finally:
        os.close(descriptor)
    path_details = path.lstat()
    if (
        not stat.S_ISREG(details.st_mode)
        or stat.S_IMODE(details.st_mode) != 0o600
        or details.st_uid != os.getuid()
        or details.st_nlink != 1
        or len(payload) > 65_536
        or (details.st_dev, details.st_ino)
        != (path_details.st_dev, path_details.st_ino)
    ):
        raise ValueError
    return details, bytes(payload)


def decode_json(payload):
    return json.loads(
        payload.decode("utf-8"),
        object_pairs_hook=strict_object,
        parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
    )


def validate_directory(path, children):
    details = path.lstat()
    if (
        path.is_symlink()
        or path.resolve(strict=True) != path
        or not stat.S_ISDIR(details.st_mode)
        or stat.S_IMODE(details.st_mode) != 0o700
        or details.st_uid != os.getuid()
        or {entry.name for entry in path.iterdir()} != children
    ):
        raise ValueError
    return details


try:
    configured = Path(sys.argv[1])
    expected_host = sys.argv[2]
    expected_identity = sys.argv[3]
    if bool(expected_host) != bool(expected_identity):
        raise ValueError
    resolved = configured.resolve(strict=True)
    config_path = configured / "config.json"
    if (
        not configured.is_absolute()
        or configured != resolved
    ):
        raise ValueError
    for ancestor in resolved.parents:
        ancestor_details = ancestor.lstat()
        shared_write = ancestor_details.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        if (
            not stat.S_ISDIR(ancestor_details.st_mode)
            or ancestor_details.st_uid not in {0, os.getuid()}
            or (shared_write and not ancestor_details.st_mode & stat.S_ISVTX)
        ):
            raise ValueError
    root_children = {entry.name for entry in resolved.iterdir()}
    if root_children not in ({"config.json"}, {"config.json", "contexts"}):
        raise ValueError
    root_details = validate_directory(resolved, root_children)
    config_details, config_bytes = read_protected_file(config_path)
    payload = decode_json(config_bytes)
    if type(payload) is not dict or set(payload) not in (
        {"auths"},
        {"auths", "currentContext"},
    ):
        raise ValueError
    auths = payload.get("auths")
    if type(auths) is not dict or set(auths) != {"ghcr.io"}:
        raise ValueError
    credentials = auths.get("ghcr.io")
    if (
        type(credentials) is not dict
        or set(credentials) != {"auth"}
        or type(credentials.get("auth")) is not str
        or not credentials["auth"]
        or len(credentials["auth"]) > 4096
        or any(
            ord(character) <= 0x20 or ord(character) > 0x7E
            for character in credentials["auth"]
        )
    ):
        raise ValueError

    context_name = payload.get("currentContext", "default")
    if (
        type(context_name) is not str
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,127}", context_name)
        is None
    ):
        raise ValueError
    metadata_bytes = b""
    metadata_details = None
    if context_name == "default":
        if root_children != {"config.json"}:
            raise ValueError
        host = "unix:///var/run/docker.sock"
    else:
        if root_children != {"config.json", "contexts"}:
            raise ValueError
        context_id = hashlib.sha256(context_name.encode("utf-8")).hexdigest()
        contexts = resolved / "contexts"
        metadata_root = contexts / "meta"
        selected_context = metadata_root / context_id
        validate_directory(contexts, {"meta"})
        validate_directory(metadata_root, {context_id})
        validate_directory(selected_context, {"meta.json"})
        metadata_path = selected_context / "meta.json"
        metadata_details, metadata_bytes = read_protected_file(metadata_path)
        metadata = decode_json(metadata_bytes)
        if (
            type(metadata) is not dict
            or set(metadata) != {"Name", "Metadata", "Endpoints"}
            or metadata.get("Name") != context_name
            or type(metadata.get("Metadata")) is not dict
            or type(metadata.get("Endpoints")) is not dict
            or set(metadata["Endpoints"]) != {"docker"}
        ):
            raise ValueError
        endpoint = metadata["Endpoints"]["docker"]
        if (
            type(endpoint) is not dict
            or set(endpoint) != {"Host", "SkipTLSVerify"}
            or endpoint.get("SkipTLSVerify") is not False
        ):
            raise ValueError
        host = endpoint.get("Host")

    if (
        type(host) is not str
        or not host.startswith("unix:///")
        or len(host) > 4096
        or any(character.isspace() or ord(character) < 0x20 for character in host)
    ):
        raise ValueError
    socket_path = Path(host.removeprefix("unix://"))
    if (
        not socket_path.is_absolute()
        or str(socket_path) != os.path.normpath(socket_path)
        or any(part in {"", ".", ".."} for part in socket_path.parts[1:])
    ):
        raise ValueError
    socket_details = socket_path.lstat()
    socket_permissions = stat.S_IMODE(socket_details.st_mode)
    allowed_group_socket = (
        socket_details.st_uid == 0
        and socket_details.st_gid in {os.getgid(), *os.getgroups()}
        and not socket_permissions & 0o017
    )
    if (
        socket_path.is_symlink()
        or socket_path.resolve(strict=True) != socket_path
        or not stat.S_ISSOCK(socket_details.st_mode)
        or socket_details.st_uid not in {0, os.getuid()}
        or socket_permissions & 0o007
        or (socket_permissions & 0o070 and not allowed_group_socket)
    ):
        raise ValueError
    for ancestor in socket_path.parents:
        ancestor_details = ancestor.lstat()
        shared_write = ancestor_details.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        if (
            not stat.S_ISDIR(ancestor_details.st_mode)
            or ancestor_details.st_uid not in {0, os.getuid()}
            or (shared_write and not ancestor_details.st_mode & stat.S_ISVTX)
        ):
            raise ValueError

    identity_parts = [
        host.encode("utf-8"),
        config_bytes,
        metadata_bytes,
        ":".join(
            str(value)
            for value in (
                root_details.st_dev,
                root_details.st_ino,
                config_details.st_dev,
                config_details.st_ino,
                socket_details.st_dev,
                socket_details.st_ino,
                socket_details.st_uid,
                stat.S_IMODE(socket_details.st_mode),
            )
        ).encode("ascii"),
    ]
    if metadata_details is not None:
        identity_parts.append(
            f"{metadata_details.st_dev}:{metadata_details.st_ino}".encode("ascii")
        )
    authority_identity = hashlib.sha256(b"\0".join(identity_parts)).hexdigest()
    if expected_host and (
        host != expected_host or authority_identity != expected_identity
    ):
        raise ValueError
except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
    raise SystemExit(2) from None
print(host, authority_identity)
PY
}

case "$clean_environment_marker" in
    '')
        docker_authority=$(validate_publisher_docker_authority \
            "$docker_config" '' '') \
            || blocked 'BLOCKED_INVALID_DOCKER_CONFIG'
        docker_host=${docker_authority% *}
        docker_authority_identity=${docker_authority##* }
        [ -n "$docker_host" ] && [ -n "$docker_authority_identity" ] \
            && [ "$docker_authority" = "$docker_host $docker_authority_identity" ] \
            || blocked 'BLOCKED_INVALID_DOCKER_CONFIG'
        ;;
    1)
        docker_host=${TRAVEL_MAP_PUBLISH_DOCKER_HOST:-}
        docker_authority_identity=${TRAVEL_MAP_PUBLISH_DOCKER_AUTHORITY_IDENTITY:-}
        [ "$(validate_publisher_docker_authority \
            "$docker_config" "$docker_host" "$docker_authority_identity")" \
            = "$docker_host $docker_authority_identity" ] \
            || blocked 'BLOCKED_INVALID_DOCKER_CONFIG'
        ;;
    *) blocked 'BLOCKED_UNSAFE_RELEASE_ENVIRONMENT' ;;
esac

case "$clean_environment_marker" in
    '')
        private_environment=$(/usr/bin/mktemp -d \
            "$normalized_tmp_root/travel-map-publish-environment.XXXXXX") \
            || blocked 'BLOCKED_PRIVATE_PUBLISH_DIRECTORY'
        /bin/chmod 0700 "$private_environment" \
            || blocked 'BLOCKED_PRIVATE_PUBLISH_DIRECTORY'
        private_home=$private_environment/home
        private_xdg_config=$private_environment/xdg-config
        private_xdg_cache=$private_environment/xdg-cache
        private_xdg_data=$private_environment/xdg-data
        /bin/mkdir -m 0700 \
            "$private_home" "$private_xdg_config" "$private_xdg_cache" \
            "$private_xdg_data" \
            || blocked 'BLOCKED_PRIVATE_PUBLISH_DIRECTORY'
        exec /usr/bin/env -i \
            HOME=/var/empty PATH=/usr/bin:/bin TMPDIR=/tmp \
            /usr/bin/python3 -I -S - \
            "$publisher_script" "$private_environment" "$trusted_path" \
            "$normalized_tmp_root" "$record" "$expected_image_tag" \
            "$expected_image_id" "$nas_platform" "$git_sha" \
            "$expected_record_sha256" \
            "$docker_config" "$docker_host" "$docker_authority_identity" \
            "$docker_tool_identity" "$buildx_tool_identity" \
            "$docker_tool" "$buildx_tool" "$launcher_root" \
            "$publisher_repository" "$publisher_launcher_hash" <<'PY'
from __future__ import annotations

import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

(
    script,
    private_root_raw,
    trusted_path,
    tmp_root_raw,
    record,
    expected_image_tag,
    expected_image_id,
    nas_platform,
    git_sha,
    expected_record_sha256,
    docker_config,
    docker_host,
    docker_authority_identity,
    docker_tool_identity,
    buildx_tool_identity,
    source_docker_tool,
    source_buildx_tool,
    launcher_root_raw,
    publisher_repository,
    publisher_launcher_hash,
) = sys.argv[1:]
private_root = Path(private_root_raw)
launcher_root = Path(launcher_root_raw)
tmp_root = Path(tmp_root_raw)
process: subprocess.Popen[bytes] | None = None
interrupted = False
termination_deadline: float | None = None


def forward_signal(signum: int, _frame: object) -> None:
    global interrupted, termination_deadline
    interrupted = True
    termination_deadline = time.monotonic() + 5
    if process is not None:
        try:
            os.killpg(process.pid, signum)
        except ProcessLookupError:
            pass


def remove_private_root() -> None:
    details = private_root.lstat()
    if (
        private_root.parent != tmp_root
        or not private_root.name.startswith("travel-map-publish-environment.")
        or private_root.resolve(strict=True) != private_root
        or not stat.S_ISDIR(details.st_mode)
        or stat.S_IMODE(details.st_mode) != 0o700
        or details.st_uid != os.getuid()
    ):
        raise OSError
    shutil.rmtree(private_root)


def remove_launcher_root() -> None:
    details = launcher_root.lstat()
    if (
        launcher_root.parent != tmp_root
        or not launcher_root.name.startswith("travel-map-publish-launcher.")
        or launcher_root.resolve(strict=True) != launcher_root
        or not stat.S_ISDIR(details.st_mode)
        or stat.S_IMODE(details.st_mode) != 0o700
        or details.st_uid != os.getuid()
    ):
        raise OSError
    shutil.rmtree(launcher_root)


def reap_process_group() -> None:
    if process is None:
        return
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 2
    while True:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return
        if time.monotonic() >= deadline:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                return
            kill_deadline = time.monotonic() + 2
            while time.monotonic() < kill_deadline:
                try:
                    os.killpg(process.pid, 0)
                except ProcessLookupError:
                    return
                time.sleep(0.01)
            raise OSError
        time.sleep(0.01)


def drain_stdout(captured: bytearray) -> bool:
    if process is None or process.stdout is None:
        raise OSError
    reached_eof = False
    while True:
        try:
            chunk = os.read(process.stdout.fileno(), 65_536)
        except BlockingIOError:
            break
        if not chunk:
            reached_eof = True
            break
        if len(captured) <= 4096:
            captured.extend(chunk[: 4097 - len(captured)])
    return reached_eof


def validated_stdout(captured: bytes) -> bytes:
    try:
        value = captured.decode("ascii")
    except UnicodeError:
        raise OSError from None
    if re.fullmatch(
        r"ghcr\.io/h19h29-design/seoul-education-travel-map@sha256:"
        r"[0-9a-f]{64}\n",
        value,
    ) is None:
        raise OSError
    return captured


for handled_signal in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
    signal.signal(handled_signal, forward_signal)

environment = {
    "PATH": trusted_path,
    "HOME": str(private_root / "home"),
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "CI": "1",
    "TMPDIR": str(tmp_root),
    "DOCKER_CONFIG": docker_config,
    "TRAVEL_MAP_PUBLISH_DOCKER_HOST": docker_host,
    "TRAVEL_MAP_PUBLISH_DOCKER_AUTHORITY_IDENTITY": docker_authority_identity,
    "XDG_CONFIG_HOME": str(private_root / "xdg-config"),
    "XDG_CACHE_HOME": str(private_root / "xdg-cache"),
    "XDG_DATA_HOME": str(private_root / "xdg-data"),
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_NO_REPLACE_OBJECTS": "1",
    "TRAVEL_MAP_PUBLISH_PRIVATE_ROOT": str(private_root),
    "TRAVEL_MAP_PUBLISH_CLEAN_ENVIRONMENT": "1",
    "TRAVEL_MAP_PUBLISH_PRIVATE_LAUNCHER": "1",
    "TRAVEL_MAP_PUBLISH_DOCKER_IDENTITY": docker_tool_identity,
    "TRAVEL_MAP_PUBLISH_BUILDX_IDENTITY": buildx_tool_identity,
    "TRAVEL_MAP_PUBLISH_SOURCE_DOCKER_TOOL": source_docker_tool,
    "TRAVEL_MAP_PUBLISH_SOURCE_BUILDX_TOOL": source_buildx_tool,
    "TRAVEL_MAP_PUBLISH_LAUNCHER_ROOT": str(launcher_root),
    "TRAVEL_MAP_PUBLISH_REPOSITORY": publisher_repository,
    "TRAVEL_MAP_PUBLISH_LAUNCHER_SHA256": publisher_launcher_hash,
}
status = 2
failure_message: str | None = None
captured = bytearray()
try:
    if interrupted:
        raise OSError
    process = subprocess.Popen(
        [
            "/bin/sh",
            script,
            record,
            expected_image_tag,
            expected_image_id,
            nas_platform,
            git_sha,
            expected_record_sha256,
        ],
        env=environment,
        stdout=subprocess.PIPE,
        start_new_session=True,
    )
    if process.stdout is None:
        raise OSError
    os.set_blocking(process.stdout.fileno(), False)
    if interrupted and process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
    while True:
        drain_stdout(captured)
        polled = process.poll()
        if polled is not None:
            status = polled
            break
        if termination_deadline is not None and time.monotonic() >= termination_deadline:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            status = process.wait()
            break
        time.sleep(0.01)
    reap_process_group()
    drain_deadline = time.monotonic() + 1
    while not drain_stdout(captured):
        if time.monotonic() >= drain_deadline:
            raise OSError
        time.sleep(0.01)
except OSError:
    failure_message = "BLOCKED_UNSAFE_RELEASE_ENVIRONMENT"
finally:
    cleanup_failed = False
    try:
        remove_private_root()
    except OSError:
        cleanup_failed = True
    try:
        remove_launcher_root()
    except OSError:
        cleanup_failed = True
    if cleanup_failed:
        failure_message = "BLOCKED_PUBLISH_CLEANUP_FAILED"

if failure_message is not None:
    print(failure_message, file=sys.stderr)
    raise SystemExit(2)
if interrupted or status < 0:
    raise SystemExit(2)
if status == 0:
    try:
        output = validated_stdout(bytes(captured))
        signal.pthread_sigmask(
            signal.SIG_BLOCK,
            {signal.SIGHUP, signal.SIGINT, signal.SIGTERM},
        )
        if interrupted:
            raise OSError
        os.write(sys.stdout.fileno(), output)
    except OSError:
        print("BLOCKED_INVALID_PUBLISH_OUTPUT", file=sys.stderr)
        raise SystemExit(2) from None
raise SystemExit(status)
PY
        ;;
    1) ;;
    *) blocked 'BLOCKED_UNSAFE_RELEASE_ENVIRONMENT' ;;
esac

unset CPATH LIBRARY_PATH MANPATH SDKROOT __CF_USER_TEXT_ENCODING
/usr/bin/python3 -I -S - \
    "$trusted_path" "$normalized_tmp_root" "$docker_config" \
    "$TRAVEL_MAP_PUBLISH_DOCKER_HOST" \
    "$TRAVEL_MAP_PUBLISH_DOCKER_AUTHORITY_IDENTITY" \
    "$TRAVEL_MAP_PUBLISH_DOCKER_IDENTITY" \
    "$TRAVEL_MAP_PUBLISH_BUILDX_IDENTITY" \
    "$TRAVEL_MAP_PUBLISH_SOURCE_DOCKER_TOOL" \
    "$TRAVEL_MAP_PUBLISH_SOURCE_BUILDX_TOOL" \
    "$TRAVEL_MAP_PUBLISH_LAUNCHER_ROOT" \
    "$TRAVEL_MAP_PUBLISH_REPOSITORY" \
    "$TRAVEL_MAP_PUBLISH_LAUNCHER_SHA256" <<'PY' \
    || blocked 'BLOCKED_UNSAFE_RELEASE_ENVIRONMENT'
import os
import stat
import sys
from pathlib import Path

expected = {
    "PATH": sys.argv[1],
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "CI": "1",
    "TMPDIR": sys.argv[2],
    "DOCKER_CONFIG": sys.argv[3],
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_NO_REPLACE_OBJECTS": "1",
    "TRAVEL_MAP_PUBLISH_CLEAN_ENVIRONMENT": "1",
    "TRAVEL_MAP_PUBLISH_PRIVATE_LAUNCHER": "1",
    "TRAVEL_MAP_PUBLISH_DOCKER_HOST": sys.argv[4],
    "TRAVEL_MAP_PUBLISH_DOCKER_AUTHORITY_IDENTITY": sys.argv[5],
    "TRAVEL_MAP_PUBLISH_DOCKER_IDENTITY": sys.argv[6],
    "TRAVEL_MAP_PUBLISH_BUILDX_IDENTITY": sys.argv[7],
    "TRAVEL_MAP_PUBLISH_SOURCE_DOCKER_TOOL": sys.argv[8],
    "TRAVEL_MAP_PUBLISH_SOURCE_BUILDX_TOOL": sys.argv[9],
    "TRAVEL_MAP_PUBLISH_LAUNCHER_ROOT": sys.argv[10],
    "TRAVEL_MAP_PUBLISH_REPOSITORY": sys.argv[11],
    "TRAVEL_MAP_PUBLISH_LAUNCHER_SHA256": sys.argv[12],
}
private_layout = {
    "HOME": "home",
    "XDG_CONFIG_HOME": "xdg-config",
    "XDG_CACHE_HOME": "xdg-cache",
    "XDG_DATA_HOME": "xdg-data",
}
private_root_name = "TRAVEL_MAP_PUBLISH_PRIVATE_ROOT"
required_paths = set(private_layout) | {private_root_name}
shell_metadata = {"PWD", "SHLVL", "_"}
apple_python_metadata = {
    "CPATH",
    "LIBRARY_PATH",
    "MANPATH",
    "SDKROOT",
    "__CF_USER_TEXT_ENCODING",
}
try:
    if any(os.environ.get(name) != value for name, value in expected.items()):
        raise ValueError
    if not required_paths.issubset(os.environ):
        raise ValueError
    if (
        set(os.environ)
        - set(expected)
        - required_paths
        - shell_metadata
        - apple_python_metadata
    ):
        raise ValueError
    private_root = Path(os.environ[private_root_name])
    private_root_details = private_root.lstat()
    if (
        not private_root.is_absolute()
        or private_root.resolve(strict=True) != private_root
        or not stat.S_ISDIR(private_root_details.st_mode)
        or stat.S_IMODE(private_root_details.st_mode) != 0o700
        or private_root_details.st_uid != os.getuid()
    ):
        raise ValueError
    if {entry.name for entry in private_root.iterdir()} != set(
        private_layout.values()
    ):
        raise ValueError
    for name, leaf in private_layout.items():
        path = Path(os.environ[name])
        details = path.lstat()
        if (
            path != private_root / leaf
            or not stat.S_ISDIR(details.st_mode)
            or stat.S_IMODE(details.st_mode) != 0o700
            or details.st_uid != os.getuid()
            or any(path.iterdir())
        ):
            raise ValueError
except (OSError, ValueError):
    raise SystemExit(2) from None
PY

publisher_docker_config=$DOCKER_CONFIG
publisher_docker_host=$TRAVEL_MAP_PUBLISH_DOCKER_HOST
publisher_docker_authority_identity=$TRAVEL_MAP_PUBLISH_DOCKER_AUTHORITY_IDENTITY
source_docker_tool=$docker_tool
source_buildx_tool=$buildx_tool
expected_docker_tool_identity=$TRAVEL_MAP_PUBLISH_DOCKER_IDENTITY
expected_buildx_tool_identity=$TRAVEL_MAP_PUBLISH_BUILDX_IDENTITY
unset DOCKER_CONFIG TRAVEL_MAP_PUBLISH_DOCKER_HOST \
    TRAVEL_MAP_PUBLISH_DOCKER_AUTHORITY_IDENTITY \
    TRAVEL_MAP_PUBLISH_DOCKER_IDENTITY TRAVEL_MAP_PUBLISH_BUILDX_IDENTITY \
    TRAVEL_MAP_PUBLISH_SOURCE_DOCKER_TOOL \
    TRAVEL_MAP_PUBLISH_SOURCE_BUILDX_TOOL

[ "$(validate_publisher_docker_authority \
    "$publisher_docker_config" "$publisher_docker_host" \
    "$publisher_docker_authority_identity")" \
    = "$publisher_docker_host $publisher_docker_authority_identity" ] \
    || blocked 'BLOCKED_INVALID_DOCKER_CONFIG'

record_parent=$(/usr/bin/mktemp -d "$TMPDIR/travel-map-publish.XXXXXX") \
    || blocked 'BLOCKED_PRIVATE_PUBLISH_DIRECTORY'
/bin/chmod 0700 "$record_parent" \
    || blocked 'BLOCKED_PRIVATE_PUBLISH_DIRECTORY'
publish_bin=$record_parent/trusted-bin
/bin/mkdir -m 0700 "$publish_bin" \
    || blocked 'BLOCKED_PRIVATE_PUBLISH_DIRECTORY'
/usr/bin/python3 -I -S - \
    "$source_docker_tool" "$expected_docker_tool_identity" \
    "$source_buildx_tool" "$expected_buildx_tool_identity" \
    "$publish_bin" <<'PY' \
    || blocked 'BLOCKED_UNSAFE_RELEASE_ENVIRONMENT'
import hashlib
import os
import re
import stat
import sys
from pathlib import Path


def copy_verified(source_raw, identity_raw, destination):
    source = Path(source_raw)
    expected = identity_raw.split(":")
    if len(expected) != 6 or re.fullmatch(r"[0-9a-f]{64}", expected[5]) is None:
        raise ValueError
    source_descriptor = os.open(
        source,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        details = os.fstat(source_descriptor)
        payload = bytearray()
        while True:
            chunk = os.read(source_descriptor, 1024 * 1024)
            if not chunk:
                break
            payload.extend(chunk)
    finally:
        os.close(source_descriptor)
    actual = (
        str(details.st_uid),
        str(stat.S_IMODE(details.st_mode)),
        str(details.st_dev),
        str(details.st_ino),
        str(details.st_size),
        hashlib.sha256(payload).hexdigest(),
    )
    if (
        source.resolve(strict=True) != source
        or source.is_symlink()
        or not stat.S_ISREG(details.st_mode)
        or tuple(expected) != actual
    ):
        raise ValueError
    descriptor = os.open(
        destination,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o500,
    )
    with os.fdopen(descriptor, "wb") as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())
    destination.chmod(0o500)


try:
    root = Path(sys.argv[5])
    root_details = root.lstat()
    if (
        root.is_symlink()
        or root.resolve(strict=True) != root
        or not stat.S_ISDIR(root_details.st_mode)
        or stat.S_IMODE(root_details.st_mode) != 0o700
        or root_details.st_uid != os.getuid()
        or any(root.iterdir())
    ):
        raise ValueError
    copy_verified(sys.argv[1], sys.argv[2], root / "docker")
    copy_verified(sys.argv[3], sys.argv[4], root / "docker-buildx")
except (OSError, ValueError):
    raise SystemExit(2) from None
PY
docker_tool=$publish_bin/docker
buildx_tool=$publish_bin/docker-buildx

verify_publish_tools() {
    [ "$(validate_publisher_docker_authority \
        "$publisher_docker_config" "$publisher_docker_host" \
        "$publisher_docker_authority_identity")" \
        = "$publisher_docker_host $publisher_docker_authority_identity" ] \
        || return 1
    /usr/bin/python3 -I -S - \
        "$source_docker_tool" "$expected_docker_tool_identity" \
        "$source_buildx_tool" "$expected_buildx_tool_identity" \
        "$publish_bin" <<'PY'
import hashlib
import os
import re
import stat
import sys
from pathlib import Path


def identity(path):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        details = os.fstat(descriptor)
        payload = bytearray()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            payload.extend(chunk)
    finally:
        os.close(descriptor)
    return details, bytes(payload)


try:
    arguments = sys.argv[1:]
    for source_raw, expected_raw in (
        (arguments[0], arguments[1]),
        (arguments[2], arguments[3]),
    ):
        source = Path(source_raw)
        expected = expected_raw.split(":")
        details, payload = identity(source)
        actual = (
            str(details.st_uid),
            str(stat.S_IMODE(details.st_mode)),
            str(details.st_dev),
            str(details.st_ino),
            str(details.st_size),
            hashlib.sha256(payload).hexdigest(),
        )
        if (
            len(expected) != 6
            or re.fullmatch(r"[0-9a-f]{64}", expected[5]) is None
            or source.resolve(strict=True) != source
            or source.is_symlink()
            or not stat.S_ISREG(details.st_mode)
            or tuple(expected) != actual
        ):
            raise ValueError
    root = Path(arguments[4])
    root_details = root.lstat()
    if (
        root.is_symlink()
        or root.resolve(strict=True) != root
        or not stat.S_ISDIR(root_details.st_mode)
        or stat.S_IMODE(root_details.st_mode) != 0o700
        or root_details.st_uid != os.getuid()
        or {entry.name for entry in root.iterdir()} != {"docker", "docker-buildx"}
    ):
        raise ValueError
    for name, expected_raw in (
        ("docker", arguments[1]),
        ("docker-buildx", arguments[3]),
    ):
        path = root / name
        details, payload = identity(path)
        expected = expected_raw.split(":")
        if (
            path.is_symlink()
            or not stat.S_ISREG(details.st_mode)
            or stat.S_IMODE(details.st_mode) != 0o500
            or details.st_uid != os.getuid()
            or str(len(payload)) != expected[4]
            or hashlib.sha256(payload).hexdigest() != expected[5]
        ):
            raise ValueError
except (OSError, ValueError):
    raise SystemExit(2) from None
PY
}
tag_descriptor_initial=$record_parent/tag-descriptor-initial.json
tag_descriptor_final=$record_parent/tag-descriptor-final.json
immutable_descriptor=$record_parent/immutable-descriptor.json
tag_lookup_error=$record_parent/tag-lookup.err
root_manifest=$record_parent/root-manifest.json
publisher_uid=$(/usr/bin/id -u 2>/dev/null) \
    || blocked 'BLOCKED_PRIVATE_PUBLISH_DIRECTORY'
case "$publisher_uid" in
    ''|*[!0-9]*) blocked 'BLOCKED_PRIVATE_PUBLISH_DIRECTORY' ;;
esac
/usr/bin/python3 -I -S - "$publisher_uid" <<'PY' \
    || blocked 'BLOCKED_PRIVATE_PUBLISH_DIRECTORY'
import os
import sys

if sys.argv[1] != str(os.getuid()):
    raise SystemExit(2)
PY
lock_parent=/tmp/travel-map-publish-locks-$publisher_uid
lock_directory=$lock_parent/$git_sha
image_tag=
tagged=
owns_tagged=0
owns_lock=0
image_built=0
interrupted=0
publish_completed=0

remove_owned_tag() {
    current_tagged=$(run_docker image ls --quiet --no-trunc \
        --filter "reference=$tagged" 2>/dev/null) || return 1
    [ -n "$current_tagged" ] || return 0
    [ "$current_tagged" = "$image_id" ] || return 0
    run_docker image rm "$tagged" >/dev/null 2>&1
}

cleanup_publish() {
    status=$?
    cleanup_failed=0
    trap - EXIT HUP INT TERM

    if [ "$owns_tagged" -eq 1 ] && [ -n "$tagged" ]; then
        remove_owned_tag || cleanup_failed=1
        owns_tagged=0
    fi
    if [ -n "$record_parent" ]; then
        remove_private_directory "$record_parent" || cleanup_failed=1
    fi
    if [ "$owns_lock" -eq 1 ] && [ -n "$lock_directory" ]; then
        /bin/rmdir "$lock_directory" >/dev/null 2>&1 || cleanup_failed=1
        owns_lock=0
    fi
    if [ "$interrupted" -eq 1 ] || [ "$cleanup_failed" -ne 0 ]; then
        printf '%s\n' 'BLOCKED_PUBLISH_CLEANUP_FAILED' >&2
        status=2
    fi
    exit "$status"
}

interrupted_cleanup() {
    interrupted=1
    trap - HUP INT TERM
    exit 2
}

trap cleanup_publish EXIT
trap interrupted_cleanup HUP INT TERM

/bin/mkdir -m 0700 "$lock_parent" 2>/dev/null || true
validate_private_directory "$lock_parent" \
    || blocked 'BLOCKED_PRIVATE_PUBLISH_DIRECTORY'
# Stale locks are never removed automatically. After verifying that no publisher
# is running for this Git SHA, an operator may remove this exact empty directory.
(umask 077 && /bin/mkdir "$lock_directory") 2>/dev/null \
    || blocked 'BLOCKED_PUBLISH_LOCKED'
owns_lock=1
validate_private_directory "$lock_directory" \
    || blocked 'BLOCKED_PRIVATE_PUBLISH_DIRECTORY'

validate_approved_record() {
    /usr/bin/python3 -I -S - \
    "$record" "$expected_image_tag" "$expected_image_id" \
    "$nas_platform" "$git_sha" "$expected_record_sha256" <<'PY'
import hashlib
import os
import stat
import sys
from pathlib import Path

record_path = Path(sys.argv[1])
image_tag, image_id, platform, git_sha, expected_hash = sys.argv[2:]
try:
    parent = record_path.parent
    parent_details = parent.lstat()
    if (
        record_path.name != "gated-image.record"
        or not record_path.is_absolute()
        or parent.resolve(strict=True) != parent
        or parent.is_symlink()
        or not stat.S_ISDIR(parent_details.st_mode)
        or stat.S_IMODE(parent_details.st_mode) != 0o700
        or parent_details.st_uid != os.getuid()
        or {entry.name for entry in parent.iterdir()} != {record_path.name}
    ):
        raise ValueError
    for ancestor in parent.parents:
        ancestor_details = ancestor.stat()
        shared_write = ancestor_details.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        if (
            not stat.S_ISDIR(ancestor_details.st_mode)
            or ancestor_details.st_uid not in {0, os.getuid()}
            or (
                shared_write
                and not ancestor_details.st_mode & stat.S_ISVTX
            )
        ):
            raise ValueError
    descriptor = os.open(
        record_path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        details = os.fstat(descriptor)
        payload = os.read(descriptor, 4097)
    finally:
        os.close(descriptor)
    path_details = record_path.lstat()
    expected_payload = (
        f"imageTag={image_tag}\n"
        f"imageId={image_id}\n"
        f"platform={platform}\n"
        f"gitSha={git_sha}\n"
    ).encode("ascii")
    if (
        not stat.S_ISREG(details.st_mode)
        or stat.S_IMODE(details.st_mode) != 0o600
        or details.st_uid != os.getuid()
        or details.st_nlink != 1
        or (details.st_dev, details.st_ino)
        != (path_details.st_dev, path_details.st_ino)
        or payload != expected_payload
        or hashlib.sha256(payload).hexdigest() != expected_hash
        or {entry.name for entry in parent.iterdir()} != {record_path.name}
    ):
        raise ValueError
except (OSError, UnicodeError, ValueError):
    raise SystemExit(2) from None
PY
}

validate_approved_record || blocked 'BLOCKED_INVALID_GATE_ATTESTATION'
image_tag=$expected_image_tag
image_id=$expected_image_id

inspected=$(run_docker image inspect \
    --format '{{.Id}} {{.Os}}/{{.Architecture}}' "$image_tag") \
    || blocked 'BLOCKED_INVALID_GATE_ATTESTATION'
[ "$inspected" = "$image_id $nas_platform" ] \
    || blocked 'BLOCKED_INVALID_GATE_ATTESTATION'
exact_inspected=$(run_docker image inspect \
    --format '{{.Id}} {{.Os}}/{{.Architecture}}' "$image_id") \
    || blocked 'BLOCKED_INVALID_GATE_ATTESTATION'
[ "$exact_inspected" = "$image_id $nas_platform" ] \
    || blocked 'BLOCKED_INVALID_GATE_ATTESTATION'

image_id_hex=${image_id#sha256:}
publish_tag=$git_sha-sha256-$image_id_hex
[ "${#publish_tag}" -le 128 ] || blocked 'BLOCKED_INVALID_GATE_ATTESTATION'
tagged=$registry:$publish_tag
local_tagged=$(run_docker image ls --quiet --no-trunc \
    --filter "reference=$tagged" 2>/dev/null) \
    || blocked 'BLOCKED_DOCKER_UNAVAILABLE'
[ -z "$local_tagged" ] || blocked 'BLOCKED_PUBLISH_TAG_EXISTS'
inspected=$(run_docker image inspect \
    --format '{{.Id}} {{.Os}}/{{.Architecture}}' "$image_tag") \
    || blocked 'BLOCKED_INVALID_GATE_ATTESTATION'
[ "$inspected" = "$image_id $nas_platform" ] \
    || blocked 'BLOCKED_INVALID_GATE_ATTESTATION'

parse_descriptor() {
    /usr/bin/python3 -I -S - "$1" <<'PY'
import json
import re
import sys
from pathlib import Path

DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
ROOT_MEDIA_TYPES = {
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.oci.image.manifest.v1+json",
}

try:
    value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    if type(value) is not dict:
        raise ValueError
    digest = value.get("digest")
    media_type = value.get("mediaType")
    size = value.get("size")
    if (
        type(digest) is not str
        or DIGEST.fullmatch(digest) is None
        or media_type not in ROOT_MEDIA_TYPES
        or type(size) is not int
        or size <= 0
    ):
        raise ValueError
except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
    raise SystemExit(2) from None
print(digest, media_type, size)
PY
}

resolve_descriptor() {
    reference=$1
    destination=$2
    run_buildx imagetools inspect \
        --format '{{json .Manifest}}' \
        "$reference" > "$destination" \
        || return 1
    parse_descriptor "$destination"
}

validate_raw_descriptor() {
    /usr/bin/python3 -I -S - "$1" "$2" "$3" <<'PY'
import hashlib
import re
import sys
from pathlib import Path

DIGEST = re.compile(r"sha256:[0-9a-f]{64}")

try:
    path = Path(sys.argv[1])
    expected_digest = sys.argv[2]
    expected_size = int(sys.argv[3])
    payload = path.read_bytes()
    if (
        DIGEST.fullmatch(expected_digest) is None
        or expected_size <= 0
        or len(payload) != expected_size
        or hashlib.sha256(payload).hexdigest()
        != expected_digest.removeprefix("sha256:")
    ):
        raise ValueError
except (OSError, ValueError):
    raise SystemExit(2) from None
PY
}

validate_image_config() {
    /usr/bin/python3 -I -S - "$1" "$2" <<'PY'
import json
import sys
from pathlib import Path

try:
    value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    expected_platform = sys.argv[2]
    if type(value) is not dict:
        raise ValueError
    if expected_platform != "-":
        os_name = value.get("os")
        architecture = value.get("architecture")
        if (
            type(os_name) is not str
            or type(architecture) is not str
            or f"{os_name}/{architecture}" != expected_platform
        ):
            raise ValueError
except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
    raise SystemExit(2) from None
PY
}

inspect_image_config() {
    manifest_reference=$1
    config_destination=$2
    expected_config_platform=$3
    run_buildx imagetools inspect \
        --format '{{json .Image}}' \
        "$manifest_reference" > "$config_destination" \
        || return 1
    validate_image_config "$config_destination" "$expected_config_platform"
}

lookup_remote_tag() {
    : > "$tag_descriptor_initial"
    : > "$tag_lookup_error"
    if run_buildx imagetools inspect \
        --format '{{json .Manifest}}' \
        "$tagged" > "$tag_descriptor_initial" 2> "$tag_lookup_error"; then
        descriptor_values=$(parse_descriptor "$tag_descriptor_initial") \
            || blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'
        remote_lookup_state=existing
        return 0
    fi
    /usr/bin/python3 -I -S - \
        "$tag_descriptor_initial" "$tag_lookup_error" "$tagged" <<'PY' \
        || blocked 'BLOCKED_REMOTE_TAG_UNVERIFIED'
import re
import sys
from pathlib import Path

try:
    payload = b"".join(Path(path).read_bytes() for path in sys.argv[1:3])
    if not payload or len(payload) > 65_536:
        raise ValueError
    message = payload.decode("utf-8", errors="strict").strip().lower()
    missing_manifest = re.fullmatch(
        r"(?:error:\s*)?manifest\s+unknown"
        r"(?::\s*(?:manifest\s+unknown|not\s+found))?",
        message,
    )
    missing_exact_tag = re.fullmatch(
        r"(?:error:\s*)?" + re.escape(sys.argv[3].lower()) + r":\s*not\s+found",
        message,
    )
    ambiguous_markers = (
        "unauthorized",
        "denied",
        "timeout",
        "connection",
        "certificate",
        "credential",
        "executable",
        "helper",
        "tls",
        "network",
        "rate limit",
        "too many requests",
        "unexpected status",
        "internal server error",
        "bad gateway",
        "service unavailable",
        "gateway timeout",
    )
    if (missing_manifest is None and missing_exact_tag is None) or any(
        marker in message for marker in ambiguous_markers
    ):
        raise ValueError
except (OSError, UnicodeError, ValueError):
    raise SystemExit(2) from None
PY
    remote_lookup_state=missing
}

validate_approved_record || blocked 'BLOCKED_INVALID_GATE_ATTESTATION'
inspected=$(run_docker image inspect \
    --format '{{.Id}} {{.Os}}/{{.Architecture}}' "$image_tag") \
    || blocked 'BLOCKED_INVALID_GATE_ATTESTATION'
[ "$inspected" = "$image_id $nas_platform" ] \
    || blocked 'BLOCKED_INVALID_GATE_ATTESTATION'
lookup_remote_tag
if [ "$remote_lookup_state" = missing ]; then
    run_docker tag "$image_id" "$tagged" || blocked 'BLOCKED_IMAGE_TAGGING'
    owns_tagged=1
    tagged_inspected=$(run_docker image inspect \
        --format '{{.Id}} {{.Os}}/{{.Architecture}}' "$tagged") \
        || blocked 'BLOCKED_INVALID_GATE_ATTESTATION'
    [ "$tagged_inspected" = "$image_id $nas_platform" ] \
        || blocked 'BLOCKED_INVALID_GATE_ATTESTATION'
    lookup_remote_tag
    if [ "$remote_lookup_state" = missing ]; then
        run_docker push "$tagged" >/dev/null || blocked 'BLOCKED_IMAGE_PUSH'
        lookup_remote_tag
        [ "$remote_lookup_state" = existing ] \
            || blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'
    fi
fi

set -- $descriptor_values
[ "$#" -eq 3 ] || blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'
remote_digest=$1
remote_media_type=$2
remote_size=$3
repo_digest=$registry@$remote_digest

run_buildx imagetools inspect \
    --raw "$repo_digest" > "$root_manifest" \
    || blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'
validate_raw_descriptor "$root_manifest" "$remote_digest" "$remote_size" \
    || blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'

root_values=$(/usr/bin/python3 -I -S - \
    "$root_manifest" "$image_id" "$nas_platform" "$remote_digest" "$remote_media_type" <<'PY'
import json
import re
import sys
from pathlib import Path

DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
OCI_INDEX = "application/vnd.oci.image.index.v1+json"
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
OCI_CONFIG = "application/vnd.oci.image.config.v1+json"
DOCKER_MANIFEST = "application/vnd.docker.distribution.manifest.v2+json"
DOCKER_CONFIG = "application/vnd.docker.container.image.v1+json"
MANIFEST_MEDIA_TYPES = {OCI_MANIFEST, DOCKER_MANIFEST}
CONFIG_MEDIA_TYPES = {OCI_CONFIG, DOCKER_CONFIG}


def is_digest(value: object) -> bool:
    return type(value) is str and DIGEST.fullmatch(value) is not None


def validate_descriptor(
    value: object,
    *,
    media_types=None,
) -> dict[str, object]:
    if type(value) is not dict:
        raise ValueError
    digest = value.get("digest")
    media_type = value.get("mediaType")
    size = value.get("size")
    if (
        not is_digest(digest)
        or type(media_type) is not str
        or (media_types is not None and media_type not in media_types)
        or type(size) is not int
        or size <= 0
    ):
        raise ValueError
    return value


def validate_layers(value: object) -> None:
    if type(value) is not list or not value:
        raise ValueError
    for layer in value:
        validate_descriptor(layer)


try:
    value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    image_id = sys.argv[2]
    platform = sys.argv[3]
    remote_digest = sys.argv[4]
    remote_media_type = sys.argv[5]
    if type(value) is not dict or value.get("schemaVersion") != 2:
        raise ValueError
    if value.get("mediaType") != remote_media_type:
        raise ValueError

    if remote_media_type in MANIFEST_MEDIA_TYPES:
        config = validate_descriptor(
            value.get("config"),
            media_types=CONFIG_MEDIA_TYPES,
        )
        validate_layers(value.get("layers"))
        if config["digest"] != image_id:
            raise ValueError
        print("classic", f"{config['digest']},{config['size']}")
    elif remote_media_type == OCI_INDEX:
        manifests = value.get("manifests")
        if type(manifests) is not list or not manifests:
            raise ValueError
        runnable: list[str] = []
        attestations: list[tuple[str, int, str]] = []
        seen: set[str] = set()
        for candidate in manifests:
            descriptor = validate_descriptor(
                candidate,
                media_types={OCI_MANIFEST},
            )
            digest = descriptor["digest"]
            if digest in seen:
                raise ValueError
            seen.add(digest)
            descriptor_platform = descriptor.get("platform")
            if (
                type(descriptor_platform) is not dict
                or set(descriptor_platform) != {"os", "architecture"}
            ):
                raise ValueError
            os_name = descriptor_platform.get("os")
            architecture = descriptor_platform.get("architecture")
            annotations = descriptor.get("annotations", {})
            if type(annotations) is not dict or any(
                type(key) is not str or type(annotation) is not str
                for key, annotation in annotations.items()
            ):
                raise ValueError
            if os_name == "unknown" or architecture == "unknown":
                if os_name != "unknown" or architecture != "unknown":
                    raise ValueError
                reference = annotations.get("vnd.docker.reference.digest")
                if (
                    annotations.get("vnd.docker.reference.type")
                    != "attestation-manifest"
                    or not is_digest(reference)
                ):
                    raise ValueError
                attestations.append((digest, descriptor["size"], reference))
            else:
                if (
                    f"{os_name}/{architecture}" != platform
                    or "vnd.docker.reference.digest" in annotations
                    or "vnd.docker.reference.type" in annotations
                ):
                    raise ValueError
                runnable.append(digest)
        if len(runnable) != 1:
            raise ValueError
        runnable_digest = runnable[0]
        if any(reference != runnable_digest for _, _, reference in attestations):
            raise ValueError
        runnable_descriptor = next(
            descriptor
            for descriptor in manifests
            if descriptor["digest"] == runnable_digest
        )
        print(
            "index",
            f"{runnable_digest},{runnable_descriptor['size']}",
            *(f"{digest},{size}" for digest, size, _ in attestations),
        )
    else:
        raise ValueError
except (OSError, ValueError, json.JSONDecodeError):
    raise SystemExit(2) from None
PY
) || blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'

validate_child_manifest() {
    child_path=$1
    child_role=$2
    expected_config_digest=$3
    /usr/bin/python3 -I -S - \
        "$child_path" "$child_role" "$expected_config_digest" <<'PY'
import json
import re
import sys
from pathlib import Path

DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
OCI_CONFIG = "application/vnd.oci.image.config.v1+json"
IN_TOTO_LAYER = "application/vnd.in-toto+json"


def validate_descriptor(value: object) -> dict[str, object]:
    if type(value) is not dict:
        raise ValueError
    if (
        type(value.get("digest")) is not str
        or DIGEST.fullmatch(value["digest"]) is None
        or type(value.get("mediaType")) is not str
        or type(value.get("size")) is not int
        or value["size"] <= 0
    ):
        raise ValueError
    return value


try:
    value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    role = sys.argv[2]
    expected_config_digest = sys.argv[3]
    if (
        type(value) is not dict
        or value.get("schemaVersion") != 2
        or value.get("mediaType") != OCI_MANIFEST
    ):
        raise ValueError
    config = validate_descriptor(value.get("config"))
    if config["mediaType"] != OCI_CONFIG:
        raise ValueError
    layers = value.get("layers")
    if type(layers) is not list or not layers:
        raise ValueError
    media_types = [validate_descriptor(layer)["mediaType"] for layer in layers]
    if role == "runnable":
        if (
            IN_TOTO_LAYER in media_types
            or config["digest"] != expected_config_digest
        ):
            raise ValueError
    elif role == "attestation":
        if (
            expected_config_digest != "-"
            or any(media_type != IN_TOTO_LAYER for media_type in media_types)
        ):
            raise ValueError
    else:
        raise ValueError
except (OSError, ValueError, json.JSONDecodeError):
    raise SystemExit(2) from None
print(f"{config['digest']},{config['size']}")
PY
}

set -- $root_values
[ "$#" -ge 1 ] || blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'
root_mode=$1
shift
case "$root_mode" in
    classic)
        [ "$#" -eq 1 ] || blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'
        classic_config_descriptor=$1
        classic_config_digest=${classic_config_descriptor%,*}
        classic_config_size=${classic_config_descriptor#*,}
        [ "$classic_config_digest,$classic_config_size" \
            = "$classic_config_descriptor" ] \
            || blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'
        classic_image_config=$record_parent/classic-image-config.json
        inspect_image_config \
            "$repo_digest" "$classic_image_config" "$nas_platform" \
            || blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'
        ;;
    index)
        [ "$#" -ge 1 ] || blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'
        runnable_descriptor=$1
        shift
        runnable_digest=${runnable_descriptor%,*}
        runnable_size=${runnable_descriptor#*,}
        [ "$runnable_digest,$runnable_size" = "$runnable_descriptor" ] \
            || blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'
        runnable_manifest=$record_parent/runnable-manifest.json
        runnable_image_config=$record_parent/runnable-image-config.json
        run_buildx imagetools inspect \
            --raw "$registry@$runnable_digest" > "$runnable_manifest" \
            || blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'
        validate_raw_descriptor \
            "$runnable_manifest" "$runnable_digest" "$runnable_size" \
            || blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'
        runnable_config_descriptor=$(validate_child_manifest \
            "$runnable_manifest" runnable "$image_id") \
            || blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'
        runnable_config_digest=${runnable_config_descriptor%,*}
        runnable_config_size=${runnable_config_descriptor#*,}
        [ "$runnable_config_digest,$runnable_config_size" \
            = "$runnable_config_descriptor" ] \
            || blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'
        inspect_image_config \
            "$registry@$runnable_digest" \
            "$runnable_image_config" "$nas_platform" \
            || blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'
        attestation_number=0
        for attestation_descriptor in "$@"; do
            attestation_number=$((attestation_number + 1))
            attestation_digest=${attestation_descriptor%,*}
            attestation_size=${attestation_descriptor#*,}
            [ "$attestation_digest,$attestation_size" \
                = "$attestation_descriptor" ] \
                || blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'
            attestation_manifest=$record_parent/attestation-manifest-$attestation_number.json
            attestation_image_config=$record_parent/attestation-image-config-$attestation_number.json
            run_buildx imagetools inspect \
                --raw "$registry@$attestation_digest" > "$attestation_manifest" \
                || blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'
            validate_raw_descriptor \
                "$attestation_manifest" "$attestation_digest" \
                "$attestation_size" \
                || blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'
            attestation_config_descriptor=$(validate_child_manifest \
                "$attestation_manifest" attestation -) \
                || blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'
            attestation_config_digest=${attestation_config_descriptor%,*}
            attestation_config_size=${attestation_config_descriptor#*,}
            [ "$attestation_config_digest,$attestation_config_size" \
                = "$attestation_config_descriptor" ] \
                || blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'
            inspect_image_config \
                "$registry@$attestation_digest" \
                "$attestation_image_config" - \
                || blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'
        done
        ;;
    *)
        blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'
        ;;
esac

immutable_values=$(resolve_descriptor "$repo_digest" "$immutable_descriptor") \
    || blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'
[ "$immutable_values" = "$remote_digest $remote_media_type $remote_size" ] \
    || blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'
final_tag_values=$(resolve_descriptor "$tagged" "$tag_descriptor_final") \
    || blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'
[ "$final_tag_values" = "$remote_digest $remote_media_type $remote_size" ] \
    || blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'

printf '%s\n' "$repo_digest"
publish_completed=1
