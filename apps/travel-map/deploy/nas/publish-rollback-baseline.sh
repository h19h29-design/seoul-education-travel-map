#!/bin/sh
set -eu

umask 077

blocked() {
    printf '%s\n' "$1" >&2
    exit 2
}

stat_owner_mode() {
    stat -c '%u:%a' "$1" 2>/dev/null || stat -f '%u:%Lp' "$1" 2>/dev/null
}

remove_private_directory() {
    directory=$1
    [ -n "$directory" ] || return 0
    [ -d "$directory" ] && [ ! -L "$directory" ] || return 1
    find "$directory" -mindepth 1 -depth -delete >/dev/null 2>&1 \
        && rmdir "$directory"
}

[ "$#" -eq 0 ] || {
    printf '%s\n' 'usage: publish-rollback-baseline.sh' >&2
    exit 64
}

rollback_sha=469c13f5afbc13af3ed9e91eaf43c20825163c6e
rollback_platform=linux/amd64
registry=ghcr.io/h19h29-design/seoul-education-travel-map
legacy_tag=seoul-education-travel-map:0.1.0

canonical_home=$(/usr/bin/python3 -I -S - <<'PY'
import os
import pwd
import stat
from pathlib import Path

try:
    entry = pwd.getpwuid(os.getuid())
    configured = Path(entry.pw_dir)
    resolved = configured.resolve(strict=True)
    metadata = resolved.stat()
    if (
        not configured.is_absolute()
        or configured != resolved
        or not resolved.is_dir()
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise ValueError
except (KeyError, OSError, ValueError):
    raise SystemExit(2) from None
print(resolved)
PY
) || blocked 'BLOCKED_UNSAFE_RELEASE_ENVIRONMENT'
safe_path=$canonical_home/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
/usr/bin/python3 -I -S - "$safe_path" <<'PY' \
    || blocked 'BLOCKED_UNSAFE_RELEASE_ENVIRONMENT'
import os
import shutil
import stat
import sys
from pathlib import Path

try:
    for tool in ("uv", "pnpm", "docker", "node", "python3", "git"):
        configured = shutil.which(tool, path=sys.argv[1])
        if configured is None:
            raise ValueError
        resolved = Path(configured).resolve(strict=True)
        metadata = resolved.stat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or not os.access(configured, os.X_OK)
            or metadata.st_uid not in {0, os.getuid()}
            or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise ValueError
except (OSError, ValueError):
    raise SystemExit(2) from None
PY
PATH=$safe_path
export PATH

normalized_tmp_root=$(/usr/bin/python3 -I -S - "${TMPDIR:-/tmp}" <<'PY'
import os
import stat
import sys
from pathlib import Path

try:
    configured = Path(sys.argv[1])
    resolved = configured.resolve(strict=True)
    metadata = resolved.stat()
    unsafe_shared_mode = metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    if (
        not configured.is_absolute()
        or not resolved.is_dir()
        or metadata.st_uid not in {0, os.getuid()}
        or (unsafe_shared_mode and not metadata.st_mode & stat.S_ISVTX)
    ):
        raise ValueError
except (OSError, ValueError):
    raise SystemExit(2) from None
print(resolved)
PY
) || blocked 'BLOCKED_UNSAFE_RELEASE_ENVIRONMENT'

clean_environment_marker=${ROLLBACK_PUBLISHER_CLEAN_ENVIRONMENT:-}
case "$clean_environment_marker" in
    '')
        exec /usr/bin/env -i \
            PATH="$safe_path" \
            HOME="$canonical_home" \
            LANG=C.UTF-8 \
            LC_ALL=C.UTF-8 \
            CI=1 \
            TMPDIR="$normalized_tmp_root" \
            ROLLBACK_SOURCE_DIRECTORY="${ROLLBACK_SOURCE_DIRECTORY:-}" \
            DOCKER_CONFIG="${DOCKER_CONFIG:-}" \
            ROLLBACK_PUBLISHER_CLEAN_ENVIRONMENT=1 \
            /bin/sh "$0"
        blocked 'BLOCKED_UNSAFE_RELEASE_ENVIRONMENT'
        ;;
    1) ;;
    *) blocked 'BLOCKED_UNSAFE_RELEASE_ENVIRONMENT' ;;
esac

# Apple's /bin/sh adds these toolchain variables even under `env -i`. They are
# never publisher inputs and must not reach Git, Docker, or validation helpers.
unset CPATH LIBRARY_PATH MANPATH SDKROOT __CF_USER_TEXT_ENCODING
/usr/bin/python3 -I -S - \
    "$safe_path" "$canonical_home" "$normalized_tmp_root" <<'PY' \
    || blocked 'BLOCKED_UNSAFE_RELEASE_ENVIRONMENT'
import os
import sys

expected = {
    "PATH": sys.argv[1],
    "HOME": sys.argv[2],
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "CI": "1",
    "TMPDIR": sys.argv[3],
    "ROLLBACK_PUBLISHER_CLEAN_ENVIRONMENT": "1",
}
allowed_inputs = {"ROLLBACK_SOURCE_DIRECTORY", "DOCKER_CONFIG"}
shell_metadata = {"PWD", "SHLVL", "_"}
apple_python_metadata = {
    "CPATH",
    "LIBRARY_PATH",
    "MANPATH",
    "SDKROOT",
    "__CF_USER_TEXT_ENCODING",
}
if any(os.environ.get(name) != value for name, value in expected.items()):
    raise SystemExit(2)
if not allowed_inputs.issubset(os.environ):
    raise SystemExit(2)
if (
    set(os.environ)
    - set(expected)
    - allowed_inputs
    - shell_metadata
    - apple_python_metadata
):
    raise SystemExit(2)
