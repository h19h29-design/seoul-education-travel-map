#!/bin/sh
set -eu

umask 077

# This gate deliberately accepts no image, command, or host-path argument. It
# validates the reviewed local release context first and uses one local image.
blocked() {
    printf '%s\n' "$1" >&2
    exit 2
}

[ "$#" -eq 0 ] || {
    printf '%s\n' 'usage: release-gate.sh' >&2
    exit 64
}

remove_private_directory() {
    directory=$1
    [ -n "$directory" ] || return 0
    [ -d "$directory" ] && [ ! -L "$directory" ] || return 1
    /usr/bin/find "$directory" -mindepth 1 -depth -delete >/dev/null 2>&1 \
        && /bin/rmdir "$directory"
}

stat_owner_mode() {
    /usr/bin/stat -c '%u:%a' "$1" 2>/dev/null \
        || /usr/bin/stat -f '%u:%Lp' "$1" 2>/dev/null
}

bootstrap_python() {
    /usr/bin/env -i \
        HOME=/var/empty \
        PATH=/usr/bin:/bin \
        TMPDIR=/tmp \
        /usr/bin/python3 -I -S "$@"
}

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
trusted_path=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
tool_search_path=$canonical_home/.local/bin:/opt/homebrew/bin:/usr/local/bin:$trusted_path
uv_cache=$canonical_home/.cache/uv
playwright_cache=$canonical_home/Library/Caches/ms-playwright
pnpm_store=$canonical_home/Library/pnpm/store/v10

