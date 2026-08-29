"""Validate and extract one exact Git tree from an archive and ls-tree record."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import stat
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

_OBJECT_ID = re.compile(r"[0-9a-f]{40}")
_GIT_MODES = frozenset({"100644", "100755"})


@dataclass(frozen=True)
class TreeEntry:
    mode: str
    object_id: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--tree", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    return parser.parse_args()


def _safe_path(raw_path: str) -> PurePosixPath:
    if (
        not raw_path
        or raw_path.startswith("/")
        or raw_path.endswith("/")
        or any(part in {"", ".", "..", ".git"} for part in raw_path.split("/"))
    ):
        raise ValueError("unsafe path")
    path = PurePosixPath(raw_path)
    if path.is_absolute() or path.as_posix() != raw_path:
        raise ValueError("non-canonical path")
    return path


def _read_tree(path: Path) -> dict[str, TreeEntry]:
    payload = path.read_bytes()
    if not payload or not payload.endswith(b"\0"):
        raise ValueError("invalid tree framing")
    entries: dict[str, TreeEntry] = {}
    for record in payload[:-1].split(b"\0"):
        header, separator, raw_path = record.partition(b"\t")
        if not separator:
            raise ValueError("invalid tree record")
        fields = header.split(b" ")
        if len(fields) != 3:
            raise ValueError("invalid tree header")
        mode_bytes, object_type, object_id_bytes = fields
        mode = mode_bytes.decode("ascii")
        object_id = object_id_bytes.decode("ascii")
        tree_path = raw_path.decode("utf-8")
        _safe_path(tree_path)
        if (
            mode not in _GIT_MODES
            or object_type != b"blob"
            or _OBJECT_ID.fullmatch(object_id) is None
            or tree_path in entries
        ):
            raise ValueError("unsupported tree entry")
        entries[tree_path] = TreeEntry(mode=mode, object_id=object_id)
    return entries


def _git_blob_id(payload: bytes) -> str:
    header = f"blob {len(payload)}\0".encode("ascii")
    return hashlib.sha1(header + payload, usedforsecurity=False).hexdigest()


def _expected_directories(paths: set[str]) -> set[str]:
    directories: set[str] = set()
    for raw_path in paths:
        parent = PurePosixPath(raw_path).parent
        while parent != PurePosixPath("."):
            directories.add(parent.as_posix())
            parent = parent.parent
    return directories


def _raw_header_path(archive_path: Path, member: tarfile.TarInfo) -> str:
    pax_path = member.pax_headers.get("path")
    if pax_path is not None:
        return pax_path
    with archive_path.open("rb") as source:
        source.seek(member.offset_data - tarfile.BLOCKSIZE)
        header = source.read(tarfile.BLOCKSIZE)
    if len(header) != tarfile.BLOCKSIZE:
        raise ValueError("truncated archive header")
    name_bytes = header[:100].split(b"\0", 1)[0]
    prefix_bytes = header[345:500].split(b"\0", 1)[0]
    if not name_bytes:
        raise ValueError("missing archive path")
    raw_path = name_bytes.decode("utf-8")
    if prefix_bytes:
        raw_path = f"{prefix_bytes.decode('utf-8')}/{raw_path}"
    return raw_path


def _validated_payloads(
    archive_path: Path,
    entries: dict[str, TreeEntry],
) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    directories: set[str] = set()
    with tarfile.open(archive_path, mode="r:") as archive:
        for member in archive:
            if member.isdir():
                archived_path = _raw_header_path(archive_path, member)
                if not archived_path.endswith("/") or archived_path.endswith("//"):
                    raise ValueError("non-canonical directory path")
                raw_path = archived_path[:-1]
                if member.name != raw_path:
                    raise ValueError("normalized directory path mismatch")
            else:
                raw_path = member.name
            _safe_path(raw_path)
            if raw_path in files or raw_path in directories:
                raise ValueError("duplicate archive entry")
            if member.isdir():
                if member.mode != 0o775:
                    raise ValueError("directory mode mismatch")
                directories.add(raw_path)
                continue
            if not member.isreg():
                raise ValueError("unsupported archive entry")
            entry = entries.get(raw_path)
            if entry is None:
                raise ValueError("archive path not in tree")
            expected_archive_mode = 0o775 if entry.mode == "100755" else 0o664
            if member.mode != expected_archive_mode:
                raise ValueError("file mode mismatch")
            source = archive.extractfile(member)
            if source is None:
                raise ValueError("missing archive payload")
            payload = source.read()
            if len(payload) != member.size or _git_blob_id(payload) != entry.object_id:
                raise ValueError("blob mismatch")
            files[raw_path] = payload
    expected_files = set(entries)
    if set(files) != expected_files:
        raise ValueError("file set mismatch")
    if directories != _expected_directories(expected_files):
        raise ValueError("directory set mismatch")
    return files


def _validate_input(path: Path) -> None:
    details = path.lstat()
    if not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid():
        raise ValueError("invalid input")


def materialize(archive: Path, tree: Path, destination: Path) -> None:
    _validate_input(archive)
    _validate_input(tree)
    if destination.exists() or destination.is_symlink():
        raise ValueError("destination already exists")
    parent_details = destination.parent.lstat()
    if (
        not stat.S_ISDIR(parent_details.st_mode)
        or stat.S_IMODE(parent_details.st_mode) != 0o700
        or parent_details.st_uid != os.getuid()
    ):
        raise ValueError("destination parent is not private")
    entries = _read_tree(tree)
    payloads = _validated_payloads(archive, entries)
    destination.mkdir(mode=0o700)
    try:
        for raw_path in sorted(
            _expected_directories(set(entries)), key=lambda p: p.count("/")
        ):
            (destination / raw_path).mkdir(mode=0o700)
        for raw_path, entry in entries.items():
            output = destination / raw_path
            descriptor = os.open(
                output,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o755 if entry.mode == "100755" else 0o644,
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payloads[raw_path])
                stream.flush()
                os.fsync(stream.fileno())
            output.chmod(0o755 if entry.mode == "100755" else 0o644)
    except Exception:
        shutil.rmtree(destination)
        raise


def main() -> int:
    args = parse_args()
    try:
        materialize(args.archive, args.tree, args.destination)
    except (OSError, UnicodeError, ValueError, tarfile.TarError):
        print("BLOCKED_INVALID_PINNED_SOURCE", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