PY
GIT_CONFIG_GLOBAL=/dev/null
GIT_CONFIG_NOSYSTEM=1
GIT_NO_REPLACE_OBJECTS=1
export GIT_CONFIG_GLOBAL GIT_CONFIG_NOSYSTEM GIT_NO_REPLACE_OBJECTS

rollback_source=${ROLLBACK_SOURCE_DIRECTORY:-}
case "$rollback_source" in
    /*) ;;
    *) blocked 'BLOCKED_INVALID_ROLLBACK_SOURCE' ;;
esac
[ -d "$rollback_source" ] && [ ! -L "$rollback_source" ] \
    || blocked 'BLOCKED_INVALID_ROLLBACK_SOURCE'
rollback_source_physical=$(CDPATH= cd -- "$rollback_source" && pwd -P) \
    || blocked 'BLOCKED_INVALID_ROLLBACK_SOURCE'
[ "$rollback_source" = "$rollback_source_physical" ] \
    || blocked 'BLOCKED_INVALID_ROLLBACK_SOURCE'

source_git() {
    git -C "$rollback_source" \
        -c core.fsmonitor=false \
        -c core.untrackedCache=false \
        "$@"
}

inside_worktree=$(source_git rev-parse --is-inside-work-tree 2>/dev/null) \
    || blocked 'BLOCKED_INVALID_ROLLBACK_SOURCE'
[ "$inside_worktree" = true ] || blocked 'BLOCKED_INVALID_ROLLBACK_SOURCE'
source_top=$(source_git rev-parse --show-toplevel 2>/dev/null) \
    || blocked 'BLOCKED_INVALID_ROLLBACK_SOURCE'
[ "$source_top" = "$rollback_source" ] || blocked 'BLOCKED_INVALID_ROLLBACK_SOURCE'
source_sha=$(source_git rev-parse HEAD 2>/dev/null) \
    || blocked 'BLOCKED_INVALID_ROLLBACK_SOURCE'
[ "$source_sha" = "$rollback_sha" ] || blocked 'BLOCKED_INVALID_ROLLBACK_SOURCE'
if source_git symbolic-ref --quiet HEAD >/dev/null 2>&1; then
    blocked 'BLOCKED_INVALID_ROLLBACK_SOURCE'
else
    symbolic_status=$?
    [ "$symbolic_status" -eq 1 ] || blocked 'BLOCKED_INVALID_ROLLBACK_SOURCE'
fi
source_status=$(source_git status --porcelain=v1 \
    --untracked-files=normal 2>/dev/null) \
    || blocked 'BLOCKED_INVALID_ROLLBACK_SOURCE'
[ -z "$source_status" ] || blocked 'BLOCKED_INVALID_ROLLBACK_SOURCE'
source_index_flags=$(source_git ls-files -v 2>/dev/null) \
    || blocked 'BLOCKED_INVALID_ROLLBACK_SOURCE'
if ! printf '%s\n' "$source_index_flags" | awk '
    BEGIN { count = 0 }
    substr($0, 1, 2) != "H " { exit 1 }
    { count += 1 }
    END { if (count == 0) exit 1 }
'; then
    blocked 'BLOCKED_INVALID_ROLLBACK_SOURCE'
fi
/usr/bin/python3 -I -S - "$rollback_source" "$rollback_sha" <<'PY' \
    || blocked 'BLOCKED_INVALID_ROLLBACK_SOURCE'
import subprocess
import sys
from pathlib import Path

excluded_parts = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "artifacts",
    "e2e",
    "node_modules",
    "playwright-report",
    "raw",
    "source",
    "test-results",
    "tests",
}
static_suffixes = {
    ".css",
    ".html",
    ".jpeg",
    ".jpg",
    ".js",
    ".png",
    ".svg",
    ".webp",
    ".woff2",
}

try:
    source = Path(sys.argv[1])
    app_root = source / "apps/travel-map/app"
    if app_root.is_symlink() or not app_root.is_dir():
        raise ValueError
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.untrackedCache=false",
            "ls-tree",
            "-r",
            "--name-only",
            "-z",
            sys.argv[2],
            "--",
            "apps/travel-map/app",
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if completed.returncode != 0:
        raise ValueError
    tracked = {
        entry.decode("utf-8", errors="strict")
        for entry in completed.stdout.split(b"\0")
        if entry
    }
    for candidate in app_root.rglob("*"):
        if candidate.is_symlink():
            raise ValueError
        if not candidate.is_file():
            continue
        relative = candidate.relative_to(app_root)
        if any(part in excluded_parts or part.startswith(".") for part in relative.parts):
            continue
        allowed = candidate.suffix == ".py" or (
            relative.parts[0] == "static" and candidate.suffix in static_suffixes
        )
        if allowed and f"apps/travel-map/app/{relative.as_posix()}" not in tracked:
            raise ValueError
except (OSError, UnicodeError, ValueError):
    raise SystemExit(2) from None
PY
source_release_gate=$rollback_source/apps/travel-map/scripts/release-gate.sh
[ -f "$source_release_gate" ] && [ ! -L "$source_release_gate" ] \
    && [ -x "$source_release_gate" ] \
    || blocked 'BLOCKED_INVALID_ROLLBACK_SOURCE'

docker_config=${DOCKER_CONFIG:-}
case "$docker_config" in
    /*) ;;
    *) blocked 'BLOCKED_INVALID_DOCKER_CONFIG' ;;
esac
[ -d "$docker_config" ] && [ ! -L "$docker_config" ] \
    || blocked 'BLOCKED_INVALID_DOCKER_CONFIG'
docker_config_physical=$(CDPATH= cd -- "$docker_config" && pwd -P) \
    || blocked 'BLOCKED_INVALID_DOCKER_CONFIG'
[ "$docker_config" = "$docker_config_physical" ] \
    || blocked 'BLOCKED_INVALID_DOCKER_CONFIG'
config_json=$docker_config/config.json
[ -f "$config_json" ] && [ ! -L "$config_json" ] \
    || blocked 'BLOCKED_INVALID_DOCKER_CONFIG'
current_uid=$(id -u) || blocked 'BLOCKED_INVALID_DOCKER_CONFIG'
[ "$(stat_owner_mode "$docker_config")" = "$current_uid:700" ] \
    || blocked 'BLOCKED_INVALID_DOCKER_CONFIG'
[ "$(stat_owner_mode "$config_json")" = "$current_uid:600" ] \
    || blocked 'BLOCKED_INVALID_DOCKER_CONFIG'

command -v docker >/dev/null 2>&1 || blocked 'BLOCKED_DOCKER_UNAVAILABLE'
docker version >/dev/null 2>&1 || blocked 'BLOCKED_DOCKER_UNAVAILABLE'

temporary=$(mktemp -d "${TMPDIR:-/tmp}/travel-map-rollback-publish.XXXXXX") \
    || blocked 'BLOCKED_PRIVATE_PUBLISH_DIRECTORY'
chmod 0700 "$temporary" || {
    remove_private_directory "$temporary" >/dev/null 2>&1 || :
    blocked 'BLOCKED_PRIVATE_PUBLISH_DIRECTORY'
}
test_environment=$temporary/test-runtime.env
build_iid_file=$temporary/rollback-build.iid
remote_manifest=$temporary/remote-manifest.json
remote_platform_manifest=$temporary/remote-platform-manifest.json
remote_image_config=$temporary/remote-image-config.json
remote_attestation_manifest=$temporary/remote-attestation-manifest.json
container_id_file=$temporary/runtime-container.cid
container=
publisher_lock=
owns_publisher_lock=0
image_id=
rollback_tag_created=0
interrupted=0

remove_owned_container() {
    owned_container=$1
    if docker rm -f "$owned_container" >/dev/null 2>&1; then
        return 0
    fi
    if docker container inspect "$owned_container" >/dev/null 2>&1; then
        return 1
    fi
    docker version >/dev/null 2>&1
}

capture_owned_container_from_cidfile() {
    [ -n "$container_id_file" ] || return 1
    recovered_container=$(/usr/bin/python3 -I -S - "$container_id_file" <<'PY'
import os
import re
import stat
import sys
from pathlib import Path

try:
    path = Path(sys.argv[1])
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_size <= 0
        or metadata.st_size > 80
    ):
        raise ValueError
    container_id = path.read_text(encoding="ascii").strip()
    if re.fullmatch(r"[0-9a-f]{64}", container_id) is None:
        raise ValueError
except (OSError, UnicodeError, ValueError):
    raise SystemExit(2) from None
print(container_id)
PY
    ) || return 1
    container=$recovered_container
}

cleanup_resources() {
    cleanup_failed=0

    if [ -z "$container" ] && [ -n "$container_id_file" ] \
        && { [ -e "$container_id_file" ] || [ -L "$container_id_file" ]; }; then
        capture_owned_container_from_cidfile || cleanup_failed=1
    fi
    if [ -n "$container" ]; then
        remove_owned_container "$container" || cleanup_failed=1
        container=
    fi
    # Docker has no compare-and-delete operation for a mutable local tag. The
    # local legacy and content-addressed rollback tags are retained so this trap
    # can never untag a competing image after a TOCTOU race. Successful output
    # requires their final immutable-ID checks. Operators may remove retained
    # tags manually after confirming those IDs.
    if [ -n "$temporary" ]; then
        remove_private_directory "$temporary" || cleanup_failed=1
        temporary=
    fi
    if [ "$owns_publisher_lock" -eq 1 ] && [ -n "$publisher_lock" ]; then
        rmdir "$publisher_lock" >/dev/null 2>&1 || cleanup_failed=1
        owns_publisher_lock=0
        publisher_lock=
    fi
    [ "$cleanup_failed" -eq 0 ]
}

cleanup_on_exit() {
    status=$?
    trap - EXIT HUP INT TERM
    cleanup_resources || {
        printf '%s\n' 'BLOCKED_ROLLBACK_PUBLISH_CLEANUP_FAILED' >&2
        status=2
    }
    [ "$interrupted" -eq 0 ] || status=2
    exit "$status"
}

interrupted_cleanup() {
    interrupted=1
    trap - HUP INT TERM
    exit 2
}

trap cleanup_on_exit EXIT
trap interrupted_cleanup HUP INT TERM

lock_root=$(/usr/bin/python3 -I -S - /tmp <<'PY'
import os
import stat
import sys
from pathlib import Path

try:
    resolved = Path(sys.argv[1]).resolve(strict=True)
    metadata = resolved.stat()
    if (
        not resolved.is_dir()
        or metadata.st_uid != 0
        or not metadata.st_mode & stat.S_ISVTX
        or not metadata.st_mode & stat.S_IWUSR
        or not metadata.st_mode & stat.S_IWOTH
    ):
        raise ValueError
except (OSError, ValueError):
    raise SystemExit(2) from None
print(resolved)
PY
) || blocked 'BLOCKED_UNSAFE_RELEASE_ENVIRONMENT'
publisher_lock=$lock_root/travel-map-rollback-baseline-$current_uid.lock
mkdir "$publisher_lock" 2>/dev/null \
    || blocked 'BLOCKED_ROLLBACK_PUBLISH_LOCKED'
owns_publisher_lock=1
chmod 0700 "$publisher_lock" \
    || blocked 'BLOCKED_ROLLBACK_PUBLISH_LOCKED'
[ "$(stat_owner_mode "$publisher_lock")" = "$current_uid:700" ] \
    || blocked 'BLOCKED_ROLLBACK_PUBLISH_LOCKED'

source_archive=$temporary/pinned-source.tar
source_tree=$temporary/pinned-source.tree
pinned_source=$temporary/pinned-source
source_git archive --format=tar --output="$source_archive" "$rollback_sha" \
    || blocked 'BLOCKED_INVALID_ROLLBACK_SOURCE'
source_git ls-tree -r -z "$rollback_sha" > "$source_tree" \
    || blocked 'BLOCKED_INVALID_ROLLBACK_SOURCE'
/usr/bin/python3 -I -S - "$source_archive" "$source_tree" "$pinned_source" <<'PY' \
    || blocked 'BLOCKED_INVALID_ROLLBACK_SOURCE'
import hashlib
import os
import re
import stat
import sys
import tarfile
from pathlib import Path, PurePosixPath

try:
    archive = Path(sys.argv[1])
    tree_records = Path(sys.argv[2]).read_bytes().split(b"\0")
    destination = Path(sys.argv[3])
    expected: dict[str, tuple[str, str]] = {}
    for record in tree_records:
        if not record:
            continue
        header, raw_name = record.split(b"\t", 1)
        mode, kind, object_id = header.decode("ascii").split(" ")
        name = raw_name.decode("utf-8", errors="strict")
        path = PurePosixPath(name)
        if (
            mode not in {"100644", "100755"}
            or kind != "blob"
            or re.fullmatch(r"[0-9a-f]{40}", object_id) is None
            or path.is_absolute()
            or ".." in path.parts
            or not path.parts
            or path.parts[0] == ".git"
            or name in expected
        ):
            raise ValueError
        expected[name] = (mode, object_id)
    if not expected:
        raise ValueError

    destination.mkdir(mode=0o700)
    with tarfile.open(archive, mode="r:") as payload:
        members = payload.getmembers()
        for member in members:
            path = PurePosixPath(member.name)
            if (
                path.is_absolute()
                or ".." in path.parts
                or not path.parts
                or path.parts[0] == ".git"
                or not (member.isfile() or member.isdir())
            ):
                raise ValueError
        payload.extractall(destination)

    actual: dict[str, tuple[str, str]] = {}
    for candidate in destination.rglob("*"):
        metadata = candidate.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError
        if candidate.is_dir():
            continue
        if not candidate.is_file():
            raise ValueError
        if metadata.st_uid != os.getuid():
            raise ValueError
        relative = candidate.relative_to(destination).as_posix()
        data = candidate.read_bytes()
        object_id = hashlib.sha1(
            f"blob {len(data)}\0".encode("ascii") + data,
            usedforsecurity=False,
        ).hexdigest()
        mode = "100755" if metadata.st_mode & 0o111 else "100644"
        actual[relative] = (mode, object_id)
    if actual != expected:
        raise ValueError

    expected_directories: set[str] = set()
    for relative in expected:
        parent = PurePosixPath(relative).parent
        while parent != PurePosixPath("."):
            expected_directories.add(parent.as_posix())
            parent = parent.parent
    actual_directories = {
        candidate.relative_to(destination).as_posix()
        for candidate in destination.rglob("*")
        if candidate.is_dir()
    }
    if actual_directories != expected_directories:
        raise ValueError

    # Git archive preserves Git's executable bit but applies tar.umask to the
    # permission bits (commonly yielding 0775/0664). Only after proving every
    # blob and execute bit above, restore the exact private working-tree modes.
    for relative, (mode, _) in expected.items():
        candidate = destination.joinpath(*PurePosixPath(relative).parts)
        candidate.chmod(0o755 if mode == "100755" else 0o644)
    directories = [destination, *(destination / item for item in expected_directories)]
    for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        directory.chmod(0o700)

    for relative, (mode, object_id) in expected.items():
        candidate = destination.joinpath(*PurePosixPath(relative).parts)
        metadata = candidate.lstat()
        data = candidate.read_bytes()
        normalized_object_id = hashlib.sha1(
            f"blob {len(data)}\0".encode("ascii") + data,
            usedforsecurity=False,
        ).hexdigest()
        normalized_mode = 0o755 if mode == "100755" else 0o644
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != normalized_mode
            or normalized_object_id != object_id
        ):
            raise ValueError
    for directory in directories:
        metadata = directory.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise ValueError
except (OSError, UnicodeError, ValueError, tarfile.TarError):
    raise SystemExit(2) from None
PY
release_gate=$pinned_source/apps/travel-map/scripts/release-gate.sh
[ -f "$release_gate" ] && [ ! -L "$release_gate" ] && [ -x "$release_gate" ] \
    || blocked 'BLOCKED_INVALID_ROLLBACK_SOURCE'
/usr/bin/python3 -I -S - "$release_gate" <<'PY' \
    || blocked 'BLOCKED_INVALID_ROLLBACK_SOURCE'
import os
import stat
import sys
from pathlib import Path

original = (
    'docker build --build-arg SNAPSHOT_ID="$snapshot_id" '
    '-t seoul-education-travel-map:0.1.0 "$context_root"'
)
instrumented = (
    'docker build --iidfile "$ROLLBACK_BUILD_IID_FILE" '
    '--build-arg SNAPSHOT_ID="$snapshot_id" '
    '-t seoul-education-travel-map:0.1.0 "$context_root"'
)
try:
    path = Path(sys.argv[1])
    metadata = path.lstat()
    payload = path.read_text(encoding="utf-8")
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o755
        or payload.count(original) != 1
        or instrumented in payload
    ):
        raise ValueError
    payload = payload.replace(original, instrumented)
    flags = os.O_WRONLY | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())
    if path.read_text(encoding="utf-8").count(instrumented) != 1:
        raise ValueError
except (OSError, UnicodeError, ValueError):
    raise SystemExit(2) from None
PY

local_legacy=$(docker image ls --quiet --no-trunc \
    --filter "reference=$legacy_tag" 2>/dev/null) \
    || blocked 'BLOCKED_DOCKER_UNAVAILABLE'
[ -z "$local_legacy" ] || blocked 'BLOCKED_LEGACY_TAG_EXISTS'
gate_temporary=$temporary/release-gate-tmp
gate_home=$temporary/release-gate-home
gate_xdg_config=$gate_home/xdg-config
gate_xdg_cache=$gate_home/xdg-cache
gate_xdg_data=$gate_home/xdg-data
gate_npm_cache=$gate_home/npm-cache
gate_pnpm_store=$gate_home/pnpm-store
uv_cache=$canonical_home/.cache/uv
playwright_browsers=$canonical_home/Library/Caches/ms-playwright
/usr/bin/python3 -I -S - "$uv_cache" "$playwright_browsers" <<'PY' \
    || blocked 'BLOCKED_UNSAFE_RELEASE_ENVIRONMENT'
import os
import stat
import sys
from pathlib import Path

try:
    for value in sys.argv[1:]:
        configured = Path(value)
        resolved = configured.resolve(strict=True)
        metadata = resolved.stat()
        if (
            not configured.is_absolute()
            or configured != resolved
            or not resolved.is_dir()
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise ValueError
except (OSError, ValueError):
    raise SystemExit(2) from None
PY
mkdir "$gate_temporary" "$gate_home" "$gate_xdg_config" \
    "$gate_xdg_cache" "$gate_xdg_data" "$gate_npm_cache" \
    "$gate_pnpm_store" || blocked 'BLOCKED_PRIVATE_PUBLISH_DIRECTORY'
for private_directory in \
    "$gate_temporary" "$gate_home" "$gate_xdg_config" \
    "$gate_xdg_cache" "$gate_xdg_data" "$gate_npm_cache" \
    "$gate_pnpm_store"
do
    [ "$(stat_owner_mode "$private_directory")" = "$current_uid:700" ] \
        || blocked 'BLOCKED_PRIVATE_PUBLISH_DIRECTORY'
done

(CDPATH= cd -- "$pinned_source" && \
    env -i \
        PATH="$safe_path" \
        HOME="$gate_home" \
        XDG_CONFIG_HOME="$gate_xdg_config" \
        XDG_CACHE_HOME="$gate_xdg_cache" \
        XDG_DATA_HOME="$gate_xdg_data" \
        LANG=C.UTF-8 \
        LC_ALL=C.UTF-8 \
        TMPDIR="$gate_temporary" \
        DOCKER_CONFIG="$docker_config" \
        ROLLBACK_BUILD_IID_FILE="$build_iid_file" \
        CI=1 \
        PYTHONWARNINGS=error \
        PYTHONNOUSERSITE=1 \
        PYTHONSAFEPATH=1 \
        UV_NO_CONFIG=1 \
        UV_CACHE_DIR="$uv_cache" \
        NPM_CONFIG_USERCONFIG=/dev/null \
        NPM_CONFIG_GLOBALCONFIG=/dev/null \
        NPM_CONFIG_CACHE="$gate_npm_cache" \
        NPM_CONFIG_STORE_DIR="$gate_pnpm_store" \
        PLAYWRIGHT_BROWSERS_PATH="$playwright_browsers" \
        DOCKER_DEFAULT_PLATFORM="$rollback_platform" \
        NAS_PLATFORM="$rollback_platform" \
        "$release_gate") >&2 \
    || blocked 'BLOCKED_ROLLBACK_RELEASE_GATE'

image_id=$(/usr/bin/python3 -I -S - "$build_iid_file" <<'PY'
import os
import re
import stat
import sys
from pathlib import Path

try:
    path = Path(sys.argv[1])
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_size <= 0
        or metadata.st_size > 80
    ):
        raise ValueError
    image_id = path.read_text(encoding="ascii").strip()
    if re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None:
        raise ValueError
except (OSError, UnicodeError, ValueError):
    raise SystemExit(2) from None
print(image_id)
PY
) || blocked 'BLOCKED_INVALID_GATE_ATTESTATION'
image_hex=${image_id#sha256:}
[ "$image_id" = "sha256:$image_hex" ] && [ "${#image_hex}" -eq 64 ] \
    || blocked 'BLOCKED_INVALID_GATE_ATTESTATION'
case "$image_hex" in
    *[!0-9a-f]*) blocked 'BLOCKED_INVALID_GATE_ATTESTATION' ;;
esac
inspected_id=$(docker image inspect --format '{{.Id}} {{.Os}}/{{.Architecture}}' \
    "$image_id" 2>/dev/null) || blocked 'BLOCKED_INVALID_GATE_ATTESTATION'
[ "$inspected_id" = "$image_id $rollback_platform" ] \
    || blocked 'BLOCKED_INVALID_GATE_ATTESTATION'
legacy_attestation=$(docker image inspect \
    --format '{{.Id}} {{.Os}}/{{.Architecture}}' \
    "$legacy_tag" 2>/dev/null) || blocked 'BLOCKED_INVALID_GATE_ATTESTATION'
[ "$legacy_attestation" = "$image_id $rollback_platform" ] \
    || blocked 'BLOCKED_INVALID_GATE_ATTESTATION'
rollback_tag=$registry:rollback-baseline-$rollback_sha-$image_hex
local_rollback=$(docker image ls --quiet --no-trunc \
    --filter "reference=$rollback_tag" 2>/dev/null) \
    || blocked 'BLOCKED_DOCKER_UNAVAILABLE'
[ -z "$local_rollback" ] || blocked 'BLOCKED_ROLLBACK_TAG_EXISTS'

/usr/bin/python3 -I -S - "$test_environment" <<'PY' \
    || blocked 'BLOCKED_PRIVATE_TEST_ENVIRONMENT'
import os
import secrets
import sys
from pathlib import Path

path = Path(sys.argv[1])
values = {
    "ENVIRONMENT": "production",
    "PUBLIC_BASE_URL": "https://travel.h19h19.com",
    "USER_DATABASE_PATH": "/data/travel-map.sqlite3",
    "KAKAO_REST_API_KEY": "test-rest-" + secrets.token_urlsafe(32),
    "SEOUL_TRANSIT_SERVICE_KEY": "test-transit-" + secrets.token_urlsafe(32),
    "OPINET_CERT_KEY": "test-opinet-" + secrets.token_urlsafe(32),
    "KAKAO_OIDC_CLIENT_ID": "test-oidc-id-" + secrets.token_urlsafe(32),
    "KAKAO_OIDC_CLIENT_SECRET": "test-oidc-secret-" + secrets.token_urlsafe(32),
    "SESSION_HMAC_KEY": secrets.token_urlsafe(32),
    "KAKAO_SUBJECT_HMAC_KEY": secrets.token_urlsafe(32),
    "DATA_ENCRYPTION_KEY_V1": secrets.token_urlsafe(32),
    "TRUSTED_PROXY_CIDRS": '["127.0.0.1/32"]',
    "ALLOWED_HOSTS": '["travel.h19h19.com","127.0.0.1","localhost"]',
    "ALLOWED_ORIGINS": '["https://travel.h19h19.com"]',
}
payload = "".join(f"{key}={value}\n" for key, value in values.items()).encode()
descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(descriptor, "wb") as output:
    output.write(payload)
    output.flush()
    os.fsync(output.fileno())
PY
[ "$(stat_owner_mode "$test_environment")" = "$current_uid:600" ] \
    || blocked 'BLOCKED_PRIVATE_TEST_ENVIRONMENT'

temporary_suffix=${temporary##*.}
case "$temporary_suffix" in
    ''|*[!A-Za-z0-9]*) blocked 'BLOCKED_PRIVATE_PUBLISH_DIRECTORY' ;;
esac
container_name=travel-map-rollback-baseline-$temporary_suffix-$$
container_run_status=0
docker run -d --name "$container_name" --cidfile "$container_id_file" \
    --user 10001:10001 --network none \
    --read-only --cap-drop ALL --security-opt no-new-privileges \
    --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m,mode=0700,uid=10001,gid=10001 \
    --tmpfs /data:rw,noexec,nosuid,nodev,size=32m,mode=0700,uid=10001,gid=10001 \
    --env-file "$test_environment" "$image_id" >&2 \
    || container_run_status=$?
if [ -f "$container_id_file" ] && [ ! -L "$container_id_file" ]; then
    capture_owned_container_from_cidfile \
        || blocked 'BLOCKED_ROLLBACK_RUNTIME_SMOKE'
fi
[ "$container_run_status" -eq 0 ] \
    || blocked 'BLOCKED_ROLLBACK_RUNTIME_SMOKE'
[ -n "$container" ] || blocked 'BLOCKED_ROLLBACK_RUNTIME_SMOKE'
docker exec -i "$container" python - >/dev/null <<'PY' \
    || blocked 'BLOCKED_ROLLBACK_RUNTIME_SMOKE'
import time
from urllib.error import URLError
from urllib.request import urlopen

deadline = time.monotonic() + 30
while True:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        break
    try:
        with urlopen(
            "http://127.0.0.1:8080/healthz", timeout=min(1, remaining)
        ) as response:
            if response.status == 200:
                raise SystemExit(0)
    except (OSError, URLError):
        pass
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        break
    time.sleep(min(0.25, remaining))
raise SystemExit(1)
PY
docker rm -f "$container" >&2 || blocked 'BLOCKED_ROLLBACK_RUNTIME_SMOKE'
container=
container_id_file=

remote_descriptor=$temporary/remote-descriptor.json
remote_lookup_error=$temporary/remote-lookup.err
remote_final_descriptor=$temporary/remote-final-descriptor.json

lookup_remote_tag() {
    : > "$remote_descriptor"
    : > "$remote_lookup_error"
    if docker buildx imagetools inspect --format '{{json .Manifest}}' \
        "$rollback_tag" > "$remote_descriptor" 2> "$remote_lookup_error"; then
        remote_lookup_state=existing
        return 0
    fi
    /usr/bin/python3 -I -S - "$remote_descriptor" "$remote_lookup_error" \
        "$rollback_tag" <<'PY' \
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

verify_remote_tag() {
    repo_descriptor_plan=$(/usr/bin/python3 -I -S - \
        "$remote_descriptor" "$registry" <<'PY'
import json
import re
import sys
from pathlib import Path

try:
    descriptor = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    if not isinstance(descriptor, dict):
        raise ValueError
    digest = descriptor.get("digest")
    media_type = descriptor.get("mediaType")
    size = descriptor.get("size")
    allowed_media_types = {
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.oci.image.index.v1+json",
    }
    if (
        not isinstance(digest, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
        or media_type not in allowed_media_types
        or type(size) is not int
        or size <= 0
    ):
        raise ValueError
except (OSError, ValueError, json.JSONDecodeError):
    raise SystemExit(2) from None
print(f"{sys.argv[2]}@{digest}", media_type, size)
PY
) || blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'
    set -- $repo_descriptor_plan
    [ "$#" -eq 3 ] || blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'
    repo_digest=$1
    remote_media_type=$2
    remote_size=$3

    docker buildx imagetools inspect --raw "$repo_digest" \
        > "$remote_manifest" 2>/dev/null \
        || blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'
    remote_plan=$(/usr/bin/python3 -I -S - \
        "$remote_manifest" "$repo_digest" "$registry" \
        "$image_id" "$remote_media_type" <<'PY'
import json
import re
import sys
from pathlib import Path

try:
    manifest = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    repo_digest, registry, image_id, media_type = sys.argv[2:]
    digest_pattern = re.compile(r"sha256:[0-9a-f]{64}")
    manifest_media_types = {
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
    }
    config_media_types = {
        "application/vnd.docker.distribution.manifest.v2+json": (
            "application/vnd.docker.container.image.v1+json"
        ),
        "application/vnd.oci.image.manifest.v1+json": (
            "application/vnd.oci.image.config.v1+json"
        ),
    }
    index_media_types = {
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.index.v1+json",
    }
    if (
        not isinstance(manifest, dict)
        or manifest.get("schemaVersion") != 2
        or manifest.get("mediaType") != media_type
    ):
        raise ValueError
    config = manifest.get("config")
    config_digest = config.get("digest") if isinstance(config, dict) else None
    config_size = config.get("size") if isinstance(config, dict) else None
    descriptors = manifest.get("manifests")
    if isinstance(config_digest, str) and descriptors is None:
        if (
            media_type not in manifest_media_types
            or digest_pattern.fullmatch(config_digest) is None
            or config_digest != image_id
            or config.get("mediaType") != config_media_types[media_type]
            or type(config_size) is not int
            or config_size <= 0
        ):
            raise ValueError
        print("single", repo_digest)
    elif isinstance(descriptors, list) and "config" not in manifest:
        if (
            media_type not in index_media_types
            or repo_digest != f"{registry}@{image_id}"
            or not descriptors
        ):
            raise ValueError
        runnable = []
        attestations = []
        descriptor_digests = set()
        for descriptor in descriptors:
            if not isinstance(descriptor, dict):
                raise ValueError
            descriptor_digest = descriptor.get("digest")
            descriptor_size = descriptor.get("size")
            platform = descriptor.get("platform")
            if (
                not isinstance(descriptor_digest, str)
                or digest_pattern.fullmatch(descriptor_digest) is None
                or descriptor_digest in descriptor_digests
                or descriptor.get("mediaType") not in manifest_media_types
                or type(descriptor_size) is not int
                or descriptor_size <= 0
                or not isinstance(platform, dict)
            ):
                raise ValueError
            descriptor_digests.add(descriptor_digest)
            if platform == {"os": "unknown", "architecture": "unknown"}:
                if descriptor.get("mediaType") != (
                    "application/vnd.oci.image.manifest.v1+json"
                ):
                    raise ValueError
                attestations.append(descriptor)
            else:
                runnable.append(descriptor)
        if len(runnable) != 1:
            raise ValueError
        runnable_platform = runnable[0]["platform"]
        if runnable_platform != {"os": "linux", "architecture": "amd64"}:
            raise ValueError
        platform_digest = runnable[0]["digest"]
        for attestation in attestations:
            annotations = attestation.get("annotations")
            if (
                not isinstance(annotations, dict)
                or annotations.get("vnd.docker.reference.digest")
                != platform_digest
                or annotations.get("vnd.docker.reference.type")
                != "attestation-manifest"
            ):
                raise ValueError
        print(
            "index",
            f"{registry}@{platform_digest}",
            runnable[0]["mediaType"],
            *(f"{registry}@{item['digest']}" for item in attestations),
        )
    else:
        raise ValueError
except (OSError, ValueError, json.JSONDecodeError):
    raise SystemExit(2) from None
PY
) || blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'
    set -- $remote_plan
    [ "$#" -ge 2 ] || blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'
    remote_kind=$1
    remote_platform_reference=$2
    shift 2
    case "$remote_kind" in
        single)
            [ "$#" -eq 0 ] || blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'
            ;;
        index)
            [ "$#" -ge 1 ] || blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'
            remote_platform_media_type=$1
            shift
            docker buildx imagetools inspect --raw "$remote_platform_reference" \
                > "$remote_platform_manifest" 2>/dev/null \
                || blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'
            /usr/bin/python3 -I -S - "$remote_platform_manifest" \
                "$remote_platform_media_type" <<'PY' \
                || blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'
import json
import re
import sys
from pathlib import Path

try:
    manifest = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    if (
        not isinstance(manifest, dict)
        or manifest.get("schemaVersion") != 2
        or manifest.get("mediaType") != sys.argv[2]
    ):
        raise ValueError
    config = manifest.get("config")
    config_digest = config.get("digest") if isinstance(config, dict) else None
    config_size = config.get("size") if isinstance(config, dict) else None
    expected_config_media_types = {
        "application/vnd.docker.distribution.manifest.v2+json": (
            "application/vnd.docker.container.image.v1+json"
        ),
        "application/vnd.oci.image.manifest.v1+json": (
            "application/vnd.oci.image.config.v1+json"
        ),
    }
    if (
        not isinstance(config_digest, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", config_digest) is None
        or config.get("mediaType") != expected_config_media_types.get(sys.argv[2])
        or type(config_size) is not int
        or config_size <= 0
        or "manifests" in manifest
    ):
        raise ValueError
except (OSError, ValueError, json.JSONDecodeError):
    raise SystemExit(2) from None
PY
            for remote_attestation_reference in "$@"; do
                docker buildx imagetools inspect --raw \
                    "$remote_attestation_reference" \
                    > "$remote_attestation_manifest" 2>/dev/null \
                    || blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'
                /usr/bin/python3 -I -S - "$remote_attestation_manifest" <<'PY' \
                    || blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'
import json
import re
import sys
from pathlib import Path

try:
    manifest = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    digest_pattern = re.compile(r"sha256:[0-9a-f]{64}")
    if (
        not isinstance(manifest, dict)
        or manifest.get("schemaVersion") != 2
        or manifest.get("mediaType")
        != "application/vnd.oci.image.manifest.v1+json"
    ):
        raise ValueError
    config = manifest.get("config")
    if (
        not isinstance(config, dict)
        or config.get("mediaType")
        != "application/vnd.oci.image.config.v1+json"
        or not isinstance(config.get("digest"), str)
        or digest_pattern.fullmatch(config["digest"]) is None
        or type(config.get("size")) is not int
        or config["size"] <= 0
    ):
        raise ValueError
    layers = manifest.get("layers")
    if not isinstance(layers, list) or not layers:
        raise ValueError
    for layer in layers:
        if not isinstance(layer, dict):
            raise ValueError
        annotations = layer.get("annotations")
        predicate_type = (
            annotations.get("in-toto.io/predicate-type")
            if isinstance(annotations, dict)
            else None
        )
        if (
            layer.get("mediaType") != "application/vnd.in-toto+json"
            or not isinstance(layer.get("digest"), str)
            or digest_pattern.fullmatch(layer["digest"]) is None
            or type(layer.get("size")) is not int
            or layer["size"] <= 0
            or not isinstance(predicate_type, str)
            or not predicate_type.startswith("https://")
        ):
            raise ValueError
except (OSError, ValueError, json.JSONDecodeError):
    raise SystemExit(2) from None
PY
            done
            ;;
        *) blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH' ;;
    esac

    docker buildx imagetools inspect --format '{{json .Image}}' \
        "$remote_platform_reference" \
        > "$remote_image_config" 2>/dev/null \
        || blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'
    /usr/bin/python3 -I -S - "$remote_image_config" <<'PY' \
        || blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'
import json
import sys
from pathlib import Path

try:
    image = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    if (
        not isinstance(image, dict)
        or image.get("os") != "linux"
        or image.get("architecture") != "amd64"
    ):
        raise ValueError
except (OSError, ValueError, json.JSONDecodeError):
    raise SystemExit(2) from None
PY

    docker buildx imagetools inspect --format '{{json .Manifest}}' \
        "$rollback_tag" > "$remote_final_descriptor" 2>/dev/null \
        || blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'
    /usr/bin/python3 -I -S - "$remote_final_descriptor" \
        "$repo_digest" "$registry" \
        "$remote_media_type" "$remote_size" <<'PY' \
        || blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'
import json
import sys
from pathlib import Path

try:
    descriptor = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    if not isinstance(descriptor, dict):
        raise ValueError
    size = descriptor.get("size")
    if (
        f"{sys.argv[3]}@{descriptor.get('digest')}" != sys.argv[2]
        or descriptor.get("mediaType") != sys.argv[4]
        or type(size) is not int
        or size != int(sys.argv[5])
    ):
        raise ValueError
except (OSError, ValueError, json.JSONDecodeError):
    raise SystemExit(2) from None
PY
}

lookup_remote_tag
if [ "$remote_lookup_state" = missing ]; then
    # Docker has no no-clobber tag primitive. The canonical publisher lock is
    # the cooperative mutation boundary; this is the final absence check under
    # that lock immediately before creating the content-addressed local tag.
    local_rollback_before_tag=$(docker image ls --quiet --no-trunc \
        --filter "reference=$rollback_tag" 2>/dev/null) \
        || blocked 'BLOCKED_DOCKER_UNAVAILABLE'
    [ -z "$local_rollback_before_tag" ] \
        || blocked 'BLOCKED_ROLLBACK_TAG_EXISTS'
    docker tag "$image_id" "$rollback_tag" >&2 \
        || blocked 'BLOCKED_ROLLBACK_IMAGE_TAGGING'
    tagged_attestation=$(docker image inspect \
        --format '{{.Id}} {{.Os}}/{{.Architecture}}' \
        "$rollback_tag" 2>/dev/null) \
        || blocked 'BLOCKED_ROLLBACK_IMAGE_TAGGING'
    [ "$tagged_attestation" = "$image_id $rollback_platform" ] \
        || blocked 'BLOCKED_ROLLBACK_IMAGE_TAGGING'
    rollback_tag_created=1
    lookup_remote_tag
    if [ "$remote_lookup_state" = missing ]; then
        docker push "$rollback_tag" >&2 || blocked 'BLOCKED_ROLLBACK_IMAGE_PUSH'
        lookup_remote_tag
        [ "$remote_lookup_state" = existing ] \
            || blocked 'BLOCKED_REMOTE_IMAGE_MISMATCH'
    fi
fi
verify_remote_tag

retained_legacy=$(docker image inspect \
    --format '{{.Id}} {{.Os}}/{{.Architecture}}' \
    "$legacy_tag" 2>/dev/null) \
    || blocked 'BLOCKED_RETAINED_LOCAL_TAG_MISMATCH'
[ "$retained_legacy" = "$image_id $rollback_platform" ] \
    || blocked 'BLOCKED_RETAINED_LOCAL_TAG_MISMATCH'
if [ "$rollback_tag_created" -eq 1 ]; then
    retained_rollback=$(docker image inspect \
        --format '{{.Id}} {{.Os}}/{{.Architecture}}' \
        "$rollback_tag" 2>/dev/null) \
        || blocked 'BLOCKED_RETAINED_LOCAL_TAG_MISMATCH'
    [ "$retained_rollback" = "$image_id $rollback_platform" ] \
        || blocked 'BLOCKED_RETAINED_LOCAL_TAG_MISMATCH'
else
    unexpected_rollback_tag=$(docker image ls --quiet --no-trunc \
        --filter "reference=$rollback_tag" 2>/dev/null) \
        || blocked 'BLOCKED_DOCKER_UNAVAILABLE'
    [ -z "$unexpected_rollback_tag" ] \
        || blocked 'BLOCKED_RETAINED_LOCAL_TAG_MISMATCH'
fi

if ! cleanup_resources; then
    trap - EXIT HUP INT TERM
    printf '%s\n' 'BLOCKED_ROLLBACK_PUBLISH_CLEANUP_FAILED' >&2
    exit 2
fi
trap - EXIT HUP INT TERM
printf '%s\n' "$repo_digest"