resolve_release_tool() {
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
uv_tool=$(resolve_release_tool uv) \
    || blocked 'BLOCKED_UNSAFE_RELEASE_ENVIRONMENT'
pnpm_tool=$(resolve_release_tool pnpm) \
    || blocked 'BLOCKED_UNSAFE_RELEASE_ENVIRONMENT'
docker_tool=$(resolve_release_tool docker) \
    || blocked 'BLOCKED_UNSAFE_RELEASE_ENVIRONMENT'
node_tool=$(resolve_release_tool node) \
    || blocked 'BLOCKED_UNSAFE_RELEASE_ENVIRONMENT'
buildx_tool=$(resolve_release_tool docker-buildx) \
    || blocked 'BLOCKED_UNSAFE_RELEASE_ENVIRONMENT'

capture_release_tool_identity() {
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
uv_tool_identity=$(capture_release_tool_identity "$uv_tool") \
    || blocked 'BLOCKED_UNSAFE_RELEASE_ENVIRONMENT'
pnpm_tool_identity=$(capture_release_tool_identity "$pnpm_tool") \
    || blocked 'BLOCKED_UNSAFE_RELEASE_ENVIRONMENT'
docker_tool_identity=$(capture_release_tool_identity "$docker_tool") \
    || blocked 'BLOCKED_UNSAFE_RELEASE_ENVIRONMENT'
node_tool_identity=$(capture_release_tool_identity "$node_tool") \
    || blocked 'BLOCKED_UNSAFE_RELEASE_ENVIRONMENT'
buildx_tool_identity=$(capture_release_tool_identity "$buildx_tool") \
    || blocked 'BLOCKED_UNSAFE_RELEASE_ENVIRONMENT'
sandbox_exec=/usr/bin/sandbox-exec
[ -x "$sandbox_exec" ] || blocked 'BLOCKED_UNSAFE_RELEASE_ENVIRONMENT'
sandbox_exec_identity=$(capture_release_tool_identity "$sandbox_exec") \
    || blocked 'BLOCKED_UNSAFE_RELEASE_ENVIRONMENT'

bootstrap_python - \
    "$uv_cache" "$playwright_cache" "$pnpm_store" <<'PY' \
    || blocked 'BLOCKED_UNSAFE_RELEASE_ENVIRONMENT'
import os
import stat
import sys
from pathlib import Path

try:
    for raw_path in sys.argv[1:]:
        configured = Path(raw_path)
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
        for ancestor in resolved.parents:
            ancestor_details = ancestor.lstat()
            if (
                ancestor.is_symlink()
                or ancestor.resolve(strict=True) != ancestor
                or not stat.S_ISDIR(ancestor_details.st_mode)
                or ancestor_details.st_uid not in {0, os.getuid()}
                or ancestor_details.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            ):
                raise ValueError
except (OSError, ValueError):
    raise SystemExit(2) from None
PY
approved_python=$(/usr/bin/env -i \
    HOME="$canonical_home" PATH="$trusted_path" TMPDIR=/tmp \
    UV_CACHE_DIR="$uv_cache" UV_PYTHON_DOWNLOADS=never UV_OFFLINE=1 \
    "$uv_tool" python find --no-project --resolve-links \
    --no-python-downloads --offline --no-config 3.12) \
    || blocked 'BLOCKED_UNSAFE_RELEASE_ENVIRONMENT'
python_runtime_root=$(bootstrap_python - "$approved_python" <<'PY'
import os
import stat
import sys
from pathlib import Path

try:
    executable = Path(sys.argv[1])
    resolved = executable.resolve(strict=True)
    details = executable.lstat()
    root = executable.parent.parent
    stdlib = root / "lib/python3.12"
    if (
        not executable.is_absolute()
        or executable != resolved
        or executable.name != "python3.12"
        or executable.parent.name != "bin"
        or executable.is_symlink()
        or not stat.S_ISREG(details.st_mode)
        or not os.access(executable, os.X_OK)
        or details.st_uid not in {0, os.getuid()}
        or details.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or root.resolve(strict=True) != root
        or not stdlib.is_dir()
        or stdlib.is_symlink()
    ):
        raise ValueError
    for ancestor in root.parents:
        ancestor_details = ancestor.lstat()
        if (
            ancestor.is_symlink()
            or ancestor.resolve(strict=True) != ancestor
            or not stat.S_ISDIR(ancestor_details.st_mode)
            or ancestor_details.st_uid not in {0, os.getuid()}
            or ancestor_details.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise ValueError
except (OSError, ValueError):
    raise SystemExit(2) from None
print(root)
PY
) || blocked 'BLOCKED_UNSAFE_RELEASE_ENVIRONMENT'
capture_trusted_tree_identity() {
    bootstrap_python - "$1" "${2:-allow-symlinks}" <<'PY'
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

try:
    root = Path(sys.argv[1])
    symlink_policy = sys.argv[2]
    if symlink_policy not in {"allow-symlinks", "reject-symlinks"}:
        raise ValueError
    root_details = root.lstat()
    if (
        root.is_symlink()
        or root.resolve(strict=True) != root
        or not stat.S_ISDIR(root_details.st_mode)
        or root_details.st_uid not in {0, os.getuid()}
        or root_details.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise ValueError
    for ancestor in root.parents:
        ancestor_details = ancestor.lstat()
        if (
            ancestor.is_symlink()
            or ancestor.resolve(strict=True) != ancestor
            or not stat.S_ISDIR(ancestor_details.st_mode)
            or ancestor_details.st_uid not in {0, os.getuid()}
            or ancestor_details.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise ValueError
    records = []
    for directory_raw, child_directories, child_files in os.walk(
        root, topdown=True, followlinks=False
    ):
        directory = Path(directory_raw)
        child_directories.sort()
        child_files.sort()
        for child in [*child_directories, *child_files]:
            path = directory / child
            details = path.lstat()
            relative = path.relative_to(root).as_posix()
            mode = stat.S_IMODE(details.st_mode)
            if stat.S_ISDIR(details.st_mode):
                if (
                    path.is_symlink()
                    or details.st_uid not in {0, os.getuid()}
                    or details.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                ):
                    raise ValueError
                records.append([relative, "directory", details.st_uid, mode])
            elif stat.S_ISREG(details.st_mode):
                if (
                    details.st_uid not in {0, os.getuid()}
                    or details.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                ):
                    raise ValueError
                descriptor = os.open(
                    path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                )
                try:
                    opened = os.fstat(descriptor)
                    digest = hashlib.sha256()
                    while True:
                        chunk = os.read(descriptor, 1024 * 1024)
                        if not chunk:
                            break
                        digest.update(chunk)
                finally:
                    os.close(descriptor)
                if (opened.st_dev, opened.st_ino) != (details.st_dev, details.st_ino):
                    raise ValueError
                records.append(
                    [relative, "file", details.st_uid, mode, details.st_size, digest.hexdigest()]
                )
            elif stat.S_ISLNK(details.st_mode):
                if (
                    symlink_policy == "reject-symlinks"
                    or details.st_uid not in {0, os.getuid()}
                ):
                    raise ValueError
                target = os.readlink(path)
                resolved = path.resolve(strict=True)
                if root not in resolved.parents and resolved != root:
                    raise ValueError
                records.append([relative, "symlink", details.st_uid, mode, target])
            else:
                raise ValueError
    if not records:
        raise ValueError
    payload = json.dumps(records, ensure_ascii=True, separators=(",", ":")).encode(
        "ascii"
    )
except (OSError, ValueError):
    raise SystemExit(2) from None
print(hashlib.sha256(payload).hexdigest())
PY
}

# pnpm v10's projects namespace is tool metadata: validate the directory itself,
# but never enumerate or resolve its untrusted children.
capture_pnpm_store_identity() {
    bootstrap_python - "$1" "${2:-source}" "${3:-identity}" <<'PY'
import hashlib
import json
import os
import stat
import sys
from pathlib import Path


def safe_directory(details):
    return (
        stat.S_ISDIR(details.st_mode)
        and details.st_uid in {0, os.getuid()}
        and not details.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    )


def digest_file(descriptor):
    digest = hashlib.sha256()
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)


def walk(directory_descriptor, prefix, records):
    for name in sorted(os.listdir(directory_descriptor)):
        details = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        relative = f"{prefix}/{name}"
        mode = stat.S_IMODE(details.st_mode)
        if stat.S_ISDIR(details.st_mode):
            if not safe_directory(details):
                raise ValueError
            child = os.open(
                name,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_descriptor,
            )
            try:
                if os.fstat(child) != details:
                    raise ValueError
                records.append([relative, "directory", details.st_uid, mode, details.st_dev, details.st_ino])
                walk(child, relative, records)
            finally:
                os.close(child)
        elif stat.S_ISREG(details.st_mode):
            if (
                details.st_uid not in {0, os.getuid()}
                or details.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                or details.st_nlink != 1
            ):
                raise ValueError
            child = os.open(
                name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_descriptor
            )
            try:
                opened = os.fstat(child)
                if opened != details:
                    raise ValueError
                records.append(
                    [relative, "file", details.st_uid, mode, details.st_size, details.st_dev, details.st_ino, digest_file(child)]
                )
            finally:
                os.close(child)
        else:
            raise ValueError


try:
    root = Path(sys.argv[1])
    ancestor_policy = sys.argv[2]
    identity_policy = sys.argv[3]
    if ancestor_policy not in {"source", "private"}:
        raise ValueError
    if identity_policy not in {"identity", "payload", "combined"}:
        raise ValueError
    root_path_details = root.lstat()
    if (
        not root.is_absolute()
        or root.is_symlink()
        or root.resolve(strict=True) != root
        or not safe_directory(root_path_details)
    ):
        raise ValueError
    for ancestor in root.parents:
        details = ancestor.lstat()
        shared_write = details.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        if (
            ancestor.is_symlink()
            or ancestor.resolve(strict=True) != ancestor
            or not stat.S_ISDIR(details.st_mode)
            or details.st_uid not in {0, os.getuid()}
            or (shared_write and (ancestor_policy != "private" or not details.st_mode & stat.S_ISVTX))
        ):
            raise ValueError
    root_descriptor = os.open(
        root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        root_details = os.fstat(root_descriptor)
        if root_details != root_path_details:
            raise ValueError
        names = set(os.listdir(root_descriptor))
        if names != {"files", "index", "projects"}:
            raise ValueError
        records = [[".", "directory", root_details.st_uid, stat.S_IMODE(root_details.st_mode), root_details.st_dev, root_details.st_ino]]
        for name in ("files", "index", "projects"):
            details = os.stat(name, dir_fd=root_descriptor, follow_symlinks=False)
            if not safe_directory(details):
                raise ValueError
            child = os.open(
                name,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=root_descriptor,
            )
            try:
                if os.fstat(child) != details:
                    raise ValueError
                if name == "projects":
                    records.append([name, "opaque-directory", details.st_uid, stat.S_IMODE(details.st_mode), details.st_dev, details.st_ino])
                else:
                    records.append([name, "directory", details.st_uid, stat.S_IMODE(details.st_mode), details.st_dev, details.st_ino])
                    walk(child, name, records)
            finally:
                os.close(child)
    finally:
        os.close(root_descriptor)
    payload_records = [
            [record[0], record[1], record[-1] if record[1] == "file" else None]
            for record in records
            if record[0] != "." and record[0] != "projects"
        ]
except (OSError, ValueError):
    raise SystemExit(2) from None
identity = hashlib.sha256(json.dumps(records, separators=(",", ":")).encode("ascii")).hexdigest()
payload = hashlib.sha256(json.dumps(payload_records, separators=(",", ":")).encode("ascii")).hexdigest()
if identity_policy == "identity":
    print(identity)
elif identity_policy == "payload":
    print(payload)
else:
    print(identity, payload)
PY
}
python_runtime_identity=$(capture_trusted_tree_identity "$python_runtime_root") \
    || blocked 'BLOCKED_UNSAFE_RELEASE_ENVIRONMENT'
pnpm_package_root=$(bootstrap_python - "$pnpm_tool" <<'PY'
import json
import os
import stat
import sys
from pathlib import Path

try:
    executable = Path(sys.argv[1])
    root = executable.parent.parent
    package_path = root / "package.json"
    bundle = root / "dist/pnpm.mjs"
    package = json.loads(package_path.read_text(encoding="utf-8"))
    root_details = root.lstat()
    if (
        executable.parent.name != "bin"
        or root.resolve(strict=True) != root
        or root.is_symlink()
        or not stat.S_ISDIR(root_details.st_mode)
        or root_details.st_uid not in {0, os.getuid()}
        or root_details.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or type(package) is not dict
        or package.get("name") != "pnpm"
        or not bundle.is_file()
        or bundle.is_symlink()
    ):
        raise ValueError
except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
    raise SystemExit(2) from None
print(root)
PY
) || blocked 'BLOCKED_UNSAFE_RELEASE_ENVIRONMENT'
pnpm_package_identity=$(capture_trusted_tree_identity "$pnpm_package_root") \
    || blocked 'BLOCKED_UNSAFE_RELEASE_ENVIRONMENT'
playwright_cache_identity=$(capture_trusted_tree_identity "$playwright_cache") \
    || blocked 'BLOCKED_UNSAFE_RELEASE_ENVIRONMENT'
pnpm_store_baseline=$(capture_pnpm_store_identity "$pnpm_store" source combined) \
    || blocked 'BLOCKED_UNSAFE_RELEASE_ENVIRONMENT'
pnpm_store_identity=${pnpm_store_baseline%% *}
pnpm_store_payload_identity=${pnpm_store_baseline#* }
[ "$pnpm_store_identity" != "$pnpm_store_payload_identity" ] \
    || blocked 'BLOCKED_UNSAFE_RELEASE_ENVIRONMENT'
case "${TRAVEL_MAP_RELEASE_CLEAN_ENVIRONMENT:-}" in
    1) PATH=$TRAVEL_MAP_RELEASE_PRIVATE_ROOT/trusted-bin:$trusted_path ;;
    *) PATH=$trusted_path ;;
esac
export PATH

run_docker() {
    verify_release_docker_socket "$release_docker_host" || return 1
    DOCKER_HOST=$release_docker_host "$docker_tool" "$@"
}

run_buildx() {
    verify_release_docker_socket "$release_docker_host" || return 1
    DOCKER_HOST=$release_docker_host "$buildx_tool" "$@"
}

case "$0" in
    /*) script_path=$0 ;;
    *) script_path=$PWD/$0 ;;
esac
script_directory=${script_path%/*}
script_name=${script_path##*/}
script_directory=$(CDPATH= cd -- "$script_directory" && /bin/pwd -P) \
    || blocked 'BLOCKED_INVALID_RELEASE_ARTIFACT'
script_path=$script_directory/$script_name
bootstrap_python - "$script_path" <<'PY' \
    || blocked 'BLOCKED_INVALID_RELEASE_ARTIFACT'
import os
import stat
import sys
from pathlib import Path

try:
    details = Path(sys.argv[1]).lstat()
    if (
        not stat.S_ISREG(details.st_mode)
        or stat.S_IMODE(details.st_mode) != 0o755
        or details.st_uid != os.getuid()
    ):
        raise ValueError
except (OSError, ValueError):
    raise SystemExit(2) from None
PY

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
bootstrap_python - "$docker_config" <<'PY' \
    || blocked 'BLOCKED_INVALID_DOCKER_CONFIG'
import os
import stat
import sys
from pathlib import Path

try:
    configured = Path(sys.argv[1])
    resolved = configured.resolve(strict=True)
    directory = configured.lstat()
    config_path = configured / "config.json"
    config = config_path.lstat()
    if (
        not configured.is_absolute()
        or configured != resolved
        or not stat.S_ISDIR(directory.st_mode)
        or stat.S_IMODE(directory.st_mode) != 0o700
        or directory.st_uid != os.getuid()
        or not stat.S_ISREG(config.st_mode)
        or stat.S_IMODE(config.st_mode) != 0o600
        or config.st_uid != os.getuid()
        or config_path.is_symlink()
    ):
        raise ValueError
except (OSError, ValueError):
    raise SystemExit(2) from None
PY

sanitized_context_host=$(bootstrap_python - "$docker_config" <<'PY'
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

CONTEXT_NAME = "travel-map-release-local"


def secure_directory(path):
    details = path.lstat()
    if (
        path.is_symlink()
        or not stat.S_ISDIR(details.st_mode)
        or stat.S_IMODE(details.st_mode) != 0o700
        or details.st_uid != os.getuid()
    ):
        raise ValueError


try:
    root = Path(sys.argv[1])
    config_path = root / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if type(config) is not dict:
        raise ValueError
    if config.get("currentContext") != CONTEXT_NAME:
        if (
            not set(config).issubset({"currentContext"})
            or (
                "currentContext" in config
                and (
                    type(config["currentContext"]) is not str
                    or not config["currentContext"]
                )
            )
            or {entry.name for entry in root.iterdir()} != {"config.json"}
        ):
            raise ValueError
        print("")
        raise SystemExit(0)
    if set(config) != {"currentContext"}:
        raise ValueError
    context_id = hashlib.sha256(CONTEXT_NAME.encode("utf-8")).hexdigest()
    contexts = root / "contexts"
    metadata_root = contexts / "meta"
    context_root = metadata_root / context_id
    metadata_path = context_root / "meta.json"
    for directory in (root, contexts, metadata_root, context_root):
        secure_directory(directory)
    if (
        {entry.name for entry in root.iterdir()} != {"config.json", "contexts"}
        or {entry.name for entry in contexts.iterdir()} != {"meta"}
        or {entry.name for entry in metadata_root.iterdir()} != {context_id}
        or {entry.name for entry in context_root.iterdir()} != {"meta.json"}
    ):
        raise ValueError
    metadata_details = metadata_path.lstat()
    if (
        metadata_path.is_symlink()
        or not stat.S_ISREG(metadata_details.st_mode)
        or stat.S_IMODE(metadata_details.st_mode) != 0o600
        or metadata_details.st_uid != os.getuid()
    ):
        raise ValueError
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if (
        type(metadata) is not dict
        or set(metadata) != {"Name", "Metadata", "Endpoints"}
        or metadata.get("Name") != CONTEXT_NAME
        or metadata.get("Metadata") != {}
    ):
        raise ValueError
    endpoints = metadata.get("Endpoints")
    if type(endpoints) is not dict or set(endpoints) != {"docker"}:
        raise ValueError
    docker_endpoint = endpoints.get("docker")
    if (
        type(docker_endpoint) is not dict
        or set(docker_endpoint) != {"Host", "SkipTLSVerify"}
        or type(docker_endpoint.get("Host")) is not str
        or docker_endpoint.get("SkipTLSVerify") is not False
    ):
        raise ValueError
except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
    raise SystemExit(2) from None
print(docker_endpoint["Host"])
PY
) || blocked 'BLOCKED_INVALID_DOCKER_CONFIG'

clean_environment_marker=${TRAVEL_MAP_RELEASE_CLEAN_ENVIRONMENT:-}

verify_release_docker_socket() {
    bootstrap_python - "$1" <<'PY'
import os
import stat
import sys
from pathlib import Path

try:
    raw = sys.argv[1]
    if not raw.startswith("unix://"):
        raise ValueError
    path = Path(raw.removeprefix("unix://"))
    details = path.lstat()
    permissions = stat.S_IMODE(details.st_mode)
    allowed_group_socket = (
        details.st_uid == 0
        and details.st_gid in {os.getgid(), *os.getgroups()}
        and not permissions & 0o017
    )
    if (
        path.is_symlink()
        or path.resolve(strict=True) != path
        or not stat.S_ISSOCK(details.st_mode)
        or details.st_uid not in {0, os.getuid()}
        or permissions & 0o007
        or (permissions & 0o070 and not allowed_group_socket)
    ):
        raise ValueError
    for ancestor in path.parent.parents:
        ancestor_details = ancestor.lstat()
        shared_write = ancestor_details.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        if (
            ancestor.is_symlink()
            or ancestor.resolve(strict=True) != ancestor
            or not stat.S_ISDIR(ancestor_details.st_mode)
            or ancestor_details.st_uid not in {0, os.getuid()}
            or (shared_write and not ancestor_details.st_mode & stat.S_ISVTX)
        ):
            raise ValueError
    parent = path.parent
    parent_details = parent.lstat()
    shared_write = parent_details.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    if (
        parent.is_symlink()
        or parent.resolve(strict=True) != parent
        or not stat.S_ISDIR(parent_details.st_mode)
        or parent_details.st_uid not in {0, os.getuid()}
        or (shared_write and not parent_details.st_mode & stat.S_ISVTX)
    ):
        raise ValueError
except (OSError, ValueError):
    raise SystemExit(2) from None
PY
}

discard_private_environment() {
    bootstrap_python - "$1" "$normalized_tmp_root" <<'PY'
import os
import stat
import sys
from pathlib import Path


def remove_non_directory(directory_descriptor, name, expected):
    quarantine_name = None
    for _ in range(16):
        candidate = ".release-gate-cleanup-" + os.urandom(16).hex()
        try:
            os.mkdir(candidate, 0o700, dir_fd=directory_descriptor)
        except FileExistsError:
            continue
        quarantine_name = candidate
        break
    if quarantine_name is None:
        raise OSError
    quarantine_descriptor = os.open(
        quarantine_name,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=directory_descriptor,
    )
    quarantine_details = os.fstat(quarantine_descriptor)
    quarantine_identity = (
        quarantine_details.st_dev,
        quarantine_details.st_ino,
        stat.S_IFMT(quarantine_details.st_mode),
    )
    if (
        (current := os.stat(
            quarantine_name, dir_fd=directory_descriptor, follow_symlinks=False
        )).st_dev,
        current.st_ino,
        stat.S_IFMT(current.st_mode),
    ) != quarantine_identity:
        os.close(quarantine_descriptor)
        raise OSError
    try:
        os.rename(
            name,
            name,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=quarantine_descriptor,
        )
        current = os.stat(name, dir_fd=quarantine_descriptor, follow_symlinks=False)
        if (
            current.st_dev,
            current.st_ino,
            stat.S_IFMT(current.st_mode),
        ) != (
            expected.st_dev,
            expected.st_ino,
            stat.S_IFMT(expected.st_mode),
        ):
            # Leaving an unexpected entry quarantined is safer than replacing
            # a name that a concurrent writer may have recreated.
            raise OSError
        os.unlink(name, dir_fd=quarantine_descriptor)
    finally:
        os.close(quarantine_descriptor)
        current = os.stat(
            quarantine_name, dir_fd=directory_descriptor, follow_symlinks=False
        )
        if (
            current.st_dev,
            current.st_ino,
            stat.S_IFMT(current.st_mode),
        ) != quarantine_identity:
            raise OSError
        os.rmdir(quarantine_name, dir_fd=directory_descriptor)


def remove_contents(directory_descriptor):
    os.fchmod(directory_descriptor, 0o700)
    for name in os.listdir(directory_descriptor):
        details = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        if stat.S_ISDIR(details.st_mode):
            child = os.open(
                name,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_descriptor,
            )
            try:
                if os.fstat(child) != details:
                    raise OSError
                remove_contents(child)
            finally:
                os.close(child)
            current = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
            if (
                current.st_dev,
                current.st_ino,
                stat.S_IFMT(current.st_mode),
            ) != (
                details.st_dev,
                details.st_ino,
                stat.S_IFMT(details.st_mode),
            ):
                raise OSError
            os.rmdir(name, dir_fd=directory_descriptor)
        else:
            remove_non_directory(directory_descriptor, name, details)


try:
    root = Path(sys.argv[1])
    parent = Path(sys.argv[2])
    if root.parent != parent or not root.name.startswith("travel-map-release-environment."):
        raise OSError
    parent_descriptor = os.open(
        parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        expected = os.stat(root, follow_symlinks=False)
        root_descriptor = os.open(
            root.name,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_descriptor,
        )
        try:
            bound_root = os.fstat(root_descriptor)
            if bound_root != expected:
                raise OSError
            remove_contents(root_descriptor)
        finally:
            os.close(root_descriptor)
        current_root = os.stat(root.name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (
            current_root.st_dev,
            current_root.st_ino,
            stat.S_IFMT(current_root.st_mode),
        ) != (
            bound_root.st_dev,
            bound_root.st_ino,
            stat.S_IFMT(bound_root.st_mode),
        ):
            raise OSError
        os.rmdir(root.name, dir_fd=parent_descriptor)
    finally:
        os.close(parent_descriptor)
except OSError:
    raise SystemExit(2) from None
PY
}

case "$clean_environment_marker" in
    '')
        private_environment=$(/usr/bin/mktemp -d \
            "$normalized_tmp_root/travel-map-release-environment.XXXXXX") \
            || blocked 'BLOCKED_PRIVATE_DIRECTORY'
        /bin/chmod 0700 "$private_environment" \
            || blocked 'BLOCKED_PRIVATE_DIRECTORY'
        private_home=$private_environment/home
        private_xdg_config=$private_environment/xdg-config
        private_xdg_cache=$private_environment/xdg-cache
        private_xdg_data=$private_environment/xdg-data
        private_npm_cache=$private_environment/npm-cache
        private_pnpm_home=$private_environment/pnpm-home
        private_docker_config=$private_environment/docker-config
        private_trusted_bin=$private_environment/trusted-bin
        private_release_record=$private_environment/release-record
        private_pnpm_package=$private_environment/pnpm-package
        private_pnpm_store=$private_environment/pnpm-store/v10
        /bin/mkdir -m 0700 \
            "$private_home" "$private_xdg_config" "$private_xdg_cache" \
            "$private_xdg_data" "$private_npm_cache" "$private_pnpm_home" \
            "$private_docker_config" "$private_trusted_bin" \
            "$private_release_record" \
            || blocked 'BLOCKED_PRIVATE_DIRECTORY'
        if ! bootstrap_python - \
            "$private_docker_config" "$private_trusted_bin" \
            "$uv_tool" "$node_tool" "$docker_tool" "$buildx_tool" \
            "$pnpm_package_root" "$private_pnpm_package" \
            "$pnpm_store" "$private_pnpm_store" "$pnpm_store_baseline" <<'PY'
import ctypes
import hashlib
import json
import os
import shutil
import stat
import sys
from pathlib import Path


def write_exclusive(path, payload, mode):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(descriptor, "wb") as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())
    path.chmod(mode)


def copy_exclusive(source, destination):
    source_descriptor = os.open(
        source,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        source_details = os.fstat(source_descriptor)
        if not stat.S_ISREG(source_details.st_mode):
            raise ValueError
        destination_descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o500,
        )
        with os.fdopen(source_descriptor, "rb", closefd=False) as input_file:
            with os.fdopen(destination_descriptor, "wb") as output_file:
                shutil.copyfileobj(input_file, output_file)
                output_file.flush()
                os.fsync(output_file.fileno())
        destination.chmod(0o500)
    finally:
        os.close(source_descriptor)


def copy_tree(source, destination):
    source = source.resolve(strict=True)
    destination.mkdir(mode=0o700)

    def visit(source_directory, destination_directory):
        for entry in sorted(os.scandir(source_directory), key=lambda item: item.name):
            source_path = Path(entry.path)
            destination_path = destination_directory / entry.name
            details = source_path.lstat()
            if stat.S_ISDIR(details.st_mode):
                if (
                    source_path.is_symlink()
                    or details.st_uid not in {0, os.getuid()}
                    or details.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                ):
                    raise ValueError
                destination_path.mkdir(mode=stat.S_IMODE(details.st_mode))
                destination_path.chmod(stat.S_IMODE(details.st_mode))
                visit(source_path, destination_path)
            elif stat.S_ISREG(details.st_mode):
                if (
                    details.st_uid not in {0, os.getuid()}
                    or details.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                ):
                    raise ValueError
                source_descriptor = os.open(
                    source_path,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                )
                try:
                    opened = os.fstat(source_descriptor)
                    if (opened.st_dev, opened.st_ino) != (
                        details.st_dev,
                        details.st_ino,
                    ):
                        raise ValueError
                    destination_descriptor = os.open(
                        destination_path,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        stat.S_IMODE(details.st_mode),
                    )
                    with os.fdopen(destination_descriptor, "wb") as output:
                        while True:
                            chunk = os.read(source_descriptor, 1024 * 1024)
                            if not chunk:
                                break
                            output.write(chunk)
                        output.flush()
                        os.fsync(output.fileno())
                    destination_path.chmod(stat.S_IMODE(details.st_mode))
                finally:
                    os.close(source_descriptor)
            elif stat.S_ISLNK(details.st_mode):
                target = os.readlink(source_path)
                resolved = source_path.resolve(strict=True)
                if source not in resolved.parents and resolved != source:
                    raise ValueError
                destination_path.symlink_to(target)
            else:
                raise ValueError

    visit(source, destination)


def clone_pnpm_store(source, destination, approved_baseline):
    clone = ctypes.CDLL(None, use_errno=True).fclonefileat
    clone.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    clone.restype = ctypes.c_int

    def safe_directory(details):
        return (
            stat.S_ISDIR(details.st_mode)
            and details.st_uid in {0, os.getuid()}
            and not details.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        )

    approved_identity, approved_payload = approved_baseline.split(" ")
    records = []

    def record_file(descriptor, details, relative):
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        os.lseek(descriptor, 0, os.SEEK_SET)
        records.append(
            [
                relative,
                "file",
                details.st_uid,
                stat.S_IMODE(details.st_mode),
                details.st_size,
                details.st_dev,
                details.st_ino,
                digest.hexdigest(),
            ]
        )

    def clone_tree(source_descriptor, destination_descriptor, prefix):
        for name in sorted(os.listdir(source_descriptor)):
            details = os.stat(name, dir_fd=source_descriptor, follow_symlinks=False)
            relative = f"{prefix}/{name}"
            if stat.S_ISDIR(details.st_mode):
                if not safe_directory(details):
                    raise ValueError
                os.mkdir(name, 0o700, dir_fd=destination_descriptor)
                child_source = os.open(
                    name,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=source_descriptor,
                )
                child_destination = os.open(
                    name,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=destination_descriptor,
                )
                try:
                    if os.fstat(child_source) != details:
                        raise ValueError
                    records.append(
                        [
                            relative,
                            "directory",
                            details.st_uid,
                            stat.S_IMODE(details.st_mode),
                            details.st_dev,
                            details.st_ino,
                        ]
                    )
                    clone_tree(child_source, child_destination, relative)
                finally:
                    os.close(child_destination)
                    os.close(child_source)
                os.chmod(name, 0o500, dir_fd=destination_descriptor, follow_symlinks=False)
            elif stat.S_ISREG(details.st_mode):
                if (
                    details.st_uid not in {0, os.getuid()}
                    or details.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                    or details.st_nlink != 1
                ):
                    raise ValueError
                source_file = os.open(
                    name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=source_descriptor
                )
                try:
                    if os.fstat(source_file) != details:
                        raise ValueError
                    record_file(source_file, details, relative)
                    if clone(source_file, destination_descriptor, name.encode(), 0) != 0:
                        raise OSError(ctypes.get_errno(), "fclonefileat")
                finally:
                    os.close(source_file)
                cloned = os.stat(name, dir_fd=destination_descriptor, follow_symlinks=False)
                if not stat.S_ISREG(cloned.st_mode) or cloned.st_nlink != 1:
                    raise ValueError
                os.chmod(name, 0o400, dir_fd=destination_descriptor, follow_symlinks=False)
            else:
                raise ValueError

    source_root = Path(source)
    destination_root = Path(destination)
    source_descriptor = os.open(
        source_root,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        root_details = os.fstat(source_descriptor)
        source_details = source_root.lstat()
        if source_details != root_details or not safe_directory(root_details):
            raise ValueError
        if set(os.listdir(source_descriptor)) != {"files", "index", "projects"}:
            raise ValueError
        records.append(
            [
                ".",
                "directory",
                root_details.st_uid,
                stat.S_IMODE(root_details.st_mode),
                root_details.st_dev,
                root_details.st_ino,
            ]
        )
        destination_root.parent.mkdir(mode=0o700)
        destination_root.mkdir(mode=0o700)
        destination_descriptor = os.open(
            destination_root,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            for name in ("files", "index"):
                details = os.stat(name, dir_fd=source_descriptor, follow_symlinks=False)
                if not safe_directory(details):
                    raise ValueError
                os.mkdir(name, 0o700, dir_fd=destination_descriptor)
                source_child = os.open(
                    name,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=source_descriptor,
                )
                destination_child = os.open(
                    name,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=destination_descriptor,
                )
                try:
                    if os.fstat(source_child) != details:
                        raise ValueError
                    records.append(
                        [
                            name,
                            "directory",
                            details.st_uid,
                            stat.S_IMODE(details.st_mode),
                            details.st_dev,
                            details.st_ino,
                        ]
                    )
                    clone_tree(source_child, destination_child, name)
                finally:
                    os.close(destination_child)
                    os.close(source_child)
                os.chmod(name, 0o500, dir_fd=destination_descriptor, follow_symlinks=False)
            project_details = os.stat("projects", dir_fd=source_descriptor, follow_symlinks=False)
            if not safe_directory(project_details):
                raise ValueError
            project_descriptor = os.open(
                "projects",
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=source_descriptor,
            )
            try:
                if os.fstat(project_descriptor) != project_details:
                    raise ValueError
            finally:
                os.close(project_descriptor)
            records.append(
                [
                    "projects",
                    "opaque-directory",
                    project_details.st_uid,
                    stat.S_IMODE(project_details.st_mode),
                    project_details.st_dev,
                    project_details.st_ino,
                ]
            )
            os.mkdir("projects", 0o700, dir_fd=destination_descriptor)
            source_identity = hashlib.sha256(
                json.dumps(records, separators=(",", ":")).encode("ascii")
            ).hexdigest()
            source_payload = hashlib.sha256(
                json.dumps(
                    [
                        [record[0], record[1], record[-1] if record[1] == "file" else None]
                        for record in records
                        if record[0] != "." and record[0] != "projects"
                    ],
                    separators=(",", ":"),
                ).encode("ascii")
            ).hexdigest()
            if source_identity != approved_identity or source_payload != approved_payload:
                raise ValueError
            os.fchmod(destination_descriptor, 0o500)
        finally:
            os.close(destination_descriptor)
    finally:
        os.close(source_descriptor)


try:
    docker_config = Path(sys.argv[1])
    trusted_bin = Path(sys.argv[2])
    for directory in (docker_config, trusted_bin):
        details = directory.lstat()
        if (
            directory.is_symlink()
            or not stat.S_ISDIR(details.st_mode)
            or stat.S_IMODE(details.st_mode) != 0o700
            or details.st_uid != os.getuid()
        ):
            raise ValueError
    write_exclusive(docker_config / "config.json", b"{}\n", 0o600)
    for name, executable in (
        ("uv", sys.argv[3]),
        ("node", sys.argv[4]),
        ("docker", sys.argv[5]),
        ("docker-buildx", sys.argv[6]),
    ):
        copy_exclusive(Path(executable), trusted_bin / name)
    copy_tree(Path(sys.argv[7]), Path(sys.argv[8]))
    clone_pnpm_store(sys.argv[9], sys.argv[10], sys.argv[11])
except (OSError, ValueError):
    raise SystemExit(2) from None
PY
        then
            discard_private_environment "$private_environment" \
                || blocked 'BLOCKED_GATE_CLEANUP_FAILED'
            blocked 'BLOCKED_PRIVATE_DIRECTORY'
        fi
        private_pnpm_store_identity=$(capture_pnpm_store_identity "$private_pnpm_store" private) \
            || blocked 'BLOCKED_PRIVATE_DIRECTORY'
        private_pnpm_store_payload_identity=$(capture_pnpm_store_identity \
            "$private_pnpm_store" private payload) \
            || blocked 'BLOCKED_PRIVATE_DIRECTORY'
        [ "$private_pnpm_store_payload_identity" = "$pnpm_store_payload_identity" ] \
            || blocked 'BLOCKED_PRIVATE_DIRECTORY'
        docker_host=$(/usr/bin/env -i \
            HOME=/var/empty PATH="$trusted_path" TMPDIR=/tmp \
            DOCKER_CONFIG="$docker_config" \
            "$docker_tool" context inspect \
            --format '{{.Endpoints.docker.Host}}' 2>/dev/null) \
            || blocked 'BLOCKED_INVALID_DOCKER_CONFIG'
        if [ -n "$sanitized_context_host" ] \
            && [ "$docker_host" != "$sanitized_context_host" ]; then
            blocked 'BLOCKED_INVALID_DOCKER_CONFIG'
        fi
        docker_host=$(bootstrap_python - "$docker_host" <<'PY'
import os
import sys
from pathlib import Path

try:
    raw = sys.argv[1]
    if not raw.startswith("unix://") or any(ord(character) < 0x20 for character in raw):
        raise ValueError
    socket_path = Path(raw.removeprefix("unix://"))
    if (
        not socket_path.is_absolute()
        or str(socket_path) != os.path.normpath(socket_path)
        or any(part in {"", ".", ".."} for part in socket_path.parts[1:])
    ):
        raise ValueError
except (OSError, ValueError):
    raise SystemExit(2) from None
print(raw)
PY
) || blocked 'BLOCKED_INVALID_DOCKER_CONFIG'
        verify_release_docker_socket "$docker_host" \
            || blocked 'BLOCKED_INVALID_DOCKER_CONFIG'
        exec /usr/bin/env -i \
            HOME=/var/empty PATH=/usr/bin:/bin TMPDIR=/tmp \
            /usr/bin/python3 -I -S - \
            "$script_path" "$private_environment" "$trusted_path" \
            "$private_trusted_bin" \
            "$normalized_tmp_root" "${NAS_PLATFORM:-}" \
            "${RELEASE_GATE_IMAGE_RECORD:-}" "$private_docker_config" \
            "$docker_host" \
            "$uv_cache" "$playwright_cache" "$private_pnpm_store" \
            "$uv_tool_identity" "$pnpm_tool_identity" \
            "$docker_tool_identity" "$node_tool_identity" \
            "$buildx_tool_identity" "$approved_python" \
            "$python_runtime_root" "$python_runtime_identity" \
            "$pnpm_package_root" "$pnpm_package_identity" \
            "$playwright_cache_identity" "$pnpm_store" "$pnpm_store_identity" \
            "$private_pnpm_store_identity" "$sandbox_exec" \
            "$sandbox_exec_identity" <<'PY'
from __future__ import annotations

import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
import hashlib
import json
from pathlib import Path

(
    script,
    private_root_raw,
    trusted_path,
    trusted_bin,
    tmp_root_raw,
    nas_platform,
    record_path,
    docker_config,
    docker_host,
    uv_cache,
    playwright_cache,
    pnpm_store,
    uv_tool_identity,
    pnpm_tool_identity,
    docker_tool_identity,
    node_tool_identity,
    buildx_tool_identity,
    approved_python,
    python_runtime_root,
    python_runtime_identity,
    pnpm_package_root,
    pnpm_package_identity,
    playwright_cache_identity,
    pnpm_source_store,
    pnpm_store_identity,
    private_pnpm_store_identity,
    sandbox_exec_raw,
    sandbox_exec_identity,
) = sys.argv[1:]
private_root = Path(private_root_raw)
tmp_root = Path(tmp_root_raw)
requested_record = Path(record_path) if record_path else None
staged_record = (
    private_root / "release-record" / "gated-image.record"
    if requested_record is not None
    else None
)
process: subprocess.Popen[bytes] | None = None
interrupted = False
termination_deadline: float | None = None


def verify_sandbox_exec() -> Path:
    path = Path(sandbox_exec_raw)
    expected = sandbox_exec_identity.split(":")
    if len(expected) != 6 or re.fullmatch(r"[0-9a-f]{64}", expected[5]) is None:
        raise OSError
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        details = os.fstat(descriptor)
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    finally:
        os.close(descriptor)
    if (
        path != Path("/usr/bin/sandbox-exec")
        or path.is_symlink()
        or path.resolve(strict=True) != path
        or not stat.S_ISREG(details.st_mode)
        or tuple(expected)
        != (
            str(details.st_uid),
            str(stat.S_IMODE(details.st_mode)),
            str(details.st_dev),
            str(details.st_ino),
            str(details.st_size),
            digest.hexdigest(),
        )
    ):
        raise OSError
    return path


try:
    sandbox_exec = verify_sandbox_exec()
except OSError:
    print("BLOCKED_UNSAFE_RELEASE_ENVIRONMENT", file=sys.stderr)
    raise SystemExit(2) from None

private_store = Path(pnpm_store)
profile = "\n".join(
    (
        "(version 1)",
        "(allow default)",
        f"(deny file-write* (literal {json.dumps(str(private_store.parent.parent))}))",
        f"(deny file-write* (literal {json.dumps(str(private_store.parent))}))",
        f"(deny file-write* (literal {json.dumps(str(private_store))}))",
        f"(deny file-write* (subpath {json.dumps(str(private_store / 'files'))}))",
        f"(deny file-write* (subpath {json.dumps(str(private_store / 'index'))}))",
    )
)


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
    def remove_non_directory(directory_descriptor: int, name: str, expected: os.stat_result) -> None:
        quarantine_name: str | None = None
        for _ in range(16):
            candidate = ".release-gate-cleanup-" + os.urandom(16).hex()
            try:
                os.mkdir(candidate, 0o700, dir_fd=directory_descriptor)
            except FileExistsError:
                continue
            quarantine_name = candidate
            break
        if quarantine_name is None:
            raise OSError
        quarantine_descriptor = os.open(
            quarantine_name,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_descriptor,
        )
        quarantine_details = os.fstat(quarantine_descriptor)
        quarantine_identity = (
            quarantine_details.st_dev,
            quarantine_details.st_ino,
            stat.S_IFMT(quarantine_details.st_mode),
        )
        if (
            (current := os.stat(
                quarantine_name, dir_fd=directory_descriptor, follow_symlinks=False
            )).st_dev,
            current.st_ino,
            stat.S_IFMT(current.st_mode),
        ) != quarantine_identity:
            os.close(quarantine_descriptor)
            raise OSError
        try:
            os.rename(
                name,
                name,
                src_dir_fd=directory_descriptor,
                dst_dir_fd=quarantine_descriptor,
            )
            current = os.stat(name, dir_fd=quarantine_descriptor, follow_symlinks=False)
            if (
                current.st_dev,
                current.st_ino,
                stat.S_IFMT(current.st_mode),
            ) != (
                expected.st_dev,
                expected.st_ino,
                stat.S_IFMT(expected.st_mode),
            ):
                # Leaving an unexpected entry quarantined is safer than replacing
                # a name that a concurrent writer may have recreated.
                raise OSError
            os.unlink(name, dir_fd=quarantine_descriptor)
        finally:
            os.close(quarantine_descriptor)
            current = os.stat(
                quarantine_name, dir_fd=directory_descriptor, follow_symlinks=False
            )
            if (
                current.st_dev,
                current.st_ino,
                stat.S_IFMT(current.st_mode),
            ) != quarantine_identity:
                raise OSError
            os.rmdir(quarantine_name, dir_fd=directory_descriptor)

    def remove_contents(directory_descriptor: int) -> None:
        os.fchmod(directory_descriptor, 0o700)
        for name in os.listdir(directory_descriptor):
            details = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
            if stat.S_ISDIR(details.st_mode):
                child = os.open(
                    name,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=directory_descriptor,
                )
                try:
                    if os.fstat(child) != details:
                        raise OSError
                    remove_contents(child)
                finally:
                    os.close(child)
                current = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
                if (
                    current.st_dev,
                    current.st_ino,
                    stat.S_IFMT(current.st_mode),
                ) != (
                    details.st_dev,
                    details.st_ino,
                    stat.S_IFMT(details.st_mode),
                ):
                    raise OSError
                os.rmdir(name, dir_fd=directory_descriptor)
            else:
                remove_non_directory(directory_descriptor, name, details)

    if private_root.parent != tmp_root or not private_root.name.startswith(
        "travel-map-release-environment."
    ):
        raise OSError
    parent_descriptor = os.open(
        tmp_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        expected = os.stat(private_root, follow_symlinks=False)
        if (
            not stat.S_ISDIR(expected.st_mode)
            or stat.S_IMODE(expected.st_mode) != 0o700
            or expected.st_uid != os.getuid()
        ):
            raise OSError
        root_descriptor = os.open(
            private_root.name,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_descriptor,
        )
        try:
            bound_root = os.fstat(root_descriptor)
            if bound_root != expected:
                raise OSError
            remove_contents(root_descriptor)
        finally:
            os.close(root_descriptor)
        current_root = os.stat(
            private_root.name, dir_fd=parent_descriptor, follow_symlinks=False
        )
        if (
            current_root.st_dev,
            current_root.st_ino,
            stat.S_IFMT(current_root.st_mode),
        ) != (
            bound_root.st_dev,
            bound_root.st_ino,
            stat.S_IFMT(bound_root.st_mode),
        ):
            raise OSError
        os.rmdir(private_root.name, dir_fd=parent_descriptor)
    finally:
        os.close(parent_descriptor)


def open_requested_record_parent(*, require_empty: bool) -> int:
    if requested_record is None:
        raise OSError
    parent = requested_record.parent
    if (
        requested_record.name != "gated-image.record"
        or not requested_record.is_absolute()
        or parent.resolve(strict=True) != parent
        or parent.is_symlink()
    ):
        raise OSError
    for ancestor in parent.parents:
        details = ancestor.lstat()
        shared_write = details.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        if (
            ancestor.is_symlink()
            or ancestor.resolve(strict=True) != ancestor
            or not stat.S_ISDIR(details.st_mode)
            or details.st_uid not in {0, os.getuid()}
            or (shared_write and not details.st_mode & stat.S_ISVTX)
        ):
            raise OSError
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(
        os, "O_NOFOLLOW", 0
    )
    descriptor = os.open(parent, flags)
    try:
        details = os.fstat(descriptor)
        path_details = parent.lstat()
        children = os.listdir(descriptor)
        try:
            os.stat(
                requested_record.name,
                dir_fd=descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            record_absent = True
        else:
            record_absent = False
        if (
            not stat.S_ISDIR(details.st_mode)
            or stat.S_IMODE(details.st_mode) != 0o700
            or details.st_uid != os.getuid()
            or (details.st_dev, details.st_ino)
            != (path_details.st_dev, path_details.st_ino)
            or not record_absent
            or (require_empty and children)
        ):
            raise OSError
    except OSError:
        os.close(descriptor)
        raise
    return descriptor


def validate_requested_record() -> None:
    descriptor = open_requested_record_parent(require_empty=True)
    os.close(descriptor)


def read_staged_record() -> bytes | None:
    if staged_record is None:
        return None
    details = staged_record.lstat()
    payload = staged_record.read_bytes()
    try:
        lines = payload.decode("ascii").splitlines(keepends=True)
    except UnicodeError:
        raise OSError from None
    values: dict[str, str] = {}
    for line in lines:
        key, separator, value = line[:-1].partition("=")
        if not separator or key in values:
            raise OSError
        values[key] = value
    if (
        staged_record.is_symlink()
        or not stat.S_ISREG(details.st_mode)
        or stat.S_IMODE(details.st_mode) != 0o600
        or details.st_uid != os.getuid()
        or details.st_nlink != 1
        or len(lines) != 4
        or any(not line.endswith("\n") for line in lines)
        or set(values) != {"imageTag", "imageId", "platform", "gitSha"}
    ):
        raise OSError
    git_sha = values["gitSha"]
    if (
        values["platform"] != nas_platform
        or not re.fullmatch(r"[0-9a-f]{40}", git_sha)
        or values["imageTag"] != f"seoul-education-travel-map:release-gate-{git_sha}"
        or re.fullmatch(r"sha256:[0-9a-f]{64}", values["imageId"]) is None
    ):
        raise OSError
    return payload


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
    if captured != b"ENCRYPTED_STORAGE_IMAGE_GATE_OK\n":
        raise OSError
    return captured


def rollback_committed_record(commit: tuple[int, tuple[int, int]]) -> None:
    parent_descriptor, identity = commit
    try:
        current = os.stat(
            requested_record.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (current.st_dev, current.st_ino) != identity:
            raise OSError
        os.unlink(requested_record.name, dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)
    finally:
        os.close(parent_descriptor)


def commit_record(payload: bytes | None) -> tuple[int, tuple[int, int]]:
    if requested_record is None:
        raise OSError
    if payload is None:
        raise OSError
    blocked_signals = {signal.SIGHUP, signal.SIGINT, signal.SIGTERM}
    signal.pthread_sigmask(signal.SIG_BLOCK, blocked_signals)
    if interrupted:
        raise OSError
    parent_descriptor = open_requested_record_parent(require_empty=True)
    descriptor = -1
    temporary_name = f".gated-image.{os.getpid()}"
    linked_identity: tuple[int, int] | None = None
    try:
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=parent_descriptor,
        )
        written = 0
        while written < len(payload):
            written += os.write(descriptor, payload[written:])
        os.fsync(descriptor)
        temporary_details = os.fstat(descriptor)
        os.link(
            temporary_name,
            requested_record.name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        linked_details = os.stat(
            requested_record.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (linked_details.st_dev, linked_details.st_ino) != (
            temporary_details.st_dev,
            temporary_details.st_ino,
        ):
            raise OSError
        linked_identity = (linked_details.st_dev, linked_details.st_ino)
        os.unlink(temporary_name, dir_fd=parent_descriptor)
        committed_descriptor = os.open(
            requested_record.name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_descriptor,
        )
        try:
            committed_details = os.fstat(committed_descriptor)
            committed_payload = os.read(committed_descriptor, len(payload) + 1)
        finally:
            os.close(committed_descriptor)
        if (
            not stat.S_ISREG(committed_details.st_mode)
            or stat.S_IMODE(committed_details.st_mode) != 0o600
            or committed_details.st_uid != os.getuid()
            or committed_details.st_nlink != 1
            or (committed_details.st_dev, committed_details.st_ino)
            != linked_identity
            or committed_payload != payload
            or set(os.listdir(parent_descriptor)) != {requested_record.name}
        ):
            raise OSError
        os.fsync(parent_descriptor)
        committed = (parent_descriptor, linked_identity)
        parent_descriptor = -1
        return committed
    except OSError:
        if linked_identity is not None:
            try:
                current = os.stat(
                    requested_record.name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
                if (current.st_dev, current.st_ino) == linked_identity:
                    os.unlink(requested_record.name, dir_fd=parent_descriptor)
                    os.fsync(parent_descriptor)
            except OSError:
                pass
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if parent_descriptor >= 0:
            try:
                os.unlink(temporary_name, dir_fd=parent_descriptor)
            except FileNotFoundError:
                pass
            os.close(parent_descriptor)


for handled_signal in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
    signal.signal(handled_signal, forward_signal)

try:
    validate_requested_record()
except OSError:
    print("BLOCKED_INVALID_IMAGE_RECORD_PATH", file=sys.stderr)
    raise SystemExit(2) from None

environment = {
    "PATH": f"{trusted_bin}:{trusted_path}",
    "HOME": str(private_root / "home"),
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "CI": "1",
    "TMPDIR": str(tmp_root),
    "NAS_PLATFORM": nas_platform,
    "RELEASE_GATE_IMAGE_RECORD": str(staged_record) if staged_record else "",
    "DOCKER_CONFIG": docker_config,
    "TRAVEL_MAP_RELEASE_DOCKER_HOST": docker_host,
    "UV_CACHE_DIR": uv_cache,
    "PLAYWRIGHT_BROWSERS_PATH": playwright_cache,
    "PNPM_STORE_DIR": pnpm_store,
    "XDG_CONFIG_HOME": str(private_root / "xdg-config"),
    "XDG_CACHE_HOME": str(private_root / "xdg-cache"),
    "XDG_DATA_HOME": str(private_root / "xdg-data"),
    "npm_config_cache": str(private_root / "npm-cache"),
    "npm_config_userconfig": "/dev/null",
    "npm_config_globalconfig": "/dev/null",
    "PNPM_HOME": str(private_root / "pnpm-home"),
    "PYTHONDONTWRITEBYTECODE": "1",
    "UV_PROJECT_ENVIRONMENT": str(
        private_root / "xdg-cache" / "test-uv-environment"
    ),
    "UV_LINK_MODE": "copy",
    "UV_PYTHON": approved_python,
    "UV_PYTHON_DOWNLOADS": "never",
    "UV_OFFLINE": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_NO_REPLACE_OBJECTS": "1",
    "TRAVEL_MAP_RELEASE_PRIVATE_ROOT": str(private_root),
    "TRAVEL_MAP_RELEASE_CLEAN_ENVIRONMENT": "1",
    "TRAVEL_MAP_RELEASE_UV_IDENTITY": uv_tool_identity,
    "TRAVEL_MAP_RELEASE_PNPM_IDENTITY": pnpm_tool_identity,
    "TRAVEL_MAP_RELEASE_DOCKER_IDENTITY": docker_tool_identity,
    "TRAVEL_MAP_RELEASE_NODE_IDENTITY": node_tool_identity,
    "TRAVEL_MAP_RELEASE_BUILDX_IDENTITY": buildx_tool_identity,
    "TRAVEL_MAP_RELEASE_PYTHON_RUNTIME": python_runtime_root,
    "TRAVEL_MAP_RELEASE_PYTHON_RUNTIME_IDENTITY": python_runtime_identity,
    "TRAVEL_MAP_RELEASE_PNPM_PACKAGE": pnpm_package_root,
    "TRAVEL_MAP_RELEASE_PNPM_PACKAGE_IDENTITY": pnpm_package_identity,
    "TRAVEL_MAP_RELEASE_PLAYWRIGHT_IDENTITY": playwright_cache_identity,
    "TRAVEL_MAP_RELEASE_PNPM_SOURCE_STORE": pnpm_source_store,
    "TRAVEL_MAP_RELEASE_PNPM_STORE_IDENTITY": pnpm_store_identity,
    "TRAVEL_MAP_RELEASE_PRIVATE_PNPM_STORE_IDENTITY": private_pnpm_store_identity,
    "TRAVEL_MAP_RELEASE_PNPM_SANDBOX_EXEC": str(sandbox_exec),
    "TRAVEL_MAP_RELEASE_PNPM_SANDBOX_PROFILE": profile,
}
status = 2
failure_message: str | None = None
record_payload: bytes | None = None
captured = bytearray()
try:
    if interrupted:
        raise OSError
    process = subprocess.Popen(
        ["/bin/sh", script],
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
        try:
            status = process.wait(timeout=0.1)
            break
        except subprocess.TimeoutExpired:
            if termination_deadline is not None and time.monotonic() >= termination_deadline:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                status = process.wait()
                break
    reap_process_group()
    drain_deadline = time.monotonic() + 1
    while not drain_stdout(captured):
        if time.monotonic() >= drain_deadline:
            raise OSError
        time.sleep(0.01)
    if status == 0 and not interrupted:
        record_payload = read_staged_record()
except OSError:
    failure_message = "BLOCKED_UNSAFE_RELEASE_ENVIRONMENT"
finally:
    try:
        remove_private_root()
    except OSError:
        failure_message = "BLOCKED_GATE_CLEANUP_FAILED"

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
        committed_record = commit_record(record_payload)
        try:
            if os.write(sys.stdout.fileno(), output) != len(output):
                raise OSError
        except OSError:
            rollback_committed_record(committed_record)
            raise
        else:
            os.close(committed_record[0])
    except OSError:
        print("BLOCKED_IMAGE_ATTESTATION", file=sys.stderr)
        raise SystemExit(2) from None
raise SystemExit(status)
PY
        ;;
    1) ;;
    *) blocked 'BLOCKED_UNSAFE_RELEASE_ENVIRONMENT' ;;
esac

# Apple's /bin/sh exports these implementation variables under env -i. They
# are not release inputs and must never reach Git, Python, tests, or Docker.
unset CPATH LIBRARY_PATH MANPATH SDKROOT __CF_USER_TEXT_ENCODING
/usr/bin/python3 -I -S - \
    "$TRAVEL_MAP_RELEASE_PRIVATE_ROOT/trusted-bin:$trusted_path" \
    "$normalized_tmp_root" "$DOCKER_CONFIG" "$uv_cache" \
    "$playwright_cache" "$PNPM_STORE_DIR" "$uv_tool" "$node_tool" \
    "$TRAVEL_MAP_RELEASE_DOCKER_HOST" \
    "${TRAVEL_MAP_RELEASE_UV_IDENTITY:-}" \
    "${TRAVEL_MAP_RELEASE_PNPM_IDENTITY:-}" \
    "${TRAVEL_MAP_RELEASE_DOCKER_IDENTITY:-}" \
    "${TRAVEL_MAP_RELEASE_NODE_IDENTITY:-}" \
    "${TRAVEL_MAP_RELEASE_BUILDX_IDENTITY:-}" \
    "${UV_PYTHON:-}" "${TRAVEL_MAP_RELEASE_PYTHON_RUNTIME:-}" \
    "${TRAVEL_MAP_RELEASE_PYTHON_RUNTIME_IDENTITY:-}" \
    "${TRAVEL_MAP_RELEASE_PNPM_PACKAGE:-}" \
    "${TRAVEL_MAP_RELEASE_PNPM_PACKAGE_IDENTITY:-}" \
    "${TRAVEL_MAP_RELEASE_PLAYWRIGHT_IDENTITY:-}" \
    "${TRAVEL_MAP_RELEASE_PNPM_SOURCE_STORE:-}" \
    "${TRAVEL_MAP_RELEASE_PNPM_STORE_IDENTITY:-}" \
    "${TRAVEL_MAP_RELEASE_PRIVATE_PNPM_STORE_IDENTITY:-}" \
    "$docker_tool" "$buildx_tool" \
    "${TRAVEL_MAP_RELEASE_PNPM_SANDBOX_EXEC:-}" \
    "${TRAVEL_MAP_RELEASE_PNPM_SANDBOX_PROFILE:-}" <<'PY' \
    || blocked 'BLOCKED_UNSAFE_RELEASE_ENVIRONMENT'
import os
import hashlib
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
    "UV_CACHE_DIR": sys.argv[4],
    "PLAYWRIGHT_BROWSERS_PATH": sys.argv[5],
    "PNPM_STORE_DIR": sys.argv[6],
    "PYTHONDONTWRITEBYTECODE": "1",
    "UV_LINK_MODE": "copy",
    "UV_PYTHON": sys.argv[15],
    "UV_PYTHON_DOWNLOADS": "never",
    "UV_OFFLINE": "1",
    "npm_config_userconfig": "/dev/null",
    "npm_config_globalconfig": "/dev/null",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_NO_REPLACE_OBJECTS": "1",
    "TRAVEL_MAP_RELEASE_CLEAN_ENVIRONMENT": "1",
    "TRAVEL_MAP_RELEASE_DOCKER_HOST": sys.argv[9],
    "TRAVEL_MAP_RELEASE_UV_IDENTITY": sys.argv[10],
    "TRAVEL_MAP_RELEASE_PNPM_IDENTITY": sys.argv[11],
    "TRAVEL_MAP_RELEASE_DOCKER_IDENTITY": sys.argv[12],
    "TRAVEL_MAP_RELEASE_NODE_IDENTITY": sys.argv[13],
    "TRAVEL_MAP_RELEASE_BUILDX_IDENTITY": sys.argv[14],
    "TRAVEL_MAP_RELEASE_PYTHON_RUNTIME": sys.argv[16],
    "TRAVEL_MAP_RELEASE_PYTHON_RUNTIME_IDENTITY": sys.argv[17],
    "TRAVEL_MAP_RELEASE_PNPM_PACKAGE": sys.argv[18],
    "TRAVEL_MAP_RELEASE_PNPM_PACKAGE_IDENTITY": sys.argv[19],
    "TRAVEL_MAP_RELEASE_PLAYWRIGHT_IDENTITY": sys.argv[20],
    "TRAVEL_MAP_RELEASE_PNPM_SOURCE_STORE": sys.argv[21],
    "TRAVEL_MAP_RELEASE_PNPM_STORE_IDENTITY": sys.argv[22],
    "TRAVEL_MAP_RELEASE_PRIVATE_PNPM_STORE_IDENTITY": sys.argv[23],
    "TRAVEL_MAP_RELEASE_PNPM_SANDBOX_EXEC": sys.argv[26],
    "TRAVEL_MAP_RELEASE_PNPM_SANDBOX_PROFILE": sys.argv[27],
}
private_layout = {
    "HOME": "home",
    "XDG_CONFIG_HOME": "xdg-config",
    "XDG_CACHE_HOME": "xdg-cache",
    "XDG_DATA_HOME": "xdg-data",
    "npm_config_cache": "npm-cache",
    "PNPM_HOME": "pnpm-home",
}
private_root_name = "TRAVEL_MAP_RELEASE_PRIVATE_ROOT"
derived_paths = {"UV_PROJECT_ENVIRONMENT": "xdg-cache/test-uv-environment"}
controlled_layout = {
    "docker-config",
    "trusted-bin",
    "release-record",
    "pnpm-package",
    "pnpm-store",
}
required_paths = set(private_layout) | set(derived_paths) | {private_root_name}
allowed_inputs = {"NAS_PLATFORM", "RELEASE_GATE_IMAGE_RECORD"}
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
    if not required_paths.issubset(os.environ) or not allowed_inputs.issubset(os.environ):
        raise ValueError
    if (
        set(os.environ)
        - set(expected)
        - required_paths
        - allowed_inputs
        - shell_metadata
        - apple_python_metadata
    ):
        raise ValueError
    private_root = Path(os.environ[private_root_name])
    private_root_details = private_root.lstat()
    if (
        not private_root.is_absolute()
        or private_root.parent != Path(expected["TMPDIR"])
        or not private_root.name.startswith("travel-map-release-environment.")
        or private_root.resolve(strict=True) != private_root
        or not stat.S_ISDIR(private_root_details.st_mode)
        or stat.S_IMODE(private_root_details.st_mode) != 0o700
        or private_root_details.st_uid != os.getuid()
    ):
        raise ValueError
    if {entry.name for entry in private_root.iterdir()} != (
        set(private_layout.values()) | controlled_layout
    ):
        raise ValueError
    if any(
        Path(os.environ[name]) != private_root / relative
        for name, relative in derived_paths.items()
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
    docker_config = private_root / "docker-config"
    config = docker_config / "config.json"
    config_details = config.lstat()
    if (
        Path(os.environ["DOCKER_CONFIG"]) != docker_config
        or config.is_symlink()
        or not stat.S_ISREG(config_details.st_mode)
        or stat.S_IMODE(config_details.st_mode) != 0o600
        or config_details.st_uid != os.getuid()
        or config.read_bytes() != b"{}\n"
    ):
        raise ValueError
    trusted_bin = private_root / "trusted-bin"
    if {entry.name for entry in trusted_bin.iterdir()} != {
        "uv",
        "node",
        "docker",
        "docker-buildx",
    }:
        raise ValueError
    private_tools = (
        ("uv", sys.argv[7], sys.argv[10]),
        ("node", sys.argv[8], sys.argv[13]),
        ("docker", sys.argv[24], sys.argv[12]),
        ("docker-buildx", sys.argv[25], sys.argv[14]),
    )
    for name, executable, identity in private_tools:
        path = trusted_bin / name
        details = path.lstat()
        expected = identity.split(":")
        payload = path.read_bytes()
        if (
            len(expected) != 6
            or Path(executable).resolve(strict=True) != Path(executable)
            or path.is_symlink()
            or not stat.S_ISREG(details.st_mode)
            or stat.S_IMODE(details.st_mode) != 0o500
            or details.st_uid != os.getuid()
            or str(len(payload)) != expected[4]
            or hashlib.sha256(payload).hexdigest() != expected[5]
        ):
            raise ValueError
    release_record = private_root / "release-record"
    record_details = release_record.lstat()
    if (
        release_record.is_symlink()
        or not stat.S_ISDIR(record_details.st_mode)
        or stat.S_IMODE(record_details.st_mode) != 0o700
        or record_details.st_uid != os.getuid()
        or any(release_record.iterdir())
    ):
        raise ValueError
except (OSError, ValueError):
    raise SystemExit(2) from None
PY

release_docker_host=$TRAVEL_MAP_RELEASE_DOCKER_HOST
source_uv_tool=$uv_tool
source_pnpm_tool=$pnpm_tool
source_docker_tool=$docker_tool
source_node_tool=$node_tool
source_buildx_tool=$buildx_tool
uv_tool=$TRAVEL_MAP_RELEASE_PRIVATE_ROOT/trusted-bin/uv
pnpm_tool=$TRAVEL_MAP_RELEASE_PRIVATE_ROOT/pnpm-package/bin/pnpm.mjs
docker_tool=$TRAVEL_MAP_RELEASE_PRIVATE_ROOT/trusted-bin/docker
node_tool=$TRAVEL_MAP_RELEASE_PRIVATE_ROOT/trusted-bin/node
buildx_tool=$TRAVEL_MAP_RELEASE_PRIVATE_ROOT/trusted-bin/docker-buildx
expected_uv_tool_identity=$TRAVEL_MAP_RELEASE_UV_IDENTITY
expected_pnpm_tool_identity=$TRAVEL_MAP_RELEASE_PNPM_IDENTITY
expected_docker_tool_identity=$TRAVEL_MAP_RELEASE_DOCKER_IDENTITY
expected_node_tool_identity=$TRAVEL_MAP_RELEASE_NODE_IDENTITY
expected_buildx_tool_identity=$TRAVEL_MAP_RELEASE_BUILDX_IDENTITY
approved_python=$UV_PYTHON
python_runtime_root=$TRAVEL_MAP_RELEASE_PYTHON_RUNTIME
expected_python_runtime_identity=$TRAVEL_MAP_RELEASE_PYTHON_RUNTIME_IDENTITY
pnpm_package_root=$TRAVEL_MAP_RELEASE_PNPM_PACKAGE
expected_pnpm_package_identity=$TRAVEL_MAP_RELEASE_PNPM_PACKAGE_IDENTITY
expected_playwright_cache_identity=$TRAVEL_MAP_RELEASE_PLAYWRIGHT_IDENTITY
source_pnpm_store=$TRAVEL_MAP_RELEASE_PNPM_SOURCE_STORE
expected_pnpm_store_identity=$TRAVEL_MAP_RELEASE_PNPM_STORE_IDENTITY
expected_private_pnpm_store_identity=$TRAVEL_MAP_RELEASE_PRIVATE_PNPM_STORE_IDENTITY
unset TRAVEL_MAP_RELEASE_DOCKER_HOST \
    TRAVEL_MAP_RELEASE_UV_IDENTITY TRAVEL_MAP_RELEASE_PNPM_IDENTITY \
    TRAVEL_MAP_RELEASE_DOCKER_IDENTITY TRAVEL_MAP_RELEASE_NODE_IDENTITY \
    TRAVEL_MAP_RELEASE_BUILDX_IDENTITY \
    TRAVEL_MAP_RELEASE_PYTHON_RUNTIME \
    TRAVEL_MAP_RELEASE_PYTHON_RUNTIME_IDENTITY \
    TRAVEL_MAP_RELEASE_PNPM_PACKAGE \
    TRAVEL_MAP_RELEASE_PNPM_PACKAGE_IDENTITY \
    TRAVEL_MAP_RELEASE_PLAYWRIGHT_IDENTITY \
    TRAVEL_MAP_RELEASE_PNPM_SOURCE_STORE \
    TRAVEL_MAP_RELEASE_PNPM_STORE_IDENTITY \
    TRAVEL_MAP_RELEASE_PRIVATE_PNPM_STORE_IDENTITY

trusted_uv_cache=$UV_CACHE_DIR
test_uv_environment=$UV_PROJECT_ENVIRONMENT
test_uv_cache=$XDG_CACHE_HOME/test-tool-cache
/bin/mkdir -m 0700 "$test_uv_cache" \
    || blocked 'BLOCKED_PRIVATE_DIRECTORY'
UV_CACHE_DIR=$test_uv_cache
export UV_CACHE_DIR

repo_root=$(CDPATH= cd -- "$script_directory/../../.." && /bin/pwd -P) \
    || blocked 'BLOCKED_INVALID_RELEASE_ARTIFACT'
cd "$repo_root"

gate_parent=
gate_data=
context_parent=
pristine_parent=
gate_image=
gate_container=
record_written=0
gate_completed=0
interrupted=0
record_path_valid=0
host_uid=
host_gid=

run_chown_helper() {
    run_docker run --rm --user 0:0 --network none --read-only \
        --cap-drop ALL --cap-add CHOWN --cap-add FOWNER \
        --security-opt no-new-privileges \
        --mount "type=bind,src=$gate_data,dst=/data" \
        --entrypoint /bin/sh "$gate_image" \
        -eu -c "chown $1:$2 /data; chmod 0700 /data"
}

remove_unfinished_record() {
    [ "$record_path_valid" -eq 1 ] || return 0
    [ -n "$record_path" ] || return 1
    /usr/bin/python3 -I -S - "$record_path" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
if path.exists() and not path.is_symlink() and path.is_file():
    path.unlink()
elif path.exists() or path.is_symlink():
    raise SystemExit(1)
PY
}

cleanup_gate_data() {
    [ -n "$gate_parent" ] || return 0
    [ -n "$gate_data" ] || return 1
    [ "$gate_data" = "$gate_parent/data" ] || return 1
    [ -d "$gate_parent" ] && [ ! -L "$gate_parent" ] || return 1
    [ -d "$gate_data" ] && [ ! -L "$gate_data" ] || return 1

    run_docker run --rm --user 10001:10001 --network none --read-only \
        --cap-drop ALL --security-opt no-new-privileges \
        --mount "type=bind,src=$gate_data,dst=/data" \
        --entrypoint /bin/sh "$gate_image" \
        -eu -c 'find /data -mindepth 1 -depth -delete'
    run_chown_helper "$host_uid" "$host_gid"
    /bin/rmdir "$gate_data" && /bin/rmdir "$gate_parent"
    gate_data=
    gate_parent=
}

cleanup() {
    status=$?
    cleanup_failed=0
    trap - EXIT HUP INT TERM

    if [ "$gate_completed" -ne 1 ] || [ "$interrupted" -eq 1 ]; then
        remove_unfinished_record >/dev/null 2>&1 || cleanup_failed=1
    fi
    if [ -n "$gate_container" ]; then
        run_docker rm -f "$gate_container" >/dev/null 2>&1 || cleanup_failed=1
        gate_container=
    fi
    if [ -n "$gate_parent" ]; then
        cleanup_gate_data >/dev/null 2>&1 || cleanup_failed=1
    fi
    if [ -n "$context_parent" ]; then
        remove_private_directory "$context_parent" || cleanup_failed=1
        context_parent=
    fi
    if [ -n "$pristine_parent" ]; then
        remove_private_directory "$pristine_parent" || cleanup_failed=1
        pristine_parent=
    fi
    if [ "$cleanup_failed" -ne 0 ]; then
        printf '%s\n' 'BLOCKED_GATE_CLEANUP_FAILED' >&2
        status=2
    fi
    exit "$status"
}

interrupted_cleanup() {
    interrupted=1
    trap - HUP INT TERM
    # Let run_untrusted_verified reap its child and perform its mandatory
    # post-invocation identities before this shell's EXIT cleanup runs.
    return 0
}

trap cleanup EXIT
trap interrupted_cleanup HUP INT TERM

case "${NAS_PLATFORM:-}" in
    linux/amd64|linux/arm64) ;;
    *) blocked 'BLOCKED_NAS_PLATFORM_UNVERIFIED' ;;
esac

repo_git() {
    /usr/bin/git -C "$repo_root" \
        -c core.fsmonitor=false \
        -c core.untrackedCache=false \
        "$@"
}

verify_reviewed_git_source() {
    /usr/bin/python3 -I -S - "$repo_root" "$git_sha" /usr/bin/git <<'PY'
import re
import subprocess
import sys
from pathlib import Path


def git(*arguments):
    return subprocess.run(
        [
            sys.argv[3],
            "-C",
            str(root),
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.untrackedCache=false",
            *arguments,
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )


try:
    root = Path(sys.argv[1])
    expected_sha = sys.argv[2]
    top = git("rev-parse", "--show-toplevel")
    head = git("rev-parse", "--verify", "HEAD^{commit}")
    status = git(
        "status",
        "--porcelain=v1",
        "-z",
        "--ignore-submodules=all",
        "--untracked-files=normal",
    )
    index = git("ls-files", "-v", "-z")
    if any(result.returncode != 0 for result in (top, head, status, index)):
        raise ValueError
    if Path(top.stdout.decode("utf-8").strip()) != root:
        raise ValueError
    sha = head.stdout.decode("ascii").strip()
    entries = [entry for entry in index.stdout.split(b"\0") if entry]
    if (
        re.fullmatch(r"[0-9a-f]{40}", expected_sha) is None
        or sha != expected_sha
        or status.stdout
        or not entries
        or any(not entry.startswith(b"H ") for entry in entries)
    ):
        raise ValueError
except (OSError, UnicodeError, ValueError):
    raise SystemExit(2) from None
PY
}

verify_materialized_source() {
    /usr/bin/python3 -I -S - \
        "$1" "$2" "$3" "$4" "$5" <<'PY'
import hashlib
import os
import re
import stat
import sys
from pathlib import Path, PurePosixPath


def blob_id(payload):
    header = f"blob {len(payload)}\0".encode("ascii")
    return hashlib.sha1(header + payload, usedforsecurity=False).hexdigest()


def safe_path(raw_path):
    if (
        not raw_path
        or raw_path.startswith("/")
        or raw_path.endswith("/")
        or any(part in {"", ".", "..", ".git"} for part in raw_path.split("/"))
    ):
        raise ValueError
    path = PurePosixPath(raw_path)
    if path.is_absolute() or path.as_posix() != raw_path:
        raise ValueError
    return path


try:
    source = Path(sys.argv[1])
    tree = Path(sys.argv[2])
    source_parent = Path(sys.argv[3])
    tree_parent = Path(sys.argv[4])
    require_exact_set = sys.argv[5] == "exact"
    source_parent_details = source_parent.lstat()
    tree_parent_details = tree_parent.lstat()
    source_details = source.lstat()
    tree_details = tree.lstat()
    if (
        source.parent != source_parent
        or tree.parent != tree_parent
        or source_parent.resolve(strict=True) != source_parent
        or tree_parent.resolve(strict=True) != tree_parent
        or not stat.S_ISDIR(source_parent_details.st_mode)
        or stat.S_IMODE(source_parent_details.st_mode) != 0o700
        or source_parent_details.st_uid != os.getuid()
        or not stat.S_ISDIR(tree_parent_details.st_mode)
        or stat.S_IMODE(tree_parent_details.st_mode) != 0o700
        or tree_parent_details.st_uid != os.getuid()
        or source.resolve(strict=True) != source
        or not stat.S_ISDIR(source_details.st_mode)
        or stat.S_IMODE(source_details.st_mode) != 0o700
        or source_details.st_uid != os.getuid()
        or tree.is_symlink()
        or not stat.S_ISREG(tree_details.st_mode)
        or stat.S_IMODE(tree_details.st_mode) != 0o600
        or tree_details.st_uid != os.getuid()
    ):
        raise ValueError
    tree_payload = tree.read_bytes()
    if not tree_payload or not tree_payload.endswith(b"\0"):
        raise ValueError
    entries = {}
    directories = set()
    for record in tree_payload[:-1].split(b"\0"):
        header, separator, raw_path_bytes = record.partition(b"\t")
        fields = header.split(b" ")
        if not separator or len(fields) != 3:
            raise ValueError
        mode, object_type, object_id_bytes = fields
        raw_path = raw_path_bytes.decode("utf-8")
        path = safe_path(raw_path)
        object_id = object_id_bytes.decode("ascii")
        if (
            mode not in {b"100644", b"100755"}
            or object_type != b"blob"
            or re.fullmatch(r"[0-9a-f]{40}", object_id) is None
            or raw_path in entries
        ):
            raise ValueError
        entries[raw_path] = (mode, object_id)
        parent = path.parent
        while parent != PurePosixPath("."):
            directories.add(parent.as_posix())
            parent = parent.parent
    if not entries:
        raise ValueError
    for raw_directory in directories:
        directory = source / raw_directory
        details = directory.lstat()
        if (
            directory.is_symlink()
            or not stat.S_ISDIR(details.st_mode)
            or stat.S_IMODE(details.st_mode) != 0o700
            or details.st_uid != os.getuid()
        ):
            raise ValueError
    validated_files = set()
    for raw_path, (mode, expected_id) in entries.items():
        path = source / raw_path
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            details = os.fstat(descriptor)
            expected_mode = 0o755 if mode == b"100755" else 0o644
            if (
                not stat.S_ISREG(details.st_mode)
                or stat.S_IMODE(details.st_mode) != expected_mode
                or details.st_uid != os.getuid()
            ):
                raise ValueError
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                payload = stream.read()
        finally:
            os.close(descriptor)
        if blob_id(payload) != expected_id:
            raise ValueError
        validated_files.add(raw_path)
    if validated_files != set(entries):
        raise ValueError
    if require_exact_set:
        actual_files = set()
        actual_directories = set()
        for directory, child_directories, child_files in os.walk(
            source, followlinks=False
        ):
            current = Path(directory)
            relative_directory = current.relative_to(source)
            if relative_directory != Path("."):
                actual_directories.add(relative_directory.as_posix())
            for child in child_directories:
                path = current / child
                if path.is_symlink():
                    raise ValueError
            for child in child_files:
                path = current / child
                if path.is_symlink() or not path.is_file():
                    raise ValueError
                actual_files.add(path.relative_to(source).as_posix())
        if actual_files != set(entries) or actual_directories != directories:
            raise ValueError
except (OSError, UnicodeError, ValueError):
    raise SystemExit(2) from None
PY
}

run_untrusted() {
    /usr/bin/python3 -I -S - "$@" <<'PY'
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

process = None
interrupted = False
termination_deadline = None


def forward(signum, _frame):
    global interrupted, termination_deadline
    interrupted = True
    termination_deadline = time.monotonic() + 4
    if process is not None:
        try:
            os.killpg(process.pid, signum)
        except ProcessLookupError:
            pass


def reap_group():
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


for handled in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
    signal.signal(handled, forward)

try:
    if interrupted:
        raise OSError
    command = sys.argv[1:]
    if Path(command[0]).name in {"pnpm", "pnpm.mjs"}:
        sandbox_exec = os.environ.get("TRAVEL_MAP_RELEASE_PNPM_SANDBOX_EXEC")
        sandbox_profile = os.environ.get("TRAVEL_MAP_RELEASE_PNPM_SANDBOX_PROFILE")
        if not sandbox_exec or not sandbox_profile:
            raise OSError
        command = [sandbox_exec, "-p", sandbox_profile, *command]
    process = subprocess.Popen(command, start_new_session=True)
    if interrupted and process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
    while True:
        try:
            status = process.wait(timeout=0.1)
            break
        except subprocess.TimeoutExpired:
            if (
                termination_deadline is not None
                and time.monotonic() >= termination_deadline
            ):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                status = process.wait(timeout=1)
                break
    reap_group()
except (OSError, subprocess.TimeoutExpired, ValueError):
    if process is not None and process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
    raise SystemExit(2) from None
if interrupted or status < 0:
    raise SystemExit(2)
raise SystemExit(status)
PY
}

verify_runtime_anchors() {
    /usr/bin/python3 -I -S - \
        "$source_uv_tool" "$expected_uv_tool_identity" \
        "$source_pnpm_tool" "$expected_pnpm_tool_identity" \
        "$source_docker_tool" "$expected_docker_tool_identity" \
        "$source_node_tool" "$expected_node_tool_identity" \
        "$source_buildx_tool" "$expected_buildx_tool_identity" \
        "$TRAVEL_MAP_RELEASE_PRIVATE_ROOT/trusted-bin" \
        "$DOCKER_CONFIG" "$approved_python" "$python_runtime_root" \
        "$expected_python_runtime_identity" "$pnpm_package_root" \
        "$expected_pnpm_package_identity" "$PLAYWRIGHT_BROWSERS_PATH" \
        "$expected_playwright_cache_identity" "$trusted_uv_cache" \
        <<'PY'
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path


def verify_tool(raw_path, raw_expected):
    path = Path(raw_path)
    expected = raw_expected.split(":")
    if len(expected) != 6 or re.fullmatch(r"[0-9a-f]{64}", expected[5]) is None:
        raise ValueError
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        details = os.fstat(descriptor)
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            payload = source.read()
    finally:
        os.close(descriptor)
    actual = (
        str(details.st_uid),
        str(stat.S_IMODE(details.st_mode)),
        str(details.st_dev),
        str(details.st_ino),
        str(details.st_size),
        hashlib.sha256(payload).hexdigest(),
    )
    if (
        path.resolve(strict=True) != path
        or path.is_symlink()
        or not stat.S_ISREG(details.st_mode)
        or tuple(expected) != actual
    ):
        raise ValueError


def validate_trusted_root(root, *, strict_ancestors=True):
    root_details = root.lstat()
    if (
        root.is_symlink()
        or root.resolve(strict=True) != root
        or not stat.S_ISDIR(root_details.st_mode)
        or root_details.st_uid not in {0, os.getuid()}
        or root_details.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise ValueError
    if strict_ancestors:
        for ancestor in root.parents:
            ancestor_details = ancestor.lstat()
            if (
                ancestor.is_symlink()
                or ancestor.resolve(strict=True) != ancestor
                or not stat.S_ISDIR(ancestor_details.st_mode)
                or ancestor_details.st_uid not in {0, os.getuid()}
                or ancestor_details.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            ):
                raise ValueError


def runtime_identity(root, *, strict_ancestors=True, reject_symlinks=False):
    validate_trusted_root(root, strict_ancestors=strict_ancestors)
    records = []
    for directory_raw, child_directories, child_files in os.walk(
        root, topdown=True, followlinks=False
    ):
        directory = Path(directory_raw)
        child_directories.sort()
        child_files.sort()
        for child in [*child_directories, *child_files]:
            path = directory / child
            details = path.lstat()
            relative = path.relative_to(root).as_posix()
            mode = stat.S_IMODE(details.st_mode)
            if stat.S_ISDIR(details.st_mode):
                if (
                    path.is_symlink()
                    or details.st_uid not in {0, os.getuid()}
                    or details.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                ):
                    raise ValueError
                records.append([relative, "directory", details.st_uid, mode])
            elif stat.S_ISREG(details.st_mode):
                if (
                    details.st_uid not in {0, os.getuid()}
                    or details.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                ):
                    raise ValueError
                descriptor = os.open(
                    path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                )
                try:
                    opened = os.fstat(descriptor)
                    digest = hashlib.sha256()
                    while True:
                        chunk = os.read(descriptor, 1024 * 1024)
                        if not chunk:
                            break
                        digest.update(chunk)
                finally:
                    os.close(descriptor)
                if (opened.st_dev, opened.st_ino) != (details.st_dev, details.st_ino):
                    raise ValueError
                records.append(
                    [relative, "file", details.st_uid, mode, details.st_size, digest.hexdigest()]
                )
            elif stat.S_ISLNK(details.st_mode):
                if reject_symlinks or details.st_uid not in {0, os.getuid()}:
                    raise ValueError
                target = os.readlink(path)
                resolved = path.resolve(strict=True)
                if root not in resolved.parents and resolved != root:
                    raise ValueError
                records.append([relative, "symlink", details.st_uid, mode, target])
            else:
                raise ValueError
    if not records:
        raise ValueError
    payload = json.dumps(records, ensure_ascii=True, separators=(",", ":")).encode(
        "ascii"
    )
    return hashlib.sha256(payload).hexdigest()


try:
    arguments = sys.argv[1:]
    tool_pairs = tuple(zip(arguments[0:10:2], arguments[1:10:2]))
    if len(tool_pairs) != 5:
        raise ValueError
    for path, expected in tool_pairs:
        verify_tool(path, expected)
    trusted_bin = Path(arguments[10])
    docker_config = Path(arguments[11])
    approved_python = Path(arguments[12])
    python_runtime_root = Path(arguments[13])
    expected_python_runtime_identity = arguments[14]
    pnpm_package_root = Path(arguments[15])
    expected_pnpm_package_identity = arguments[16]
    playwright_cache = Path(arguments[17])
    expected_playwright_cache_identity = arguments[18]
    trusted_uv_cache = Path(arguments[19])
    validate_trusted_root(trusted_uv_cache)
    if (
        approved_python != python_runtime_root / "bin/python3.12"
        or approved_python.resolve(strict=True) != approved_python
        or not approved_python.is_file()
        or approved_python.is_symlink()
        or re.fullmatch(r"[0-9a-f]{64}", expected_python_runtime_identity) is None
        or runtime_identity(python_runtime_root)
        != expected_python_runtime_identity
    ):
        raise ValueError
    if (
        re.fullmatch(r"[0-9a-f]{64}", expected_playwright_cache_identity) is None
        or runtime_identity(playwright_cache) != expected_playwright_cache_identity
    ):
        raise ValueError
    if (
        Path(arguments[2]).parent.parent != pnpm_package_root
        or Path(arguments[2]).parent.name != "bin"
        or re.fullmatch(r"[0-9a-f]{64}", expected_pnpm_package_identity) is None
        or runtime_identity(pnpm_package_root) != expected_pnpm_package_identity
    ):
        raise ValueError
    private_pnpm_package = trusted_bin.parent / "pnpm-package"
    if (
        runtime_identity(private_pnpm_package, strict_ancestors=False)
        != expected_pnpm_package_identity
    ):
        raise ValueError
    for directory in (trusted_bin, docker_config):
        details = directory.lstat()
        if (
            directory.is_symlink()
            or directory.resolve(strict=True) != directory
            or not stat.S_ISDIR(details.st_mode)
            or stat.S_IMODE(details.st_mode) != 0o700
            or details.st_uid != os.getuid()
        ):
            raise ValueError
    if {entry.name for entry in trusted_bin.iterdir()} != {
        "uv",
        "node",
        "docker",
        "docker-buildx",
    }:
        raise ValueError
    private_tools = {
        "uv": arguments[1],
        "node": arguments[7],
        "docker": arguments[5],
        "docker-buildx": arguments[9],
    }
    for name, raw_expected in private_tools.items():
        path = trusted_bin / name
        details = path.lstat()
        expected = raw_expected.split(":")
        payload = path.read_bytes()
        if (
            len(expected) != 6
            or path.is_symlink()
            or not stat.S_ISREG(details.st_mode)
            or stat.S_IMODE(details.st_mode) != 0o500
            or details.st_uid != os.getuid()
            or str(len(payload)) != expected[4]
            or hashlib.sha256(payload).hexdigest() != expected[5]
        ):
            raise ValueError
    config = docker_config / "config.json"
    config_details = config.lstat()
    if (
        {entry.name for entry in docker_config.iterdir()} != {"config.json"}
        or config.is_symlink()
        or not stat.S_ISREG(config_details.st_mode)
        or stat.S_IMODE(config_details.st_mode) != 0o600
        or config_details.st_uid != os.getuid()
        or config.read_bytes() != b"{}\n"
    ):
        raise ValueError
except (OSError, ValueError):
    raise SystemExit(2) from None
PY
}

verify_pnpm_stores() {
    set +e
    actual_source_pnpm_store_identity=$(capture_pnpm_store_identity \
        "$source_pnpm_store")
    source_capture_status=$?
    actual_private_pnpm_store_identity=$(capture_pnpm_store_identity \
        "$PNPM_STORE_DIR" private)
    private_capture_status=$?
    set -e
    [ "$source_capture_status" -eq 0 ] \
        && [ "$private_capture_status" -eq 0 ] || return 1
    [ "$actual_source_pnpm_store_identity" = "$expected_pnpm_store_identity" ] \
        && [ "$actual_private_pnpm_store_identity" = "$expected_private_pnpm_store_identity" ]
}

run_untrusted_verified() {
    verify_runtime_anchors || return 2
    case "$1" in
        "$pnpm_tool") verify_pnpm_stores || return 2 ;;
    esac
    set +e
    run_untrusted "$@"
    untrusted_status=$?
    set -e
    case "$1" in
        "$pnpm_tool") verify_pnpm_stores || return 2 ;;
    esac
    verify_runtime_anchors || return 2
    return "$untrusted_status"
}

verify_docker_context() {
    /usr/bin/python3 -I -S - "$1" "$2" "$3" <<'PY'
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

action, root_raw, manifest_raw = sys.argv[1:]
root = Path(root_raw)
manifest = Path(manifest_raw)


def snapshot():
    root_details = root.lstat()
    if (
        root.is_symlink()
        or not stat.S_ISDIR(root_details.st_mode)
        or stat.S_IMODE(root_details.st_mode) != 0o700
        or root_details.st_uid != os.getuid()
    ):
        raise ValueError
    directories = {}
    files = {}
    for directory_raw, child_directories, child_files in os.walk(
        root, followlinks=False
    ):
        directory = Path(directory_raw)
        for child in child_directories:
            path = directory / child
            details = path.lstat()
            if path.is_symlink() or not stat.S_ISDIR(details.st_mode):
                raise ValueError
            directories[path.relative_to(root).as_posix()] = {
                "mode": stat.S_IMODE(details.st_mode),
                "uid": details.st_uid,
            }
        for child in child_files:
            path = directory / child
            descriptor = os.open(
                path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                details = os.fstat(descriptor)
                if not stat.S_ISREG(details.st_mode):
                    raise ValueError
                with os.fdopen(descriptor, "rb", closefd=False) as source:
                    payload = source.read()
            finally:
                os.close(descriptor)
            files[path.relative_to(root).as_posix()] = {
                "mode": stat.S_IMODE(details.st_mode),
                "uid": details.st_uid,
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
    if not files or "Dockerfile" not in files:
        raise ValueError
    return {"directories": directories, "files": files}


try:
    current = snapshot()
    if action == "capture":
        if manifest.exists() or manifest.is_symlink() or manifest.parent != root.parent:
            raise ValueError
        payload = (
            json.dumps(current, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        descriptor = os.open(
            manifest,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
    elif action == "verify":
        details = manifest.lstat()
        if (
            manifest.is_symlink()
            or not stat.S_ISREG(details.st_mode)
            or stat.S_IMODE(details.st_mode) != 0o600
            or details.st_uid != os.getuid()
            or json.loads(manifest.read_text(encoding="utf-8")) != current
        ):
            raise ValueError
    else:
        raise ValueError
except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
    raise SystemExit(2) from None
PY
}

set +e
git_sha=$(/usr/bin/python3 -I -S - "$repo_root" /usr/bin/git <<'PY'
import re
import subprocess
import sys
from pathlib import Path


def git(*arguments):
    return subprocess.run(
        [
            sys.argv[2],
            "-C",
            str(root),
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.untrackedCache=false",
            *arguments,
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )


try:
    root = Path(sys.argv[1])
    top = git("rev-parse", "--show-toplevel")
    head = git("rev-parse", "--verify", "HEAD^{commit}")
    status = git(
        "status",
        "--porcelain=v1",
        "-z",
        "--ignore-submodules=all",
        "--untracked-files=normal",
    )
    index = git("ls-files", "-v", "-z")
    if any(result.returncode != 0 for result in (top, head, status, index)):
        raise ValueError
    if Path(top.stdout.decode("utf-8").strip()) != root:
        raise ValueError
    sha = head.stdout.decode("ascii").strip()
    if re.fullmatch(r"[0-9a-f]{40}", sha) is None:
        raise ValueError
    entries = [entry for entry in index.stdout.split(b"\0") if entry]
    if status.stdout or not entries or any(not entry.startswith(b"H ") for entry in entries):
        raise SystemExit(3)
except (OSError, UnicodeError, ValueError):
    raise SystemExit(2) from None
print(sha)
PY
)
source_status=$?
set -e
case "$source_status" in
    0) ;;
    3) blocked 'BLOCKED_DIRTY_RELEASE_SOURCE' ;;
    *) blocked 'BLOCKED_INVALID_RELEASE_ARTIFACT' ;;
esac
gate_image="seoul-education-travel-map:release-gate-$git_sha"

record_path=${RELEASE_GATE_IMAGE_RECORD:-}
[ -n "$record_path" ] || blocked 'BLOCKED_INVALID_IMAGE_RECORD_PATH'
if [ -n "$record_path" ]; then
    case "$record_path" in
        /*/gated-image.record) ;;
        *) blocked 'BLOCKED_INVALID_IMAGE_RECORD_PATH' ;;
    esac
    record_parent=${record_path%/gated-image.record}
    [ -n "$record_parent" ] || blocked 'BLOCKED_INVALID_IMAGE_RECORD_PATH'
    [ ! -e "$record_path" ] && [ ! -L "$record_path" ] || blocked 'BLOCKED_INVALID_IMAGE_RECORD_PATH'
    [ -d "$record_parent" ] && [ ! -L "$record_parent" ] || blocked 'BLOCKED_INVALID_IMAGE_RECORD_PATH'
    record_parent_physical=$(CDPATH= cd -- "$record_parent" && /bin/pwd -P) \
        || blocked 'BLOCKED_INVALID_IMAGE_RECORD_PATH'
    [ "$record_parent" = "$record_parent_physical" ] || blocked 'BLOCKED_INVALID_IMAGE_RECORD_PATH'
    case "$(stat_owner_mode "$record_parent")" in
        "$(/usr/bin/id -u):700") ;;
        *) blocked 'BLOCKED_INVALID_IMAGE_RECORD_PATH' ;;
    esac
    record_path_valid=1
fi

context_parent=$(/usr/bin/mktemp -d "$TMPDIR/travel-map-release.XXXXXX") \
    || blocked 'BLOCKED_PRIVATE_DIRECTORY'
/bin/chmod 0700 "$context_parent" || blocked 'BLOCKED_PRIVATE_DIRECTORY'
source_archive=$context_parent/source.tar
source_tree=$context_parent/source-tree.bin
pinned_source=$context_parent/pinned-source
pristine_source=
materializer_source=$repo_root/apps/travel-map/scripts/materialize-pinned-source.py
trusted_materializer=$context_parent/materialize-pinned-source.py
context_root=

copy_verified_materializer() {
    verified_destination=$1
/usr/bin/python3 -I -S - \
    "$repo_root" "$git_sha" "$script_path" "$materializer_source" \
    "$verified_destination" <<'PY'
import hashlib
import os
import re
import stat
import subprocess
import sys
from pathlib import Path


def git_blob_id(payload):
    header = f"blob {len(payload)}\0".encode("ascii")
    return hashlib.sha1(header + payload, usedforsecurity=False).hexdigest()


def read_artifact(path, expected_mode):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or stat.S_IMODE(details.st_mode) != expected_mode
            or details.st_uid != os.getuid()
        ):
            raise ValueError
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            return stream.read()
    finally:
        os.close(descriptor)


try:
    root = Path(sys.argv[1])
    git_sha = sys.argv[2]
    gate = Path(sys.argv[3])
    materializer = Path(sys.argv[4])
    destination = Path(sys.argv[5])
    artifacts = {
        "apps/travel-map/scripts/release-gate.sh": (gate, "100755", 0o755),
        "apps/travel-map/scripts/materialize-pinned-source.py": (
            materializer,
            "100644",
            0o644,
        ),
    }
    if not re.fullmatch(r"[0-9a-f]{40}", git_sha):
        raise ValueError
    for relative, (path, _, _) in artifacts.items():
        if path.resolve(strict=True) != root / relative:
            raise ValueError
    completed = subprocess.run(
        [
            "/usr/bin/git",
            "-C",
            str(root),
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.untrackedCache=false",
            "ls-tree",
            "-z",
            "--full-tree",
            git_sha,
            "--",
            *artifacts,
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if completed.returncode != 0:
        raise ValueError
    tree_entries = {}
    for record in completed.stdout.split(b"\0"):
        if not record:
            continue
        header, separator, raw_path = record.partition(b"\t")
        fields = header.split(b" ")
        if not separator or len(fields) != 3:
            raise ValueError
        mode, object_type, object_id = fields
        relative = raw_path.decode("utf-8")
        if relative in tree_entries or object_type != b"blob":
            raise ValueError
        tree_entries[relative] = (mode.decode("ascii"), object_id.decode("ascii"))
    if set(tree_entries) != set(artifacts):
        raise ValueError
    payloads = {}
    for relative, (path, git_mode, file_mode) in artifacts.items():
        payload = read_artifact(path, file_mode)
        recorded_mode, recorded_id = tree_entries[relative]
        if recorded_mode != git_mode or git_blob_id(payload) != recorded_id:
            raise ValueError
        payloads[relative] = payload
    parent = destination.parent.lstat()
    if (
        not stat.S_ISDIR(parent.st_mode)
        or stat.S_IMODE(parent.st_mode) != 0o700
        or parent.st_uid != os.getuid()
        or destination.exists()
        or destination.is_symlink()
    ):
        raise ValueError
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(payloads["apps/travel-map/scripts/materialize-pinned-source.py"])
        output.flush()
        os.fsync(output.fileno())
except (OSError, UnicodeError, ValueError):
    raise SystemExit(2) from None
PY
}

copy_verified_materializer "$trusted_materializer" \
    || blocked 'BLOCKED_INVALID_RELEASE_ARTIFACT'
trusted_materializer_blob=$(repo_git rev-parse \
    "$git_sha:apps/travel-map/scripts/materialize-pinned-source.py") \
    || blocked 'BLOCKED_INVALID_RELEASE_ARTIFACT'
[ "${#trusted_materializer_blob}" -eq 40 ] \
    || blocked 'BLOCKED_INVALID_RELEASE_ARTIFACT'
case "$trusted_materializer_blob" in
    *[!0-9a-f]*) blocked 'BLOCKED_INVALID_RELEASE_ARTIFACT' ;;
esac

repo_git -c tar.umask=0002 archive --format=tar \
    --output "$source_archive" "$git_sha" \
    || blocked 'BLOCKED_INVALID_RELEASE_ARTIFACT'
repo_git ls-tree -r -z --full-tree "$git_sha" > "$source_tree" \
    || blocked 'BLOCKED_INVALID_RELEASE_ARTIFACT'
/bin/chmod 0600 "$source_archive" "$source_tree" \
    || blocked 'BLOCKED_INVALID_RELEASE_ARTIFACT'
/usr/bin/python3 -I -S \
    "$trusted_materializer" \
    --archive "$source_archive" \
    --tree "$source_tree" \
    --destination "$pinned_source" \
    || blocked 'BLOCKED_INVALID_RELEASE_ARTIFACT'
cd "$pinned_source"

PYTHONWARNINGS=error
UV_CACHE_DIR=$trusted_uv_cache
UV_PROJECT_ENVIRONMENT=$test_uv_environment
export PYTHONWARNINGS UV_CACHE_DIR UV_PROJECT_ENVIRONMENT
run_untrusted_verified "$uv_tool" sync --project apps/travel-map --locked --dev \
    --python "$approved_python" --no-python-downloads --offline \
    || blocked 'BLOCKED_INVALID_RELEASE_ARTIFACT'
UV_CACHE_DIR=$test_uv_cache
export UV_CACHE_DIR
run_untrusted_verified "$uv_tool" run --locked --no-sync \
    --python "$approved_python" --no-python-downloads --offline \
    --project apps/travel-map pytest apps/travel-map/tests -q \
    || blocked 'BLOCKED_INVALID_RELEASE_ARTIFACT'
run_untrusted_verified "$uv_tool" run --locked --no-sync \
    --python "$approved_python" --no-python-downloads --offline \
    --project apps/travel-map ruff check \
    apps/travel-map/app apps/travel-map/tests apps/travel-map/scripts \
    || blocked 'BLOCKED_INVALID_RELEASE_ARTIFACT'
run_untrusted_verified "$uv_tool" run --locked --no-sync \
    --python "$approved_python" --no-python-downloads --offline \
    --project apps/travel-map ruff format --check \
    apps/travel-map/app apps/travel-map/tests apps/travel-map/scripts \
    || blocked 'BLOCKED_INVALID_RELEASE_ARTIFACT'
run_untrusted_verified "$uv_tool" run --locked --no-sync \
    --python "$approved_python" --no-python-downloads --offline \
    --project apps/travel-map mypy \
    apps/travel-map/app apps/travel-map/scripts \
    || blocked 'BLOCKED_INVALID_RELEASE_ARTIFACT'
run_untrusted_verified "$pnpm_tool" --store-dir "$PNPM_STORE_DIR" \
    --dir apps/travel-map install --frozen-lockfile --offline \
    || blocked 'BLOCKED_INVALID_RELEASE_ARTIFACT'
run_untrusted_verified "$pnpm_tool" --dir apps/travel-map test:e2e \
    || blocked 'BLOCKED_INVALID_RELEASE_ARTIFACT'

# A tool may create private caches, but it may not change the reviewed tracked
# path set, modes, or bytes. Re-read the unchanged HEAD and compare the complete
# tracked tree before preparing any Docker context.
verify_reviewed_git_source || blocked 'BLOCKED_INVALID_RELEASE_ARTIFACT'
/usr/bin/python3 -I -S - \
    "$trusted_materializer" "$trusted_materializer_blob" <<'PY' \
    || blocked 'BLOCKED_INVALID_RELEASE_ARTIFACT'
import hashlib
import os
import re
import stat
import sys
from pathlib import Path


def blob_id(payload):
    header = f"blob {len(payload)}\0".encode("ascii")
    return hashlib.sha1(header + payload, usedforsecurity=False).hexdigest()


try:
    path = Path(sys.argv[1])
    expected = sys.argv[2]
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or stat.S_IMODE(details.st_mode) != 0o600
            or details.st_uid != os.getuid()
            or re.fullmatch(r"[0-9a-f]{40}", expected) is None
        ):
            raise ValueError
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            payload = source.read()
    finally:
        os.close(descriptor)
    if blob_id(payload) != expected:
        raise ValueError
except (OSError, ValueError):
    raise SystemExit(2) from None
PY

pristine_parent=$(/usr/bin/mktemp -d \
    "$TMPDIR/travel-map-pristine.XXXXXX") \
    || blocked 'BLOCKED_PRIVATE_DIRECTORY'
/bin/chmod 0700 "$pristine_parent" \
    || blocked 'BLOCKED_PRIVATE_DIRECTORY'
fresh_materializer=$pristine_parent/materialize-pinned-source.py
copy_verified_materializer "$fresh_materializer" \
    || blocked 'BLOCKED_INVALID_RELEASE_ARTIFACT'
pristine_archive=$(/usr/bin/mktemp \
    "$pristine_parent/source.XXXXXX.tar") \
    || blocked 'BLOCKED_PRIVATE_DIRECTORY'
pristine_tree=$(/usr/bin/mktemp \
    "$pristine_parent/source-tree.XXXXXX.bin") \
    || blocked 'BLOCKED_PRIVATE_DIRECTORY'
pristine_source=$pristine_parent/pristine-source
context_root=$pristine_parent/context
context_manifest=$pristine_parent/context-manifest.json
repo_git -c tar.umask=0002 archive --format=tar \
    --output "$pristine_archive" "$git_sha" \
    || blocked 'BLOCKED_INVALID_RELEASE_ARTIFACT'
repo_git ls-tree -r -z --full-tree "$git_sha" > "$pristine_tree" \
    || blocked 'BLOCKED_INVALID_RELEASE_ARTIFACT'
/bin/chmod 0600 "$pristine_archive" "$pristine_tree" \
    || blocked 'BLOCKED_INVALID_RELEASE_ARTIFACT'
verify_reviewed_git_source || blocked 'BLOCKED_INVALID_RELEASE_ARTIFACT'
verify_materialized_source \
    "$pinned_source" "$pristine_tree" \
    "$context_parent" "$pristine_parent" tracked \
    || blocked 'BLOCKED_INVALID_RELEASE_ARTIFACT'

# Materialize a new pristine source only after the test tree passes. The Docker
# context is prepared from this second exact HEAD extraction, never from the
# source tree that executed tests or package scripts.
/usr/bin/python3 -I -S \
    "$fresh_materializer" \
    --archive "$pristine_archive" \
    --tree "$pristine_tree" \
    --destination "$pristine_source" \
    || blocked 'BLOCKED_INVALID_RELEASE_ARTIFACT'
cd "$pristine_source"
prepare_environment_parent=$(/usr/bin/mktemp -d \
    "$XDG_CACHE_HOME/prepare-environment.XXXXXX") \
    || blocked 'BLOCKED_PRIVATE_DIRECTORY'
/bin/chmod 0700 "$prepare_environment_parent" \
    || blocked 'BLOCKED_PRIVATE_DIRECTORY'
prepare_uv_environment=$prepare_environment_parent/venv
prepare_uv_cache=$prepare_environment_parent/cache
/bin/mkdir -m 0700 "$prepare_uv_cache" \
    || blocked 'BLOCKED_PRIVATE_DIRECTORY'
UV_PROJECT_ENVIRONMENT=$prepare_uv_environment
UV_CACHE_DIR=$prepare_uv_cache
export UV_PROJECT_ENVIRONMENT UV_CACHE_DIR
UV_CACHE_DIR=$trusted_uv_cache
export UV_CACHE_DIR
run_untrusted_verified "$uv_tool" sync --project apps/travel-map --locked --dev \
    --python "$approved_python" --no-python-downloads --offline \
    || blocked 'BLOCKED_INVALID_RELEASE_ARTIFACT'
UV_CACHE_DIR=$prepare_uv_cache
export UV_CACHE_DIR
if ! snapshot_id=$(run_untrusted_verified \
    "$uv_tool" run --locked --no-sync \
    --python "$approved_python" --no-python-downloads --offline \
    --project apps/travel-map python \
    apps/travel-map/scripts/prepare-release-context.py \
    --source apps/travel-map --destination "$context_root" 2>/dev/null); then
    blocked 'BLOCKED_INVALID_RELEASE_ARTIFACT'
fi
verify_docker_context capture "$context_root" "$context_manifest" \
    || blocked 'BLOCKED_INVALID_RELEASE_ARTIFACT'
verify_materialized_source \
    "$pristine_source" "$pristine_tree" \
    "$pristine_parent" "$pristine_parent" exact \
    || blocked 'BLOCKED_INVALID_RELEASE_ARTIFACT'

verify_runtime_anchors || blocked 'BLOCKED_INVALID_RELEASE_ARTIFACT'
if ! run_docker version >/dev/null 2>&1; then
    blocked 'BLOCKED_DOCKER_UNAVAILABLE'
fi
verify_docker_context verify "$context_root" "$context_manifest" \
    || blocked 'BLOCKED_INVALID_RELEASE_ARTIFACT'
verify_runtime_anchors || blocked 'BLOCKED_INVALID_RELEASE_ARTIFACT'

run_buildx build --platform "$NAS_PLATFORM" \
    --build-arg SNAPSHOT_ID="$snapshot_id" \
    --load --tag "$gate_image" "$context_root"
image_id=$(run_docker image inspect --format '{{.Id}}' "$gate_image") \
    || blocked 'BLOCKED_IMAGE_ATTESTATION'
image_digest=${image_id#sha256:}
[ "$image_digest" != "$image_id" ] && [ "${#image_digest}" -eq 64 ] \
    || blocked 'BLOCKED_IMAGE_ATTESTATION'
case "$image_digest" in *[!0-9a-f]*) blocked 'BLOCKED_IMAGE_ATTESTATION' ;; esac

gate_parent=$(/usr/bin/mktemp -d "$TMPDIR/travel-map-image-gate.XXXXXX") \
    || blocked 'BLOCKED_PRIVATE_DIRECTORY'
/bin/chmod 0700 "$gate_parent" || blocked 'BLOCKED_PRIVATE_DIRECTORY'
gate_data=$gate_parent/data
(umask 077 && /bin/mkdir "$gate_data") || blocked 'BLOCKED_PRIVATE_DIRECTORY'
host_uid=$(/usr/bin/id -u)
host_gid=$(/usr/bin/id -g)
case "$host_uid:$host_gid" in
    *[!0-9:]*) blocked 'BLOCKED_INVALID_HOST_ID' ;;
esac
[ "$gate_data" = "$gate_parent/data" ] || blocked 'BLOCKED_UNSAFE_GATE_PATH'
case "$gate_data" in
    *,*) blocked 'BLOCKED_UNSAFE_GATE_PATH' ;;
esac

run_chown_helper 10001 10001 || blocked 'BLOCKED_PRIVATE_DIRECTORY'

run_isolated() {
    run_docker run --rm --user 10001:10001 --network none --read-only \
        --cap-drop ALL --security-opt no-new-privileges \
        --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m,mode=0700,uid=10001,gid=10001 \
        --mount "type=bind,src=$gate_data,dst=/data" \
        "$gate_image" "$@"
}

run_isolated /bin/sh -eu -c \
    'umask 077; python -m app.storage.migrations migrate --database /data/travel-map.sqlite3; exec python -m app.storage.migrations verify --database /data/travel-map.sqlite3' \
    || blocked 'BLOCKED_ENCRYPTED_STORAGE_MIGRATION'

storage_sentinel=$(/usr/bin/python3 -I -S \
    -c 'import secrets; print("travel-map-image-gate-" + secrets.token_urlsafe(24))') \
    || blocked 'BLOCKED_PRIVATE_SENTINEL'
storage_smoke_output=$(run_docker run --rm --user 10001:10001 --network none --read-only \
    --cap-drop ALL --security-opt no-new-privileges \
    --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m,mode=0700,uid=10001,gid=10001 \
    --mount "type=bind,src=$gate_data,dst=/data" \
    --env "STORAGE_SENTINEL=$storage_sentinel" \
    --entrypoint /bin/sh "$gate_image" -eu -c 'umask 077; exec python -' <<'PY'
import asyncio
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.storage.crypto import PayloadCipher
from app.storage.database import SqliteDatabase
from app.storage.history import HistoryRepository
from app.storage.models import DEFAULT_USER_SETTINGS, HistoryRecalculationDraft, HistorySummary
from app.storage.user_settings import UserSettingsRepository
from app.storage.users import UserSessionRepository
from app.trips.models import TripPattern


async def smoke() -> None:
    sentinel = os.environ["STORAGE_SENTINEL"]
    now = datetime.now(UTC)
    database = SqliteDatabase(Path("/data/travel-map.sqlite3"))
    database.verify_current_schema()
    cipher = PayloadCipher(keys={1: os.urandom(32)})
    users = UserSessionRepository(database)
    user = await users.upsert_user_and_insert_session(
        subject_hmac=os.urandom(32),
        token_hmac=os.urandom(32),
        csrf_hmac=os.urandom(32),
        now=now,
        expires_at=now + timedelta(hours=1),
    )
    settings = UserSettingsRepository(database, cipher)
    settings_value = replace(DEFAULT_USER_SETTINGS, default_origin_site_id=sentinel)
    await settings.replace(user_id=user.id, settings=settings_value)
    assert await settings.get(user_id=user.id) == settings_value
    history = HistoryRepository(database, cipher, clock=lambda: now)
    metadata = await history.create(
        user_id=user.id,
        draft=HistoryRecalculationDraft(
            origin_site_id="image-gate-origin",
            origin_name=sentinel,
            destination_name=sentinel,
            destination_address=sentinel,
            trip_pattern=TripPattern.ROUND_TRIP,
            starts_at=now,
            ends_at=now + timedelta(minutes=60),
        ),
        summary=HistorySummary(
            classification="IMAGE_GATE",
            allowance_status="KNOWN",
            allowance_krw=0,
            route_legs=(),
            rule_set_id="image-gate-rule",
            effective_from="2026-01-01",
        ),
    )
    detail = await history.get(user_id=user.id, history_id=metadata.id)
    assert detail is not None
    assert detail.draft.destination_address == sentinel
    await database.checkpoint_truncate()


asyncio.run(smoke())
print("ENCRYPTED_STORAGE_SMOKE_OK")
PY
) || blocked 'BLOCKED_ENCRYPTED_STORAGE_SMOKE'
[ "$storage_smoke_output" = 'ENCRYPTED_STORAGE_SMOKE_OK' ] \
    || blocked 'BLOCKED_ENCRYPTED_STORAGE_SMOKE'

set -- $(/usr/bin/python3 -I -S - <<'PY'
import secrets

for prefix in (
    "test-rest-",
    "test-transit-",
    "test-opinet-",
    "test-oidc-id-",
    "test-oidc-secret-",
    "",
    "",
    "",
):
    print(prefix + secrets.token_urlsafe(32))
PY
)
test_rest_key=$1
test_transit_key=$2
test_opinet_key=$3
test_oidc_id=$4
test_oidc_secret=$5
test_session_key=$6
test_subject_key=$7
test_data_key=$8

gate_container="travel-map-release-gate-$$"
run_docker run -d --name "$gate_container" --user 10001:10001 \
    --network none --read-only \
    --cap-drop ALL --security-opt no-new-privileges \
    --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m,mode=0700,uid=10001,gid=10001 \
    --mount "type=bind,src=$gate_data,dst=/data" \
    --env ENVIRONMENT=production \
    --env PUBLIC_BASE_URL=https://travel.h19h19.com \
    --env USER_DATABASE_PATH=/data/travel-map.sqlite3 \
    --env KAKAO_REST_API_KEY="$test_rest_key" \
    --env SEOUL_TRANSIT_SERVICE_KEY="$test_transit_key" \
    --env OPINET_CERT_KEY="$test_opinet_key" \
    --env KAKAO_OIDC_CLIENT_ID="$test_oidc_id" \
    --env KAKAO_OIDC_CLIENT_SECRET="$test_oidc_secret" \
    --env SESSION_HMAC_KEY="$test_session_key" \
    --env KAKAO_SUBJECT_HMAC_KEY="$test_subject_key" \
    --env DATA_ENCRYPTION_KEY_V1="$test_data_key" \
    --env TRUSTED_PROXY_CIDRS='["127.0.0.1/32"]' \
    --env ALLOWED_HOSTS='["travel.h19h19.com","127.0.0.1","localhost"]' \
    --env ALLOWED_ORIGINS='["https://travel.h19h19.com"]' \
    "$gate_image" >/dev/null || blocked 'BLOCKED_ENCRYPTED_STORAGE_RUNTIME'

run_docker exec -i "$gate_container" python - <<'PY' \
    || blocked 'BLOCKED_ENCRYPTED_STORAGE_RUNTIME'
from urllib.request import urlopen

with urlopen("http://127.0.0.1:8080/healthz", timeout=5) as response:
    assert response.status == 200
PY
run_docker exec -i "$gate_container" python - <<'PY' \
    || blocked 'BLOCKED_ENCRYPTED_STORAGE_MODE'
import os
import stat

for path in ("/data/travel-map.sqlite3", "/data/travel-map.sqlite3-wal", "/data/travel-map.sqlite3-shm"):
    if os.path.exists(path):
        details = os.stat(path)
        assert details.st_uid == 10001 and details.st_gid == 10001
        assert stat.S_IMODE(details.st_mode) == 0o600
PY

container_logs=$TRAVEL_MAP_RELEASE_PRIVATE_ROOT/container-logs.txt
(umask 077 && : > "$container_logs") \
    || blocked 'BLOCKED_ENCRYPTED_STORAGE_RUNTIME'
run_docker logs "$gate_container" > "$container_logs" 2>/dev/null \
    || blocked 'BLOCKED_ENCRYPTED_STORAGE_RUNTIME'

for value in "$storage_sentinel" "$test_rest_key" "$test_transit_key" \
    "$test_opinet_key" "$test_oidc_id" "$test_oidc_secret" "$test_session_key" \
    "$test_subject_key" "$test_data_key"; do
    set +e
    /usr/bin/grep -aF -q -- "$value" "$container_logs"
    log_scan_status=$?
    set -e
    case "$log_scan_status" in
        0) blocked 'BLOCKED_PLAINTEXT_IN_STORAGE' ;;
        1) ;;
        *) blocked 'BLOCKED_ENCRYPTED_STORAGE_RUNTIME' ;;
    esac
    set +e
    printf '%s' "$value" | run_docker exec -i "$gate_container" python -c '
import sys
from pathlib import Path

sentinel = sys.stdin.buffer.read()
if not sentinel:
    raise SystemExit(2)
for path in (
    Path("/data/travel-map.sqlite3"),
    Path("/data/travel-map.sqlite3-wal"),
    Path("/data/travel-map.sqlite3-shm"),
):
    try:
        payload = path.read_bytes()
    except FileNotFoundError:
        continue
    if sentinel in payload:
        raise SystemExit(3)
' >/dev/null 2>&1
    storage_scan_status=$?
    set -e
    case "$storage_scan_status" in
        0) ;;
        3)
            blocked 'BLOCKED_PLAINTEXT_IN_STORAGE'
            ;;
        *) blocked 'BLOCKED_ENCRYPTED_STORAGE_RUNTIME' ;;
    esac
done

run_docker rm -f "$gate_container" >/dev/null \
    || blocked 'BLOCKED_ENCRYPTED_STORAGE_RUNTIME'
gate_container=

# A record is emitted only after every check and private-directory cleanup has
# succeeded. The record owner deliberately retains the reviewed image tag.
cleanup_gate_data || blocked 'BLOCKED_GATE_CLEANUP_FAILED'
cd "$TRAVEL_MAP_RELEASE_PRIVATE_ROOT" \
    || blocked 'BLOCKED_GATE_CLEANUP_FAILED'
remove_private_directory "$pristine_parent" \
    || blocked 'BLOCKED_GATE_CLEANUP_FAILED'
pristine_parent=
remove_private_directory "$context_parent" || blocked 'BLOCKED_GATE_CLEANUP_FAILED'
context_parent=

verify_runtime_anchors || blocked 'BLOCKED_IMAGE_ATTESTATION'
final_inspected=$(run_docker image inspect \
    --format '{{.Id}} {{.Os}}/{{.Architecture}}' "$gate_image") \
    || blocked 'BLOCKED_IMAGE_ATTESTATION'
[ "$final_inspected" = "$image_id $NAS_PLATFORM" ] \
    || blocked 'BLOCKED_IMAGE_ATTESTATION'

if [ -n "$record_path" ]; then
    /usr/bin/python3 -I -S - \
        "$record_path" "$gate_image" "$image_id" "$NAS_PLATFORM" "$git_sha" <<'PY' \
        || blocked 'BLOCKED_IMAGE_ATTESTATION'
import os
import re
import sys
from pathlib import Path

record, image_tag, image_id, platform, git_sha = sys.argv[1:]
record_path = Path(record)
image_digest = image_id.removeprefix("sha256:")
if (
    record_path.name != "gated-image.record"
    or not record_path.is_absolute()
    or record_path.exists()
    or record_path.is_symlink()
    or not re.fullmatch(r"seoul-education-travel-map:release-gate-[0-9a-f]{40}", image_tag)
    or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id)
    or platform not in {"linux/amd64", "linux/arm64"}
    or not re.fullmatch(r"[0-9a-f]{40}", git_sha)
):
    raise SystemExit(2)
payload = (
    f"imageTag={image_tag}\n"
    f"imageId=sha256:{image_digest}\n"
    f"platform={platform}\n"
    f"gitSha={git_sha}\n"
).encode("ascii")
temporary = record_path.with_name(f".{record_path.name}.tmp-{os.getpid()}")
try:
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, record_path)
finally:
    try:
        temporary.unlink()
    except FileNotFoundError:
        pass
PY
    record_written=1
fi

printf '%s\n' 'ENCRYPTED_STORAGE_IMAGE_GATE_OK'
gate_completed=1
