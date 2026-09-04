#!/bin/sh
set -eu

umask 077

blocked() {
    printf '%s\n' "$1" >&2
    exit 2
}

remove_private_directory() {
    directory=$1
    expected_identity=$2
    expected_parent=$3
    expected_prefix=$4
    [ -n "$directory" ] && [ -n "$expected_identity" ] \
        && [ -n "$expected_parent" ] && [ -n "$expected_prefix" ] \
        || return 1
    /usr/bin/python3 -I -S - \
        "$directory" "$expected_identity" "$expected_parent" "$expected_prefix" <<'PY'
import ctypes
import errno
import hashlib
import os
import stat
import sys
from pathlib import Path


def identity(details):
    return details.st_dev, details.st_ino


def rename_no_replace(source_fd, destination_fd, name):
    libc = ctypes.CDLL(None, use_errno=True)
    encoded = os.fsencode(name)
    if sys.platform == "darwin":
        operation = getattr(libc, "renameatx_np", None)
        flags = 0x00000004
    elif sys.platform.startswith("linux"):
        operation = getattr(libc, "renameat2", None)
        flags = 0x00000001
    else:
        operation = None
        flags = 0
    if operation is None:
        raise OSError
    operation.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    operation.restype = ctypes.c_int
    if operation(source_fd, encoded, destination_fd, encoded, flags) != 0:
        error = ctypes.get_errno()
        if error in {errno.EEXIST, errno.ENOTEMPTY}:
            raise OSError from None
        raise OSError(error, os.strerror(error))


def matching_entries(directory_descriptor, expected):
    matches = []
    for candidate in os.listdir(directory_descriptor):
        try:
            details = os.stat(
                candidate,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            continue
        if identity(details) == expected:
            matches.append(candidate)
    return matches


def remove_non_directory(directory_descriptor, name, expected):
    quarantine_name = None
    for _ in range(16):
        candidate = ".travel-map-publish-cleanup-" + os.urandom(16).hex()
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
    quarantine_identity = identity(os.fstat(quarantine_descriptor))
    try:
        if identity(os.stat(quarantine_name, dir_fd=directory_descriptor, follow_symlinks=False)) != quarantine_identity:
            raise OSError
        os.rename(name, name, src_dir_fd=directory_descriptor, dst_dir_fd=quarantine_descriptor)
        if identity(os.stat(name, dir_fd=quarantine_descriptor, follow_symlinks=False)) != identity(expected):
            raise OSError
        os.unlink(name, dir_fd=quarantine_descriptor)
    finally:
        os.close(quarantine_descriptor)
        if identity(os.stat(quarantine_name, dir_fd=directory_descriptor, follow_symlinks=False)) != quarantine_identity:
            raise OSError
        os.rmdir(quarantine_name, dir_fd=directory_descriptor)


def remove_contents(descriptor):
    os.fchmod(descriptor, 0o700)
    for name in os.listdir(descriptor):
        details = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if stat.S_ISDIR(details.st_mode):
            child = os.open(name, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0), dir_fd=descriptor)
            try:
                if identity(os.fstat(child)) != identity(details):
                    raise OSError
                remove_contents(child)
            finally:
                os.close(child)
            if identity(os.stat(name, dir_fd=descriptor, follow_symlinks=False)) != identity(details):
                raise OSError
            os.rmdir(name, dir_fd=descriptor)
        else:
            remove_non_directory(descriptor, name, details)


try:
    root = Path(sys.argv[1])
    expected_identity = tuple(int(value) for value in sys.argv[2].split(":"))
    parent = Path(sys.argv[3]).resolve(strict=True)
    prefix = sys.argv[4]
    if len(expected_identity) != 2 or not root.is_absolute() or root.parent.resolve(strict=True) != parent or not root.name.startswith(prefix):
        raise OSError
    parent_descriptor = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        candidates = matching_entries(parent_descriptor, expected_identity)
        if len(candidates) != 1:
            raise OSError
        name = candidates[0]
        details = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if not stat.S_ISDIR(details.st_mode) or stat.S_IMODE(details.st_mode) != 0o700 or details.st_uid != os.getuid():
            raise OSError
        root_descriptor = os.open(name, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_descriptor)
        try:
            if identity(os.fstat(root_descriptor)) != expected_identity:
                raise OSError
            remove_contents(root_descriptor)
        finally:
            os.close(root_descriptor)
        if identity(os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)) != expected_identity:
            raise OSError
        os.rmdir(name, dir_fd=parent_descriptor)
    finally:
        os.close(parent_descriptor)
except OSError:
    raise SystemExit(2) from None
PY
}

create_owned_private_directory() {
    /usr/bin/python3 -I -S - "$1" "$2" <<'PY'
import os
import stat
import sys
from pathlib import Path


def identity(details):
    return details.st_dev, details.st_ino


def discard_retained_empty_directory(parent_descriptor, descriptor):
    retained_identity = identity(os.fstat(descriptor))
    matches = []
    for candidate in os.listdir(parent_descriptor):
        try:
            details = os.stat(
                candidate,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            continue
        if identity(details) == retained_identity:
            matches.append(candidate)
    if len(matches) != 1:
        raise OSError
    os.rmdir(matches[0], dir_fd=parent_descriptor)


try:
    parent = Path(sys.argv[1]).resolve(strict=True)
    prefix = sys.argv[2]
    if not prefix or "/" in prefix:
        raise OSError
    parent_descriptor = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        descriptor = None
        published = False
        for _ in range(16):
            name = prefix + os.urandom(16).hex()
            try:
                os.mkdir(name, 0o700, dir_fd=parent_descriptor)
            except FileExistsError:
                continue
            descriptor = os.open(name, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_descriptor)
            break
        if descriptor is None:
            raise OSError
        try:
            details = os.fstat(descriptor)
            if identity(os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)) != identity(details) or not stat.S_ISDIR(details.st_mode):
                raise OSError
            os.fchmod(descriptor, 0o700)
            details = os.fstat(descriptor)
            if identity(os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)) != identity(details) or stat.S_IMODE(details.st_mode) != 0o700 or details.st_uid != os.getuid():
                raise OSError
            print(f"{parent / name} {details.st_dev}:{details.st_ino}")
            published = True
        finally:
            if not published:
                discard_retained_empty_directory(parent_descriptor, descriptor)
            os.close(descriptor)
    finally:
        os.close(parent_descriptor)
except (OSError, ValueError, TypeError):
    raise SystemExit(2) from None
PY
}

validate_owned_private_directory() {
    /usr/bin/python3 -I -S - "$1" "$2" "$3" "$4" <<'PY'
import os
import stat
import sys
from pathlib import Path

try:
    root = Path(sys.argv[1])
    expected = tuple(int(value) for value in sys.argv[2].split(":"))
    parent = Path(sys.argv[3]).resolve(strict=True)
    prefix = sys.argv[4]
    if len(expected) != 2 or root.parent.resolve(strict=True) != parent or not root.name.startswith(prefix):
        raise OSError
    parent_descriptor = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        details = os.stat(root.name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (details.st_dev, details.st_ino) != expected or not stat.S_ISDIR(details.st_mode) or stat.S_IMODE(details.st_mode) != 0o700 or details.st_uid != os.getuid():
            raise OSError
        descriptor = os.open(root.name, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_descriptor)
        try:
            details = os.fstat(descriptor)
            if (details.st_dev, details.st_ino) != expected:
                raise OSError
        finally:
            os.close(descriptor)
    finally:
        os.close(parent_descriptor)
except (OSError, ValueError, TypeError):
    raise SystemExit(2) from None
PY
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
        entries = {entry.name for entry in launcher_root.iterdir()}
        if entries != {launcher.name}:
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
launcher_root_identity=
private_environment=
private_environment_identity=
record_parent=
record_parent_identity=
normalized_tmp_root=
supervisor_pid=
outer_interrupted=0
run_verified_initial_launcher() {
    exec /usr/bin/env -i \
        HOME=/var/empty PATH=/usr/bin:/bin TMPDIR=/tmp \
        DOCKER_CONFIG="${DOCKER_CONFIG:-}" \
        /usr/bin/python3 -I -S - \
        "$launcher_root" "$launcher_root_identity" "$publisher_script" \
        "$publisher_launcher_hash" "$publisher_repository" \
        "${DOCKER_CONFIG:-}" "$@" <<'PY'
import ctypes
import errno
import hashlib
import os
import re
import select
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

root = Path(sys.argv[1])
root_expected = tuple(int(value) for value in sys.argv[2].split(":"))
source = Path(sys.argv[3])
expected_hash = sys.argv[4]
repository = sys.argv[5]
docker_config = sys.argv[6]
script_arguments = sys.argv[7:]
tmp_root = Path("/tmp").resolve(strict=True)
handled_signals = {signal.SIGHUP, signal.SIGINT, signal.SIGTERM}
signal.pthread_sigmask(signal.SIG_BLOCK, handled_signals)
first_signal = 0
process = None


def remember_signal(signum, _frame):
    global first_signal
    if first_signal == 0:
        first_signal = signum


for handled_signal in handled_signals:
    signal.signal(handled_signal, remember_signal)

def identity(details):
    return details.st_dev, details.st_ino


def rename_no_replace(source_fd, destination_fd, name):
    libc = ctypes.CDLL(None, use_errno=True)
    encoded = os.fsencode(name)
    if sys.platform == "darwin":
        operation = getattr(libc, "renameatx_np", None)
        flags = 0x00000004
    elif sys.platform.startswith("linux"):
        operation = getattr(libc, "renameat2", None)
        flags = 0x00000001
    else:
        operation = None
        flags = 0
    if operation is None:
        raise OSError
    operation.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    operation.restype = ctypes.c_int
    if operation(source_fd, encoded, destination_fd, encoded, flags) != 0:
        error = ctypes.get_errno()
        if error in {errno.EEXIST, errno.ENOTEMPTY}:
            raise OSError from None
        raise OSError(error, os.strerror(error))


def create_private_root():
    parent_fd = os.open(
        tmp_root,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    descriptor = None
    created_name = None
    created_expected = None
    published = False
    try:
        for _ in range(16):
            name = "travel-map-publish-environment." + os.urandom(16).hex()
            try:
                os.mkdir(name, 0o700, dir_fd=parent_fd)
            except FileExistsError:
                continue
            created_name = name
            descriptor = os.open(
                name,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_fd,
            )
            break
        if descriptor is None:
            raise OSError
        os.fchmod(descriptor, 0o700)
        details = os.fstat(descriptor)
        created_expected = identity(details)
        path_details = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            identity(details) != identity(path_details)
            or not stat.S_ISDIR(details.st_mode)
            or stat.S_IMODE(details.st_mode) != 0o700
            or details.st_uid != os.getuid()
        ):
            raise OSError
        published = True
        return tmp_root / name, created_expected
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if not published and created_name is not None:
            try:
                details = os.stat(
                    created_name, dir_fd=parent_fd, follow_symlinks=False
                )
                if (
                    created_expected is not None
                    and identity(details) == created_expected
                    and stat.S_ISDIR(details.st_mode)
                ):
                    os.rmdir(created_name, dir_fd=parent_fd)
            except OSError:
                pass
        os.close(parent_fd)


def create_cleanup_quarantine(directory_fd):
    quarantine_fd = None
    for _ in range(16):
        quarantine_name = ".travel-map-cleanup." + os.urandom(16).hex()
        try:
            os.mkdir(quarantine_name, 0o700, dir_fd=directory_fd)
        except FileExistsError:
            continue
        quarantine_fd = os.open(
            quarantine_name,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_fd,
        )
        details = os.fstat(quarantine_fd)
        path_details = os.stat(
            quarantine_name, dir_fd=directory_fd, follow_symlinks=False
        )
        if (
            identity(details) != identity(path_details)
            or not stat.S_ISDIR(details.st_mode)
            or details.st_uid != os.getuid()
        ):
            os.close(quarantine_fd)
            raise OSError
        return quarantine_name, quarantine_fd, identity(details)
    raise OSError


def restore_quarantined_entry(quarantine_fd, directory_fd, name):
    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        rename_no_replace(quarantine_fd, directory_fd, name)
        return
    raise OSError


def remove_quarantined_entry(directory_fd, name, expected):
    quarantine_name, quarantine_fd, quarantine_expected = create_cleanup_quarantine(
        directory_fd
    )
    moved = False
    try:
        os.rename(
            name,
            name,
            src_dir_fd=directory_fd,
            dst_dir_fd=quarantine_fd,
        )
        moved = True
        details = os.stat(name, dir_fd=quarantine_fd, follow_symlinks=False)
        if identity(details) != expected:
            restore_quarantined_entry(quarantine_fd, directory_fd, name)
            moved = False
            raise OSError
        unsafe = False
        if stat.S_ISDIR(details.st_mode):
            child_fd = os.open(
                name,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=quarantine_fd,
            )
            try:
                if identity(os.fstat(child_fd)) != expected:
                    raise OSError
                unsafe = remove_contents(child_fd)
            finally:
                os.close(child_fd)
            if (
                identity(
                    os.stat(name, dir_fd=quarantine_fd, follow_symlinks=False)
                )
                != expected
            ):
                raise OSError
            os.rmdir(name, dir_fd=quarantine_fd)
        else:
            if details.st_nlink != 1:
                unsafe = True
            if (
                identity(
                    os.stat(name, dir_fd=quarantine_fd, follow_symlinks=False)
                )
                != expected
            ):
                raise OSError
            os.unlink(name, dir_fd=quarantine_fd)
        moved = False
        return unsafe
    finally:
        if moved:
            try:
                restore_quarantined_entry(quarantine_fd, directory_fd, name)
            except OSError:
                pass
        quarantine_details = os.fstat(quarantine_fd)
        quarantine_empty = not os.listdir(quarantine_fd)
        os.close(quarantine_fd)
        if (
            identity(quarantine_details) != quarantine_expected
            or not quarantine_empty
            or identity(
                os.stat(
                    quarantine_name,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            )
            != quarantine_expected
        ):
            raise OSError
        os.rmdir(quarantine_name, dir_fd=directory_fd)


def remove_contents(directory_fd):
    unsafe = False
    os.fchmod(directory_fd, 0o700)
    for name in os.listdir(directory_fd):
        details = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        unsafe = (
            remove_quarantined_entry(directory_fd, name, identity(details)) or unsafe
        )
    return unsafe


def cleanup_owned_root(path, expected, prefix):
    if (
        len(expected) != 2
        or path.parent != tmp_root
        or not path.name.startswith(prefix)
    ):
        return False
    parent_fd = os.open(
        tmp_root,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    unsafe = False
    try:
        matches = []
        for candidate in os.listdir(parent_fd):
            try:
                details = os.stat(candidate, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if identity(details) == expected:
                matches.append(candidate)
        try:
            named_details = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            named_details = None
        if named_details is not None and identity(named_details) != expected:
            unsafe = True
        if len(matches) > 1:
            return False
        if matches:
            name = matches[0]
            details = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if (
                not stat.S_ISDIR(details.st_mode)
                or details.st_uid != os.getuid()
            ):
                return False
            unsafe = remove_quarantined_entry(parent_fd, name, expected) or unsafe
        for candidate in os.listdir(parent_fd):
            try:
                details = os.stat(candidate, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if identity(details) == expected:
                return False
        try:
            os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            unsafe = True
        return not unsafe
    except OSError:
        return False
    finally:
        os.close(parent_fd)


def read_verified_script():
    if (
        len(root_expected) != 2
        or source.parent != root
        or source.name != "publish-reviewed-image.sh"
        or re.fullmatch(r"[0-9a-f]{64}", expected_hash) is None
    ):
        raise OSError
    root_fd = os.open(
        root,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        if (os.fstat(root_fd).st_dev, os.fstat(root_fd).st_ino) != root_expected:
            raise OSError
        source_fd = os.open(
            source.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=root_fd
        )
        try:
            before = os.fstat(source_fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or stat.S_IMODE(before.st_mode) != 0o500
                or before.st_uid != os.getuid()
                or before.st_nlink != 1
            ):
                raise OSError
            payload = bytearray()
            while True:
                chunk = os.read(source_fd, 1024 * 1024)
                if not chunk:
                    break
                payload.extend(chunk)
            after = os.fstat(source_fd)
            path_details = os.stat(source.name, dir_fd=root_fd, follow_symlinks=False)
            if (
                (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
                or (before.st_dev, before.st_ino)
                != (path_details.st_dev, path_details.st_ino)
                or hashlib.sha256(payload).hexdigest() != expected_hash
            ):
                raise OSError
        finally:
            os.close(source_fd)
    finally:
        os.close(root_fd)
    return bytes(payload)


def trusted_process_state(pid):
    completed = subprocess.run(
        ["/bin/ps", "-o", "pid=,state=", "-p", str(pid)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env={"PATH": "/usr/bin:/bin"},
        check=False,
    )
    if completed.returncode != 0:
        raise OSError("publisher process snapshot failed")
    lines = completed.stdout.splitlines()
    if len(lines) != 1:
        raise OSError
    fields = lines[0].split()
    if (
        len(fields) != 2
        or fields[0] != str(pid).encode("ascii")
        or re.fullmatch(rb"[A-Za-z]+", fields[1]) is None
    ):
        raise OSError
    return fields[1]


class ExitObserver:
    def __init__(self, pid):
        self.pid = pid
        self.exited = False
        self.queue = None
        self.waitid_available = hasattr(os, "waitid") and hasattr(os, "WNOWAIT")
        if self.waitid_available:
            return
        if not (
            hasattr(select, "kqueue")
            and hasattr(select, "KQ_FILTER_PROC")
            and hasattr(select, "KQ_NOTE_EXIT")
        ):
            raise OSError
        self.queue = select.kqueue()
        try:
            self.queue.control(
                [
                    select.kevent(
                        pid,
                        filter=select.KQ_FILTER_PROC,
                        flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE,
                        fflags=select.KQ_NOTE_EXIT,
                    )
                ],
                0,
                0,
            )
        except ProcessLookupError:
            if trusted_process_state(pid) != b"Z":
                self.queue.close()
                self.queue = None
                raise OSError from None
            self.exited = True

    def has_exited(self):
        if self.exited:
            return True
        if self.waitid_available:
            try:
                result = os.waitid(
                    os.P_PID,
                    self.pid,
                    os.WEXITED | os.WNOHANG | os.WNOWAIT,
                )
            except ChildProcessError:
                raise OSError from None
            self.exited = result is not None
        else:
            if self.queue is None:
                raise OSError
            self.exited = bool(self.queue.control(None, 1, 0))
        return self.exited

    def close(self):
        if self.queue is not None:
            self.queue.close()
            self.queue = None


class IncompleteProcessSnapshot(OSError):
    pass


def process_group_members(group_id):
    completed = subprocess.run(
        ["/bin/ps", "-axo", "pid=,pgid="],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env={"PATH": "/usr/bin:/bin"},
        check=False,
    )
    if completed.returncode != 0:
        raise OSError("publisher process snapshot failed")
    members = set()
    observer_anchor = False
    group_anchor = False
    for line in completed.stdout.splitlines():
        fields = line.split()
        if (
            len(fields) != 2
            or re.fullmatch(rb"[0-9]+", fields[0]) is None
            or re.fullmatch(rb"[0-9]+", fields[1]) is None
        ):
            raise OSError
        pid, pgid = (int(value) for value in fields)
        if pid == os.getpid():
            if pgid != os.getpgrp():
                raise OSError("publisher observer anchor changed groups")
            observer_anchor = True
        if pgid == group_id:
            if pid == group_id:
                group_anchor = True
            else:
                members.add(pid)
    if not observer_anchor:
        raise OSError("publisher process snapshot omitted its observer")
    if not group_anchor:
        try:
            os.killpg(group_id, 0)
        except ProcessLookupError:
            return set()
        except PermissionError:
            raise OSError("publisher process group permission denied") from None
        if leader_reaped:
            raise IncompleteProcessSnapshot(
                "publisher process snapshot omitted a live group member"
            )
        raise OSError("publisher process snapshot omitted its group leader")
    if not members:
        try:
            os.killpg(group_id, 0)
        except ProcessLookupError:
            return set()
        except PermissionError:
            try:
                leader_state = trusted_process_state(group_id)
            except OSError:
                leader_state = None
            if leader_state == b"Z":
                raise IncompleteProcessSnapshot(
                    "publisher process snapshot retained a zombie group leader"
                )
            raise OSError("publisher process group permission denied") from None
        raise IncompleteProcessSnapshot(
            "publisher process snapshot omitted a live group member"
        )
    return members


def signal_owned_group(signum):
    if process is None:
        raise OSError
    if signum != 0 and leader_reaped:
        raise IncompleteProcessSnapshot(
            "publisher process-group leader identity is no longer owned"
        )
    try:
        os.killpg(process.pid, signum)
    except ProcessLookupError:
        pass


def wait_for_leader_exit(observer, deadline):
    while not observer.has_exited():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)
    return True


def stop_owned_group(observer, term_sent_at=None):
    global leader_reaped
    if process is None:
        raise OSError

    def reap_if_exited():
        global leader_reaped
        if leader_reaped or not observer.has_exited():
            return leader_reaped
        try:
            process.wait(timeout=2)
        except subprocess.SubprocessError:
            raise OSError from None
        leader_reaped = True
        return True

    def group_absent_after_reap():
        if not leader_reaped:
            return False
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return True
        except OSError:
            return False
        return False

    if not observer.has_exited():
        if term_sent_at is None:
            signal_owned_group(signal.SIGTERM)
            term_sent_at = time.monotonic()
        if not wait_for_leader_exit(observer, term_sent_at + 5):
            if not leader_reaped:
                signal_owned_group(signal.SIGKILL)
            if not wait_for_leader_exit(observer, time.monotonic() + 2):
                raise OSError
    while True:
        try:
            members = process_group_members(process.pid)
        except IncompleteProcessSnapshot:
            reap_if_exited()
            if group_absent_after_reap():
                return
            time.sleep(0.01)
            continue
        except OSError:
            if not leader_reaped:
                try:
                    signal_owned_group(signal.SIGKILL)
                except (IncompleteProcessSnapshot, OSError):
                    pass
            reap_if_exited()
            if group_absent_after_reap():
                return
            time.sleep(0.01)
            continue
        else:
            if leader_reaped:
                if not members:
                    return
                time.sleep(0.01)
                continue
            break
    if not members:
        return
    if term_sent_at is None:
        signal_owned_group(signal.SIGTERM)
        term_sent_at = time.monotonic()
    deadline = term_sent_at + 15
    while True:
        try:
            members = process_group_members(process.pid)
        except (IncompleteProcessSnapshot, OSError):
            if not leader_reaped:
                try:
                    signal_owned_group(signal.SIGKILL)
                except (IncompleteProcessSnapshot, OSError):
                    pass
            reap_if_exited()
            if group_absent_after_reap():
                return
            time.sleep(0.01)
            continue
        if not members:
            return
        if leader_reaped:
            time.sleep(0.01)
            continue
        if time.monotonic() >= deadline:
            signal_owned_group(signal.SIGKILL)
            while True:
                try:
                    members = process_group_members(process.pid)
                except (IncompleteProcessSnapshot, OSError):
                    if not leader_reaped:
                        try:
                            signal_owned_group(signal.SIGKILL)
                        except (IncompleteProcessSnapshot, OSError):
                            pass
                    reap_if_exited()
                    if group_absent_after_reap():
                        return
                    time.sleep(0.01)
                    continue
                if not members:
                    return
                time.sleep(0.01)
        time.sleep(0.01)


def emergency_stop_owned_group():
    global leader_reaped
    if process is None:
        raise OSError
    if not leader_reaped:
        signal_owned_group(signal.SIGKILL)
    while True:
        if not leader_reaped:
            try:
                process.wait(timeout=0.01)
                leader_reaped = True
            except subprocess.TimeoutExpired:
                pass
            except subprocess.SubprocessError:
                raise OSError from None
        try:
            members = process_group_members(process.pid)
        except (IncompleteProcessSnapshot, OSError):
            time.sleep(0.01)
            continue
        if leader_reaped and not members:
            return
        time.sleep(0.01)


def drain_stdout(captured):
    if process is None or process.stdout is None:
        raise OSError
    eof = False
    while True:
        try:
            chunk = os.read(process.stdout.fileno(), 65536)
        except BlockingIOError:
            break
        if not chunk:
            eof = True
            break
        if len(captured) <= 4096:
            captured.extend(chunk[: 4097 - len(captured)])
    return eof


private_root = None
private_expected = None
captured = bytearray()
status = 2
cleanup_ok = False
exit_observer = None
leader_reaped = False
quiescence_proven = False
input_open = False
group_term_sent_at = None
try:
    public_stdout_details = os.fstat(1)
    public_pipe_buf = os.fpathconf(1, "PC_PIPE_BUF")
    if not stat.S_ISFIFO(public_stdout_details.st_mode) or public_pipe_buf < 256:
        raise OSError
    payload = read_verified_script()
    private_root, private_expected = create_private_root()
    environment = {
        "HOME": "/var/empty",
        "PATH": "/usr/bin:/bin",
        "TMPDIR": str(tmp_root),
        "DOCKER_CONFIG": docker_config,
        "TRAVEL_MAP_PUBLISH_PRIVATE_LAUNCHER": "1",
        "TRAVEL_MAP_PUBLISH_REPOSITORY": repository,
        "TRAVEL_MAP_PUBLISH_LAUNCHER_ROOT": str(root),
        "TRAVEL_MAP_PUBLISH_LAUNCHER_IDENTITY": f"{root_expected[0]}:{root_expected[1]}",
        "TRAVEL_MAP_PUBLISH_LAUNCHER_SHA256": expected_hash,
        "TRAVEL_MAP_PUBLISH_PRECREATED_ROOT": str(private_root),
        "TRAVEL_MAP_PUBLISH_PRECREATED_IDENTITY": f"{private_expected[0]}:{private_expected[1]}",
    }
    process = subprocess.Popen(
        [
            "/usr/bin/python3",
            "-I",
            "-S",
            "-c",
            "import os, signal, sys; "
            "signal.pthread_sigmask(signal.SIG_UNBLOCK, "
            "{signal.SIGHUP, signal.SIGINT, signal.SIGTERM}); "
            "os.execve('/bin/sh', sys.argv[1:], os.environ)",
            "/bin/sh",
            "-c",
            'script=$(cat) || exit 2; eval "$script"',
            str(source),
            *script_arguments,
        ],
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        start_new_session=True,
        close_fds=True,
    )
    if os.getpgid(process.pid) != process.pid:
        raise OSError
    exit_observer = ExitObserver(process.pid)
    if process.stdin is None or process.stdout is None:
        raise OSError
    os.set_blocking(process.stdin.fileno(), False)
    os.set_blocking(process.stdout.fileno(), False)
    pending = memoryview(payload)
    input_open = True
    termination_deadline = None
    signal.pthread_sigmask(signal.SIG_UNBLOCK, handled_signals)
    while True:
        drain_stdout(captured)
        if first_signal and termination_deadline is None:
            group_term_sent_at = time.monotonic()
            termination_deadline = time.monotonic() + 5
            signal_owned_group(first_signal)
        if pending and input_open:
            try:
                written = os.write(process.stdin.fileno(), pending)
                if written <= 0:
                    raise OSError
                pending = pending[written:]
            except BlockingIOError:
                pass
            except BrokenPipeError:
                raise OSError from None
        elif input_open:
            process.stdin.close()
            input_open = False
        if exit_observer.has_exited():
            if pending:
                raise OSError
            break
        if termination_deadline is not None and time.monotonic() >= termination_deadline:
            signal_owned_group(signal.SIGKILL)
            if not wait_for_leader_exit(exit_observer, time.monotonic() + 2):
                raise OSError
        time.sleep(0.01)
    stop_owned_group(exit_observer, group_term_sent_at)
    quiescence_proven = True
    # A detached cleanup broker (or its supervisor fallback) may still own the
    # stdout pipe while it proves the publisher process group is quiescent.
    eof_deadline = time.monotonic() + 45
    while not drain_stdout(captured):
        if time.monotonic() >= eof_deadline:
            raise OSError
        time.sleep(0.01)
    status = process.wait(timeout=2)
    leader_reaped = True
except (OSError, ValueError, TypeError, subprocess.SubprocessError):
    status = 2
finally:
    signal.pthread_sigmask(signal.SIG_BLOCK, handled_signals)
    if process is not None and not leader_reaped:
        try:
            if process.stdin is not None and not process.stdin.closed:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            input_open = False
            if exit_observer is None:
                emergency_stop_owned_group()
                quiescence_proven = True
            else:
                stop_owned_group(exit_observer, group_term_sent_at)
                quiescence_proven = True
                process.wait(timeout=2)
            leader_reaped = True
        except (OSError, subprocess.SubprocessError):
            status = 2
    if exit_observer is not None:
        exit_observer.close()
    if process is None or quiescence_proven:
        private_clean = (
            private_root is not None
            and private_expected is not None
            and cleanup_owned_root(
                private_root, private_expected, "travel-map-publish-environment."
            )
        )
        launcher_clean = cleanup_owned_root(
            root, root_expected, "travel-map-publish-launcher."
        )
    else:
        private_clean = False
        launcher_clean = False
    cleanup_ok = private_clean and launcher_clean

valid_output = re.fullmatch(
    rb"ghcr\.io/h19h29-design/seoul-education-travel-map@sha256:[0-9a-f]{64}\n",
    bytes(captured),
) is not None
pending_at_publication = bool(signal.sigpending() & handled_signals)
if (
    first_signal
    or pending_at_publication
    or status != 0
    or not cleanup_ok
    or not valid_output
):
    raise SystemExit(2)
if len(captured) > public_pipe_buf:
    raise SystemExit(2)
try:
    if os.write(1, captured) != len(captured):
        raise OSError
except OSError:
    raise SystemExit(2)
raise SystemExit(0)
PY
}
verify_private_directory_absent() {
    /usr/bin/python3 -I -S - "$1" "$2" "$3" "$4" <<'PY'
import os
import sys
from pathlib import Path

try:
    root = Path(sys.argv[1])
    expected = tuple(int(value) for value in sys.argv[2].split(":"))
    parent = Path(sys.argv[3]).resolve(strict=True)
    prefix = sys.argv[4]
    if (
        len(expected) != 2 or not root.is_absolute() or not prefix
        or root.parent.resolve(strict=True) != parent or not root.name.startswith(prefix)
    ):
        raise OSError
    parent_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        for name in os.listdir(parent_fd):
            try:
                details = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if (details.st_dev, details.st_ino) == expected:
                raise OSError
        try:
            os.stat(root.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise OSError
    finally:
        os.close(parent_fd)
except (OSError, ValueError, TypeError):
    raise SystemExit(2) from None
PY
}
cleanup_outer_launcher() {
    cleanup_status=0
    status=${1:-2}
    if [ -n "$private_environment" ] && [ -n "$normalized_tmp_root" ]; then
        remove_private_directory "$private_environment" \
            "$private_environment_identity" "$normalized_tmp_root" \
            travel-map-publish-environment. \
            || verify_private_directory_absent "$private_environment" \
                "$private_environment_identity" "$normalized_tmp_root" \
                travel-map-publish-environment. \
            || cleanup_status=1
    fi
    if [ -n "$launcher_root" ]; then
        remove_private_directory "$launcher_root" "$launcher_root_identity" /tmp \
            travel-map-publish-launcher. \
            || verify_private_directory_absent "$launcher_root" \
                "$launcher_root_identity" /tmp travel-map-publish-launcher. \
            || cleanup_status=1
    fi
    if [ "$cleanup_status" -ne 0 ] || [ "$outer_interrupted" -ne 0 ]; then
        exit 2
    fi
    exit "$status"
}
cleanup_outer_signal() {
    trap '' HUP INT TERM
    outer_interrupted=1
    signal_pid=${supervisor_pid:-${!:-}}
    if [ -n "$signal_pid" ]; then
        /bin/kill -TERM "$signal_pid" 2>/dev/null || true
        /bin/kill -TERM -- -"$signal_pid" 2>/dev/null || true
        signal_ticks=0
        while /bin/kill -0 "$signal_pid" 2>/dev/null; do
            if [ "$signal_ticks" -ge 1200 ]; then
                /bin/kill -KILL "$signal_pid" 2>/dev/null || true
                /bin/kill -KILL -- -"$signal_pid" 2>/dev/null || true
                break
            fi
            /bin/sleep 0.01
            signal_ticks=$((signal_ticks + 1))
        done
        signal_status=0
        wait "$signal_pid" 2>/dev/null || signal_status=$?
        if verify_private_directory_absent "$launcher_root" \
                "$launcher_root_identity" /tmp travel-map-publish-launcher.; then
            launcher_root=
            launcher_root_identity=
        fi
        supervisor_pid=
    fi
    exit 2
}
arm_private_launcher_cleanup() {
    launcher_root=${TRAVEL_MAP_PUBLISH_LAUNCHER_ROOT:-}
    supplied_launcher_identity=${TRAVEL_MAP_PUBLISH_LAUNCHER_IDENTITY:-}
    launcher_validation=$(/usr/bin/python3 -I -S - \
        "$launcher_root" "$invoked_publisher_script" "$supplied_launcher_identity" <<'PY'
import os
import stat
import sys
from pathlib import Path

try:
    root = Path(sys.argv[1])
    launcher = Path(sys.argv[2])
    expected_identity = sys.argv[3]
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
    actual_identity = f"{root_details.st_dev}:{root_details.st_ino}"
    print(f"{actual_identity} {int(expected_identity == actual_identity)}")
except (OSError, ValueError):
    raise SystemExit(2) from None
PY
    ) || blocked 'BLOCKED_INVALID_PUBLISH_CONTEXT'
    launcher_root_identity=${launcher_validation% *}
    launcher_identity_matches=${launcher_validation##* }
    [ -n "$launcher_root_identity" ] \
        && [ "$launcher_validation" = "$launcher_root_identity $launcher_identity_matches" ] \
        || blocked 'BLOCKED_INVALID_PUBLISH_CONTEXT'
    trap cleanup_outer_signal HUP INT TERM
    [ "$launcher_identity_matches" = 1 ] \
        || blocked 'BLOCKED_INVALID_PUBLISH_CONTEXT'
}
load_verified_private_launcher() {
    publisher_repository=${TRAVEL_MAP_PUBLISH_REPOSITORY:-}
    launcher_root=${TRAVEL_MAP_PUBLISH_LAUNCHER_ROOT:-}
    launcher_root_identity=${TRAVEL_MAP_PUBLISH_LAUNCHER_IDENTITY:-}
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
                launcher_creation=$(create_owned_private_directory \
                    "$normalized_launcher_tmp" 'travel-map-publish-launcher.') \
                    || blocked 'BLOCKED_INVALID_PUBLISH_CONTEXT'
                trap 'cleanup_outer_launcher "$?"' EXIT
                trap cleanup_outer_signal HUP INT TERM
                launcher_root=${launcher_creation% *}
                launcher_root_identity=${launcher_creation##* }
                [ -n "$launcher_root" ] && [ -n "$launcher_root_identity" ] \
                    || blocked 'BLOCKED_INVALID_PUBLISH_CONTEXT'
                validate_owned_private_directory \
                    "$launcher_root" "$launcher_root_identity" \
                    "$normalized_launcher_tmp" 'travel-map-publish-launcher.' \
                    || blocked 'BLOCKED_INVALID_PUBLISH_CONTEXT'
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
                run_verified_initial_launcher "$@"
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
        shared_write = parent_details.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        if (
            not stat.S_ISDIR(parent_details.st_mode)
            or parent_details.st_uid not in {0, os.getuid()}
            or (shared_write and not parent_details.st_mode & stat.S_ISVTX)
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
    case "${1:-}" in
        tag|push) validate_publish_lock_authority || return 1 ;;
    esac
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
        private_environment=${TRAVEL_MAP_PUBLISH_PRECREATED_ROOT:-}
        private_environment_identity=${TRAVEL_MAP_PUBLISH_PRECREATED_IDENTITY:-}
        [ -n "$private_environment" ] && [ -n "$private_environment_identity" ] \
            || blocked 'BLOCKED_PRIVATE_PUBLISH_DIRECTORY'
        validate_owned_private_directory \
            "$private_environment" "$private_environment_identity" \
            "$normalized_tmp_root" 'travel-map-publish-environment.' \
            || blocked 'BLOCKED_PRIVATE_PUBLISH_DIRECTORY'
        private_home=$private_environment/home
        private_xdg_config=$private_environment/xdg-config
        private_xdg_cache=$private_environment/xdg-cache
        private_xdg_data=$private_environment/xdg-data
        /bin/mkdir -m 0700 \
            "$private_home" "$private_xdg_config" "$private_xdg_cache" \
            "$private_xdg_data" \
            || blocked 'BLOCKED_PRIVATE_PUBLISH_DIRECTORY'
        /usr/bin/env -i \
            HOME=/var/empty PATH=/usr/bin:/bin TMPDIR=/tmp \
            /usr/bin/python3 -I -S - \
            "$publisher_script" "$private_environment" "$trusted_path" \
            "$normalized_tmp_root" "$record" "$expected_image_tag" \
            "$expected_image_id" "$nas_platform" "$git_sha" \
            "$expected_record_sha256" \
            "$docker_config" "$docker_host" "$docker_authority_identity" \
            "$docker_tool_identity" "$buildx_tool_identity" \
            "$docker_tool" "$buildx_tool" "$launcher_root" \
            "$launcher_root_identity" "$publisher_repository" \
            "$publisher_launcher_hash" <<'PY' &
from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import re
import select
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

STAGE_B_CHILD_LAUNCHER = """\
import fcntl
import os
import signal
import stat
import sys

try:
    pid_fd_text = sys.argv[1]
    tag_fd_text = os.environ["TRAVEL_MAP_PUBLISH_TAG_ARM_FD"]
    fallback_fd_text = os.environ[
        "TRAVEL_MAP_PUBLISH_FALLBACK_TAG_ARM_FD"
    ]
    if not all(value.isdecimal() for value in (
        pid_fd_text, tag_fd_text, fallback_fd_text
    )):
        raise OSError
    pid_fd, tag_fd, fallback_fd = (
        int(pid_fd_text), int(tag_fd_text), int(fallback_fd_text)
    )
    source_descriptors = (pid_fd, tag_fd, fallback_fd)
    if (
        any(descriptor <= 2 for descriptor in source_descriptors)
        or len(set(source_descriptors)) != len(source_descriptors)
        or any(
            not stat.S_ISFIFO(os.fstat(descriptor).st_mode)
            for descriptor in source_descriptors
        )
    ):
        raise OSError
    pid_payload = f"{os.getpid()}\\n".encode("ascii")
    if os.write(pid_fd, pid_payload) != len(pid_payload):
        raise OSError
    os.close(pid_fd)

    copies = []
    targets = (8, 9)
    try:
        for descriptor in (tag_fd, fallback_fd):
            copies.append(
                fcntl.fcntl(descriptor, fcntl.F_DUPFD_CLOEXEC, 10)
            )
        for descriptor, target in zip(copies, targets):
            os.dup2(descriptor, target, inheritable=True)
    finally:
        for descriptor in set((tag_fd, fallback_fd, *copies)) - set(targets):
            os.close(descriptor)
    os.environ["TRAVEL_MAP_PUBLISH_TAG_ARM_FD"] = str(targets[0])
    os.environ["TRAVEL_MAP_PUBLISH_FALLBACK_TAG_ARM_FD"] = str(targets[1])
    signal.pthread_sigmask(
        signal.SIG_UNBLOCK, {signal.SIGHUP, signal.SIGINT, signal.SIGTERM}
    )
    os.execve("/bin/sh", sys.argv[2:], os.environ)
except (KeyError, OSError, ValueError):
    raise SystemExit(2) from None
"""

handled_signals = {signal.SIGHUP, signal.SIGINT, signal.SIGTERM}
signal.pthread_sigmask(signal.SIG_BLOCK, handled_signals)


class CanonicalLockLeafCollision(Exception):
    pass


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
    launcher_root_identity,
    publisher_repository,
    publisher_launcher_hash,
) = sys.argv[1:]
private_root = Path(private_root_raw)
launcher_root = Path(launcher_root_raw)
tmp_root = Path(tmp_root_raw)
process: subprocess.Popen[bytes] | None = None
broker_pid: int | None = None
broker_control_write: int | None = None
broker_status_read: int | None = None
broker_pid_write: int | None = None
broker_tag_write: int | None = None
broker_fallback_tag_read: int | None = None
broker_fallback_tag_write: int | None = None
broker_exit_status: int | None = None
interrupted = False
pending_signal = 0
signal_received_at: float | None = None
termination_deadline: float | None = None
script_input: int | None = None
signal_forwarded = False
publisher_process_group = os.getpgrp()


def enable_child_subreaper() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    operation = getattr(ctypes.CDLL(None, use_errno=True), "prctl", None)
    if operation is None:
        raise OSError
    operation.argtypes = [ctypes.c_int, *([ctypes.c_ulong] * 4)]
    operation.restype = ctypes.c_int
    if operation(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return True


try:
    subreaper_enabled = enable_child_subreaper()
except OSError:
    raise SystemExit(2) from None


def read_verified_launcher_script() -> bytes:
    expected_root = tuple(int(value) for value in launcher_root_identity.split(":"))
    if (
        len(expected_root) != 2
        or Path(script) != launcher_root / "publish-reviewed-image.sh"
        or re.fullmatch(r"[0-9a-f]{64}", publisher_launcher_hash) is None
    ):
        raise OSError
    root_fd = os.open(
        launcher_root,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        if (os.fstat(root_fd).st_dev, os.fstat(root_fd).st_ino) != expected_root:
            raise OSError
        source_fd = os.open(
            "publish-reviewed-image.sh",
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=root_fd,
        )
        try:
            before = os.fstat(source_fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or stat.S_IMODE(before.st_mode) != 0o500
                or before.st_uid != os.getuid()
                or before.st_nlink != 1
            ):
                raise OSError
            payload = bytearray()
            while True:
                chunk = os.read(source_fd, 1024 * 1024)
                if not chunk:
                    break
                payload.extend(chunk)
            after = os.fstat(source_fd)
            path_details = os.stat(
                "publish-reviewed-image.sh", dir_fd=root_fd, follow_symlinks=False
            )
            if (
                (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
                or (before.st_dev, before.st_ino)
                != (path_details.st_dev, path_details.st_ino)
                or hashlib.sha256(payload).hexdigest() != publisher_launcher_hash
            ):
                raise OSError
        finally:
            os.close(source_fd)
        return bytes(payload)
    finally:
        os.close(root_fd)


def forward_signal(signum: int, _frame: object) -> None:
    global pending_signal
    if pending_signal == 0:
        pending_signal = signum


def reap_process() -> None:
    if process is None:
        return
    if process.poll() is not None:
        return
    try:
        os.kill(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 5
    while process.poll() is None:
        if time.monotonic() >= deadline:
            try:
                os.kill(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                return
            try:
                process.wait(timeout=2)
            except subprocess.SubprocessError:
                raise OSError from None
            return
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


def write_broker_line(descriptor: int, payload: bytes) -> None:
    if len(payload) > 4096 or not payload.endswith(b"\n"):
        raise OSError
    if os.write(descriptor, payload) != len(payload):
        raise OSError


def read_broker_line(descriptor: int, deadline: float) -> bytes | None:
    payload = bytearray()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OSError
        ready, _, _ = select.select([descriptor], [], [], min(remaining, 0.1))
        if not ready:
            continue
        chunk = os.read(descriptor, 1)
        if not chunk:
            if payload:
                raise OSError
            return None
        payload.extend(chunk)
        if len(payload) > 4096:
            raise OSError
        if chunk == b"\n":
            return bytes(payload)


def create_broker_directory(parent: Path, prefix: str) -> tuple[Path, tuple[int, int]]:
    parent_fd = os.open(
        parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    created_name = None
    created_expected = None
    descriptor = None
    published = False
    try:
        for _ in range(16):
            name = prefix + os.urandom(16).hex()
            try:
                os.mkdir(name, 0o700, dir_fd=parent_fd)
            except FileExistsError:
                continue
            created_name = name
            descriptor = os.open(
                name,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_fd,
            )
            os.fchmod(descriptor, 0o700)
            created_expected = descriptor_identity(os.fstat(descriptor))
            details = os.fstat(descriptor)
            path_details = os.stat(
                name, dir_fd=parent_fd, follow_symlinks=False
            )
            if (
                descriptor_identity(path_details) != created_expected
                or
                not stat.S_ISDIR(details.st_mode)
                or stat.S_IMODE(details.st_mode) != 0o700
                or details.st_uid != os.getuid()
            ):
                raise OSError
            published = True
            return parent / name, created_expected
        raise OSError
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if not published and created_expected is not None:
            try:
                matches = matching_identity(parent_fd, created_expected)
                if len(matches) == 1:
                    details = os.stat(
                        matches[0], dir_fd=parent_fd, follow_symlinks=False
                    )
                    if stat.S_ISDIR(details.st_mode):
                        os.rmdir(matches[0], dir_fd=parent_fd)
            except OSError:
                pass
        os.close(parent_fd)


def create_broker_lock() -> tuple[
    Path, tuple[int, int], tuple[int, int], bool
]:
    lock_root = tmp_root
    public_lock_root = Path("/tmp")
    parent_name = f"travel-map-publish-locks-{os.getuid()}"
    lock_parent = public_lock_root / parent_name
    root_fd = os.open(
        lock_root,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    parent_fd = None
    lock_fd = None
    parent_created = False
    lock_created = False
    parent_expected = None
    lock_expected = None
    staging_name = None
    published = False
    try:
        try:
            parent_fd = os.open(
                parent_name,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=root_fd,
            )
        except FileNotFoundError:
            # SAME_UID_CREATION_BOUNDARY: mkdirat has no atomic descriptor-returning
            # form on macOS or Linux. A deliberately hostile same-UID actor inside
            # the mkdir/open syscall gap requires a separate UID or sandbox. The
            # unpredictable staging name is opened first and only then published
            # to the canonical name with a no-replace rename.
            for _ in range(16):
                staging_name = ".travel-map-lock-parent." + os.urandom(16).hex()
                try:
                    os.mkdir(staging_name, 0o700, dir_fd=root_fd)
                except FileExistsError:
                    continue
                parent_fd = os.open(
                    staging_name,
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=root_fd,
                )
                os.fchmod(parent_fd, 0o700)
                parent_expected = descriptor_identity(os.fstat(parent_fd))
                staging_details = os.stat(
                    staging_name, dir_fd=root_fd, follow_symlinks=False
                )
                if descriptor_identity(staging_details) != parent_expected:
                    raise OSError
                rename_no_replace(
                    root_fd,
                    root_fd,
                    staging_name,
                    parent_name,
                )
                staging_name = None
                parent_created = True
                break
            if parent_fd is None:
                raise OSError
        parent_details = os.fstat(parent_fd)
        parent_path_details = os.stat(
            parent_name, dir_fd=root_fd, follow_symlinks=False
        )
        if parent_expected is None:
            parent_expected = descriptor_identity(parent_details)
        if (
            descriptor_identity(parent_path_details) != parent_expected
            or not stat.S_ISDIR(parent_details.st_mode)
            or stat.S_IMODE(parent_details.st_mode) != 0o700
            or parent_details.st_uid != os.getuid()
        ):
            raise OSError
        try:
            os.mkdir(git_sha, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            raise CanonicalLockLeafCollision from None
        lock_created = True
        lock_fd = os.open(
            git_sha,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        os.fchmod(lock_fd, 0o700)
        details = os.fstat(lock_fd)
        lock_expected = descriptor_identity(details)
        path_details = os.stat(git_sha, dir_fd=parent_fd, follow_symlinks=False)
        if (
            descriptor_identity(path_details) != lock_expected
            or not stat.S_ISDIR(details.st_mode)
            or stat.S_IMODE(details.st_mode) != 0o700
            or details.st_uid != os.getuid()
        ):
            raise OSError
        published = True
        return lock_parent / git_sha, lock_expected, parent_expected, parent_created
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        if not published and lock_created and parent_fd is not None and lock_expected:
            try:
                matches = matching_identity(parent_fd, lock_expected)
                if len(matches) == 1:
                    details = os.stat(
                        matches[0], dir_fd=parent_fd, follow_symlinks=False
                    )
                    if stat.S_ISDIR(details.st_mode):
                        os.rmdir(matches[0], dir_fd=parent_fd)
            except OSError:
                pass
        if parent_fd is not None:
            os.close(parent_fd)
        if not published and parent_created and parent_expected is not None:
            try:
                matches = matching_identity(root_fd, parent_expected)
                if len(matches) == 1:
                    os.rmdir(matches[0], dir_fd=root_fd)
            except OSError:
                pass
        if staging_name is not None and parent_expected is not None:
            try:
                matches = matching_identity(root_fd, parent_expected)
                if len(matches) == 1:
                    os.rmdir(matches[0], dir_fd=root_fd)
            except OSError:
                pass
        os.close(root_fd)


def descriptor_identity(details: os.stat_result) -> tuple[int, int]:
    return details.st_dev, details.st_ino


def rename_no_replace(
    source_fd: int,
    destination_fd: int,
    source_name: str,
    destination_name: str | None = None,
) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    encoded_source = os.fsencode(source_name)
    encoded_destination = os.fsencode(
        source_name if destination_name is None else destination_name
    )
    if sys.platform == "darwin":
        operation = getattr(libc, "renameatx_np", None)
        flags = 0x00000004
    elif sys.platform.startswith("linux"):
        operation = getattr(libc, "renameat2", None)
        flags = 0x00000001
    else:
        operation = None
        flags = 0
    if operation is None:
        raise OSError
    operation.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    operation.restype = ctypes.c_int
    if (
        operation(
            source_fd,
            encoded_source,
            destination_fd,
            encoded_destination,
            flags,
        )
        != 0
    ):
        error = ctypes.get_errno()
        if error in {errno.EEXIST, errno.ENOTEMPTY}:
            raise OSError from None
        raise OSError(error, os.strerror(error))


def matching_identity(parent_fd: int, expected: tuple[int, int]) -> list[str]:
    matches = []
    for candidate in os.listdir(parent_fd):
        try:
            details = os.stat(candidate, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if descriptor_identity(details) == expected:
            matches.append(candidate)
    return matches


def create_broker_quarantine(directory_fd: int) -> tuple[str, int, tuple[int, int]]:
    for _ in range(16):
        name = ".travel-map-cleanup." + os.urandom(16).hex()
        try:
            os.mkdir(name, 0o700, dir_fd=directory_fd)
        except FileExistsError:
            continue
        descriptor = os.open(
            name,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_fd,
        )
        expected = descriptor_identity(os.fstat(descriptor))
        details = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            descriptor_identity(details) != expected
            or not stat.S_ISDIR(details.st_mode)
            or details.st_uid != os.getuid()
        ):
            os.close(descriptor)
            raise OSError
        return name, descriptor, expected
    raise OSError


def restore_broker_entry(quarantine_fd: int, directory_fd: int, name: str) -> None:
    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        rename_no_replace(quarantine_fd, directory_fd, name)
        return
    raise OSError


def remove_broker_entry(directory_fd: int, name: str, expected: tuple[int, int]) -> bool:
    quarantine_name, quarantine_fd, quarantine_expected = create_broker_quarantine(
        directory_fd
    )
    moved = False
    unsafe = False
    try:
        os.rename(name, name, src_dir_fd=directory_fd, dst_dir_fd=quarantine_fd)
        moved = True
        details = os.stat(name, dir_fd=quarantine_fd, follow_symlinks=False)
        if descriptor_identity(details) != expected:
            restore_broker_entry(quarantine_fd, directory_fd, name)
            moved = False
            raise OSError
        if stat.S_ISDIR(details.st_mode):
            child_fd = os.open(
                name,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=quarantine_fd,
            )
            try:
                if descriptor_identity(os.fstat(child_fd)) != expected:
                    raise OSError
                unsafe = remove_broker_contents(child_fd)
            except OSError:
                restore_broker_entry(quarantine_fd, directory_fd, name)
                moved = False
                raise
            finally:
                os.close(child_fd)
            final = os.stat(name, dir_fd=quarantine_fd, follow_symlinks=False)
            if descriptor_identity(final) != expected:
                raise OSError
            os.rmdir(name, dir_fd=quarantine_fd)
        else:
            final = os.stat(name, dir_fd=quarantine_fd, follow_symlinks=False)
            if descriptor_identity(final) != expected or final.st_nlink != 1:
                restore_broker_entry(quarantine_fd, directory_fd, name)
                moved = False
                raise OSError
            os.unlink(name, dir_fd=quarantine_fd)
        moved = False
    finally:
        if moved:
            try:
                restore_broker_entry(quarantine_fd, directory_fd, name)
            except OSError:
                unsafe = True
        os.close(quarantine_fd)
        final_quarantine = os.stat(
            quarantine_name, dir_fd=directory_fd, follow_symlinks=False
        )
        if descriptor_identity(final_quarantine) != quarantine_expected:
            raise OSError
        os.rmdir(quarantine_name, dir_fd=directory_fd)
    return unsafe


def remove_broker_contents(directory_fd: int) -> bool:
    unsafe = False
    os.fchmod(directory_fd, 0o700)
    while True:
        names = os.listdir(directory_fd)
        if not names:
            return unsafe
        if any(name.startswith(".travel-map-cleanup.") for name in names):
            return True
        name = names[0]
        details = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        unsafe = (
            remove_broker_entry(directory_fd, name, descriptor_identity(details))
            or unsafe
        )


def cleanup_broker_directory(
    path: Path,
    expected: tuple[int, int],
    parent: Path,
    prefix: str,
) -> bool:
    if path.parent != parent or not path.name.startswith(prefix):
        return False
    parent_fd = os.open(
        parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    unsafe = False
    try:
        matches = matching_identity(parent_fd, expected)
        try:
            named = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            named = None
        if named is not None and descriptor_identity(named) != expected:
            unsafe = True
        if len(matches) != 1:
            return False
        name = matches[0]
        details = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISDIR(details.st_mode)
            or stat.S_IMODE(details.st_mode) != 0o700
            or details.st_uid != os.getuid()
        ):
            return False
        unsafe = remove_broker_entry(parent_fd, name, expected) or unsafe
        return not matching_identity(parent_fd, expected) and not unsafe
    except OSError:
        return False
    finally:
        os.close(parent_fd)


def cleanup_broker_lock(
    path: Path,
    expected: tuple[int, int],
    parent_expected: tuple[int, int],
    parent_created: bool,
) -> bool:
    root = tmp_root
    public_root = Path("/tmp")
    parent_name = path.parent.name
    if path.parent.parent != public_root or path.name != git_sha:
        return False
    root_fd = os.open(
        root,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    parent_fd = None
    try:
        parent_matches = matching_identity(root_fd, parent_expected)
        if len(parent_matches) != 1:
            return False
        retained_parent = parent_matches[0]
        parent_fd = os.open(
            retained_parent,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=root_fd,
        )
        parent_details = os.fstat(parent_fd)
        if (
            descriptor_identity(parent_details) != parent_expected
            or not stat.S_ISDIR(parent_details.st_mode)
            or stat.S_IMODE(parent_details.st_mode) != 0o700
            or parent_details.st_uid != os.getuid()
        ):
            return False
        unsafe = False
        try:
            named = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            named = None
        if named is not None and descriptor_identity(named) != expected:
            unsafe = True
        matches = matching_identity(parent_fd, expected)
        if len(matches) != 1:
            return False
        retained_lock = matches[0]
        details = os.stat(retained_lock, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISDIR(details.st_mode)
            or stat.S_IMODE(details.st_mode) != 0o700
            or details.st_uid != os.getuid()
        ):
            return False
        unsafe = remove_broker_entry(parent_fd, retained_lock, expected) or unsafe
        clean = not matching_identity(parent_fd, expected) and not unsafe
        if clean and parent_created and not os.listdir(parent_fd):
            os.close(parent_fd)
            parent_fd = None
            remove_broker_entry(root_fd, retained_parent, parent_expected)
        return clean
    except OSError:
        return False
    finally:
        if parent_fd is not None:
            os.close(parent_fd)
        os.close(root_fd)


def verify_broker_docker_tool() -> None:
    fields = docker_tool_identity.split(":")
    if len(fields) != 6:
        raise OSError
    expected_uid, expected_mode, expected_dev, expected_ino, expected_size = (
        int(value) for value in fields[:5]
    )
    expected_hash = fields[5]
    tool = Path(source_docker_tool)
    descriptor = os.open(tool, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        details = os.fstat(descriptor)
        payload = bytearray()
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            payload.extend(chunk)
    finally:
        os.close(descriptor)
    path_details = tool.lstat()
    if (
        (details.st_dev, details.st_ino) != (path_details.st_dev, path_details.st_ino)
        or details.st_uid != expected_uid
        or stat.S_IMODE(details.st_mode) != expected_mode
        or details.st_dev != expected_dev
        or details.st_ino != expected_ino
        or details.st_size != expected_size
        or hashlib.sha256(payload).hexdigest() != expected_hash
    ):
        raise OSError


def cleanup_broker_tag(tagged: str) -> bool:
    verify_broker_docker_tool()
    environment = {
        "PATH": trusted_path,
        "DOCKER_CONFIG": docker_config,
        "DOCKER_HOST": docker_host,
    }
    listed = subprocess.run(
        [
            source_docker_tool,
            "image",
            "ls",
            "--quiet",
            "--no-trunc",
            "--filter",
            f"reference={tagged}",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=environment,
        check=False,
        close_fds=True,
        timeout=5,
    )
    if listed.returncode != 0:
        return False
    values = listed.stdout.splitlines()
    if not values:
        return True
    if values != [expected_image_id.encode("ascii")]:
        return False
    verify_broker_docker_tool()
    removed = subprocess.run(
        [source_docker_tool, "image", "rm", tagged],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=environment,
        check=False,
        close_fds=True,
        timeout=5,
    )
    return removed.returncode == 0


def broker_process_table() -> dict[int, tuple[int, int, int, str]]:
    for attempt in range(3):
        try:
            completed = subprocess.run(
                ["/bin/ps", "-axo", "pid=,ppid=,pgid=,lstart="],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env={"PATH": "/usr/bin:/bin"},
                check=False,
                timeout=2,
            )
            if completed.returncode != 0:
                raise OSError
            records = {}
            for line in completed.stdout.decode("ascii").splitlines():
                fields = line.split()
                if len(fields) != 8 or any(
                    re.fullmatch(r"[0-9]+", value) is None for value in fields[:3]
                ):
                    raise OSError
                pid, ppid, pgid = (int(value) for value in fields[:3])
                if pid <= 0 or ppid < 0 or pgid <= 0 or not fields[3:]:
                    raise OSError
                records[pid] = (pid, ppid, pgid, " ".join(fields[3:]))
            observing = records.get(os.getpid())
            if (
                observing is None
                or observing[0] != os.getpid()
                or observing[1] < 0
                or observing[2] <= 0
                or not observing[3]
            ):
                raise OSError
            return records
        except (OSError, UnicodeError, ValueError, subprocess.SubprocessError):
            if attempt == 2:
                raise OSError from None
            time.sleep(0.01)
    raise OSError


def broker_stable_identity(
    identity_value: tuple[int, int, int, str],
) -> tuple[int, int, int, str]:
    return identity_value


def same_broker_process(
    current: tuple[int, int, int, str] | None,
    expected: tuple[int, int, int, str],
) -> bool:
    return (
        current is not None
        and broker_stable_identity(current) == broker_stable_identity(expected)
    )


publisher_group_leader_leases: dict[int, tuple[int, int, int, str] | None] = {}


def capture_publisher_group_leader(
    group_id: int,
) -> tuple[int, int, int, str] | None:
    try:
        records = broker_process_table()
    except (OSError, UnicodeError, ValueError, subprocess.SubprocessError):
        return None
    leader = records.get(group_id)
    if (
        leader is None
        or leader[0] != group_id
        or leader[2] != group_id
    ):
        return None
    return leader


def publisher_group_leader_is_current(
    group_id: int,
    expected: tuple[int, int, int, str] | None,
) -> bool:
    if (
        expected is None
        or expected[0] != group_id
        or expected[2] != group_id
    ):
        return False
    try:
        current = broker_process_table().get(group_id)
    except (OSError, UnicodeError, ValueError, subprocess.SubprocessError):
        return False
    return same_broker_process(current, expected)


class BrokerExitObserver:
    def __init__(self, pid: int):
        self.pid = pid
        self.queue = None
        self.pidfd = None
        if sys.platform == "darwin" and hasattr(select, "kqueue"):
            self.queue = select.kqueue()
            self.queue.control(
                [
                    select.kevent(
                        pid,
                        filter=select.KQ_FILTER_PROC,
                        flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE,
                        fflags=select.KQ_NOTE_EXIT,
                    )
                ],
                0,
                0,
            )
        elif hasattr(os, "pidfd_open"):
            self.pidfd = os.pidfd_open(pid)
        else:
            raise OSError

    def has_exited(self) -> bool:
        if self.queue is not None:
            return bool(self.queue.control(None, 1, 0))
        if self.pidfd is None:
            raise OSError
        ready, _, _ = select.select([self.pidfd], [], [], 0)
        return bool(ready)

    def close(self) -> None:
        if self.queue is not None:
            self.queue.close()
            self.queue = None
        if self.pidfd is not None:
            os.close(self.pidfd)
            self.pidfd = None


def extend_broker_owned_tree(
    shell_identity: tuple[int, int, int, str],
    captured: tuple[tuple[int, int, int, str], ...],
    protected: tuple[tuple[int, int, int, str], ...],
) -> tuple[tuple[int, int, int, str], ...]:
    records = broker_process_table()
    protected_pids = {identity_value[0] for identity_value in protected}
    if shell_identity[0] in protected_pids:
        raise OSError
    retained = {
        identity_value[0]: identity_value
        for identity_value in captured
        if identity_value[0] not in protected_pids
    }
    retained[shell_identity[0]] = shell_identity
    for pid, identity_value in records.items():
        if identity_value[2] == shell_identity[2] and pid not in protected_pids:
            retained.setdefault(pid, identity_value)
    if same_broker_process(records.get(shell_identity[0]), shell_identity):
        owned = {shell_identity[0]}
        changed = True
        while changed:
            changed = False
            for pid, identity_value in records.items():
                if (
                    identity_value[1] in owned
                    and pid not in owned
                    and pid not in protected_pids
                ):
                    owned.add(pid)
                    changed = True
        for pid in owned:
            identity_value = records[pid]
            retained.setdefault(pid, identity_value)
    return tuple(retained[key] for key in sorted(retained))


def live_broker_identities(
    captured: tuple[tuple[int, int, int, str], ...],
) -> tuple[tuple[int, int, int, str], ...]:
    records = broker_process_table()
    return tuple(
        identity_value
        for identity_value in captured
        if same_broker_process(records.get(identity_value[0]), identity_value)
    )


def signal_broker_tree(
    identities: tuple[tuple[int, int, int, str], ...],
    signum: int,
) -> None:
    for identity_value in reversed(identities):
        live = broker_process_table()
        if not same_broker_process(live.get(identity_value[0]), identity_value):
            continue
        try:
            os.kill(identity_value[0], signum)
        except ProcessLookupError:
            pass


def stop_broker_shell(
    shell_identity: tuple[int, int, int, str],
    observer: BrokerExitObserver,
    protected: tuple[tuple[int, int, int, str], ...],
) -> None:
    captured = extend_broker_owned_tree(shell_identity, (), protected)
    live = live_broker_identities(captured)
    if not live:
        return
    signal_broker_tree(live, signal.SIGTERM)
    deadline = time.monotonic() + 3
    while True:
        captured = extend_broker_owned_tree(shell_identity, captured, protected)
        live = live_broker_identities(captured)
        if not live:
            break
        if time.monotonic() >= deadline:
            signal_broker_tree(live, signal.SIGKILL)
            kill_deadline = time.monotonic() + 2
            while True:
                captured = extend_broker_owned_tree(
                    shell_identity, captured, protected
                )
                live = live_broker_identities(captured)
                if not live:
                    break
                if time.monotonic() >= kill_deadline:
                    raise OSError
                time.sleep(0.01)
            break
        time.sleep(0.01)
    if not observer.has_exited() and same_broker_process(
        broker_process_table().get(shell_identity[0]), shell_identity
    ):
        raise OSError


def broker_protected_group(
    shell_identity: tuple[int, int, int, str],
) -> tuple[tuple[int, int, int, str], ...]:
    records = broker_process_table()
    protected = []
    current = os.getpid()
    while True:
        identity_value = records.get(current)
        if identity_value is None or identity_value[2] != shell_identity[2]:
            break
        protected.append(identity_value)
        if identity_value[1] == current:
            break
        current = identity_value[1]
    if (
        not protected
        or protected[0][0] != os.getpid()
        or not same_broker_process(records.get(os.getpid()), protected[0])
    ):
        raise OSError
    return tuple(protected)


def initial_broker_protected_group(
    publisher_group: int,
) -> tuple[tuple[int, int, int, str], ...]:
    records = broker_process_table()
    protected = []
    current = os.getppid()
    while True:
        identity_value = records.get(current)
        if identity_value is None or identity_value[2] != publisher_group:
            break
        protected.append(identity_value)
        if identity_value[1] == current:
            break
        current = identity_value[1]
    if not protected or protected[0][0] != os.getppid():
        raise OSError
    return tuple(protected)


def publisher_group_exists(group_id: int) -> bool:
    try:
        os.killpg(group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        raise OSError("publisher process group permission denied") from None
    return True


class IncompletePublisherGroupSnapshot(OSError):
    pass


def reap_owned_direct_children() -> None:
    while True:
        try:
            waited, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        except InterruptedError:
            continue
        if waited == 0:
            return


def reap_reparented_publisher_children(
    _signum: int | None = None,
    _frame: object | None = None,
) -> None:
    while True:
        try:
            waited, _ = os.waitpid(-publisher_process_group, os.WNOHANG)
        except ChildProcessError:
            return
        except InterruptedError:
            continue
        if waited == 0:
            return


def publisher_group_quiescent(group_id: int) -> bool:
    completed = subprocess.run(
        ["/bin/ps", "-axo", "pid=,pgid=,stat="],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        close_fds=True,
    )
    if completed.returncode != 0:
        raise OSError
    members = []
    observing = False
    group_leader = False
    try:
        for raw_line in completed.stdout.decode("ascii").splitlines():
            fields = raw_line.split(maxsplit=2)
            if len(fields) < 2:
                raise ValueError
            pid_value, group_value = (int(value) for value in fields[:2])
            if pid_value <= 0 or group_value <= 0:
                raise ValueError
            if pid_value == os.getpid():
                if len(fields) != 3 or not fields[2]:
                    raise ValueError
                observing = True
            if group_value == group_id:
                if len(fields) != 3 or not fields[2]:
                    raise ValueError
                if pid_value == group_id:
                    group_leader = True
                members.append(fields[2])
    except (UnicodeError, ValueError):
        raise OSError("publisher process snapshot was malformed") from None
    # Empty or zombie-only snapshots are not cleanup proof. Reap only direct
    # children owned here, then require kernel-confirmed group absence.
    if not observing:
        raise OSError("publisher process snapshot omitted its observer")
    if not members:
        reap_owned_direct_children()
        if not publisher_group_exists(group_id):
            return True
    elif not any(not state.startswith("Z") for state in members):
        reap_owned_direct_children()
    if not group_leader:
        raise IncompletePublisherGroupSnapshot(
            "publisher process snapshot omitted its group leader"
        )
    if any(not state.startswith("Z") for state in members):
        return False
    reap_owned_direct_children()
    if not publisher_group_exists(group_id):
        return True
    raise IncompletePublisherGroupSnapshot(
        "publisher process snapshot did not prove the publisher group gone"
    )


def wait_for_publisher_group_observation(group_id: int) -> bool | None:
    try:
        return publisher_group_quiescent(group_id)
    except IncompletePublisherGroupSnapshot:
        return None
    except (OSError, subprocess.SubprocessError):
        return None


def _emergency_stop_publisher_group(
    group_id: int,
    leader_anchor: tuple[int, int, int, str] | None,
) -> bool:
    if group_id <= 1 or group_id == os.getpgrp():
        return False

    anchor_lost = leader_anchor is None

    def observe_until_certain() -> bool:
        while True:
            observed = wait_for_publisher_group_observation(group_id)
            if observed is not None:
                return observed
            time.sleep(0.01)

    def retain_owner_until_absent() -> bool:
        while True:
            if observe_until_certain():
                return True
            time.sleep(0.01)

    def lease_is_current() -> bool:
        nonlocal anchor_lost
        if anchor_lost:
            return False
        if not publisher_group_leader_is_current(group_id, leader_anchor):
            anchor_lost = True
            return False
        return True

    try:
        observation = observe_until_certain()
        if observation:
            return True

        # A missing or changed group leader is an ownership loss, not an
        # authorization to signal a recycled process group. Keep the broker's
        # resources owned and observe until the kernel proves the group gone.
        if not lease_is_current():
            return retain_owner_until_absent()

        # Re-read both the group existence and the exact leader identity
        # immediately before each non-zero group signal.
        if not publisher_group_exists(group_id):
            return retain_owner_until_absent()
        if not lease_is_current():
            return retain_owner_until_absent()
        try:
            os.killpg(group_id, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError:
            raise OSError("publisher process group TERM failed") from None

        term_deadline = time.monotonic() + 3
        while True:
            observation = wait_for_publisher_group_observation(group_id)
            if observation is True:
                return True
            if observation is None:
                time.sleep(0.01)
                continue
            if not lease_is_current():
                return retain_owner_until_absent()
            if time.monotonic() >= term_deadline:
                break
            time.sleep(0.01)

        # A group with an unproven or recycled leader must never receive KILL.
        if not lease_is_current() or not publisher_group_exists(group_id):
            return retain_owner_until_absent()
        if not lease_is_current():
            return retain_owner_until_absent()
        try:
            os.killpg(group_id, signal.SIGKILL)
        except ProcessLookupError:
            return retain_owner_until_absent()
        except OSError:
            raise OSError("publisher process group KILL failed") from None
        while True:
            observation = wait_for_publisher_group_observation(group_id)
            if observation is True:
                return True
            time.sleep(0.01)
    except (OSError, subprocess.SubprocessError):
        return retain_owner_until_absent()


def emergency_stop_publisher_group(group_id: int) -> bool:
    return _emergency_stop_publisher_group(
        group_id,
        publisher_group_leader_leases.get(group_id),
    )


def broker_process(
    control_read: int,
    status_write: int,
    pid_read: int,
    tag_read: int,
    precreated_record_root: Path,
    precreated_record_expected: tuple[int, int],
    precreated_lock_path: Path,
    precreated_lock_expected: tuple[int, int],
    precreated_lock_parent_expected: tuple[int, int],
    precreated_lock_parent_created: bool,
) -> None:
    status = 2
    record_root = precreated_record_root
    record_expected = precreated_record_expected
    lock_path = precreated_lock_path
    lock_expected = precreated_lock_expected
    lock_parent_expected = precreated_lock_parent_expected
    lock_parent_created = precreated_lock_parent_created
    tag_armed = False
    publisher_group = os.getpgrp()
    protected_group = None
    shell_observer = None
    tagged = (
        "ghcr.io/h19h29-design/seoul-education-travel-map:"
        f"{git_sha}-sha256-{expected_image_id.removeprefix('sha256:')}"
    )
    try:
        if publisher_group <= 1 or publisher_group == os.getpid():
            raise OSError
        protected_group = initial_broker_protected_group(publisher_group)
        publisher_group_leader_leases[publisher_group] = next(
            (
                identity_value
                for identity_value in protected_group
                if identity_value[0] == publisher_group
                and identity_value[2] == publisher_group
            ),
            None,
        )
        os.setsid()
        if os.getpgrp() != os.getpid():
            raise OSError
        for handled_signal in handled_signals:
            signal.signal(handled_signal, signal.SIG_IGN)
        signal.pthread_sigmask(signal.SIG_UNBLOCK, handled_signals)
        write_broker_line(
            status_write,
            (
                f"ARM {record_root} {record_expected[0]}:{record_expected[1]} "
                f"{lock_path} {lock_expected[0]}:{lock_expected[1]} "
                f"{lock_parent_expected[0]}:{lock_parent_expected[1]} "
                f"{int(lock_parent_created)}\n"
            ).encode("ascii"),
        )
        pid_line = read_broker_line(pid_read, time.monotonic() + 90)
        if pid_line is None or re.fullmatch(rb"[0-9]+\n", pid_line) is None:
            raise OSError
        shell_pid = int(pid_line)
        shell_identity = broker_process_table().get(shell_pid)
        if shell_identity is None or shell_identity[2] != publisher_group:
            raise OSError
        shell_observer = BrokerExitObserver(shell_pid)
        request = read_broker_line(control_read, time.monotonic() + 180)
        if request not in {None, b"DONE\n", b"ABORT\n"}:
            raise OSError
        stop_broker_shell(shell_identity, shell_observer, protected_group)
        shell_observer.close()
        shell_observer = None
        tag_record = read_broker_line(tag_read, time.monotonic() + 1)
        if tag_record == b"TAG\n":
            tag_armed = True
        elif tag_record is not None:
            raise OSError
        record_clean = cleanup_broker_directory(
            record_root,
            record_expected,
            tmp_root,
            "travel-map-publish.",
        )
        lock_clean = cleanup_broker_lock(
            lock_path,
            lock_expected,
            lock_parent_expected,
            lock_parent_created,
        )
        tag_clean = not tag_armed or cleanup_broker_tag(tagged)
        if not (record_clean and lock_clean and tag_clean):
            raise OSError
        try:
            write_broker_line(status_write, b"DONE\n")
        except OSError:
            pass
        status = 0
    except (OSError, ValueError, subprocess.SubprocessError):
        if shell_observer is not None:
            try:
                shell_observer.close()
            except OSError:
                pass
        # Resource deletion is forbidden until the anchored publisher group is
        # proven empty. This path intentionally does not depend on ps/observer.
        if emergency_stop_publisher_group(publisher_group):
            if not tag_armed:
                try:
                    tag_record = read_broker_line(
                        tag_read, time.monotonic() + 0.05
                    )
                    tag_armed = tag_record == b"TAG\n"
                except OSError:
                    pass
            if tag_armed:
                try:
                    cleanup_broker_tag(tagged)
                except (OSError, subprocess.SubprocessError):
                    pass
            if record_root is not None and record_expected is not None:
                try:
                    cleanup_broker_directory(
                        record_root,
                        record_expected,
                        tmp_root,
                        "travel-map-publish.",
                    )
                except OSError:
                    pass
            if (
                lock_path is not None
                and lock_expected is not None
                and lock_parent_expected is not None
            ):
                try:
                    cleanup_broker_lock(
                        lock_path,
                        lock_expected,
                        lock_parent_expected,
                        lock_parent_created,
                    )
                except OSError:
                    pass
    finally:
        for descriptor in (control_read, status_write, pid_read, tag_read):
            try:
                os.close(descriptor)
            except OSError:
                pass
    os._exit(status)


def arm_resource_broker() -> tuple[str, str, str, str, str, str]:
    global broker_pid
    global broker_control_write
    global broker_status_read
    global broker_pid_write
    global broker_tag_write
    global broker_fallback_tag_read
    global broker_fallback_tag_write
    global broker_record_root
    global broker_record_identity
    global broker_lock_path
    global broker_lock_identity
    global broker_lock_parent_identity
    global broker_lock_parent_created
    record_root = None
    record_expected = None
    lock_path = None
    lock_expected = None
    lock_parent_expected = None
    lock_parent_created = False
    try:
        # The supervisor creates and identity-binds both resources before fork.
        # Their metadata remains available to fallback cleanup if the broker
        # exits before it can publish ARM.
        record_root, record_expected = create_broker_directory(
            tmp_root, "travel-map-publish."
        )
        (
            lock_path,
            lock_expected,
            lock_parent_expected,
            lock_parent_created,
        ) = create_broker_lock()
    except CanonicalLockLeafCollision:
        if (
            record_root is None
            or record_expected is None
            or not cleanup_broker_directory(
                record_root,
                record_expected,
                tmp_root,
                "travel-map-publish.",
            )
            or not fallback_identity_absent(tmp_root, record_expected)
        ):
            raise OSError from None
        raise
    except OSError:
        if record_root is not None and record_expected is not None:
            try:
                cleanup_broker_directory(
                    record_root,
                    record_expected,
                    tmp_root,
                    "travel-map-publish.",
                )
            except OSError:
                pass
        raise
    if (
        lock_path is None
        or lock_expected is None
        or lock_parent_expected is None
    ):
        raise OSError
    broker_record_root = str(record_root)
    broker_record_identity = f"{record_expected[0]}:{record_expected[1]}"
    broker_lock_path = str(lock_path)
    broker_lock_identity = f"{lock_expected[0]}:{lock_expected[1]}"
    broker_lock_parent_identity = (
        f"{lock_parent_expected[0]}:{lock_parent_expected[1]}"
    )
    broker_lock_parent_created = "1" if lock_parent_created else "0"
    control_read, control_write = os.pipe()
    status_read, status_write = os.pipe()
    pid_read, pid_write = os.pipe()
    tag_read, tag_write = os.pipe()
    fallback_tag_read, fallback_tag_write = os.pipe()
    broker_pid = os.fork()
    if broker_pid == 0:
        for descriptor in (
            control_write,
            status_read,
            pid_write,
            tag_write,
            fallback_tag_read,
            fallback_tag_write,
        ):
            os.close(descriptor)
        broker_process(
            control_read,
            status_write,
            pid_read,
            tag_read,
            record_root,
            record_expected,
            lock_path,
            lock_expected,
            lock_parent_expected,
            lock_parent_created,
        )
        os._exit(2)
    for descriptor in (control_read, status_write, pid_read, tag_read):
        os.close(descriptor)
    broker_control_write = control_write
    broker_status_read = status_read
    broker_pid_write = pid_write
    broker_tag_write = tag_write
    broker_fallback_tag_read = fallback_tag_read
    broker_fallback_tag_write = fallback_tag_write
    arm = read_broker_line(status_read, time.monotonic() + 10)
    if arm is None:
        raise OSError
    fields = arm.decode("ascii").rstrip("\n").split(" ")
    if (
        len(fields) != 7
        or fields[0] != "ARM"
        or re.fullmatch(r"[0-9]+:[0-9]+", fields[2]) is None
        or re.fullmatch(r"[0-9]+:[0-9]+", fields[4]) is None
        or re.fullmatch(r"[0-9]+:[0-9]+", fields[5]) is None
        or fields[6] not in {"0", "1"}
    ):
        raise OSError
    return fields[1], fields[2], fields[3], fields[4], fields[5], fields[6]


def poll_resource_broker() -> bool:
    global broker_exit_status
    if broker_pid is None:
        return True
    if broker_exit_status is not None:
        return True
    try:
        waited, raw_status = os.waitpid(broker_pid, os.WNOHANG)
    except ChildProcessError:
        broker_exit_status = 2
        return True
    if waited == broker_pid:
        broker_exit_status = os.waitstatus_to_exitcode(raw_status)
        return True
    return False


def finish_resource_broker(request: bytes) -> bool:
    global broker_control_write
    global broker_status_read
    global broker_pid_write
    global broker_tag_write
    global broker_fallback_tag_write
    global broker_exit_status
    if broker_pid_write is not None:
        try:
            os.close(broker_pid_write)
        except OSError:
            pass
        broker_pid_write = None
    if broker_tag_write is not None:
        try:
            os.close(broker_tag_write)
        except OSError:
            pass
        broker_tag_write = None
    if broker_fallback_tag_write is not None:
        try:
            os.close(broker_fallback_tag_write)
        except OSError:
            pass
        broker_fallback_tag_write = None
    if broker_control_write is not None:
        try:
            write_broker_line(broker_control_write, request)
        except OSError:
            pass
        try:
            os.close(broker_control_write)
        except OSError:
            pass
        broker_control_write = None
    terminal = None
    if broker_status_read is not None:
        try:
            terminal = read_broker_line(
                broker_status_read,
                time.monotonic() + 40,
            )
        except OSError:
            terminal = None
        try:
            os.close(broker_status_read)
        except OSError:
            pass
        broker_status_read = None
    if broker_pid is not None and broker_exit_status is None:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                waited, raw_status = os.waitpid(broker_pid, os.WNOHANG)
            except ChildProcessError:
                if broker_exit_status is None:
                    broker_exit_status = 2
                break
            if waited == broker_pid:
                broker_exit_status = os.waitstatus_to_exitcode(raw_status)
                break
            time.sleep(0.01)
    if broker_pid is not None and broker_exit_status is None:
        try:
            os.kill(broker_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            waited, raw_status = os.waitpid(broker_pid, 0)
            if waited == broker_pid:
                broker_exit_status = os.waitstatus_to_exitcode(raw_status)
        except ChildProcessError:
            broker_exit_status = 2
    return terminal == b"DONE\n" and broker_exit_status == 0


def close_fallback_tag_reader() -> None:
    global broker_fallback_tag_read
    if broker_fallback_tag_read is not None:
        try:
            os.close(broker_fallback_tag_read)
        except OSError:
            pass
        broker_fallback_tag_read = None


def fallback_identity_absent(parent: Path, expected: tuple[int, int]) -> bool:
    descriptor = os.open(
        parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        return not matching_identity(descriptor, expected)
    finally:
        os.close(descriptor)


def fallback_lock_absent(
    parent_expected: tuple[int, int],
    lock_expected: tuple[int, int],
) -> bool:
    root_fd = os.open(
        tmp_root,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        parent_matches = matching_identity(root_fd, parent_expected)
        if not parent_matches:
            return True
        if len(parent_matches) != 1:
            return False
        parent_fd = os.open(
            parent_matches[0],
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=root_fd,
        )
        try:
            return not matching_identity(parent_fd, lock_expected)
        finally:
            os.close(parent_fd)
    finally:
        os.close(root_fd)


def fallback_resource_cleanup() -> bool:
    global broker_fallback_tag_read
    publisher_group = os.getpgrp()
    if publisher_group <= 1 or publisher_group == os.getpid():
        return False
    publisher_group_leader_leases[publisher_group] = capture_publisher_group_leader(
        publisher_group
    )
    try:
        os.setsid()
    except OSError:
        return False
    for handled_signal in handled_signals:
        signal.signal(handled_signal, signal.SIG_IGN)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, handled_signals)
    if not emergency_stop_publisher_group(publisher_group):
        return False
    tag_armed = False
    try:
        if broker_fallback_tag_read is not None:
            tag_record = read_broker_line(
                broker_fallback_tag_read, time.monotonic() + 1
            )
            if tag_record == b"TAG\n":
                tag_armed = True
            elif tag_record is not None:
                return False
    except OSError:
        return False
    finally:
        close_fallback_tag_reader()
    try:
        record_expected = tuple(
            int(value) for value in broker_record_identity.split(":")
        )
        lock_expected = tuple(
            int(value) for value in broker_lock_identity.split(":")
        )
        lock_parent_expected = tuple(
            int(value) for value in broker_lock_parent_identity.split(":")
        )
        parent_created = broker_lock_parent_created == "1"
        if (
            len(record_expected) != 2
            or len(lock_expected) != 2
            or len(lock_parent_expected) != 2
            or broker_lock_parent_created not in {"0", "1"}
        ):
            return False
        cleanup_broker_directory(
            Path(broker_record_root),
            record_expected,
            tmp_root,
            "travel-map-publish.",
        )
        cleanup_broker_lock(
            Path(broker_lock_path),
            lock_expected,
            lock_parent_expected,
            parent_created,
        )
        tagged = (
            "ghcr.io/h19h29-design/seoul-education-travel-map:"
            f"{git_sha}-sha256-{expected_image_id.removeprefix('sha256:')}"
        )
        tag_clean = not tag_armed or cleanup_broker_tag(tagged)
        return (
            fallback_identity_absent(tmp_root, record_expected)
            and fallback_lock_absent(lock_parent_expected, lock_expected)
            and tag_clean
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


for handled_signal in handled_signals:
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
    "TRAVEL_MAP_PUBLISH_LAUNCHER_IDENTITY": launcher_root_identity,
    "TRAVEL_MAP_PUBLISH_REPOSITORY": publisher_repository,
    "TRAVEL_MAP_PUBLISH_LAUNCHER_SHA256": publisher_launcher_hash,
}
status = 2
failure_message: str | None = None
canonical_lock_blocked = False
captured = bytearray()
broker_record_root = ""
broker_record_identity = ""
broker_lock_path = ""
broker_lock_identity = ""
broker_lock_parent_identity = ""
broker_lock_parent_created = ""
try:
    if interrupted:
        raise OSError
    (
        broker_record_root,
        broker_record_identity,
        broker_lock_path,
        broker_lock_identity,
        broker_lock_parent_identity,
        broker_lock_parent_created,
    ) = arm_resource_broker()
    script_payload = read_verified_launcher_script()
    environment["TRAVEL_MAP_PUBLISH_RECORD_ROOT"] = broker_record_root
    environment["TRAVEL_MAP_PUBLISH_RECORD_IDENTITY"] = broker_record_identity
    environment["TRAVEL_MAP_PUBLISH_LOCK_DIRECTORY"] = broker_lock_path
    environment["TRAVEL_MAP_PUBLISH_LOCK_IDENTITY"] = broker_lock_identity
    environment["TRAVEL_MAP_PUBLISH_LOCK_PARENT_IDENTITY"] = (
        broker_lock_parent_identity
    )
    if (
        broker_pid_write is None
        or broker_tag_write is None
        or broker_fallback_tag_write is None
    ):
        raise OSError
    environment["TRAVEL_MAP_PUBLISH_TAG_ARM_FD"] = str(broker_tag_write)
    environment["TRAVEL_MAP_PUBLISH_FALLBACK_TAG_ARM_FD"] = str(
        broker_fallback_tag_write
    )
    process = subprocess.Popen(
        [
            "/usr/bin/python3",
            "-I",
            "-S",
            "-c",
            STAGE_B_CHILD_LAUNCHER,
            str(broker_pid_write),
            "/bin/sh",
            "-c",
            'script=$(cat) || exit 2; eval "$script"',
            script,
            record,
            expected_image_tag,
            expected_image_id,
            nas_platform,
            git_sha,
            expected_record_sha256,
        ],
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        pass_fds=(
            broker_pid_write,
            broker_tag_write,
            broker_fallback_tag_write,
        ),
        close_fds=True,
    )
    os.close(broker_pid_write)
    broker_pid_write = None
    os.close(broker_tag_write)
    broker_tag_write = None
    os.close(broker_fallback_tag_write)
    broker_fallback_tag_write = None
    if process.stdout is None or process.stdin is None:
        raise OSError
    os.set_blocking(process.stdout.fileno(), False)
    script_input = process.stdin.fileno()
    os.set_blocking(script_input, False)
    payload_view = memoryview(script_payload)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, handled_signals)
    while True:
        drain_stdout(captured)
        if poll_resource_broker():
            raise OSError
        if pending_signal and termination_deadline is None:
            interrupted = True
            signal_received_at = time.monotonic()
            termination_deadline = time.monotonic() + 5
        if pending_signal and not signal_forwarded:
            try:
                os.kill(process.pid, pending_signal)
            except ProcessLookupError:
                pass
            signal_forwarded = True
        if payload_view:
            try:
                written = os.write(script_input, payload_view)
                if written <= 0:
                    raise OSError
                payload_view = payload_view[written:]
            except BlockingIOError:
                pass
            except BrokenPipeError:
                raise OSError from None
        elif script_input is not None:
            process.stdin.close()
            script_input = None
        polled = process.poll()
        if polled is not None:
            if payload_view:
                raise OSError
            status = polled
            break
        if termination_deadline is not None and time.monotonic() >= termination_deadline:
            try:
                os.kill(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            status = process.wait()
            break
        time.sleep(0.01)
    reap_process()
    drain_stdout(captured)
except CanonicalLockLeafCollision:
    canonical_lock_blocked = True
except OSError:
    if process is not None:
        try:
            if script_input is not None and process.stdin is not None:
                process.stdin.close()
                script_input = None
            reap_process()
            if process.poll() is None:
                process.wait(timeout=1)
        except (OSError, subprocess.SubprocessError):
            try:
                os.kill(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                if process.poll() is None:
                    process.wait(timeout=1)
            except subprocess.SubprocessError:
                pass
    failure_message = "BLOCKED_UNSAFE_RELEASE_ENVIRONMENT"
finally:
    if script_input is not None:
        try:
            if process is not None and process.stdin is not None:
                process.stdin.close()
        except OSError:
            pass

if canonical_lock_blocked:
    print("BLOCKED_PUBLISH_LOCKED", file=sys.stderr)
    raise SystemExit(2)

if failure_message is None and not interrupted and status == 0:
    broker_request = b"DONE\n"
else:
    broker_request = b"ABORT\n"
previous_sigchld = None
if subreaper_enabled:
    previous_sigchld = signal.signal(
        signal.SIGCHLD,
        reap_reparented_publisher_children,
    )
    reap_reparented_publisher_children()
try:
    broker_clean = finish_resource_broker(broker_request)
finally:
    if previous_sigchld is not None:
        signal.signal(signal.SIGCHLD, previous_sigchld)
        reap_reparented_publisher_children()
if broker_clean:
    close_fallback_tag_reader()
else:
    fallback_resource_cleanup()
    raise SystemExit(2)

if failure_message is not None:
    print(failure_message, file=sys.stderr)
    raise SystemExit(2)
if interrupted or status < 0:
    if signal_received_at is not None and status != 2:
        while time.monotonic() < signal_received_at + 5:
            time.sleep(0.01)
    raise SystemExit(2)
if status == 0:
    try:
        output = validated_stdout(bytes(captured))
        signal.pthread_sigmask(
            signal.SIG_BLOCK,
            {signal.SIGHUP, signal.SIGINT, signal.SIGTERM},
        )
        pending_before_write = signal.sigpending() & handled_signals
        if (
            pending_signal
            or interrupted
            or pending_before_write
        ):
            raise OSError
        output_details = os.fstat(1)
        output_pipe_buf = os.fpathconf(1, "PC_PIPE_BUF")
        if (
            not stat.S_ISFIFO(output_details.st_mode)
            or len(output) > output_pipe_buf
        ):
            raise OSError
        for handled_signal in handled_signals:
            signal.signal(handled_signal, signal.SIG_DFL)
        signal.pthread_sigmask(signal.SIG_UNBLOCK, handled_signals)
        if os.write(1, output) != len(output):
            raise OSError
    except OSError:
        print("BLOCKED_INVALID_PUBLISH_OUTPUT", file=sys.stderr)
        raise SystemExit(2) from None
raise SystemExit(status)
PY
        supervisor_pid=$!
        supervisor_status=0
        wait "$supervisor_pid" || supervisor_status=$?
        supervisor_pid=
        exit "$supervisor_status"
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
    "$TRAVEL_MAP_PUBLISH_LAUNCHER_IDENTITY" \
    "$TRAVEL_MAP_PUBLISH_REPOSITORY" \
    "$TRAVEL_MAP_PUBLISH_LAUNCHER_SHA256" \
    "$TRAVEL_MAP_PUBLISH_RECORD_ROOT" \
    "$TRAVEL_MAP_PUBLISH_RECORD_IDENTITY" \
    "$TRAVEL_MAP_PUBLISH_LOCK_DIRECTORY" \
    "$TRAVEL_MAP_PUBLISH_LOCK_IDENTITY" \
    "$TRAVEL_MAP_PUBLISH_LOCK_PARENT_IDENTITY" \
    "$TRAVEL_MAP_PUBLISH_TAG_ARM_FD" \
    "$TRAVEL_MAP_PUBLISH_FALLBACK_TAG_ARM_FD" <<'PY' \
    || blocked 'BLOCKED_UNSAFE_RELEASE_ENVIRONMENT'
import os
import re
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
    "TRAVEL_MAP_PUBLISH_LAUNCHER_IDENTITY": sys.argv[11],
    "TRAVEL_MAP_PUBLISH_REPOSITORY": sys.argv[12],
    "TRAVEL_MAP_PUBLISH_LAUNCHER_SHA256": sys.argv[13],
    "TRAVEL_MAP_PUBLISH_RECORD_ROOT": sys.argv[14],
    "TRAVEL_MAP_PUBLISH_RECORD_IDENTITY": sys.argv[15],
    "TRAVEL_MAP_PUBLISH_LOCK_DIRECTORY": sys.argv[16],
    "TRAVEL_MAP_PUBLISH_LOCK_IDENTITY": sys.argv[17],
    "TRAVEL_MAP_PUBLISH_LOCK_PARENT_IDENTITY": sys.argv[18],
    "TRAVEL_MAP_PUBLISH_TAG_ARM_FD": sys.argv[19],
    "TRAVEL_MAP_PUBLISH_FALLBACK_TAG_ARM_FD": sys.argv[20],
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
    record_root = Path(expected["TRAVEL_MAP_PUBLISH_RECORD_ROOT"])
    record_identity = tuple(
        int(value)
        for value in expected["TRAVEL_MAP_PUBLISH_RECORD_IDENTITY"].split(":")
    )
    record_details = record_root.lstat()
    if (
        len(record_identity) != 2
        or not record_root.is_absolute()
        or record_root.resolve(strict=True) != record_root
        or record_root.parent != Path(expected["TMPDIR"])
        or not record_root.name.startswith("travel-map-publish.")
        or (record_details.st_dev, record_details.st_ino) != record_identity
        or not stat.S_ISDIR(record_details.st_mode)
        or stat.S_IMODE(record_details.st_mode) != 0o700
        or record_details.st_uid != os.getuid()
        or any(record_root.iterdir())
    ):
        raise ValueError
    lock_directory = Path(expected["TRAVEL_MAP_PUBLISH_LOCK_DIRECTORY"])
    lock_identity = tuple(
        int(value)
        for value in expected["TRAVEL_MAP_PUBLISH_LOCK_IDENTITY"].split(":")
    )
    lock_parent_identity = tuple(
        int(value)
        for value in expected[
            "TRAVEL_MAP_PUBLISH_LOCK_PARENT_IDENTITY"
        ].split(":")
    )
    lock_parent = Path(f"/tmp/travel-map-publish-locks-{os.getuid()}")
    lock_parent_details = lock_parent.lstat()
    lock_details = lock_directory.lstat()
    if (
        len(lock_identity) != 2
        or len(lock_parent_identity) != 2
        or lock_directory.parent != lock_parent
        or re.fullmatch(r"[0-9a-f]{40}", lock_directory.name) is None
        or lock_parent.is_symlink()
        or not stat.S_ISDIR(lock_parent_details.st_mode)
        or (lock_parent_details.st_dev, lock_parent_details.st_ino)
        != lock_parent_identity
        or stat.S_IMODE(lock_parent_details.st_mode) != 0o700
        or lock_parent_details.st_uid != os.getuid()
        or lock_directory.is_symlink()
        or (lock_details.st_dev, lock_details.st_ino) != lock_identity
        or not stat.S_ISDIR(lock_details.st_mode)
        or stat.S_IMODE(lock_details.st_mode) != 0o700
        or lock_details.st_uid != os.getuid()
        or any(lock_directory.iterdir())
    ):
        raise ValueError
    tag_arm_fd = int(expected["TRAVEL_MAP_PUBLISH_TAG_ARM_FD"])
    fallback_tag_arm_fd = int(
        expected["TRAVEL_MAP_PUBLISH_FALLBACK_TAG_ARM_FD"]
    )
    if (
        tag_arm_fd <= 2
        or fallback_tag_arm_fd <= 2
        or fallback_tag_arm_fd == tag_arm_fd
        or not stat.S_ISFIFO(os.fstat(tag_arm_fd).st_mode)
        or not stat.S_ISFIFO(os.fstat(fallback_tag_arm_fd).st_mode)
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
record_parent=$TRAVEL_MAP_PUBLISH_RECORD_ROOT
record_parent_identity=$TRAVEL_MAP_PUBLISH_RECORD_IDENTITY
lock_directory=$TRAVEL_MAP_PUBLISH_LOCK_DIRECTORY
lock_directory_identity=$TRAVEL_MAP_PUBLISH_LOCK_IDENTITY
lock_parent_identity=$TRAVEL_MAP_PUBLISH_LOCK_PARENT_IDENTITY
tag_arm_fd=$TRAVEL_MAP_PUBLISH_TAG_ARM_FD
fallback_tag_arm_fd=$TRAVEL_MAP_PUBLISH_FALLBACK_TAG_ARM_FD
unset DOCKER_CONFIG TRAVEL_MAP_PUBLISH_DOCKER_HOST \
    TRAVEL_MAP_PUBLISH_DOCKER_AUTHORITY_IDENTITY \
    TRAVEL_MAP_PUBLISH_DOCKER_IDENTITY TRAVEL_MAP_PUBLISH_BUILDX_IDENTITY \
    TRAVEL_MAP_PUBLISH_SOURCE_DOCKER_TOOL \
    TRAVEL_MAP_PUBLISH_SOURCE_BUILDX_TOOL \
    TRAVEL_MAP_PUBLISH_RECORD_ROOT TRAVEL_MAP_PUBLISH_RECORD_IDENTITY \
    TRAVEL_MAP_PUBLISH_LOCK_DIRECTORY TRAVEL_MAP_PUBLISH_LOCK_IDENTITY \
    TRAVEL_MAP_PUBLISH_LOCK_PARENT_IDENTITY \
    TRAVEL_MAP_PUBLISH_TAG_ARM_FD \
    TRAVEL_MAP_PUBLISH_FALLBACK_TAG_ARM_FD

[ "$(validate_publisher_docker_authority \
    "$publisher_docker_config" "$publisher_docker_host" \
    "$publisher_docker_authority_identity")" \
    = "$publisher_docker_host $publisher_docker_authority_identity" ] \
    || blocked 'BLOCKED_INVALID_DOCKER_CONFIG'

trap 'exit 2' HUP INT TERM
[ -n "$record_parent" ] && [ -n "$record_parent_identity" ] \
    || blocked 'BLOCKED_PRIVATE_PUBLISH_DIRECTORY'
validate_owned_private_directory \
    "$record_parent" "$record_parent_identity" "$TMPDIR" travel-map-publish. \
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

arm_owned_tag() {
    [ "$tag_arm_fd" = 8 ] || return 1
    [ "$fallback_tag_arm_fd" = 9 ] || return 1
    case "$tag_arm_fd" in
        ''|*[!0-9]*) return 1 ;;
    esac
    case "$fallback_tag_arm_fd" in
        ''|*[!0-9]*) return 1 ;;
    esac
    /usr/bin/python3 -I -S - \
        "$fallback_tag_arm_fd" "$tag_arm_fd" <<'PY' || return 1
import os
import stat
import sys

try:
    descriptors = tuple(int(value) for value in sys.argv[1:])
    if len(descriptors) != 2 or len(set(descriptors)) != 2:
        raise OSError
    for descriptor in descriptors:
        if descriptor <= 2 or not stat.S_ISFIFO(os.fstat(descriptor).st_mode):
            raise OSError
        if os.write(descriptor, b"TAG\n") != 4:
            raise OSError
except (OSError, ValueError):
    raise SystemExit(2) from None
PY
    exec 9>&-
    fallback_tag_arm_fd=
    exec 8>&-
    tag_arm_fd=
}

cleanup_publish() {
    status=$?
    cleanup_failed=0
    trap - EXIT HUP INT TERM

    if [ "$owns_tagged" -eq 1 ] && [ -n "$tagged" ]; then
        owns_tagged=0
    fi
    if [ -n "$record_parent" ]; then
        :
    fi
    if [ "$owns_lock" -eq 1 ] && [ -n "$lock_directory" ]; then
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

validate_private_directory "$lock_parent" \
    || blocked 'BLOCKED_PRIVATE_PUBLISH_DIRECTORY'
validate_owned_private_directory \
    "$lock_directory" "$lock_directory_identity" "$lock_parent" "$git_sha" \
    || blocked 'BLOCKED_PRIVATE_PUBLISH_DIRECTORY'

validate_publish_lock_authority() {
    /usr/bin/python3 -I -S - \
        "$lock_parent" "$lock_parent_identity" \
        "$lock_directory" "$lock_directory_identity" "$git_sha" <<'PY'
import os
import stat
import sys
from pathlib import Path

try:
    public_parent = Path(sys.argv[1])
    parent_expected = tuple(int(value) for value in sys.argv[2].split(":"))
    lock_path = Path(sys.argv[3])
    lock_expected = tuple(int(value) for value in sys.argv[4].split(":"))
    expected_name = sys.argv[5]
    public_root = Path("/tmp")
    root = public_root.resolve(strict=True)
    if (
        len(parent_expected) != 2
        or len(lock_expected) != 2
        or public_parent.parent != public_root
        or public_parent.name != f"travel-map-publish-locks-{os.getuid()}"
        or lock_path != public_parent / expected_name
    ):
        raise OSError
    root_fd = os.open(
        root,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        parent_path_details = os.stat(
            public_parent.name, dir_fd=root_fd, follow_symlinks=False
        )
        parent_fd = os.open(
            public_parent.name,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=root_fd,
        )
        try:
            parent_details = os.fstat(parent_fd)
            if (
                (parent_details.st_dev, parent_details.st_ino)
                != parent_expected
                or (parent_path_details.st_dev, parent_path_details.st_ino)
                != parent_expected
                or not stat.S_ISDIR(parent_details.st_mode)
                or stat.S_IMODE(parent_details.st_mode) != 0o700
                or parent_details.st_uid != os.getuid()
            ):
                raise OSError
            lock_path_details = os.stat(
                expected_name, dir_fd=parent_fd, follow_symlinks=False
            )
            lock_fd = os.open(
                expected_name,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_fd,
            )
            try:
                lock_details = os.fstat(lock_fd)
                if (
                    (lock_details.st_dev, lock_details.st_ino) != lock_expected
                    or (
                        lock_path_details.st_dev,
                        lock_path_details.st_ino,
                    )
                    != lock_expected
                    or not stat.S_ISDIR(lock_details.st_mode)
                    or stat.S_IMODE(lock_details.st_mode) != 0o700
                    or lock_details.st_uid != os.getuid()
                    or os.listdir(lock_fd)
                ):
                    raise OSError
            finally:
                os.close(lock_fd)
        finally:
            os.close(parent_fd)
    finally:
        os.close(root_fd)
except (OSError, ValueError):
    raise SystemExit(2) from None
PY
}

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
    validate_publish_lock_authority \
        || blocked 'BLOCKED_PUBLISH_LOCK_AUTHORITY'
    arm_owned_tag || blocked 'BLOCKED_IMAGE_TAGGING'
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
