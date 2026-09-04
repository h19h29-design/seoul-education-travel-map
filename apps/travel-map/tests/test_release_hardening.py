import hashlib
import io
import json
import os
import pty
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tarfile
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.test_release import _image_manifest, _run_publish_reviewed_image

ROOT = Path("apps/travel-map")
MATERIALIZE = ROOT / "scripts/materialize-pinned-source.py"
SAFE_BLOB = "b7767f67d3494c9c8df2deaac7129f48ce33d2f7"
SCRIPT_BLOB = "1a2485251c33a70432394c93fb89330ef214bfc9"
TRUSTED_PATH = "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
TOOL_SEARCH_PATH_ASSIGNMENT = (
    "tool_search_path=$canonical_home/.local/bin:/opt/homebrew/bin:/usr/local/bin:"
    "$trusted_path"
)
UV_CACHE_ASSIGNMENT = "uv_cache=$canonical_home/.cache/uv"
PLAYWRIGHT_CACHE_ASSIGNMENT = (
    "playwright_cache=$canonical_home/Library/Caches/ms-playwright"
)
PNPM_STORE_ASSIGNMENT = "pnpm_store=$canonical_home/Library/pnpm/store/v10"
_TEST_SOCKETS: dict[Path, socket.socket | None] = {}


def _release_test_socket_path(tmp_path: Path, suffix: str = "") -> Path:
    digest = hashlib.sha256(str(tmp_path).encode("utf-8")).hexdigest()[:12]
    path = Path("/private/tmp") / f"tm-release-{os.getpid()}-{digest}{suffix}.sock"
    if path not in _TEST_SOCKETS:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(path))
            path.chmod(0o600)
            listener.listen(1)
        except BaseException:
            listener.close()
            path.unlink(missing_ok=True)
            raise
        _TEST_SOCKETS[path] = listener
    return path


def _close_release_test_socket(path: Path) -> None:
    listener = _TEST_SOCKETS.get(path)
    if listener is not None:
        listener.close()
        _TEST_SOCKETS[path] = None
    path.unlink(missing_ok=True)


@pytest.fixture(autouse=True)
def _cleanup_release_test_sockets():
    yield
    for path, listener in tuple(_TEST_SOCKETS.items()):
        if listener is not None:
            listener.close()
        try:
            details = path.lstat()
        except FileNotFoundError:
            _TEST_SOCKETS.pop(path, None)
            continue
        if (
            path.parent == Path("/private/tmp")
            and path.name.startswith("tm-release-")
            and (
                stat.S_ISREG(details.st_mode)
                or stat.S_ISSOCK(details.st_mode)
                or path.is_symlink()
            )
        ):
            path.unlink()
            _TEST_SOCKETS.pop(path, None)


def _tree_entry(mode: str, digest: str, path: str) -> bytes:
    return f"{mode} blob {digest}\t{path}".encode() + b"\0"


def _write_archive(
    path: Path,
    entries: list[tuple[str, bytes | None, int, bytes | None, str]],
) -> None:
    with tarfile.open(path, "w") as archive:
        for name, kind, mode, payload, linkname in entries:
            info = tarfile.TarInfo(name)
            info.mode = mode
            if kind is None:
                info.type = tarfile.REGTYPE
                assert payload is not None
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
            else:
                info.type = kind
                info.linkname = linkname
                archive.addfile(info)


def _run_materializer(
    tmp_path: Path,
    *,
    archive_entries: list[tuple[str, bytes | None, int, bytes | None, str]],
    tree: bytes,
) -> subprocess.CompletedProcess[str]:
    archive_path = tmp_path / "source.tar"
    tree_path = tmp_path / "tree.bin"
    _write_archive(archive_path, archive_entries)
    tree_path.write_bytes(tree)
    return subprocess.run(
        [
            "/usr/bin/python3",
            "-I",
            "-S",
            str(MATERIALIZE.resolve()),
            "--archive",
            str(archive_path),
            "--tree",
            str(tree_path),
            "--destination",
            str(tmp_path / "pinned-source"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


# Production break caught: the release gate extracting bytes without proving
# their path, Git mode, and Git blob identity against the exact reviewed tree.
def test_materializer_extracts_only_the_exact_tree_with_git_modes(
    tmp_path: Path,
) -> None:
    completed = _run_materializer(
        tmp_path,
        archive_entries=[
            ("app/", tarfile.DIRTYPE, 0o775, None, ""),
            ("app/safe.txt", None, 0o664, b"safe\n", ""),
            ("run.sh", None, 0o775, b"#!/bin/sh\n", ""),
        ],
        tree=(
            _tree_entry("100644", SAFE_BLOB, "app/safe.txt")
            + _tree_entry("100755", SCRIPT_BLOB, "run.sh")
        ),
    )

    destination = tmp_path / "pinned-source"
    assert completed.returncode == 0
    assert completed.stdout == ""
    assert completed.stderr == ""
    assert (destination / "app/safe.txt").read_bytes() == b"safe\n"
    assert (destination / "run.sh").read_bytes() == b"#!/bin/sh\n"
    assert (destination / "app/safe.txt").stat().st_mode & 0o777 == 0o644
    assert (destination / "run.sh").stat().st_mode & 0o777 == 0o755
    assert destination.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize(
    ("case", "archive_entries", "tree"),
    (
        (
            "absolute",
            [("/safe.txt", None, 0o664, b"safe\n", "")],
            _tree_entry("100644", SAFE_BLOB, "safe.txt"),
        ),
        (
            "parent-traversal",
            [("../safe.txt", None, 0o664, b"safe\n", "")],
            _tree_entry("100644", SAFE_BLOB, "safe.txt"),
        ),
        (
            "git-metadata",
            [(".git/config", None, 0o664, b"safe\n", "")],
            _tree_entry("100644", SAFE_BLOB, ".git/config"),
        ),
        (
            "symlink",
            [("safe.txt", tarfile.SYMTYPE, 0o777, None, "outside")],
            _tree_entry("100644", SAFE_BLOB, "safe.txt"),
        ),
        (
            "hardlink",
            [("safe.txt", tarfile.LNKTYPE, 0o777, None, "outside")],
            _tree_entry("100644", SAFE_BLOB, "safe.txt"),
        ),
        (
            "fifo",
            [("safe.txt", tarfile.FIFOTYPE, 0o644, None, "")],
            _tree_entry("100644", SAFE_BLOB, "safe.txt"),
        ),
        (
            "mode-mismatch",
            [("safe.txt", None, 0o775, b"safe\n", "")],
            _tree_entry("100644", SAFE_BLOB, "safe.txt"),
        ),
        (
            "blob-mismatch",
            [("safe.txt", None, 0o664, b"evil\n", "")],
            _tree_entry("100644", SAFE_BLOB, "safe.txt"),
        ),
        (
            "set-extra",
            [
                ("safe.txt", None, 0o664, b"safe\n", ""),
                ("extra.txt", None, 0o664, b"safe\n", ""),
            ],
            _tree_entry("100644", SAFE_BLOB, "safe.txt"),
        ),
        (
            "set-missing",
            [],
            _tree_entry("100644", SAFE_BLOB, "safe.txt"),
        ),
        (
            "directory-extra-trailing-slash",
            [
                ("app//", tarfile.DIRTYPE, 0o775, None, ""),
                ("app/safe.txt", None, 0o664, b"safe\n", ""),
            ],
            _tree_entry("100644", SAFE_BLOB, "app/safe.txt"),
        ),
        (
            "directory-setgid-mode",
            [
                ("app/", tarfile.DIRTYPE, 0o2775, None, ""),
                ("app/safe.txt", None, 0o664, b"safe\n", ""),
            ],
            _tree_entry("100644", SAFE_BLOB, "app/safe.txt"),
        ),
        (
            "file-setuid-mode",
            [("safe.txt", None, 0o4664, b"safe\n", "")],
            _tree_entry("100644", SAFE_BLOB, "safe.txt"),
        ),
    ),
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_materializer_rejects_unsafe_or_nonidentical_archives(
    tmp_path: Path,
    case: str,
    archive_entries: list[tuple[str, bytes | None, int, bytes | None, str]],
    tree: bytes,
) -> None:
    completed = _run_materializer(
        tmp_path,
        archive_entries=archive_entries,
        tree=tree,
    )

    assert case
    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "BLOCKED_INVALID_PINNED_SOURCE\n"
    assert not (tmp_path / "pinned-source").exists()


# Production break caught: an alternate Git object type or executable mode can
# enter the archive even though the extractor only expects regular blobs.
@pytest.mark.parametrize(
    "tree",
    (
        f"120000 blob {SAFE_BLOB}\tsafe.txt\0".encode(),
        f"100644 tree {SAFE_BLOB}\tsafe.txt\0".encode(),
        f"100600 blob {SAFE_BLOB}\tsafe.txt\0".encode(),
        f"100644 blob {SAFE_BLOB}\t../safe.txt\0".encode(),
    ),
)
def test_materializer_rejects_non_regular_or_unsafe_tree_entries(
    tmp_path: Path,
    tree: bytes,
) -> None:
    completed = _run_materializer(
        tmp_path,
        archive_entries=[("safe.txt", None, 0o664, b"safe\n", "")],
        tree=tree,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "BLOCKED_INVALID_PINNED_SOURCE\n"
    assert not (tmp_path / "pinned-source").exists()


def _replace_once(source: str, old: str, new: str) -> str:
    assert source.count(old) == 1, f"expected one anchored replacement for {old!r}"
    return source.replace(old, new, 1)


def _read_recorded_root(marker: Path, prefix: str) -> Path:
    raw = marker.read_text(encoding="utf-8").splitlines()[0].strip()
    assert raw and os.path.isabs(raw)
    root = Path(raw)
    assert root.parent in {Path("/tmp"), Path("/private/tmp")}
    assert root.name.startswith(prefix)
    return root


def _cleanup_recorded_root(marker: Path, prefix: str) -> Path:
    root = _read_recorded_root(marker, prefix)
    if not root.exists() and not root.is_symlink():
        return root
    identity_raw = marker.with_name(marker.name + ".identity").read_text(
        encoding="ascii"
    ).strip()
    identity_fields = identity_raw.split(":")
    assert len(identity_fields) == 2 and all(
        field.isascii() and field.isdecimal() for field in identity_fields
    )
    _cleanup_exact_owned_root(
        root, (int(identity_fields[0]), int(identity_fields[1])), prefix
    )
    return root


def _cleanup_exact_owned_root(
    root: Path,
    expected: tuple[int, int],
    prefix: str,
) -> None:
    assert root.is_absolute()
    tmp_roots = {Path("/tmp"), Path("/private/tmp")}
    nested_quarantine = (
        root.parent.parent in tmp_roots
        and root.parent.name.startswith(".travel-map-cleanup.")
    )
    assert root.parent in tmp_roots or nested_quarantine
    assert root.name.startswith(prefix)
    try:
        details = root.lstat()
    except FileNotFoundError:
        return
    if (details.st_dev, details.st_ino) != expected:
        return
    assert stat.S_ISDIR(details.st_mode)
    assert stat.S_IMODE(details.st_mode) == 0o700
    assert details.st_uid == os.getuid()
    assert not root.is_symlink()
    shutil.rmtree(root)
    if nested_quarantine:
        quarantine = root.parent
        quarantine_details = quarantine.lstat()
        assert stat.S_ISDIR(quarantine_details.st_mode)
        assert quarantine_details.st_uid == os.getuid()
        if not any(quarantine.iterdir()):
            quarantine.rmdir()


def test_cleanup_recorded_root_preserves_replacement_inode(tmp_path: Path) -> None:
    root = Path("/private/tmp") / (
        f"travel-map-publish-launcher.teardown-{os.getpid()}-{time.monotonic_ns()}"
    )
    displaced = root.with_name(root.name + ".owned")
    marker = tmp_path / "recorded-root"
    identity_marker = marker.with_name(marker.name + ".identity")
    root.mkdir(mode=0o700)
    owned_details = root.lstat()
    owned_identity = (owned_details.st_dev, owned_details.st_ino)
    marker.write_text(str(root) + "\n", encoding="ascii")
    identity_marker.write_text(
        f"{owned_identity[0]}:{owned_identity[1]}\n", encoding="ascii"
    )
    root.rename(displaced)
    root.mkdir(mode=0o700)
    replacement_details = root.lstat()
    replacement_identity = (replacement_details.st_dev, replacement_details.st_ino)
    replacement_marker = root / "replacement-marker"
    replacement_marker.write_text("replacement\n", encoding="ascii")
    try:
        _cleanup_recorded_root(marker, "travel-map-publish-launcher.")

        assert replacement_marker.read_text(encoding="ascii") == "replacement\n"
        assert displaced.is_dir() and not displaced.is_symlink()
    finally:
        _cleanup_exact_owned_root(
            root, replacement_identity, "travel-map-publish-launcher."
        )
        _cleanup_exact_owned_root(
            displaced, owned_identity, "travel-map-publish-launcher."
        )


def _find_exact_owned_root(
    expected: tuple[int, int],
    prefix: str,
) -> Path:
    for tmp_root in {Path("/tmp"), Path("/private/tmp")}:
        if not tmp_root.exists():
            continue
        for candidate in tmp_root.iterdir():
            candidates = [candidate]
            try:
                candidate_details = candidate.lstat()
            except FileNotFoundError:
                continue
            if (
                stat.S_ISDIR(candidate_details.st_mode)
                and not candidate.is_symlink()
                and candidate.name.startswith(".travel-map-cleanup.")
            ):
                candidates.extend(candidate.iterdir())
            for owned in candidates:
                if not owned.name.startswith(prefix):
                    continue
                try:
                    details = owned.lstat()
                except FileNotFoundError:
                    continue
                if (details.st_dev, details.st_ino) == expected:
                    return owned
    raise AssertionError(f"owned root {expected!r} was not found")


def _find_identity_under(root: Path, expected: tuple[int, int]) -> Path | None:
    for directory, names, files in os.walk(root, followlinks=False):
        for name in [*names, *files]:
            candidate = Path(directory) / name
            try:
                details = candidate.lstat()
            except FileNotFoundError:
                continue
            if (details.st_dev, details.st_ino) == expected:
                return candidate
    return None


def _rewrite_same_inode(path: Path, payload: str) -> None:
    before = path.stat()
    path.chmod(0o700)
    with path.open("wb") as stream:
        stream.write(payload.encode("ascii"))
        stream.flush()
        os.fsync(stream.fileno())
    path.chmod(0o500)
    after = path.stat()
    assert after.st_ino == before.st_ino
    assert stat.S_IMODE(after.st_mode) == 0o500


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    ppid: int
    pgid: int
    lstart: str

@dataclass(frozen=True)
class OwnedProcessTree:
    root: ProcessIdentity | None
    descendants: tuple[ProcessIdentity, ...]
    diagnostic: str

def _read_process_table() -> dict[int, ProcessIdentity]:
    completed = subprocess.run(["/bin/ps", "-axo", "pid=,ppid=,pgid=,lstart=,command="], check=True, capture_output=True, text=True)
    table = {}
    for line in completed.stdout.splitlines():
        fields = line.strip().split(None, 8)
        if len(fields) == 9:
            try: pid, ppid, pgid = map(int, fields[:3])
            except ValueError: continue
            table[pid] = ProcessIdentity(pid, ppid, pgid, " ".join(fields[3:8]))
    return table


def _publisher_process_groups_for_fixture(
    root_identity: ProcessIdentity,
) -> OwnedProcessTree:
    records = _read_process_table()
    if records.get(root_identity.pid) != root_identity:
        return OwnedProcessTree(None, (), repr(root_identity))
    descendants = {root_identity.pid}
    changed = True
    while changed:
        changed = False
        for pid, identity in records.items():
            if identity.ppid in descendants and pid not in descendants:
                descendants.add(pid)
                changed = True
    retained = tuple(records[pid] for pid in descendants if pid in records)
    return OwnedProcessTree(root_identity, retained, repr(retained))


def _kill_publisher_process_groups(tree: OwnedProcessTree) -> None:
    own_group = os.getpgrp()
    for group in sorted({item.pgid for item in tree.descendants}):
        live = _read_process_table()
        anchors = [item for item in tree.descendants if live.get(item.pid) == item]
        owned = {item.pid for item in anchors}
        members = [item for item in live.values() if item.pgid == group]
        if not any(item.pgid == group for item in anchors):
            continue
        if group == own_group or group <= 0:
            continue
        if any(item.pid not in owned for item in members):
            continue
        try:
            os.killpg(group, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            for anchor in anchors:
                current = _read_process_table()
                if current.get(anchor.pid) != anchor:
                    continue
                try:
                    os.kill(anchor.pid, signal.SIGKILL)
                except (PermissionError, ProcessLookupError):
                    pass


def test_publisher_cleanup_revalidates_stale_identity_before_each_group_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = ProcessIdentity(1001, 1, 7001, "first-start")
    second = ProcessIdentity(1002, 1001, 7002, "second-start")
    stale = ProcessIdentity(1002, 1001, 7002, "reused-start")
    unrelated = ProcessIdentity(1003, 1, 7002, "unrelated-start")
    tree = OwnedProcessTree(first, (first, second), "fixture")
    snapshots = iter(
        (
            {first.pid: first, second.pid: second},
            {
                first.pid: first,
                second.pid: stale,
                unrelated.pid: unrelated,
            },
        )
    )
    calls: list[int] = []
    monkeypatch.setattr(
        sys.modules[__name__], "_read_process_table", lambda: next(snapshots)
    )
    monkeypatch.setattr(os, "killpg", lambda group, _signal: calls.append(group))

    _kill_publisher_process_groups(tree)

    assert calls == [7001]


def test_publisher_rejects_reused_root_pid_before_descendant_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = ProcessIdentity(2001, 1, 8001, "captured-start")
    reused = ProcessIdentity(2001, 1, 8002, "reused-start")
    descendant = ProcessIdentity(2002, 2001, 8002, "descendant-start")
    monkeypatch.setattr(
        sys.modules[__name__],
        "_read_process_table",
        lambda: {reused.pid: reused, descendant.pid: descendant},
    )

    tree = _publisher_process_groups_for_fixture(captured)

    assert tree.root is None
    assert tree.descendants == ()


def test_publisher_reap_control_rejects_reused_former_pgid_after_leader_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leader = ProcessIdentity(2101, 1, 8101, "leader-start")
    child = ProcessIdentity(2102, 2101, 8101, "child-start")
    reused_leader = ProcessIdentity(2101, 1, 8101, "reused-start")
    outsider = ProcessIdentity(2103, 1, 8101, "outsider-start")
    snapshots = iter(
        (
            {leader.pid: leader, child.pid: child},
            {
                reused_leader.pid: reused_leader,
                outsider.pid: outsider,
            },
        )
    )
    calls: list[int] = []
    monkeypatch.setattr(
        sys.modules[__name__], "_read_process_table", lambda: next(snapshots)
    )
    monkeypatch.setattr(os, "killpg", lambda group, _signal: calls.append(group))

    tree = _publisher_process_groups_for_fixture(leader)
    assert tree.root == leader
    _kill_publisher_process_groups(tree)

    assert calls == []


def test_publisher_timeout_cleanup_excludes_unrelated_same_commandline_process(
    tmp_path: Path,
) -> None:
    commandline_reference = str(tmp_path)
    publisher = subprocess.Popen(
        [
            "/bin/sh",
            "-c",
            f"trap : TERM; /bin/sleep 30 # {commandline_reference}",
        ],
        start_new_session=True,
    )
    unrelated = subprocess.Popen(
        [
            "/bin/sh",
            "-c",
            f"trap : TERM; /bin/sleep 30 # {commandline_reference}",
        ],
        start_new_session=True,
    )
    try:
        publisher_identity = _read_process_table()[publisher.pid]
        tree = _publisher_process_groups_for_fixture(publisher_identity)
        groups = {item.pgid for item in tree.descendants}
        diagnostic = tree.diagnostic
        assert os.getpgid(publisher.pid) in groups, diagnostic
        assert os.getpgid(unrelated.pid) not in groups, diagnostic
    finally:
        for child in (publisher, unrelated):
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=5)


def _git(repository: Path, *arguments: str, input_bytes: bytes | None = None) -> str:
    completed = subprocess.run(
        ["/usr/bin/git", "-C", str(repository), *arguments],
        input=input_bytes,
        check=True,
        capture_output=True,
    )
    return completed.stdout.decode().strip()


def _write_executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


def _write_gate_fake_tools(
    fake_bin: Path,
    events: Path,
    *,
    uv_cache: Path | None = None,
    playwright_cache: Path | None = None,
    pnpm_store: Path | None = None,
) -> None:
    fake_bin.mkdir()
    python_runtime = fake_bin.parent / "approved-python-runtime"
    python_executable = python_runtime / "bin/python3.12"
    python_executable.parent.mkdir(parents=True)
    _write_executable(python_executable, "#!/bin/sh\nexit 0\n")
    python_stdlib = python_runtime / "lib/python3.12"
    python_stdlib.mkdir(parents=True)
    (python_stdlib / "os.py").write_text(
        "# reviewed Python 3.12 standard library fixture\n",
        encoding="utf-8",
    )
    pnpm_root = fake_bin / "pnpm-package"
    pnpm_executable = pnpm_root / "bin/pnpm.mjs"
    pnpm_executable.parent.mkdir(parents=True)
    pnpm_bundle = pnpm_root / "dist/pnpm.mjs"
    pnpm_bundle.parent.mkdir(parents=True)
    pnpm_bundle.write_text("// reviewed pnpm bundle fixture\n", encoding="utf-8")
    (pnpm_root / "package.json").write_text(
        '{"name":"pnpm","version":"fixture"}\n',
        encoding="utf-8",
    )
    (fake_bin / "git").symlink_to("/Library/Developer/CommandLineTools/usr/bin/git")
    _write_executable(
        fake_bin / "python3",
        "#!/bin/sh\nprintf '%s\\n' 'ambient PATH python3 executed' >&2\nexit 97\n",
    )
    _write_executable(
        fake_bin / "node",
        f"""#!/usr/bin/python3
import json
import os
import sys
from pathlib import Path

with Path({str(events)!r}).open("a", encoding="utf-8") as output:
    output.write(json.dumps({{"tool": "nested-node", "args": sys.argv[1:], "executable": sys.argv[0]}}) + "\\n")
if sys.argv[1:] and not sys.argv[1].startswith("--nested-resolution-probe"):
    os.execv("/usr/bin/python3", ["/usr/bin/python3", *sys.argv[1:]])
""",
    )
    event_literal = repr(str(events))
    python_find_event_literal = repr(str(events.with_suffix(".uv-python-find")))
    core_injection = events.with_suffix(".core-injection")
    for tool, system_tool in (
        ("dirname", "/usr/bin/dirname"),
        ("mktemp", "/usr/bin/mktemp"),
        ("grep", "/usr/bin/grep"),
    ):
        _write_executable(
            fake_bin / tool,
            f"#!/bin/sh\nprintf '%s\\n' {tool!r} >> {str(core_injection)!r}\n"
            f'exec {system_tool} "$@"\n',
        )
    pause_literal = repr(str(events.with_suffix(".pause")))
    entered_literal = repr(str(events.with_suffix(".entered")))
    mutation_literal = repr(str(events.with_suffix(".source-mutation")))
    helper_mutation_literal = repr(str(events.with_suffix(".helper-mutation")))
    extra_injection_literal = repr(str(events.with_suffix(".pristine-extra")))
    pristine_injected_literal = repr(str(events.with_suffix(".pristine-injected")))
    context_injection_literal = repr(str(events.with_suffix(".context-injection")))
    context_injector_pid_literal = repr(
        str(events.with_suffix(".context-injector-pid"))
    )
    anchor_poison_literal = repr(str(events.with_suffix(".anchor-poison")))
    anchor_poisoned_literal = repr(str(events.with_suffix(".anchor-poisoned")))
    anchor_executed_literal = repr(str(events.with_suffix(".anchor-executed")))
    runtime_poison_literal = repr(str(events.with_suffix(".python-runtime-poison")))
    runtime_poisoned_literal = repr(str(events.with_suffix(".python-runtime-poisoned")))
    python_executable_literal = repr(str(python_executable.resolve()))
    python_stdlib_literal = repr(str(python_stdlib.resolve()))
    pnpm_executable_literal = repr(str(pnpm_executable.resolve(strict=False)))
    pnpm_bundle_literal = repr(str(pnpm_bundle.resolve()))
    pnpm_poison_literal = repr(str(events.with_suffix(".pnpm-bundle-poison")))
    pnpm_poisoned_literal = repr(str(events.with_suffix(".pnpm-bundle-poisoned")))
    pnpm_root_replace_literal = repr(str(events.with_suffix(".pnpm-root-replace")))
    pnpm_root_replace_result_literal = repr(
        str(events.with_suffix(".pnpm-root-replace-result"))
    )
    browser_poison_literal = repr(str(events.with_suffix(".browser-cache-poison")))
    browser_poisoned_literal = repr(str(events.with_suffix(".browser-cache-poisoned")))
    browser_cache = playwright_cache or fake_bin.parent / "playwright-cache"
    browser_executable_literal = repr(str(browser_cache / "chromium/browser"))
    cache_swap_literal = repr(str(events.with_suffix(".cache-ancestor-swap")))
    cache_swapped_literal = repr(str(events.with_suffix(".cache-ancestor-swapped")))
    browser_executed_literal = repr(str(events.with_suffix(".browser-executed")))
    browser_cache_literal = repr(str(browser_cache))
    uv_cache_root = uv_cache or fake_bin.parent / "uv-cache"
    uv_cache_literal = repr(str(uv_cache_root))
    uv_attack_literal = repr(str(events.with_suffix(".uv-cache-attack")))
    uv_executed_literal = repr(str(events.with_suffix(".uv-cache-executed")))
    pnpm_store_root = pnpm_store or fake_bin.parent / "pnpm-store"
    store_attack_literal = repr(str(events.with_suffix(".pnpm-store-attack")))
    pnpm_store_literal = repr(str(pnpm_store_root))
    source_tools_literal = repr(str(fake_bin.resolve()))
    linger_pid_literal = repr(str(events.with_suffix(".linger-pid")))
    logs_failure_literal = repr(str(events.with_suffix(".logs-failure")))
    unreadable_db_literal = repr(str(events.with_suffix(".unreadable-db")))
    malicious_helper_literal = repr(
        """import argparse
import tarfile
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--archive", type=Path, required=True)
parser.add_argument("--tree", type=Path, required=True)
parser.add_argument("--destination", type=Path, required=True)
args = parser.parse_args()
with tarfile.open(args.archive, "r:") as archive:
    archive.extractall(args.destination)
        """
    )
    context_injector_literal = repr(
        """import time
from pathlib import Path

deadline = time.monotonic() + 30
while time.monotonic() < deadline:
    for root in Path("/tmp").glob("travel-map-pristine.*"):
        target = root / "context" / "Dockerfile"
        if target.is_file():
            while time.monotonic() < deadline:
                target.write_text("FROM injected-background\\n", encoding="utf-8")
                time.sleep(0.001)
            raise SystemExit(0)
    time.sleep(0.005)
"""
    )
    stale_lock_literal = repr(str(events.with_suffix(".stale-lock")))
    _write_executable(
        fake_bin / "uv",
        f"""#!/usr/bin/python3
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

event_path = Path({event_literal})
cwd = Path.cwd()
args = sys.argv[1:]
if args[:2] == ["python", "find"]:
    with Path({python_find_event_literal}).open("w", encoding="utf-8") as output:
        output.write(json.dumps({{
            "tool": "uv",
            "args": args,
            "environment": dict(sorted(os.environ.items())),
            "cwd": str(cwd),
        }}) + "\\n")
    required = {{
        "--no-project",
        "--resolve-links",
        "--no-python-downloads",
        "--offline",
        "--no-config",
    }}
    if not required.issubset(args) or args[-1] != "3.12":
        event_path.with_suffix(".python-download-attempt").write_text(
            "unsafe python discovery\\n", encoding="utf-8"
        )
        raise SystemExit(88)
    print({python_executable_literal})
    raise SystemExit(0)
if "sync" in args and Path({uv_attack_literal}).exists():
    candidates = (
        Path({uv_cache_literal}) / "archive-v0/reviewed-wheel/payload.py",
        Path({uv_cache_literal}) / "wheels-v6/pypi/escape/1.0-py3-none-any/payload.py",
    )
    for candidate in candidates:
        if candidate.is_file():
            subprocess.run([sys.executable, str(candidate)], check=False)
            Path({uv_executed_literal}).write_text(
                "executed\\n",
                encoding="utf-8",
            )
python_environment = Path(os.environ["UV_PROJECT_ENVIRONMENT"])
pollution = python_environment / "lib/python3.12/site-packages/sitecustomize.py"
ignored = (
    "apps/travel-map/app/injected.py",
    "apps/travel-map/conftest.py",
    "apps/travel-map/e2e/injected.spec.ts",
    "apps/travel-map/.venv/ambient.txt",
    "apps/travel-map/node_modules/ambient.txt",
)
cache_root = Path(os.environ["UV_CACHE_DIR"])
cache_links = []
for directory, child_directories, child_files in os.walk(
    cache_root, topdown=True, followlinks=False
):
    for child in [*child_directories, *child_files]:
        candidate = Path(directory) / child
        if not candidate.is_symlink():
            continue
        target_text = os.readlink(candidate)
        target_relative = None
        try:
            target = candidate.parent.joinpath(target_text).resolve(strict=True)
            if target == cache_root or cache_root in target.parents:
                target_relative = target.relative_to(cache_root).as_posix()
        except (OSError, RuntimeError):
            pass
        cache_links.append(
            [candidate.relative_to(cache_root).as_posix(), target_text, target_relative]
        )
payload = {{
    "tool": "uv",
    "args": sys.argv[1:],
    "cwd": str(cwd),
    "environment": sorted(os.environ),
    "home": os.environ["HOME"],
    "home_mode": stat.S_IMODE(Path(os.environ["HOME"]).stat().st_mode),
    "docker_config_mode": stat.S_IMODE(Path(os.environ["DOCKER_CONFIG"]).stat().st_mode),
    "docker_json_mode": stat.S_IMODE((Path(os.environ["DOCKER_CONFIG"]) / "config.json").stat().st_mode),
    "docker_config_payload": (Path(os.environ["DOCKER_CONFIG"]) / "config.json").read_text(encoding="utf-8"),
    "uv_project_environment": str(python_environment),
    "uv_cache_dir": os.environ["UV_CACHE_DIR"],
    "uv_lock_mode": (
        stat.S_IMODE((Path(os.environ["UV_CACHE_DIR"]) / ".lock").stat().st_mode)
        if (Path(os.environ["UV_CACHE_DIR"]) / ".lock").exists()
        else None
    ),
    "uv_lock_payload": (
        (Path(os.environ["UV_CACHE_DIR"]) / ".lock").read_text(encoding="utf-8")
        if (Path(os.environ["UV_CACHE_DIR"]) / ".lock").is_file()
        else None
    ),
    "uv_reviewed_payload": (
        (Path(os.environ["UV_CACHE_DIR"]) / "archive-v0/reviewed-wheel/payload.py").read_text(
            encoding="utf-8"
        )
        if (Path(os.environ["UV_CACHE_DIR"]) / "archive-v0/reviewed-wheel/payload.py").is_file()
        else None
    ),
    "uv_cache_links": sorted(cache_links),
    "uv_python": os.environ.get("UV_PYTHON"),
    "uv_python_downloads": os.environ.get("UV_PYTHON_DOWNLOADS"),
    "uv_offline": os.environ.get("UV_OFFLINE"),
    "python_environment_polluted": pollution.exists(),
    "ignored_present": [name for name in ignored if (cwd / name).exists()],
    "reviewed": (
        (cwd / "apps/travel-map/app/reviewed.py").read_text(encoding="utf-8")
        if (cwd / "apps/travel-map/app/reviewed.py").exists()
        else "<missing>"
    ),
}}
with event_path.open("a", encoding="utf-8") as output:
    output.write(json.dumps(payload) + "\\n")
pause_path = Path({pause_literal})
if pause_path.exists():
    Path({entered_literal}).write_text("entered\\n", encoding="utf-8")
    while pause_path.exists():
        import time

        time.sleep(0.05)
stale_lock = Path({stale_lock_literal})
if stale_lock.exists():
    if "--locked" in args:
        raise SystemExit(86)
    (cwd / "apps/travel-map/uv.lock").write_text(
        "ambient lock rewrite\\n", encoding="utf-8"
    )
mutation = Path({mutation_literal})
if mutation.exists() and "pytest" in args:
    target = cwd / "apps/travel-map/app/reviewed.py"
    if mutation.read_text(encoding="utf-8").strip() == "delete":
        target.unlink()
    else:
        target.write_text("mutated after tests began\\n", encoding="utf-8")
if "pytest" in args:
    pollution.parent.mkdir(parents=True, exist_ok=True)
    pollution.write_text(
        "raise SystemExit('test environment reached pristine prepare')\\n",
        encoding="utf-8",
    )
    if Path({runtime_poison_literal}).exists():
        target = Path({python_stdlib_literal}) / "os.py"
        replacement = target.with_name("os.py.poisoned")
        replacement.write_text("# poisoned Python stdlib\\n", encoding="utf-8")
        os.replace(replacement, target)
        Path({runtime_poisoned_literal}).write_text(
            "poisoned\\n", encoding="utf-8"
        )
    if Path({pnpm_poison_literal}).exists():
        target = Path({pnpm_bundle_literal})
        replacement = target.with_name("pnpm.mjs.poisoned")
        replacement.write_text("// poisoned pnpm bundle\\n", encoding="utf-8")
        os.replace(replacement, target)
        Path({pnpm_poisoned_literal}).write_text(
            "poisoned\\n", encoding="utf-8"
        )
    if Path({browser_poison_literal}).exists():
        target = Path({browser_executable_literal})
        replacement = target.with_name("browser.poisoned")
        replacement.write_text("#!/bin/sh\\nexit 99\\n", encoding="utf-8")
        replacement.chmod(0o755)
        os.replace(replacement, target)
        Path({browser_poisoned_literal}).write_text(
            "poisoned\\n", encoding="utf-8"
        )
    if Path({helper_mutation_literal}).exists():
        helper = cwd.parent / "materialize-pinned-source.py"
        helper.write_text(
            {malicious_helper_literal},
            encoding="utf-8",
        )
    if Path({context_injection_literal}).exists():
        injector = {context_injector_literal}
        context_injector_pid = os.fork()
        if context_injector_pid == 0:
            null_descriptor = os.open(os.devnull, os.O_RDWR)
            for descriptor in (0, 1, 2):
                os.dup2(null_descriptor, descriptor)
            os.execv(
                "/usr/bin/python3",
                ["/usr/bin/python3", "-I", "-S", "-c", injector],
            )
        Path({context_injector_pid_literal}).write_text(
            str(context_injector_pid), encoding="ascii"
        )
    anchor_poison = Path({anchor_poison_literal})
    if anchor_poison.exists():
        attack = anchor_poison.read_text(encoding="utf-8").strip()
        malicious = (
            "#!/bin/sh\\n"
            + "printf '%s\\\\n' executed >> "
            + {anchor_executed_literal!r}
            + "\\n"
            + "printf '%s\\\\n' reviewed-snapshot\\n"
            + "exit 0\\n"
        )
        if attack.startswith("resolved-"):
            name = attack.removeprefix("resolved-")
            target = (
                Path({pnpm_executable_literal})
                if name == "pnpm"
                else Path({source_tools_literal}) / name
            )
        elif attack.startswith("trusted-"):
            name = attack.removeprefix("trusted-")
            target = Path(os.environ["PATH"].split(":", 1)[0]) / name
        elif attack == "docker-plugin":
            target = Path(os.environ["DOCKER_CONFIG"]) / "cli-plugins/docker-buildx"
            target.parent.mkdir(mode=0o700)
        else:
            raise SystemExit(87)
        replacement = target.with_name(target.name + ".poisoned")
        replacement.write_text(malicious, encoding="utf-8")
        replacement.chmod(0o700)
        os.replace(replacement, target)
        Path({anchor_poisoned_literal}).write_text(attack + "\\n", encoding="utf-8")
    linger_pid = Path({linger_pid_literal})
    if linger_pid.exists():
        lingering_pid = os.fork()
        if lingering_pid == 0:
            null_descriptor = os.open(os.devnull, os.O_RDWR)
            for descriptor in (0, 1, 2):
                os.dup2(null_descriptor, descriptor)
            os.execv("/bin/sleep", ["/bin/sleep", "30"])
        linger_pid.write_text(str(lingering_pid), encoding="ascii")
if any(argument.endswith("/prepare-release-context.py") for argument in args):
    destination = Path(args[args.index("--destination") + 1])
    destination.mkdir(mode=0o700)
    (destination / "Dockerfile").write_text("FROM scratch\\n", encoding="utf-8")
    if Path({extra_injection_literal}).exists():
        target = cwd / "apps/travel-map/app/injected-after-tests.py"
        target.write_text(
            "raise SystemExit('injected after tests')\\n",
            encoding="utf-8",
        )
        Path({pristine_injected_literal}).write_text(
            "injected\\n", encoding="utf-8"
        )
    print("reviewed-snapshot")
raise SystemExit(0)
""",
    )
    _write_executable(
        pnpm_executable,
        f"""#!/usr/bin/env node
import json
import os
import subprocess
import sys
from pathlib import Path
with Path({event_literal}).open("a", encoding="utf-8") as output:
    output.write(json.dumps({{"tool": "pnpm", "args": sys.argv[1:], "cwd": str(Path.cwd()), "environment": dict(sorted(os.environ.items())), "pnpm_store_dir": os.environ["PNPM_STORE_DIR"], "docker_config_payload": (Path(os.environ["DOCKER_CONFIG"]) / "config.json").read_text(encoding="utf-8"), "executable": sys.argv[0]}}) + "\\n")
if Path({pnpm_root_replace_literal}).exists():
    store = Path(os.environ["PNPM_STORE_DIR"])
    files = store / "files"
    backup = store / "files.replacement"
    try:
        if Path({pnpm_root_replace_literal}).read_text(encoding="utf-8").strip() == "chmod":
            store.chmod(0o700)
        os.replace(files, backup)
        if Path({pnpm_root_replace_literal}).read_text(encoding="utf-8").strip() == "chmod":
            replacement = store / "files.malicious"
            replacement.mkdir(mode=0o700)
            marker = replacement / "consumed-marker"
            marker.write_text("malicious\\n", encoding="utf-8")
            os.replace(replacement, files)
            if marker.is_file():
                Path({pnpm_root_replace_result_literal}).write_text("consumed\\n", encoding="utf-8")
            os.replace(files, replacement)
        os.replace(backup, files)
        if Path({pnpm_root_replace_literal}).read_text(encoding="utf-8").strip() == "chmod":
            store.chmod(0o500)
    except OSError:
        Path({pnpm_root_replace_result_literal}).write_text("chmod-blocked\\n" if Path({pnpm_root_replace_literal}).read_text(encoding="utf-8").strip() == "chmod" else "blocked\\n", encoding="utf-8")
    else:
        Path({pnpm_root_replace_result_literal}).write_text("replaced\\n", encoding="utf-8")
if "install" in sys.argv[1:] and Path({store_attack_literal}).exists():
    payload = Path({pnpm_store_literal}) / "files/malicious-package/install.py"
    if payload.is_file():
        subprocess.run([sys.executable, str(payload)], check=False)
if "test:e2e" in sys.argv[1:]:
    if Path({cache_swap_literal}).exists():
        original = Path({browser_cache_literal})
        backup = original.with_name(original.name + ".reviewed")
        replacement = original.with_name(original.name + ".replacement")
        malicious = replacement / "chromium/browser"
        malicious.parent.mkdir(mode=0o700, parents=True)
        malicious.write_text(
            "#!/usr/bin/python3\\n"
            "from pathlib import Path\\n"
            f"Path({browser_executed_literal}).write_text('executed\\\\n', encoding='utf-8')\\n"
            "raise SystemExit(99)\\n",
            encoding="utf-8",
        )
        malicious.chmod(0o755)
        os.replace(original, backup)
        os.replace(replacement, original)
        Path({cache_swapped_literal}).write_text("swapped\\n", encoding="utf-8")
        subprocess.run([str(original / "chromium/browser")], check=False)
    subprocess.run(["node", "--nested-resolution-probe"], check=True)
    subprocess.run(["uv", "--locked", "--nested-resolution-probe"], check=True)
""",
    )
    (fake_bin / "pnpm").symlink_to(pnpm_executable)
    _write_executable(
        fake_bin / "docker",
        f"""#!/usr/bin/python3
import json
import os
import shutil
import sys
from pathlib import Path

args = sys.argv[1:]
logs_failure = Path({logs_failure_literal})
unreadable_db = Path({unreadable_db_literal})
expected_host = "unix://" + {str(_release_test_socket_path(fake_bin.parent))!r}
docker_config_payload = (
    Path(os.environ["DOCKER_CONFIG"]) / "config.json"
).read_text(encoding="utf-8")
is_context_inspect = args == [
    "context", "inspect", "--format", "{{{{.Endpoints.docker.Host}}}}"
]
context_dockerfile = None
if args[:2] == ["buildx", "build"]:
    candidate = Path(args[-1]) / "Dockerfile"
    if candidate.is_file():
        context_dockerfile = candidate.read_text(encoding="utf-8")
recorded_config_payload = (
    "<protected-bootstrap-config>" if is_context_inspect else docker_config_payload
)
with Path({event_literal}).open("a", encoding="utf-8") as output:
    output.write(json.dumps({{"tool": "docker", "args": args, "cwd": str(Path.cwd()), "environment": sorted(os.environ), "docker_config_path": os.environ["DOCKER_CONFIG"], "docker_config_payload": recorded_config_payload, "docker_host": os.environ.get("DOCKER_HOST"), "context_dockerfile": context_dockerfile}}) + "\\n")
image_id = "sha256:" + "a" * 64
if is_context_inspect:
    context_name = json.loads(docker_config_payload).get("currentContext")
    if context_name == "colima-test":
        print(expected_host)
        raise SystemExit(0)
    if context_name == "travel-map-release-local":
        context_id = __import__("hashlib").sha256(
            context_name.encode("utf-8")
        ).hexdigest()
        metadata = json.loads(
            (
                Path(os.environ["DOCKER_CONFIG"])
                / "contexts"
                / "meta"
                / context_id
                / "meta.json"
            ).read_text(encoding="utf-8")
        )
        print(metadata["Endpoints"]["docker"]["Host"])
        raise SystemExit(0)
    if context_name == "remote-test":
        print("tcp://attacker.invalid:2375")
        raise SystemExit(0)
    else:
        raise SystemExit(92)
if os.environ.get("DOCKER_HOST") != expected_host:
    raise SystemExit(93)
if args == ["version"]:
    raise SystemExit(0)
if args[:2] == ["buildx", "build"]:
    context = Path(args[-1])
    if not context.is_dir() or not (context / "Dockerfile").is_file():
        raise SystemExit(91)
    raise SystemExit(0)
if args[:3] == ["image", "inspect", "--format"]:
    if ".Os" in args[3] or ".Architecture" in args[3]:
        print(image_id + " linux/amd64")
    else:
        print(image_id)
    raise SystemExit(0)
if args[:2] == ["image", "rm"]:
    raise SystemExit(0)
if args and args[0] == "run":
    mount_source = None
    for argument in args:
        if argument.startswith("type=bind,src=") and ",dst=/data" in argument:
            mount_source = Path(argument.split("src=", 1)[1].split(",dst=", 1)[0])
            break
    if mount_source is not None and "find /data -mindepth 1 -depth -delete" in " ".join(args):
        for child in mount_source.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
    stdin = sys.stdin.read()
    if "HistoryRepository" in stdin:
        print("ENCRYPTED_STORAGE_SMOKE_OK")
    elif "-d" in args:
        if unreadable_db.exists() and mount_source is not None:
            database = mount_source / "travel-map.sqlite3"
            database.write_bytes(b"opaque database fixture")
            database.chmod(0o000)
            unreadable_db.with_suffix(".unreadable-db-created").write_text(
                "created\\n", encoding="utf-8"
            )
        print("fake-container-id")
    raise SystemExit(0)
if args and args[0] == "logs" and logs_failure.exists():
    raise SystemExit(95)
if (
    args and args[0] == "exec"
    and unreadable_db.exists()
    and "travel-map.sqlite3" in " ".join(args)
):
    sys.stdin.read()
    raise SystemExit(96)
if args and args[0] in {{"exec", "logs", "rm"}}:
    sys.stdin.read()
    raise SystemExit(0)
print("unexpected fake docker invocation", file=sys.stderr)
raise SystemExit(91)
""",
    )
    _write_executable(
        fake_bin / "docker-buildx",
        '#!/bin/sh\nexec "${0%/*}/docker" buildx "$@"\n',
    )


def _release_gate_repository(
    tmp_path: Path,
    *,
    cleanup_pause: tuple[Path, Path] | None = None,
    cleanup_swap_sync: tuple[Path, Path] | None = None,
    cleanup_file_swap_sync: tuple[Path, Path] | None = None,
    cleanup_quarantine_swap_sync: tuple[Path, Path] | None = None,
    cleanup_quarantine_mismatch_sync: tuple[Path, Path] | None = None,
    cleanup_root_swap_sync: tuple[Path, Path] | None = None,
    cleanup_root_prebind_sync: tuple[Path, Path] | None = None,
    cache_parent: Path | None = None,
) -> tuple[Path, Path, Path, Path]:
    repository = tmp_path / "repository"
    scripts = repository / "apps/travel-map/scripts"
    scripts.mkdir(parents=True)
    fake_bin = tmp_path / "safe-bin"
    events = tmp_path / "events.jsonl"
    trusted_cache_parent = cache_parent or tmp_path
    if cache_parent is not None:
        trusted_cache_parent.mkdir(mode=0o700)
    uv_cache = trusted_cache_parent / "uv-cache"
    playwright_cache = trusted_cache_parent / "playwright-cache"
    pnpm_store = trusted_cache_parent / "pnpm-store"
    _write_gate_fake_tools(
        fake_bin,
        events,
        uv_cache=uv_cache,
        playwright_cache=playwright_cache,
        pnpm_store=pnpm_store,
    )
    uv_cache.mkdir(mode=0o700)
    uv_archive = uv_cache / "archive-v0/reviewed-wheel"
    uv_archive.mkdir(mode=0o700, parents=True)
    (uv_archive / "payload.py").write_text(
        "REVIEWED_UV_CACHE = True\n",
        encoding="utf-8",
    )
    uv_wheel = uv_cache / "wheels-v6/pypi/reviewed/1.0-py3-none-any"
    uv_wheel.parent.mkdir(mode=0o700, parents=True)
    uv_wheel.symlink_to("../../../archive-v0/reviewed-wheel")
    uv_lock = uv_cache / ".lock"
    uv_lock.write_text("mutable lock metadata\n", encoding="utf-8")
    uv_lock.chmod(0o666)
    playwright_cache.mkdir(mode=0o700)
    browser_executable = playwright_cache / "chromium/browser"
    browser_executable.parent.mkdir(mode=0o700)
    _write_executable(browser_executable, "#!/bin/sh\nexit 0\n")
    pnpm_store.mkdir(mode=0o700)
    for section in ("files", "index", "projects"):
        (pnpm_store / section).mkdir(mode=0o700)
    (pnpm_store / "files/reviewed-store-entry").write_text(
        "reviewed store fixture\n",
        encoding="utf-8",
    )
    (pnpm_store / "index/reviewed-index-entry").write_text(
        "reviewed index fixture\n",
        encoding="utf-8",
    )

    gate_source = (ROOT / "scripts/release-gate.sh").read_text(encoding="utf-8")
    gate_source = _replace_once(
        gate_source,
        TOOL_SEARCH_PATH_ASSIGNMENT,
        f"tool_search_path={fake_bin}:$trusted_path",
    )
    gate_source = _replace_once(
        gate_source,
        UV_CACHE_ASSIGNMENT,
        f"uv_cache={uv_cache}",
    )
    gate_source = _replace_once(
        gate_source,
        PLAYWRIGHT_CACHE_ASSIGNMENT,
        f"playwright_cache={playwright_cache}",
    )
    gate_source = _replace_once(
        gate_source,
        PNPM_STORE_ASSIGNMENT,
        f"pnpm_store={pnpm_store}",
    )
    if cleanup_swap_sync is not None:
        pause_path, entered_path = cleanup_swap_sync
        if os.environ.get("TRAVEL_MAP_TEST_LEGACY_PATH_CLEANUP") == "1":
            start = gate_source.index("def remove_private_root() -> None:\n")
            end = gate_source.index("\n\ndef open_requested_record_parent", start)
            legacy_cleanup = f"""def remove_private_root() -> None:
    details = private_root.lstat()
    if (
        private_root.parent != tmp_root
        or not private_root.name.startswith("travel-map-release-environment.")
        or private_root.resolve(strict=True) != private_root
        or not stat.S_ISDIR(details.st_mode)
        or stat.S_IMODE(details.st_mode) != 0o700
        or details.st_uid != os.getuid()
    ):
        raise OSError
    for directory, _children, _files in os.walk(
        private_root, topdown=True, followlinks=False
    ):
        directory = Path(directory)
        details = directory.lstat()
        if directory.name == "projects":
            Path({str(entered_path)!r}).write_text(
                str(private_root), encoding="utf-8"
            )
            while Path({str(pause_path)!r}).exists():
                time.sleep(0.01)
        directory.chmod(0o700)
    shutil.rmtree(private_root)
"""
            gate_source = gate_source[:start] + legacy_cleanup + gate_source[end:]
        else:
            gate_source = _replace_once(
                gate_source,
                (
                    "                    if os.fstat(child) != details:\n"
                    "                        raise OSError\n"
                    "                    remove_contents(child)\n"
                ),
                (
                    "                    if os.fstat(child) != details:\n"
                    "                        raise OSError\n"
                    '                    if name == "projects":\n'
                    f"                        Path({str(entered_path)!r}).write_text(\n"
                    '                            str(private_root), encoding="utf-8"\n'
                    "                        )\n"
                    f"                        while Path({str(pause_path)!r}).exists():\n"
                    "                            time.sleep(0.01)\n"
                    "                    remove_contents(child)\n"
                ),
            )
    if cleanup_file_swap_sync is not None:
        pause_path, entered_path = cleanup_file_swap_sync
        start = gate_source.index("def remove_private_root() -> None:\n")
        anchor = (
            "            details = os.stat(name, dir_fd=directory_descriptor, "
            "follow_symlinks=False)\n"
        )
        position = gate_source.index(anchor, start)
        gate_source = (
            gate_source[:position]
            + anchor
            + '            if name == "cleanup-regular-entry" and not stat.S_ISDIR(details.st_mode):\n'
            + f"                Path({str(entered_path)!r}).write_text(\n"
            + '                    str(private_root), encoding="utf-8"\n'
            + "                )\n"
            + f"                while Path({str(pause_path)!r}).exists():\n"
            + "                    time.sleep(0.01)\n"
            + gate_source[position + len(anchor) :]
        )
    if cleanup_quarantine_swap_sync is not None:
        pause_path, entered_path = cleanup_quarantine_swap_sync
        start = gate_source.index("def remove_private_root() -> None:\n")
        anchor = "        try:\n            os.rename(\n"
        position = gate_source.index(anchor, start)
        gate_source = (
            gate_source[:position]
            + '        if name == "cleanup-regular-entry":\n'
            + f"            Path({str(entered_path)!r}).write_text(\n"
            + '                str(private_root), encoding="utf-8"\n'
            + "            )\n"
            + f"            while Path({str(pause_path)!r}).exists():\n"
            + "                time.sleep(0.01)\n"
            + gate_source[position:]
        )
    if cleanup_quarantine_mismatch_sync is not None:
        pause_path, entered_path = cleanup_quarantine_mismatch_sync
        start = gate_source.index("def remove_private_root() -> None:\n")
        anchor = (
            "            ):\n                # Leaving an unexpected entry quarantined"
        )
        position = gate_source.index(anchor, start)
        gate_source = (
            gate_source[:position]
            + "            ):\n"
            + f"                Path({str(entered_path)!r}).write_text(\n"
            + '                    str(private_root), encoding="utf-8"\n'
            + "                )\n"
            + f"                while Path({str(pause_path)!r}).exists():\n"
            + "                    time.sleep(0.01)\n"
            + gate_source[position + len("            ):\n") :]
        )
    if cleanup_root_swap_sync is not None:
        pause_path, entered_path = cleanup_root_swap_sync
        root_cleanup = gate_source.index("def remove_private_root() -> None:\n")
        pause_anchor = "            if bound_root != expected:\n"
        pause_position = gate_source.index(pause_anchor, root_cleanup)
        original = (
            pause_anchor
            + "                raise OSError\n"
            + "            remove_contents(root_descriptor)\n"
        )
        replacement = (
            pause_anchor
            + "                raise OSError\n"
            + f"            Path({str(entered_path)!r}).write_text(\n"
            + '                str(private_root), encoding="utf-8"\n'
            + "            )\n"
            + f"            while Path({str(pause_path)!r}).exists():\n"
            + "                time.sleep(0.01)\n"
            + "            remove_contents(root_descriptor)\n"
        )
        gate_source = (
            gate_source[:pause_position]
            + replacement
            + gate_source[pause_position + len(original) :]
        )
    if cleanup_root_prebind_sync is not None:
        pause_path, entered_path = cleanup_root_prebind_sync
        root_cleanup = gate_source.index("discard_private_environment() {\n")
        anchor = "        candidates = []\n"
        position = gate_source.index(anchor, root_cleanup)
        gate_source = (
            gate_source[:position]
            + f"        Path({str(entered_path)!r}).write_text(\n"
            + '            f"{root}\\n{os.getpid()}", encoding="utf-8"\n'
            + "        )\n"
            + "        import time\n"
            + f"        while Path({str(pause_path)!r}).exists():\n"
            + "            time.sleep(0.01)\n"
            + gate_source[position:]
        )
    if cleanup_pause is not None:
        pause_path, entered_path = cleanup_pause
        gate_source = _replace_once(
            gate_source,
            "def remove_private_root() -> None:\n",
            (
                "def remove_private_root() -> None:\n"
                f"    Path({str(entered_path)!r}).write_text(\n"
                '        str(private_root), encoding="utf-8"\n'
                "    )\n"
                f"    while Path({str(pause_path)!r}).exists():\n"
                "        time.sleep(0.01)\n"
            ),
        )
        gate_source = _replace_once(
            gate_source,
            "trusted_uv_cache=$UV_CACHE_DIR\n",
            (
                'fast_git_sha=$(/usr/bin/git -C "$script_directory/../../.." '
                "rev-parse HEAD)\n"
                '/usr/bin/python3 -I -S - "$RELEASE_GATE_IMAGE_RECORD" '
                '"$fast_git_sha" "$NAS_PLATFORM" <<\'PY\'\n'
                "import os\n"
                "import sys\n"
                "record, git_sha, platform = sys.argv[1:]\n"
                "descriptor = os.open(record, os.O_WRONLY | os.O_CREAT | "
                "os.O_EXCL, 0o600)\n"
                "with os.fdopen(descriptor, 'w', encoding='ascii') as output:\n"
                "    output.write(\n"
                "        f'imageTag=seoul-education-travel-map:release-gate-{git_sha}\\n'\n"
                "        + 'imageId=sha256:" + "a" * 64 + "\\n'\n"
                "        + f'platform={platform}\\n'\n"
                "        + f'gitSha={git_sha}\\n'\n"
                "    )\n"
                "PY\n"
                "exit 0\n"
                "trusted_uv_cache=$UV_CACHE_DIR\n"
            ),
        )
    gate = scripts / "release-gate.sh"
    _write_executable(gate, gate_source)
    shutil.copy2(ROOT / "scripts/materialize-pinned-source.py", scripts)
    (scripts / "prepare-release-context.py").write_text(
        "raise SystemExit('fake uv owns this boundary')\n",
        encoding="utf-8",
    )
    app = repository / "apps/travel-map/app"
    app.mkdir(parents=True)
    (app / "reviewed.py").write_text("reviewed\n", encoding="utf-8")
    (repository / "apps/travel-map/uv.lock").write_text(
        "reviewed lock\n", encoding="utf-8"
    )
    (repository / ".gitignore").write_text(
        """apps/travel-map/app/injected.py
apps/travel-map/conftest.py
apps/travel-map/e2e/
apps/travel-map/.venv/
apps/travel-map/node_modules/
""",
        encoding="utf-8",
    )
    _git(repository, "init", "-q")
    _git(repository, "config", "user.name", "Release Test")
    _git(repository, "config", "user.email", "release-test@example.invalid")
    _git(repository, "add", ".")
    _git(repository, "commit", "-qm", "reviewed source")

    (app / "injected.py").write_text("raise SystemExit('ambient app')\n")
    (repository / "apps/travel-map/conftest.py").write_text(
        "raise SystemExit('ambient conftest')\n"
    )
    for relative in (
        "apps/travel-map/e2e/injected.spec.ts",
        "apps/travel-map/.venv/ambient.txt",
        "apps/travel-map/node_modules/ambient.txt",
    ):
        injected = repository / relative
        injected.parent.mkdir(parents=True, exist_ok=True)
        injected.write_text("ambient ignored injection\n", encoding="utf-8")
    return repository, gate, fake_bin, events


def _protected_docker_config(
    tmp_path: Path,
    *,
    payload: str | None = None,
) -> Path:
    if payload is None:
        docker_config, _ = _sanitized_docker_context(
            tmp_path,
            host="unix://" + str(_release_test_socket_path(tmp_path)),
        )
        return docker_config
    docker_config = tmp_path / "protected-docker"
    docker_config.mkdir(mode=0o700)
    config_json = docker_config / "config.json"
    config_json.write_text(payload, encoding="utf-8")
    config_json.chmod(0o600)
    return docker_config


def _sanitized_docker_context(
    tmp_path: Path,
    *,
    host: str,
) -> tuple[Path, Path]:
    context_name = "travel-map-release-local"
    docker_config = tmp_path / "sanitized-docker"
    docker_config.mkdir(mode=0o700)
    config_json = docker_config / "config.json"
    config_json.write_text(
        json.dumps({"currentContext": context_name}, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    config_json.chmod(0o600)
    context_id = hashlib.sha256(context_name.encode("utf-8")).hexdigest()
    context_root = docker_config / "contexts" / "meta" / context_id
    context_root.mkdir(parents=True, mode=0o700)
    for directory in (
        docker_config / "contexts",
        docker_config / "contexts" / "meta",
        context_root,
    ):
        directory.chmod(0o700)
    metadata_path = context_root / "meta.json"
    metadata_path.write_text(
        json.dumps(
            {
                "Name": context_name,
                "Metadata": {},
                "Endpoints": {
                    "docker": {"Host": host, "SkipTLSVerify": False},
                },
            },
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    metadata_path.chmod(0o600)
    return docker_config, metadata_path


def _release_gate_invocation(
    tmp_path: Path,
    gate: Path,
    *,
    docker_config: Path,
) -> tuple[list[str], Path, dict[str, str], Path]:
    record_parent = tmp_path / "record"
    record_parent.mkdir(mode=0o700)
    record = record_parent / "gated-image.record"
    attacker_tmp = tmp_path / "attacker-tmp"
    attacker_tmp.mkdir()
    attacker_python = tmp_path / "attacker-python"
    attacker_python.mkdir()
    (attacker_python / "sitecustomize.py").write_text(
        "raise SystemExit('ambient sitecustomize executed')\n"
    )
    environment = dict(os.environ)
    environment.update(
        {
            "PATH": "/nonexistent",
            "HOME": str(tmp_path / "ambient-home"),
            "TMPDIR": str(attacker_tmp),
            "PYTHONPATH": str(attacker_python),
            "PYTHONWARNINGS": "ignore",
            "PYTEST_ADDOPTS": "--collect-only",
            "GIT_DIR": str(tmp_path / "attacker-git-dir"),
            "GIT_WORK_TREE": str(tmp_path / "attacker-git-worktree"),
            "DOCKER_CONFIG": str(docker_config),
            "DOCKER_HOST": "tcp://attacker.invalid:2375",
            "DOCKER_CONTEXT": "ambient-context",
            "BUILDKIT_HOST": "tcp://attacker.invalid:1234",
            "TRAVEL_MAP_RELEASE_DOCKER_HOST": (
                "unix://" + str(tmp_path / "attacker-docker.sock")
            ),
            "KAKAO_REST_API_KEY": "ambient-rest-secret",
            "SEOUL_TRANSIT_SERVICE_KEY": "ambient-transit-secret",
            "OPINET_CERT_KEY": "ambient-opinet-secret",
            "KAKAO_OIDC_CLIENT_ID": "ambient-oidc-id",
            "KAKAO_OIDC_CLIENT_SECRET": "ambient-oidc-secret",
            "NAS_PLATFORM": "linux/amd64",
            "RELEASE_GATE_IMAGE_RECORD": str(record),
        }
    )
    return ["/bin/sh", str(gate)], gate.parents[3], environment, record


def _run_release_gate(
    tmp_path: Path,
    gate: Path,
    *,
    docker_config: Path,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    command, cwd, environment, record = _release_gate_invocation(
        tmp_path,
        gate,
        docker_config=docker_config,
    )
    return (
        subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        ),
        record,
    )


def _inject_direct_materializer_failure(repository: Path, gate: Path) -> None:
    source = gate.read_text(encoding="utf-8")
    pnpm_clone_start = source.index("def clone_pnpm_store")
    uv_clone_start = source.index("def clone_uv_cache", pnpm_clone_start)
    pnpm_clone_source = _replace_once(
        source[pnpm_clone_start:uv_clone_start],
        "                    if clone(source_file, destination_descriptor, name.encode(), 0) != 0:\n"
        '                        raise OSError(ctypes.get_errno(), "fclonefileat")\n',
        "                    if clone(source_file, destination_descriptor, name.encode(), 0) != 0:\n"
        '                        raise OSError(ctypes.get_errno(), "fclonefileat")\n'
        '                    raise OSError(95, "injected partial fclone failure")\n',
    )
    _write_executable(
        gate,
        source[:pnpm_clone_start] + pnpm_clone_source + source[uv_clone_start:],
    )
    _git(repository, "add", str(gate.relative_to(repository)))
    _git(repository, "commit", "-qm", "inject direct materializer failure")


def _wait_for_release_gate_anchor(
    process: subprocess.Popen[str], anchor: Path, *, timeout: float = 15
) -> None:
    deadline = time.monotonic() + timeout
    while not anchor.exists():
        assert process.poll() is None, "release gate exited before its race anchor"
        assert time.monotonic() < deadline, "release gate did not reach its race anchor"
        time.sleep(0.01)


# Production break caught: ignored files and ambient Python/Git/Docker/provider
# state can otherwise change what a standalone gate verifies and sends to Docker.
def test_release_gate_uses_clean_environment_and_exact_head_source(
    tmp_path: Path,
) -> None:
    repository, gate, _, events_path = _release_gate_repository(tmp_path)
    docker_config, _ = _sanitized_docker_context(
        tmp_path,
        host="unix://" + str(_release_test_socket_path(tmp_path)),
    )

    completed, record = _run_release_gate(
        tmp_path,
        gate,
        docker_config=docker_config,
    )

    assert completed.returncode == 0
    assert completed.stdout == "ENCRYPTED_STORAGE_IMAGE_GATE_OK\n"
    assert completed.stderr == ""
    assert record.read_text(encoding="ascii").splitlines() == [
        f"imageTag=seoul-education-travel-map:release-gate-{_git(repository, 'rev-parse', 'HEAD')}",
        "imageId=sha256:" + "a" * 64,
        "platform=linux/amd64",
        f"gitSha={_git(repository, 'rev-parse', 'HEAD')}",
    ]
    events = [json.loads(line) for line in events_path.read_text().splitlines()]
    first_uv = next(event for event in events if event["tool"] == "uv")
    assert Path(first_uv["cwd"]).name == "pinned-source"
    assert first_uv["reviewed"] == "reviewed\n"
    assert first_uv["ignored_present"] == []
    assert first_uv["home_mode"] == 0o700
    assert first_uv["docker_config_mode"] == 0o700
    assert first_uv["docker_json_mode"] == 0o600
    assert first_uv["docker_config_payload"] == "{}\n"
    assert not events_path.with_suffix(".core-injection").exists()
    environment = set(first_uv["environment"])
    assert not environment.intersection(
        {
            "PYTHONPATH",
            "PYTEST_ADDOPTS",
            "GIT_DIR",
            "GIT_WORK_TREE",
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "BUILDKIT_HOST",
            "KAKAO_REST_API_KEY",
            "SEOUL_TRANSIT_SERVICE_KEY",
            "OPINET_CERT_KEY",
            "KAKAO_OIDC_CLIENT_ID",
            "KAKAO_OIDC_CLIENT_SECRET",
        }
    )
    uv_events = [event for event in events if event["tool"] == "uv"]
    assert uv_events
    assert all("--locked" in event["args"] for event in uv_events)
    assert all(event["uv_python"].endswith("/bin/python3.12") for event in uv_events)
    assert all(event["uv_python_downloads"] == "never" for event in uv_events)
    assert all(event["uv_offline"] == "1" for event in uv_events)
    direct_uv_events = [
        event for event in uv_events if "--nested-resolution-probe" not in event["args"]
    ]
    assert all("--no-python-downloads" in event["args"] for event in direct_uv_events)
    assert all("--offline" in event["args"] for event in direct_uv_events)
    prepare_event = next(
        event
        for event in uv_events
        if any(
            argument.endswith("/prepare-release-context.py")
            for argument in event["args"]
        )
    )
    assert Path(prepare_event["cwd"]).name == "pristine-source"
    assert prepare_event["reviewed"] == "reviewed\n"
    pytest_event = next(event for event in uv_events if "pytest" in event["args"])
    assert (
        pytest_event["uv_project_environment"]
        != prepare_event["uv_project_environment"]
    )
    assert pytest_event["uv_cache_dir"] != prepare_event["uv_cache_dir"]
    assert prepare_event["python_environment_polluted"] is False
    assert any(
        event["tool"] == "nested-node" and "--nested-resolution-probe" in event["args"]
        for event in events
    )
    assert any(
        event["tool"] == "uv" and "--nested-resolution-probe" in event["args"]
        for event in events
    )
    pnpm_events = [event for event in events if event["tool"] == "pnpm"]
    assert pnpm_events
    assert any("--offline" in event["args"] for event in pnpm_events)
    assert all(
        "/pnpm-package/bin/pnpm.mjs" in event["executable"] for event in pnpm_events
    )
    docker_events = [event for event in events if event["tool"] == "docker"]
    assert docker_events
    expected_host = "unix://" + str(_release_test_socket_path(tmp_path))
    assert any(event["args"][:2] == ["context", "inspect"] for event in docker_events)
    assert all(
        event["docker_host"] == expected_host
        for event in docker_events
        if event["args"][:2] != ["context", "inspect"]
    )


def test_release_gate_uses_owner_private_cache_for_initial_uv_python_find(
    tmp_path: Path,
) -> None:
    _, gate, _, events_path = _release_gate_repository(tmp_path)
    shared_uv_cache = tmp_path / "uv-cache"

    completed, record = _run_release_gate(
        tmp_path,
        gate,
        docker_config=_protected_docker_config(tmp_path),
    )

    assert completed.returncode == 0
    assert record.exists()
    find_event = json.loads(events_path.with_suffix(".uv-python-find").read_text())
    assert find_event["environment"].get("UV_CACHE_DIR") != str(shared_uv_cache)
    assert str(shared_uv_cache) not in find_event["args"]
    assert str(shared_uv_cache) not in find_event["environment"].values()


def test_release_gate_tolerates_unrelated_tmp_entry_disappearing_during_bootstrap_cleanup(
    tmp_path: Path,
) -> None:
    repository, gate, _, _ = _release_gate_repository(tmp_path)
    probe = (
        Path("/private/tmp")
        / f"travel-map-release-unrelated-{os.getpid()}-{tmp_path.name}"
    )
    with probe.open("x", encoding="ascii") as output:
        output.write("probe\n")
    try:
        source = gate.read_text(encoding="utf-8")
        anchor = "        for candidate in os.listdir(parent_descriptor):\n"
        assert source.count(anchor) >= 1
        replacement = (
            "        listed_candidates = os.listdir(parent_descriptor)\n"
            f"        Path({str(probe)!r}).unlink(missing_ok=True)\n"
            "        for candidate in listed_candidates:\n"
        )
        source = source.replace(anchor, replacement, 1)
        assert source.count(replacement) == 1
        _write_executable(gate, source)
        _git(repository, "add", str(gate.relative_to(repository)))
        _git(repository, "commit", "-qm", "inject unrelated bootstrap cleanup race")

        completed, record = _run_release_gate(
            tmp_path,
            gate,
            docker_config=_protected_docker_config(tmp_path),
        )

        assert completed.returncode == 0
        assert completed.stdout == "ENCRYPTED_STORAGE_IMAGE_GATE_OK\n"
        assert completed.stderr == ""
        assert record.read_text(encoding="ascii").splitlines() == [
            f"imageTag=seoul-education-travel-map:release-gate-{_git(repository, 'rev-parse', 'HEAD')}",
            "imageId=sha256:" + "a" * 64,
            "platform=linux/amd64",
            f"gitSha={_git(repository, 'rev-parse', 'HEAD')}",
        ]
        assert not probe.exists()
    finally:
        probe.unlink(missing_ok=True)


def test_release_gate_cleans_bootstrap_cache_when_signaled_during_uv_python_find(
    tmp_path: Path,
) -> None:
    _, gate, fake_bin, events_path = _release_gate_repository(tmp_path)
    pause_path = events_path.with_suffix(".uv-python-find-pause")
    entered_path = events_path.with_suffix(".uv-python-find-entered")
    pause_path.write_text("pause\n", encoding="utf-8")
    uv = fake_bin / "uv"
    source = uv.read_text(encoding="utf-8")
    find_start = source.index('if args[:2] == ["python", "find"]:')
    print_position = source.index("    print(", find_start)
    source = (
        source[:print_position]
        + f'    Path({str(entered_path)!r}).write_text("entered\\n", encoding="utf-8")\n'
        + f"    while Path({str(pause_path)!r}).exists():\n"
        + "        import time\n"
        + "        time.sleep(0.01)\n"
        + source[print_position:]
    )
    _write_executable(uv, source)

    command, cwd, environment, record = _release_gate_invocation(
        tmp_path,
        gate,
        docker_config=_protected_docker_config(tmp_path),
    )
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    bootstrap_root: Path | None = None
    bootstrap_survived = False
    try:
        _wait_for_release_gate_anchor(process, entered_path)
        find_event = json.loads(
            events_path.with_suffix(".uv-python-find").read_text(encoding="utf-8")
        )
        bootstrap_root = Path(find_event["environment"]["UV_CACHE_DIR"])
        assert bootstrap_root.is_dir()
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=10)
    finally:
        pause_path.unlink(missing_ok=True)
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        stdout, stderr = process.communicate(timeout=5)
        if bootstrap_root is not None:
            bootstrap_survived = bootstrap_root.exists()
            if bootstrap_survived:
                shutil.rmtree(bootstrap_root)

    assert process.returncode != 0
    assert stdout == ""
    assert "Traceback" not in stderr
    assert not record.exists()
    assert not bootstrap_survived


@pytest.mark.parametrize("root_kind", ("bootstrap", "main"))
def test_release_gate_binds_private_root_identity_at_creation_before_path_capture(
    tmp_path: Path,
    root_kind: str,
) -> None:
    pause_path = tmp_path / f"{root_kind}-root-create.pause"
    entered_path = tmp_path / f"{root_kind}-root-create.entered"
    invocation_path = tmp_path / f"{root_kind}-root-create.invocations"
    pause_path.write_text("pause\n", encoding="utf-8")
    repository, gate, fake_bin, events_path = _release_gate_repository(tmp_path)
    if root_kind == "bootstrap":
        uv = fake_bin / "uv"
        uv_source = uv.read_text(encoding="utf-8")
        find_start = uv_source.index('if args[:2] == ["python", "find"]:')
        sync_start = uv_source.index('if "sync" in args', find_start)
        find_source = uv_source[find_start:sync_start]
        find_source = _replace_once(
            find_source,
            "    raise SystemExit(0)\n",
            "    raise SystemExit(88)\n",
        )
        _write_executable(
            uv, uv_source[:find_start] + find_source + uv_source[sync_start:]
        )
    source = gate.read_text(encoding="utf-8")
    anchor = "        else:\n            raise OSError\n"
    target_invocation = 1 if root_kind == "bootstrap" else 2
    injection = (
        f"        invocation_path = Path({str(invocation_path)!r})\n"
        "        invocation = (\n"
        "            int(invocation_path.read_text(encoding='ascii')) + 1\n"
        "            if invocation_path.exists()\n"
        "            else 1\n"
        "        )\n"
        f"        invocation_path.write_text(str(invocation), encoding='ascii')\n"
        f"        if invocation == {target_invocation}:\n"
        f"            Path({str(entered_path)!r}).write_text(str(parent / name), encoding='utf-8')\n"
        f"            while Path({str(pause_path)!r}).exists():\n"
        "                __import__('time').sleep(0.01)\n"
    )
    source = _replace_once(source, anchor, anchor + injection)
    _write_executable(gate, source)
    _git(repository, "add", str(gate.relative_to(repository)))
    _git(repository, "commit", "-qm", f"race {root_kind} root creation")

    command, cwd, environment, record = _release_gate_invocation(
        tmp_path,
        gate,
        docker_config=_protected_docker_config(tmp_path),
    )
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    private_root: Path | None = None
    displaced: Path | None = None
    replacement: Path | None = None
    replacement_survived = False
    displaced_survived = False
    stdout = stderr = ""
    communicated = False
    try:
        _wait_for_release_gate_anchor(process, entered_path, timeout=30)
        assert invocation_path.read_text(encoding="ascii") == str(target_invocation)
        private_root = Path(entered_path.read_text(encoding="utf-8").strip())
        assert private_root.is_dir()
        displaced = private_root.with_name(private_root.name + ".displaced")
        os.replace(private_root, displaced)
        replacement = private_root
        replacement.mkdir(mode=0o700)
        unrelated = replacement / "unrelated-owner-entry"
        unrelated.write_text("must survive\n", encoding="utf-8")
        unrelated.chmod(0o600)
        assert replacement.stat().st_uid == os.getuid()
        if root_kind == "main":
            (replacement / "home").write_text("unrelated blocker\n", encoding="utf-8")
            (replacement / "home").chmod(0o600)
        pause_path.unlink()
        stdout, stderr = process.communicate(timeout=20)
        communicated = True
        replacement_survived = replacement.is_dir()
        displaced_survived = displaced.exists()
        assert process.returncode != 0
        assert stdout == ""
        assert "Traceback" not in stderr
        assert not record.exists()
        assert not events_path.exists()
        assert replacement_survived
        assert (replacement / "unrelated-owner-entry").read_text(encoding="utf-8") == (
            "must survive\n"
        )
        assert not displaced_survived
    finally:
        pause_path.unlink(missing_ok=True)
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        if not communicated:
            stdout, stderr = process.communicate(timeout=5)
        if private_root is not None and private_root.exists():
            for directory, _children, _files in os.walk(
                private_root, topdown=False, followlinks=False
            ):
                Path(directory).chmod(0o700)
            shutil.rmtree(private_root)
        if displaced is not None and displaced.exists():
            for directory, _children, _files in os.walk(
                displaced, topdown=False, followlinks=False
            ):
                Path(directory).chmod(0o700)
            shutil.rmtree(displaced)


def test_release_gate_reaps_bootstrap_find_process_group_on_signal(
    tmp_path: Path,
) -> None:
    _repository, gate, fake_bin, events_path = _release_gate_repository(tmp_path)
    pause_path = events_path.with_suffix(".uv-python-find-group.pause")
    entered_path = events_path.with_suffix(".uv-python-find-group.entered")
    child_info_path = events_path.with_suffix(".uv-python-find-group.child")
    pause_path.write_text("pause\n", encoding="utf-8")
    uv = fake_bin / "uv"
    source = uv.read_text(encoding="utf-8")
    find_start = source.index('if args[:2] == ["python", "find"]:')
    print_position = source.index("    print(", find_start)
    injected = (
        "    import signal\n"
        "    import time\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        '    os.open(os.environ["UV_CACHE_DIR"], os.O_RDONLY)\n'
        "    child_pid = os.fork()\n"
        "    if child_pid == 0:\n"
        "        while True:\n"
        "            time.sleep(1)\n"
        f"    Path({str(child_info_path)!r}).write_text(\n"
        "        f\"{os.getpid()}:{child_pid}:{os.getpgrp()}:{os.environ['UV_CACHE_DIR']}\",\n"
        '        encoding="utf-8",\n'
        "    )\n"
        f'    Path({str(entered_path)!r}).write_text("entered\\n", encoding="utf-8")\n'
        f"    while Path({str(pause_path)!r}).exists():\n"
        "        time.sleep(0.01)\n"
    )
    source = source[:print_position] + injected + source[print_position:]
    _write_executable(uv, source)

    command, cwd, environment, record = _release_gate_invocation(
        tmp_path,
        gate,
        docker_config=_protected_docker_config(tmp_path),
    )
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    bootstrap_root: Path | None = None
    child_pid: int | None = None
    bounded = False
    root_survived = False
    group_reaped = False
    try:
        _wait_for_release_gate_anchor(process, entered_path)
        parent_pid, child_raw, process_group, root_raw = child_info_path.read_text(
            encoding="utf-8"
        ).split(":", 3)
        assert int(parent_pid) != 0
        child_pid = int(child_raw)
        bootstrap_root = Path(root_raw)
        assert int(process_group) == int(parent_pid)
        assert bootstrap_root.is_dir()
        process.send_signal(signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=5)
            bounded = True
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
        assert bounded
        if process.poll() is not None:
            root_survived = bootstrap_root.exists()
    finally:
        pause_path.unlink(missing_ok=True)
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        stdout, stderr = process.communicate(timeout=5)
        if bootstrap_root is not None:
            root_survived = bootstrap_root.exists()
            if root_survived:
                shutil.rmtree(bootstrap_root)
        if child_pid is not None:
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    group_reaped = True
                    break
                time.sleep(0.01)
            else:
                group_reaped = False

    assert bounded
    assert group_reaped
    assert not root_survived
    assert process.returncode == 2
    assert stdout == ""
    assert "Traceback" not in stderr
    assert not record.exists()


@pytest.mark.parametrize("failure", ("source-identity", "docker-validation"))
def test_release_gate_cleans_materialized_private_root_before_supervisor_failure(
    tmp_path: Path,
    failure: str,
) -> None:
    repository, gate, fake_bin, _ = _release_gate_repository(tmp_path)
    entered = tmp_path / f"{failure}-private-root"
    source = gate.read_text(encoding="utf-8")
    probe = f"        printf '%s\n' \"$private_environment\" > {str(entered)!r}\n"

    if failure == "source-identity":
        source = _replace_once(
            source,
            '        [ "$actual_uv_cache_identity" = "$uv_cache_identity" ] \\\n',
            probe + '        [ "$actual_uv_cache_identity" = "forced-mismatch" ] \\\n',
        )
        expected_error = "BLOCKED_UNSAFE_RELEASE_ENVIRONMENT\n"
    else:
        source = _replace_once(
            source,
            "        docker_host=$(/usr/bin/env -i \\\n",
            probe + "        docker_host=$(/usr/bin/env -i \\\n",
        )
        docker = fake_bin / "docker"
        docker_source = docker.read_text(encoding="utf-8")
        docker_source = _replace_once(
            docker_source,
            "if is_context_inspect:\n",
            "if is_context_inspect:\n    print('tcp://attacker.invalid:2375')\n    raise SystemExit(0)\n",
        )
        _write_executable(docker, docker_source)
        expected_error = "BLOCKED_INVALID_DOCKER_CONFIG\n"
    _write_executable(gate, source)
    _git(repository, "add", "apps/travel-map/scripts/release-gate.sh")
    _git(repository, "commit", "-qm", f"force {failure} failure")

    private_root: Path | None = None
    try:
        completed, record = _run_release_gate(
            tmp_path,
            gate,
            docker_config=_protected_docker_config(tmp_path),
        )
        assert completed.returncode == 2
        assert completed.stdout == ""
        assert completed.stderr == expected_error
        assert not record.exists()
        assert entered.exists()
        private_root = Path(entered.read_text(encoding="utf-8").strip())
        assert not private_root.exists()
    finally:
        if private_root is not None and private_root.exists():
            for directory, _children, _files in os.walk(private_root, topdown=False):
                Path(directory).chmod(0o700)
            shutil.rmtree(private_root)


def test_release_gate_finishes_outer_cleanup_after_cleanup_window_signal(
    tmp_path: Path,
) -> None:
    repository, gate, _, _ = _release_gate_repository(tmp_path)
    entered = tmp_path / "outer-cleanup-entered"
    pause = tmp_path / "outer-cleanup-pause"
    pause.write_text("pause\n", encoding="utf-8")
    source = gate.read_text(encoding="utf-8")
    source = _replace_once(
        source,
        '        [ "$actual_uv_cache_identity" = "$uv_cache_identity" ] \\\n',
        '        [ "$actual_uv_cache_identity" = "forced-mismatch" ] \\\n',
    )
    cleanup_start = source.index("discard_private_environment() {\n")
    cleanup_anchor = "    root = Path(sys.argv[1])\n"
    position = source.index(cleanup_anchor, cleanup_start)
    source = (
        source[: position + len(cleanup_anchor)]
        + f"    Path({str(entered)!r}).write_text(str(root), encoding='utf-8')\n"
        + f"    while Path({str(pause)!r}).exists():\n"
        + "        __import__('time').sleep(0.01)\n"
        + source[position + len(cleanup_anchor) :]
    )
    _write_executable(gate, source)
    _git(repository, "add", "apps/travel-map/scripts/release-gate.sh")
    _git(repository, "commit", "-qm", "pause outer release cleanup")

    command, cwd, environment, record = _release_gate_invocation(
        tmp_path,
        gate,
        docker_config=_protected_docker_config(tmp_path),
    )
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    private_root: Path | None = None
    try:
        _wait_for_release_gate_anchor(process, entered, timeout=90)
        private_root = Path(entered.read_text(encoding="utf-8"))
        os.killpg(process.pid, signal.SIGTERM)
        pause.unlink()
        stdout, stderr = process.communicate(timeout=15)
        assert process.returncode == 2
        assert stdout == ""
        assert "Traceback" not in stderr
        assert not record.exists()
        assert not private_root.exists()
    finally:
        pause.unlink(missing_ok=True)
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        if private_root is not None and private_root.exists():
            for directory, _children, _files in os.walk(private_root, topdown=False):
                Path(directory).chmod(0o700)
            shutil.rmtree(private_root)


@pytest.mark.parametrize("forgery", ("extra-field", "symlink"))
def test_release_gate_rejects_forged_sanitized_docker_context(
    tmp_path: Path,
    forgery: str,
) -> None:
    _, gate, _, _events_path = _release_gate_repository(tmp_path)
    docker_config, metadata_path = _sanitized_docker_context(
        tmp_path,
        host="unix://" + str(_release_test_socket_path(tmp_path)),
    )
    if forgery == "extra-field":
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["Injected"] = "ambient"
        metadata_path.write_text(json.dumps(metadata) + "\n", encoding="utf-8")
    else:
        outside = tmp_path / "forged-meta.json"
        metadata_path.replace(outside)
        metadata_path.symlink_to(outside)

    completed, record = _run_release_gate(
        tmp_path,
        gate,
        docker_config=docker_config,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "BLOCKED_INVALID_DOCKER_CONFIG\n"
    assert not record.exists()
    assert "Traceback" not in completed.stderr


def test_release_gate_rejects_remote_docker_context_endpoint(
    tmp_path: Path,
) -> None:
    _, gate, _, _ = _release_gate_repository(tmp_path)

    completed, record = _run_release_gate(
        tmp_path,
        gate,
        docker_config=_protected_docker_config(
            tmp_path,
            payload='{"currentContext":"remote-test"}\n',
        ),
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "BLOCKED_INVALID_DOCKER_CONFIG\n"
    assert not record.exists()
    assert "tcp://attacker.invalid:2375" not in completed.stderr


@pytest.mark.parametrize(
    "socket_attack",
    ("regular", "symlink", "world-writable"),
)
def test_release_gate_rejects_unsafe_local_docker_socket(
    tmp_path: Path,
    socket_attack: str,
) -> None:
    _, gate, _, _events_path = _release_gate_repository(tmp_path)
    socket_path = _release_test_socket_path(tmp_path)
    if socket_attack == "regular":
        _close_release_test_socket(socket_path)
        socket_path.write_text("not a socket\n", encoding="utf-8")
    elif socket_attack == "symlink":
        target = _release_test_socket_path(tmp_path, "-real")
        _close_release_test_socket(socket_path)
        socket_path.symlink_to(target)
    docker_config, _ = _sanitized_docker_context(
        tmp_path,
        host="unix://" + str(socket_path),
    )
    if socket_attack == "world-writable":
        socket_path.chmod(0o666)

    completed, record = _run_release_gate(
        tmp_path,
        gate,
        docker_config=docker_config,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "BLOCKED_INVALID_DOCKER_CONFIG\n"
    assert not record.exists()


def test_release_gate_rejects_stale_uv_lock_without_rewriting_it(
    tmp_path: Path,
) -> None:
    _repository, gate, _, events_path = _release_gate_repository(tmp_path)
    events_path.with_suffix(".stale-lock").write_text("stale\n", encoding="utf-8")

    completed, record = _run_release_gate(
        tmp_path,
        gate,
        docker_config=_protected_docker_config(tmp_path),
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "BLOCKED_INVALID_RELEASE_ARTIFACT\n"
    assert not record.exists()
    events = [json.loads(line) for line in events_path.read_text().splitlines()]
    first_uv = next(event for event in events if event["tool"] == "uv")
    assert "--locked" in first_uv["args"]
    assert not any(
        event["tool"] == "docker" and event["args"][:2] == ["buildx", "build"]
        for event in events
    )


@pytest.mark.parametrize("mutation", ("modify", "delete"))
def test_release_gate_rejects_tracked_source_mutation_after_tests(
    tmp_path: Path,
    mutation: str,
) -> None:
    _, gate, _, events_path = _release_gate_repository(tmp_path)
    events_path.with_suffix(".source-mutation").write_text(
        mutation,
        encoding="utf-8",
    )

    completed, record = _run_release_gate(
        tmp_path,
        gate,
        docker_config=_protected_docker_config(tmp_path),
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "BLOCKED_INVALID_RELEASE_ARTIFACT\n"
    assert not record.exists()
    events = [json.loads(line) for line in events_path.read_text().splitlines()]
    assert not any(
        event["tool"] == "uv"
        and any(
            argument.endswith("/prepare-release-context.py")
            for argument in event["args"]
        )
        for event in events
    )
    assert not any(
        event["tool"] == "docker" and event["args"][:2] == ["buildx", "build"]
        for event in events
    )


def test_playwright_server_uses_the_locked_python_environment() -> None:
    config = (ROOT / "playwright.config.ts").read_text(encoding="utf-8")

    assert (
        'command: "uv run --locked --no-sync --project . uvicorn '
        'app.main:app --host 127.0.0.1 --port 4173"'
    ) in config


def test_release_gate_rejects_test_tampering_with_private_materializer(
    tmp_path: Path,
) -> None:
    _, gate, _, events_path = _release_gate_repository(tmp_path)
    events_path.with_suffix(".helper-mutation").write_text(
        "tamper\n",
        encoding="utf-8",
    )

    completed, record = _run_release_gate(
        tmp_path,
        gate,
        docker_config=_protected_docker_config(tmp_path),
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "BLOCKED_INVALID_RELEASE_ARTIFACT\n"
    assert not record.exists()


def test_release_gate_rejects_extra_file_injected_into_pristine_source(
    tmp_path: Path,
) -> None:
    _, gate, _, events_path = _release_gate_repository(tmp_path)
    events_path.with_suffix(".pristine-extra").write_text(
        "inject\n",
        encoding="utf-8",
    )

    completed, record = _run_release_gate(
        tmp_path,
        gate,
        docker_config=_protected_docker_config(tmp_path),
    )

    assert events_path.with_suffix(".pristine-injected").exists(), completed.stderr
    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "BLOCKED_INVALID_RELEASE_ARTIFACT\n"
    assert not record.exists()


def test_release_gate_reaps_test_descendant_before_preparing_docker_context(
    tmp_path: Path,
) -> None:
    _, gate, _, events_path = _release_gate_repository(tmp_path)
    events_path.with_suffix(".context-injection").write_text(
        "inject\n",
        encoding="utf-8",
    )
    injector_pid_path = events_path.with_suffix(".context-injector-pid")
    injector_pid: int | None = None
    try:
        completed, record = _run_release_gate(
            tmp_path,
            gate,
            docker_config=_protected_docker_config(tmp_path),
        )
        assert injector_pid_path.exists(), completed.stderr
        injector_pid = int(injector_pid_path.read_text(encoding="ascii"))
        try:
            os.kill(injector_pid, 0)
        except ProcessLookupError:
            survived = False
        else:
            survived = True
    finally:
        if injector_pid is None and injector_pid_path.exists():
            injector_pid = int(injector_pid_path.read_text(encoding="ascii"))
        if injector_pid is not None:
            try:
                os.kill(injector_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    assert completed.returncode == 0
    assert record.exists()
    assert survived is False
    events = [json.loads(line) for line in events_path.read_text().splitlines()]
    build_event = next(
        event
        for event in events
        if event["tool"] == "docker" and event["args"][:2] == ["buildx", "build"]
    )
    assert build_event["context_dockerfile"] == "FROM scratch\n"


@pytest.mark.parametrize(
    "attack",
    (
        "resolved-uv",
        "resolved-pnpm",
        "resolved-node",
        "resolved-docker",
        "resolved-docker-buildx",
        "trusted-uv",
        "trusted-node",
        "trusted-docker",
        "trusted-docker-buildx",
        "docker-plugin",
    ),
)
def test_release_gate_rejects_test_poisoning_runtime_trust_anchors(
    tmp_path: Path,
    attack: str,
) -> None:
    _, gate, _, events_path = _release_gate_repository(tmp_path)
    events_path.with_suffix(".anchor-poison").write_text(
        attack + "\n",
        encoding="utf-8",
    )

    completed, record = _run_release_gate(
        tmp_path,
        gate,
        docker_config=_protected_docker_config(tmp_path),
    )

    assert events_path.with_suffix(".anchor-poisoned").read_text() == attack + "\n"
    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "BLOCKED_INVALID_RELEASE_ARTIFACT\n"
    assert not record.exists()
    assert not events_path.with_suffix(".anchor-executed").exists()


def test_release_gate_pins_python_runtime_and_rejects_stdlib_poisoning(
    tmp_path: Path,
) -> None:
    _, gate, _, events_path = _release_gate_repository(tmp_path)
    events_path.with_suffix(".python-runtime-poison").write_text(
        "poison\n",
        encoding="utf-8",
    )

    completed, record = _run_release_gate(
        tmp_path,
        gate,
        docker_config=_protected_docker_config(tmp_path),
    )

    assert events_path.with_suffix(".python-runtime-poisoned").exists(), (
        completed.stderr
    )
    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "BLOCKED_INVALID_RELEASE_ARTIFACT\n"
    assert not record.exists()
    assert not events_path.with_suffix(".python-download-attempt").exists()


def test_release_gate_rejects_pnpm_distribution_bundle_poisoning(
    tmp_path: Path,
) -> None:
    _, gate, _, events_path = _release_gate_repository(tmp_path)
    events_path.with_suffix(".pnpm-bundle-poison").write_text(
        "poison\n",
        encoding="utf-8",
    )

    completed, record = _run_release_gate(
        tmp_path,
        gate,
        docker_config=_protected_docker_config(tmp_path),
    )

    assert events_path.with_suffix(".pnpm-bundle-poisoned").exists(), completed.stderr
    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "BLOCKED_INVALID_RELEASE_ARTIFACT\n"
    assert not record.exists()


def test_release_gate_rejects_playwright_browser_cache_poisoning(
    tmp_path: Path,
) -> None:
    _, gate, _, events_path = _release_gate_repository(tmp_path)
    events_path.with_suffix(".browser-cache-poison").write_text(
        "poison\n",
        encoding="utf-8",
    )

    completed, record = _run_release_gate(
        tmp_path,
        gate,
        docker_config=_protected_docker_config(tmp_path),
    )

    assert events_path.with_suffix(".browser-cache-poisoned").exists(), completed.stderr
    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "BLOCKED_INVALID_RELEASE_ARTIFACT\n"
    assert not record.exists()


@pytest.mark.parametrize(
    "failure_marker",
    ("logs-failure", "unreadable-db"),
)
def test_release_gate_fails_closed_when_plaintext_scan_cannot_read(
    tmp_path: Path,
    failure_marker: str,
) -> None:
    _, gate, _, events_path = _release_gate_repository(tmp_path)
    events_path.with_suffix(f".{failure_marker}").write_text(
        "fail\n",
        encoding="utf-8",
    )

    completed, record = _run_release_gate(
        tmp_path,
        gate,
        docker_config=_protected_docker_config(tmp_path),
    )

    if failure_marker == "unreadable-db":
        assert events_path.with_suffix(".unreadable-db-created").exists()
    assert completed.returncode == 2
    assert completed.stdout == ""
    assert "Traceback" not in completed.stderr
    assert not record.exists()


def test_release_gate_never_exposes_registry_credentials_to_tools(
    tmp_path: Path,
) -> None:
    marker = "write-packages-auth-marker"
    _, gate, _, events_path = _release_gate_repository(tmp_path)
    docker_config = _protected_docker_config(
        tmp_path,
        payload=(
            '{"currentContext":"colima-test",'
            f'"auths":{{"ghcr.io":{{"auth":"{marker}"}}}}}}\n'
        ),
    )

    completed, record = _run_release_gate(
        tmp_path,
        gate,
        docker_config=docker_config,
    )

    assert completed.returncode == 2
    assert not record.exists()
    serialized_events = (
        events_path.read_text(encoding="utf-8") if events_path.exists() else ""
    )
    assert marker not in completed.stdout
    assert marker not in completed.stderr
    assert marker not in serialized_events
    assert completed.stdout == ""
    assert completed.stderr == "BLOCKED_INVALID_DOCKER_CONFIG\n"
    assert serialized_events == ""


def test_release_gate_reaps_descendants_before_committing_success(
    tmp_path: Path,
) -> None:
    _, gate, _, events_path = _release_gate_repository(tmp_path)
    linger_pid_path = events_path.with_suffix(".linger-pid")
    linger_pid_path.write_text("pending", encoding="ascii")
    lingering_pid: int | None = None
    try:
        completed, record = _run_release_gate(
            tmp_path,
            gate,
            docker_config=_protected_docker_config(tmp_path),
        )
        lingering_pid = int(linger_pid_path.read_text(encoding="ascii"))
        try:
            os.kill(lingering_pid, 0)
        except ProcessLookupError:
            survived = False
        else:
            survived = True
    finally:
        if lingering_pid is not None:
            try:
                os.kill(lingering_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    assert completed.returncode == 0
    assert record.exists()
    assert not survived


def test_release_gate_forwards_direct_wrapper_signal(tmp_path: Path) -> None:
    _, gate, _, events_path = _release_gate_repository(tmp_path)
    docker_config = _protected_docker_config(tmp_path)
    command, cwd, environment, record = _release_gate_invocation(
        tmp_path,
        gate,
        docker_config=docker_config,
    )
    pause_path = events_path.with_suffix(".pause")
    entered_path = events_path.with_suffix(".entered")
    pause_path.write_text("pause\n", encoding="utf-8")
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    private_root: Path | None = None
    survived = False
    try:
        deadline = time.monotonic() + 90
        while not entered_path.exists() and process.poll() is None:
            if time.monotonic() >= deadline:
                raise AssertionError("release gate did not reach the signal boundary")
            time.sleep(0.05)
        assert process.poll() is None
        events = [json.loads(line) for line in events_path.read_text().splitlines()]
        first_uv_event = next(event for event in events if event["tool"] == "uv")
        private_root = Path(first_uv_event["home"]).parent
        process.send_signal(signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=5)
            signal_forwarded = True
        except subprocess.TimeoutExpired:
            signal_forwarded = False
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate(timeout=5)
        survived = private_root.exists()
    finally:
        pause_path.unlink(missing_ok=True)
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        if private_root is not None and private_root.exists():
            shutil.rmtree(private_root)

    assert process.returncode != 0
    assert signal_forwarded
    assert stdout == ""
    assert "Traceback" not in stderr
    assert not survived
    assert not record.exists()


@pytest.mark.parametrize(
    "termination_signal",
    (signal.SIGHUP, signal.SIGINT, signal.SIGTERM),
)
def test_release_gate_does_not_spawn_inner_after_pre_spawn_signal(
    tmp_path: Path,
    termination_signal: signal.Signals,
) -> None:
    _, gate, _, events_path = _release_gate_repository(tmp_path)
    pause_path = tmp_path / "release-pre-spawn.pause"
    entered_path = tmp_path / "release-pre-spawn.entered"
    spawned_path = tmp_path / "release-pre-spawn.spawned"
    pause_path.write_text("pause\n", encoding="utf-8")
    source = gate.read_text(encoding="utf-8")
    source = _replace_once(
        source,
        (
            "try:\n"
            "    if interrupted:\n"
            "        raise OSError\n"
            "    process = subprocess.Popen(\n"
            '        ["/bin/sh", script],\n'
        ),
        (
            "original_popen = subprocess.Popen\n"
            "def tracked_popen(*args, **kwargs):\n"
            f"    Path({str(spawned_path)!r}).write_text('spawned\\n', encoding='utf-8')\n"
            "    return original_popen(*args, **kwargs)\n"
            "subprocess.Popen = tracked_popen\n"
            "try:\n"
            f"    Path({str(entered_path)!r}).write_text('entered\\n', encoding='utf-8')\n"
            f"    while Path({str(pause_path)!r}).exists():\n"
            "        time.sleep(0.01)\n"
            "    if interrupted:\n"
            "        raise OSError\n"
            "    process = subprocess.Popen(\n"
            '        ["/bin/sh", script],\n'
        ),
    )
    _write_executable(gate, source)
    command, cwd, environment, record = _release_gate_invocation(
        tmp_path,
        gate,
        docker_config=_protected_docker_config(tmp_path),
    )
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 30
        while not entered_path.exists() and process.poll() is None:
            if time.monotonic() >= deadline:
                raise AssertionError("release supervisor did not reach pre-spawn pause")
            time.sleep(0.02)
        assert process.poll() is None
        process.send_signal(termination_signal)
        pause_path.unlink()
        stdout, stderr = process.communicate(timeout=10)
    finally:
        pause_path.unlink(missing_ok=True)
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)

    assert process.returncode == 2
    assert stdout == ""
    assert "Traceback" not in stderr
    assert not spawned_path.exists()
    assert not record.exists()
    events = [json.loads(line) for line in events_path.read_text().splitlines()]
    assert all(event["tool"] == "docker" for event in events)
    assert all(event["args"][:2] == ["context", "inspect"] for event in events)


def _fast_release_commit_gate(tmp_path: Path) -> tuple[Path, Path]:
    repository, gate, _, _ = _release_gate_repository(tmp_path)
    source = gate.read_text(encoding="utf-8")
    source = _replace_once(
        source,
        "trusted_uv_cache=$UV_CACHE_DIR\n",
        (
            'fast_git_sha=$(/usr/bin/git -C "$script_directory/../../.." '
            "rev-parse HEAD)\n"
            '/usr/bin/python3 -I -S - "$RELEASE_GATE_IMAGE_RECORD" '
            '"$fast_git_sha" "$NAS_PLATFORM" <<\'PY\'\n'
            "import os\n"
            "import sys\n"
            "record, git_sha, platform = sys.argv[1:]\n"
            "descriptor = os.open(record, os.O_WRONLY | os.O_CREAT | "
            "os.O_EXCL, 0o600)\n"
            "with os.fdopen(descriptor, 'w', encoding='ascii') as output:\n"
            "    output.write(\n"
            "        f'imageTag=seoul-education-travel-map:release-gate-{git_sha}\\n'\n"
            "        + 'imageId=sha256:" + "a" * 64 + "\\n'\n"
            "        + f'platform={platform}\\n'\n"
            "        + f'gitSha={git_sha}\\n'\n"
            "    )\n"
            "    output.flush()\n"
            "    os.fsync(output.fileno())\n"
            "PY\n"
            "printf '%s\\n' 'ENCRYPTED_STORAGE_IMAGE_GATE_OK'\n"
            "exit 0\n"
            "trusted_uv_cache=$UV_CACHE_DIR\n"
        ),
    )
    _write_executable(gate, source)
    docker_config = _protected_docker_config(tmp_path)
    assert _git(repository, "rev-parse", "HEAD")
    return gate, docker_config


def test_release_gate_does_not_delete_foreign_record_after_link_swap(
    tmp_path: Path,
) -> None:
    gate, docker_config = _fast_release_commit_gate(tmp_path)
    swapped = tmp_path / "record-link-swapped"
    source = gate.read_text(encoding="utf-8")
    source = _replace_once(
        source,
        "def commit_record(payload: bytes | None) -> tuple[int, tuple[int, int]]:\n",
        (
            "original_link = os.link\n"
            "def injected_link(*args, **kwargs):\n"
            "    original_link(*args, **kwargs)\n"
            "    requested_record.unlink()\n"
            "    descriptor = os.open(\n"
            "        requested_record, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600\n"
            "    )\n"
            "    with os.fdopen(descriptor, 'wb') as output:\n"
            "        output.write(b'foreign-record\\n')\n"
            "        output.flush()\n"
            "        os.fsync(output.fileno())\n"
            f"    Path({str(swapped)!r}).write_text('swapped\\n', encoding='utf-8')\n"
            "os.link = injected_link\n\n"
            "def commit_record(payload: bytes | None) -> tuple[int, tuple[int, int]]:\n"
        ),
    )
    _write_executable(gate, source)

    completed, record = _run_release_gate(
        tmp_path,
        gate,
        docker_config=docker_config,
    )

    assert swapped.exists()
    assert completed.returncode == 2
    assert completed.stdout == ""
    assert "Traceback" not in completed.stderr
    assert record.read_bytes() == b"foreign-record\n"


def test_release_gate_does_not_emit_evidence_when_record_link_fails(
    tmp_path: Path,
) -> None:
    gate, docker_config = _fast_release_commit_gate(tmp_path)
    failed = tmp_path / "record-link-failed"
    source = gate.read_text(encoding="utf-8")
    source = _replace_once(
        source,
        "def commit_record(payload: bytes | None) -> tuple[int, tuple[int, int]]:\n",
        (
            "def injected_link(*_args, **_kwargs):\n"
            f"    Path({str(failed)!r}).write_text('failed\\n', encoding='utf-8')\n"
            "    raise OSError('injected record link failure')\n"
            "os.link = injected_link\n\n"
            "def commit_record(payload: bytes | None) -> tuple[int, tuple[int, int]]:\n"
        ),
    )
    _write_executable(gate, source)

    completed, record = _run_release_gate(
        tmp_path,
        gate,
        docker_config=docker_config,
    )

    assert failed.exists()
    assert completed.returncode == 2
    assert completed.stdout == ""
    assert "Traceback" not in completed.stderr
    assert not record.exists()


def test_release_gate_rolls_back_owned_record_when_directory_fsync_fails(
    tmp_path: Path,
) -> None:
    gate, docker_config = _fast_release_commit_gate(tmp_path)
    failed = tmp_path / "record-dir-fsync-failed"
    source = gate.read_text(encoding="utf-8")
    source = _replace_once(
        source,
        "def commit_record(payload: bytes | None) -> tuple[int, tuple[int, int]]:\n",
        (
            "original_fsync = os.fsync\n"
            "fsync_calls = 0\n"
            "def injected_fsync(descriptor):\n"
            "    global fsync_calls\n"
            "    fsync_calls += 1\n"
            "    if fsync_calls == 2:\n"
            f"        Path({str(failed)!r}).write_text('failed\\n', encoding='utf-8')\n"
            "        raise OSError('injected directory fsync failure')\n"
            "    return original_fsync(descriptor)\n"
            "os.fsync = injected_fsync\n\n"
            "def commit_record(payload: bytes | None) -> tuple[int, tuple[int, int]]:\n"
        ),
    )
    _write_executable(gate, source)

    completed, record = _run_release_gate(
        tmp_path,
        gate,
        docker_config=docker_config,
    )

    assert failed.exists()
    assert completed.returncode == 2
    assert completed.stdout == ""
    assert "Traceback" not in completed.stderr
    assert not record.exists()


def test_release_gate_rolls_back_owned_record_when_evidence_pipe_closes(
    tmp_path: Path,
) -> None:
    gate, docker_config = _fast_release_commit_gate(tmp_path)
    command, cwd, environment, record = _release_gate_invocation(
        tmp_path,
        gate,
        docker_config=docker_config,
    )
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    process.stdout.close()
    stderr = process.stderr.read() if process.stderr is not None else ""
    returncode = process.wait(timeout=15)

    assert returncode == 2
    assert "Traceback" not in stderr
    assert not record.exists()


@pytest.mark.parametrize(
    "termination_signal",
    (signal.SIGHUP, signal.SIGINT, signal.SIGTERM),
)
def test_release_gate_does_not_commit_record_during_cleanup_signal_window(
    tmp_path: Path,
    termination_signal: signal.Signals,
) -> None:
    pause_path = tmp_path / "release-cleanup.pause"
    entered_path = tmp_path / "release-cleanup.entered"
    pause_path.write_text("pause\n", encoding="utf-8")
    _, gate, _, _ = _release_gate_repository(
        tmp_path,
        cleanup_pause=(pause_path, entered_path),
    )
    command, cwd, environment, record = _release_gate_invocation(
        tmp_path,
        gate,
        docker_config=_protected_docker_config(tmp_path),
    )
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    private_root: Path | None = None
    try:
        deadline = time.monotonic() + 90
        while not entered_path.exists() and process.poll() is None:
            if time.monotonic() >= deadline:
                raise AssertionError("release supervisor did not enter cleanup")
            time.sleep(0.05)
        assert process.poll() is None
        private_root = Path(entered_path.read_text(encoding="utf-8"))
        committed_before_signal = record.exists()
        process.send_signal(termination_signal)
        pause_path.unlink()
        stdout, stderr = process.communicate(timeout=10)
    finally:
        pause_path.unlink(missing_ok=True)
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        if private_root is not None and private_root.exists():
            shutil.rmtree(private_root)

    assert process.returncode == 2
    assert not committed_before_signal
    assert not record.exists()
    assert private_root is not None and not private_root.exists()
    assert "Traceback" not in stderr
    assert "gated-image.record" not in stdout


def _publisher_signal_fixture(
    tmp_path: Path,
    *,
    inner_body: str | None = None,
    cleanup_pause: tuple[Path, Path] | None = None,
    cleanup_root_marker: Path | None = None,
    before_supervisor_body: str | None = None,
    before_stage_b_body: str | None = None,
    signal_boundary: tuple[str, Path, Path, Path, Path] | None = None,
    supervisor_output_attack: tuple[str, Path] | None = None,
    late_output_hardlink_attack: tuple[str, Path] | None = None,
    release_gate_marker: Path | None = None,
    authority_open_probe: Path | None = None,
    private_launcher_failure_probe: Path | None = None,
    launcher_root_marker: Path | None = None,
    malformed_launcher_identity_probe: Path | None = None,
    launcher_exec_handoff: tuple[Path, Path] | None = None,
    repeated_signal_window: tuple[Path, Path] | None = None,
    final_publication_window: tuple[Path, Path] | None = None,
    first_launcher_exec_window: tuple[Path, Path] | None = None,
    launcher_script_carrier_window: tuple[Path, Path] | None = None,
    stage_b_script_carrier_window: tuple[Path, Path] | None = None,
    launcher_output_fd_attack: tuple[Path, Path, Path] | None = None,
    supervisor_output_fd_attack: tuple[Path, Path, Path] | None = None,
    launcher_handoff_fd_attack: tuple[Path, Path, Path, Path] | None = None,
    after_stage_b_spawn_body: str | None = None,
    cleanup_final_stat_race: tuple[str, Path] | None = None,
    cleanup_restore_race: tuple[Path, Path] | None = None,
    stage_b_pending_signal_window: tuple[Path, Path] | None = None,
    top_exit_observer_failure: Path | None = None,
    post_leader_exit_body: str | None = None,
    publisher_source_transform=None,
    short_stdout_write: bool = False,
) -> tuple[Path, Path, Path, Path, str, list[str]]:
    repository = tmp_path / "publisher-repository"
    travel_root = repository / "apps/travel-map"
    publisher = travel_root / "deploy/nas/publish-reviewed-image.sh"
    publisher.parent.mkdir(parents=True)
    fake_bin = tmp_path / "publisher-safe-bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "python3",
        "#!/bin/sh\nprintf '%s\\n' 'ambient PATH python3 executed' >&2\nexit 97\n",
    )
    publisher_socket = _release_test_socket_path(tmp_path, "-publisher")
    publisher_docker_host = "unix://" + str(publisher_socket)
    _write_executable(
        fake_bin / "docker",
        "#!/bin/sh\n"
        f"printf '%s\\n' docker >> {str(tmp_path / 'publisher-docker-ran')!r}\n"
        "exit 97\n",
    )
    shutil.copy2(fake_bin / "docker", fake_bin / "docker-buildx")
    (fake_bin / "docker-buildx").chmod(0o755)
    fd_attack_script: Path | None = None
    if (
        launcher_output_fd_attack is not None
        or supervisor_output_fd_attack is not None
        or launcher_handoff_fd_attack is not None
    ):
        fd_attack_script = tmp_path / "retained-fd-attack.py"
        _write_executable(
            fd_attack_script,
            """#!/usr/bin/python3
import os
import re
import signal
import sys
import time
from pathlib import Path

mode, pathname, ready_raw, second_raw, third_raw = sys.argv[1:]
ready = Path(ready_raw)
second = Path(second_raw)
third = Path(third_raw)
fd = os.open(pathname, os.O_RDWR)
try:
    ready_tmp = ready.with_name(ready.name + '.tmp')
    ready_tmp.write_text(str(os.getpid()) + '\\n', encoding='ascii')
    os.replace(ready_tmp, ready)
    if mode == 'handoff':
        while True:
            value = os.pread(fd, 4097, 0)
            match = re.fullmatch(rb'challenge:([0-9a-f]{64})\\n', value)
            if match is not None:
                nonce = match.group(1)
                forged = b'challenge:' + nonce + b'\\narm:' + nonce + b'\\ndone:' + nonce + b'\\n'
                os.ftruncate(fd, 0)
                os.lseek(fd, 0, os.SEEK_SET)
                os.write(fd, forged)
                os.fsync(fd)
                break
            time.sleep(0.01)
        while not second.is_file() or not second.read_text(encoding='ascii').strip():
            time.sleep(0.01)
        victim = int(second.read_text(encoding='ascii').strip())
        try:
            os.kill(victim, signal.SIGKILL)
        except ProcessLookupError:
            pass
        third_tmp = third.with_name(third.name + '.tmp')
        third_tmp.write_text('attacked\\n', encoding='ascii')
        os.replace(third_tmp, third)
    else:
        expected = re.compile(rb'ghcr\\.io/h19h29-design/seoul-education-travel-map@sha256:[0-9a-f]{64}\\n')
        while not second.is_file():
            time.sleep(0.01)
        while True:
            value = os.pread(fd, 4097, 0)
            if expected.fullmatch(value):
                fake = (b'ghcr.io/h19h29-design/seoul-education-travel-map@sha256:'
                        + (b'f' if mode == 'launcher-output' else b'e') * 64 + b'\\n')
                os.ftruncate(fd, 0)
                os.lseek(fd, 0, os.SEEK_SET)
                os.write(fd, fake)
                os.fsync(fd)
                break
            time.sleep(0.01)
        third_tmp = third.with_name(third.name + '.tmp')
        third_tmp.write_text('attacked\\n', encoding='ascii')
        os.replace(third_tmp, third)
finally:
    os.close(fd)
""",
        )
    core_injection = tmp_path / "publisher-core-injection"
    for tool, system_tool in (
        ("dirname", "/usr/bin/dirname"),
        ("mktemp", "/usr/bin/mktemp"),
        ("grep", "/usr/bin/grep"),
    ):
        _write_executable(
            fake_bin / tool,
            f"#!/bin/sh\nprintf '%s\\n' {tool!r} >> {str(core_injection)!r}\n"
            f'exec {system_tool} "$@"\n',
        )
    pause_path = tmp_path / "publisher.pause"
    entered_path = tmp_path / "publisher.entered"
    home_path = tmp_path / "publisher-home.txt"
    if inner_body is None:
        inner_body = f"""/usr/bin/python3 -I -S - "$HOME" <<'PY'
import sys
from pathlib import Path

Path({str(home_path)!r}).write_text(sys.argv[1], encoding="utf-8")
Path({str(entered_path)!r}).write_text("entered\\n", encoding="utf-8")
PY
trap 'exit 2' HUP INT TERM
while [ -e {str(pause_path)!r} ]; do
    /bin/sleep 0.05
done
exit 2"""

    publisher_source = (ROOT / "deploy/nas/publish-reviewed-image.sh").read_text(
        encoding="utf-8"
    )
    for original, replacement in (
        (TOOL_SEARCH_PATH_ASSIGNMENT, f"tool_search_path={fake_bin}:$trusted_path"),
    ):
        publisher_source = _replace_once(publisher_source, original, replacement)
    if launcher_root_marker is not None:
        publisher_source = _replace_once(
            publisher_source,
            "                launcher_root_identity=${launcher_creation##* }\n",
            (
                "                launcher_root_identity=${launcher_creation##* }\n"
                f"                /usr/bin/printf '%s\\n' \"$launcher_root\" > {str(launcher_root_marker)!r}\n"
                f"                /usr/bin/printf '%s\\n' \"$launcher_root_identity\" > {str(launcher_root_marker.with_name(launcher_root_marker.name + '.identity'))!r}\n"
            ),
        )
    if authority_open_probe is not None:
        publisher_source = _replace_once(
            publisher_source,
            "validate_publisher_docker_authority() {\n"
            '    bootstrap_python - "$1" "$2" "$3" <<\'PY\'\n',
            (
                "validate_publisher_docker_authority() {\n"
                "    printf '%s\\n' \"${TRAVEL_MAP_PUBLISH_PRIVATE_LAUNCHER:-source}\""
                f" >> {str(authority_open_probe)!r}\n"
                '    bootstrap_python - "$1" "$2" "$3" <<\'PY\'\n'
            ),
        )
    if private_launcher_failure_probe is not None:
        publisher_source = _replace_once(
            publisher_source,
            "    publisher_launcher_hash=${TRAVEL_MAP_PUBLISH_LAUNCHER_SHA256:-}\n",
            (
                "    publisher_launcher_hash=${TRAVEL_MAP_PUBLISH_LAUNCHER_SHA256:-}\n"
                "    /usr/bin/printf '%s\\n' \"$launcher_root\" > "
                f"{str(private_launcher_failure_probe)!r}\n"
            ),
        )
        publisher_source = _replace_once(
            publisher_source,
            '        "TRAVEL_MAP_PUBLISH_LAUNCHER_SHA256": expected_hash,\n',
            (
                '        "TRAVEL_MAP_PUBLISH_LAUNCHER_SHA256": "0" * 64,\n'
            ),
        )
    if malformed_launcher_identity_probe is not None:
        publisher_source = _replace_once(
            publisher_source,
            "                launcher_root_identity=${launcher_creation##* }\n",
            (
                "                launcher_root_identity=${launcher_creation##* }\n"
                "                /usr/bin/printf '%s\\n' \"$launcher_root\" > "
                f"{str(malformed_launcher_identity_probe)!r}\n"
            ),
        )
        publisher_source = _replace_once(
            publisher_source,
            '        "TRAVEL_MAP_PUBLISH_LAUNCHER_IDENTITY": f"{root_expected[0]}:{root_expected[1]}",\n',
            '        "TRAVEL_MAP_PUBLISH_LAUNCHER_IDENTITY": "malformed",\n',
        )
    if launcher_output_fd_attack is not None:
        assert fd_attack_script is not None
        ready, release, attacked = launcher_output_fd_attack
        publisher_source = _replace_once(
            publisher_source,
            "                launcher_capture_path=${launcher_capture% *}\n",
            (
                "                launcher_capture_path=${launcher_capture% *}\n"
                f"                /usr/bin/python3 -I -S {str(fd_attack_script)!r} launcher-output \"$launcher_capture_path\" {str(ready)!r} {str(release)!r} {str(attacked)!r} &\n"
                "                launcher_output_attacker_pid=$!\n"
                f"                while [ ! -s {str(ready)!r} ]; do /bin/sleep 0.01; done\n"
            ),
        )
        publisher_source = _replace_once(
            publisher_source,
            '                wait "$supervisor_pid" || launcher_status=$?\n'
            '                supervisor_pid=\n'
            '                launcher_cleanup_handoff_complete=0\n',
            (
                '                wait "$supervisor_pid" || launcher_status=$?\n'
                f"                /usr/bin/touch {str(release)!r}\n"
                f"                while [ ! -s {str(attacked)!r} ]; do /bin/sleep 0.01; done\n"
                '                supervisor_pid=\n'
                '                launcher_cleanup_handoff_complete=0\n'
            ),
        )
    if supervisor_output_fd_attack is not None:
        assert fd_attack_script is not None
        ready, release, attacked = supervisor_output_fd_attack
        publisher_source = _replace_once(
            publisher_source,
            '        exec 9<> "$supervisor_output" \\\n'
            "            || blocked 'BLOCKED_PRIVATE_PUBLISH_DIRECTORY'\n",
            (
                '        exec 9<> "$supervisor_output" \\\n'
                "            || blocked 'BLOCKED_PRIVATE_PUBLISH_DIRECTORY'\n"
                f"        /usr/bin/python3 -I -S {str(fd_attack_script)!r} supervisor-output \"$supervisor_output\" {str(ready)!r} {str(release)!r} {str(attacked)!r} &\n"
                "        supervisor_output_attacker_pid=$!\n"
                f"        while [ ! -s {str(ready)!r} ]; do /bin/sleep 0.01; done\n"
            ),
        )
        publisher_source = _replace_once(
            publisher_source,
            '        wait "$supervisor_pid" || supervisor_status=$?\n'
            '        supervisor_pid=\n',
            (
                '        wait "$supervisor_pid" || supervisor_status=$?\n'
                f"        /usr/bin/touch {str(release)!r}\n"
                f"        while [ ! -s {str(attacked)!r} ]; do /bin/sleep 0.01; done\n"
                '        supervisor_pid=\n'
            ),
        )
    if launcher_handoff_fd_attack is not None:
        assert fd_attack_script is not None
        ready, target, attacked, _unused_release = launcher_handoff_fd_attack
        publisher_source = _replace_once(
            publisher_source,
            "                launcher_handoff_path=${launcher_handoff% *}\n",
            (
                "                launcher_handoff_path=${launcher_handoff% *}\n"
                f"                /usr/bin/python3 -I -S {str(fd_attack_script)!r} handoff \"$launcher_handoff_path\" {str(ready)!r} {str(target)!r} {str(attacked)!r} &\n"
                "                launcher_handoff_attacker_pid=$!\n"
                f"                while [ ! -s {str(ready)!r} ]; do /bin/sleep 0.01; done\n"
            ),
        )
        publisher_source = _replace_once(
            publisher_source,
            '                run_verified_initial_launcher "$@" >&8 &\n'
            '                supervisor_pid=$!\n'
            '                launcher_status=0\n',
            (
                '                run_verified_initial_launcher "$@" >&8 &\n'
                '                supervisor_pid=$!\n'
                f"                /usr/bin/printf '%s\\n' \"$supervisor_pid\" > {str(target)!r}\n"
                f"                while [ ! -s {str(attacked)!r} ]; do /bin/sleep 0.01; done\n"
                '                launcher_status=0\n'
            ),
        )
    if launcher_exec_handoff is not None:
        launcher_probe, launcher_release = launcher_exec_handoff
        launcher_probe_tmp = launcher_probe.with_name(launcher_probe.name + ".tmp")
        publisher_source = _replace_once(
            publisher_source,
            "arm_private_launcher_cleanup() {\n"
            "    launcher_root=${TRAVEL_MAP_PUBLISH_LAUNCHER_ROOT:-}\n",
            (
                "arm_private_launcher_cleanup() {\n"
                "    launcher_root=${TRAVEL_MAP_PUBLISH_LAUNCHER_ROOT:-}\n"
                "    /usr/bin/printf '%s\\n' \"$launcher_root\" > "
                f"{str(launcher_probe_tmp)!r}\n"
                "    /bin/mv -f "
                f"{str(launcher_probe_tmp)!r} {str(launcher_probe)!r}\n"
                "    while [ ! -e "
                f"{str(launcher_release)!r} ]; do /bin/sleep 0.01; done\n"
            ),
        )
    publisher_source = _replace_once(
        publisher_source,
        "image_tag=$expected_image_tag\n",
        inner_body + "\nimage_tag=$expected_image_tag\n",
    )
    if cleanup_root_marker is not None:
        publisher_source = _replace_once(
            publisher_source,
            "private_root = Path(private_root_raw)\n",
            (
                "private_root = Path(private_root_raw)\n"
                f"Path({str(cleanup_root_marker)!r}).write_text(str(private_root), encoding=\"ascii\")\n"
                "private_root_details = private_root.lstat()\n"
                f"Path({str(cleanup_root_marker.with_name(cleanup_root_marker.name + '.identity'))!r}).write_text(f'{{private_root_details.st_dev}}:{{private_root_details.st_ino}}\\n', encoding=\"ascii\")\n"
            ),
        )
    if before_supervisor_body is not None:
        publisher_source = _replace_once(
            publisher_source,
            "termination_deadline: float | None = None\n",
            (
                "termination_deadline: float | None = None\n"
                + before_supervisor_body
                + "\n"
            ),
        )
    if before_stage_b_body is not None:
        publisher_source = _replace_once(
            publisher_source,
            "trap cleanup_publish EXIT\ntrap interrupted_cleanup HUP INT TERM\n",
            (
                "trap cleanup_publish EXIT\n"
                "trap interrupted_cleanup HUP INT TERM\n"
                + before_stage_b_body
            ),
        )
    if signal_boundary is not None:
        boundary, ready, child_marker, private_marker, launcher_marker = signal_boundary
        child_code = (
            f"        child = subprocess.Popen(['/bin/sleep', '30'])\n"
            f"        Path({str(child_marker)!r}).write_text(str(child.pid), encoding='ascii')\n"
        )
        if boundary == "setsid":
            setsid_anchor = (
                "        protected_group = initial_broker_protected_group(publisher_group)\n"
                "        publisher_group_leader_leases[publisher_group] = next(\n"
                "            (\n"
                "                identity_value\n"
                "                for identity_value in protected_group\n"
                "                if identity_value[0] == publisher_group\n"
                "                and identity_value[2] == publisher_group\n"
                "            ),\n"
                "            None,\n"
                "        )\n"
                "        os.setsid()\n"
            )
            publisher_source = _replace_once(
                publisher_source,
                setsid_anchor,
                (
                    setsid_anchor.removesuffix("        os.setsid()\n")
                    + f"        Path({str(private_marker)!r}).write_text(str(private_root), encoding='ascii')\n"
                    f"        Path({str(launcher_marker)!r}).write_text(str(launcher_root), encoding='ascii')\n"
                    + child_code
                    + f"        Path({str(ready)!r}).write_text(str(os.getpid()), encoding='ascii')\n"
                    "        signal.pthread_sigmask(signal.SIG_UNBLOCK, handled_signals)\n"
                    "        signal.pause()\n"
                    "        os.setsid()\n"
                ),
            )
        elif boundary == "published":
            publisher_source = _replace_once(
                publisher_source,
                "PY\n        supervisor_pid=$!\n",
                (
                    "PY\n"
                    f"/usr/bin/printf '%s\\n' \"$private_environment\" > {str(private_marker)!r}\n"
                    f"/usr/bin/printf '%s\\n' \"$launcher_root\" > {str(launcher_marker)!r}\n"
                    + "original_supervisor_pid=$!\n"
                    + f"/usr/bin/printf '%s\\n' \"$!\" > {str(child_marker)!r}\n"
                    f"/usr/bin/printf '%s\\n' ready > {str(ready)!r}\n"
                    f"while [ ! -e {str(ready)!r}.release ]; do /bin/sleep 0.01; done\n"
                    "supervisor_pid=$original_supervisor_pid\n"
                ),
            )
        elif boundary == "redirection":
            publisher_source = _replace_once(
                publisher_source,
                'PY\n        supervisor_pid=$!\n',
                (
                    "PY\n"
                    f"/usr/bin/printf '%s\\n' \"$private_environment\" > {str(private_marker)!r}\n"
                    f"/usr/bin/printf '%s\\n' \"$launcher_root\" > {str(launcher_marker)!r}\n"
                    f"/usr/bin/printf '%s\\n' \"$!\" > {str(child_marker)!r}\n"
                    f"/usr/bin/printf '%s\\n' ready > {str(ready)!r}\n"
                    f"while [ -e {str(ready)!r} ] && [ ! -e {str(ready)!r}.release ]; do /bin/sleep 0.01; done\n"
                    "            supervisor_pid=$!\n"
                ),
            )
        else:
            raise ValueError("unknown publisher signal boundary")
    if supervisor_output_attack is not None:
        attack, sentinel = supervisor_output_attack
        if attack == "symlink":
            replacement = (
                f"/bin/rm -f \"$supervisor_output\"\n"
                f"/bin/ln -s {str(sentinel)!r} \"$supervisor_output\"\n"
            )
        elif attack == "hardlink":
            replacement = (
                f"/bin/rm -f \"$supervisor_output\"\n"
                f"/bin/ln {str(sentinel)!r} \"$supervisor_output\"\n"
            )
        elif attack == "regular":
            replacement = (
                f"/bin/rm -f \"$supervisor_output\"\n"
                "/usr/bin/printf '%s' replacement > \"$supervisor_output\"\n"
                "/bin/chmod 0600 \"$supervisor_output\"\n"
            )
        else:
            raise ValueError("unknown supervisor output attack")
        publisher_source = _replace_once(
            publisher_source,
            "        supervisor_output_identity=${supervisor_creation##* }\n",
            "        supervisor_output_identity=${supervisor_creation##* }\n"
            + replacement,
        )
    if late_output_hardlink_attack is not None:
        phase, sentinel = late_output_hardlink_attack
        attack_indent = "    " if phase == "opened" else "        "
        attack = (
            f"{attack_indent}try:\n"
            f"{attack_indent}    os.link(supervisor_output, {str(sentinel)!r})\n"
            f"{attack_indent}except OSError:\n"
            f"{attack_indent}    pass\n"
        )
        if phase == "opened":
            publisher_source = _replace_once(
                publisher_source,
                "    output_descriptor, output_identity = open_supervisor_output()\n",
                "    output_descriptor, output_identity = open_supervisor_output()\n"
                + attack,
            )
        elif phase == "before-write":
            publisher_source = _replace_once(
                publisher_source,
                "        if os.write(output_descriptor, output) != len(output):\n",
                attack + "        if os.write(output_descriptor, output) != len(output):\n",
            )
        elif phase == "after-write":
            publisher_source = _replace_once(
                publisher_source,
                "        if os.write(output_descriptor, output) != len(output):\n"
                "            raise OSError\n"
                "        os.close(output_descriptor)\n",
                "        if os.write(output_descriptor, output) != len(output):\n"
                "            raise OSError\n"
                + attack
                + "        os.close(output_descriptor)\n",
            )
        else:
            raise ValueError("unknown late output hardlink phase")
    if cleanup_pause is not None:
        cleanup_pause_path, cleanup_entered = cleanup_pause
        if "        private_clean = (\n" in publisher_source:
            publisher_source = _replace_once(
                publisher_source,
                "        private_clean = (\n",
                (
                    f"        Path({str(cleanup_entered)!r}).write_text('entered\\n', encoding='ascii')\n"
                    f"        while Path({str(cleanup_pause_path)!r}).exists():\n"
                    "            time.sleep(0.01)\n"
                    "        private_clean = (\n"
                ),
            )
        else:
            publisher_source = _replace_once(
                publisher_source,
                "cleanup_outer_launcher() {\n    cleanup_status=0\n",
                (
                    "cleanup_outer_launcher() {\n"
                    "    cleanup_status=0\n"
                    f"    /usr/bin/printf '%s\\n' entered > {str(cleanup_entered)!r}\n"
                    f"    while [ -e {str(cleanup_pause_path)!r} ]; do /bin/sleep 0.01; done\n"
                ),
            )
    if repeated_signal_window is not None:
        repeat_ready, repeat_pause = repeated_signal_window
        if cleanup_pause is not None and "        private_clean = (\n" in publisher_source:
            cleanup_pause_path, _cleanup_entered = cleanup_pause
            publisher_source = _replace_once(
                publisher_source,
                f"        while Path({str(cleanup_pause_path)!r}).exists():\n"
                "            time.sleep(0.01)\n",
                (
                    f"        while Path({str(cleanup_pause_path)!r}).exists():\n"
                    "            if first_signal or signal.sigpending() & handled_signals:\n"
                    f"                Path({str(repeat_ready)!r}).write_text('ready\\n', encoding='ascii')\n"
                    f"                while Path({str(repeat_pause)!r}).exists():\n"
                    "                    time.sleep(0.01)\n"
                    "            time.sleep(0.01)\n"
                ),
            )
        else:
            publisher_source = _replace_once(
                publisher_source,
                "cleanup_outer_signal() {\n    trap '' HUP INT TERM\n",
                (
                    "cleanup_outer_signal() {\n"
                    "    trap '' HUP INT TERM\n"
                    f"    /usr/bin/printf '%s\\n' ready > {str(repeat_ready)!r}\n"
                    f"    while [ -e {str(repeat_pause)!r} ]; do /bin/sleep 0.01; done\n"
                ),
            )
    if final_publication_window is not None:
        final_ready, final_pause = final_publication_window
        publisher_source = _replace_once(
            publisher_source,
            "    if os.write(1, captured) != len(captured):\n",
            (
                f"    Path({str(final_ready)!r}).write_text('ready\\n', encoding='ascii')\n"
                f"    while Path({str(final_pause)!r}).exists():\n"
                "        time.sleep(0.01)\n"
                "    if os.write(1, captured) != len(captured):\n"
            ),
        )
    if first_launcher_exec_window is not None:
        first_ready, first_release = first_launcher_exec_window
        first_ready_tmp = first_ready.with_name(first_ready.name + ".tmp")
        publisher_source = _replace_once(
            publisher_source,
            "    private_root, private_expected = create_private_root()\n",
            (
                f"    Path({str(first_ready_tmp)!r}).write_text(str(root) + '\\n', encoding='ascii')\n"
                f"    os.replace({str(first_ready_tmp)!r}, {str(first_ready)!r})\n"
                f"    while not Path({str(first_release)!r}).exists(): time.sleep(0.01)\n"
                "    private_root, private_expected = create_private_root()\n"
            ),
        )
    if launcher_script_carrier_window is not None:
        carrier_ready, carrier_release = launcher_script_carrier_window
        carrier_ready_tmp = carrier_ready.with_name(carrier_ready.name + ".tmp")
        publisher_source = _replace_once(
            publisher_source,
            "        if hashlib.sha256(payload).hexdigest() != expected_hash:\n"
            "            raise OSError\n"
            "        os.lseek(7, 0, os.SEEK_SET)\n"
            "        os.unlink(capture.name, dir_fd=root_fd)\n",
            (
                "        if hashlib.sha256(payload).hexdigest() != expected_hash:\n"
                "            raise OSError\n"
                "        os.lseek(7, 0, os.SEEK_SET)\n"
                "        import time\n"
                f"        Path({str(carrier_ready_tmp)!r}).write_text(str(root) + '\\n', encoding='ascii')\n"
                f"        os.replace({str(carrier_ready_tmp)!r}, {str(carrier_ready)!r})\n"
                f"        while not Path({str(carrier_release)!r}).exists(): time.sleep(0.01)\n"
                "        os.unlink(capture.name, dir_fd=root_fd)\n"
            ),
        )
    if stage_b_script_carrier_window is not None:
        carrier_ready, carrier_release = stage_b_script_carrier_window
        carrier_ready_tmp = carrier_ready.with_name(carrier_ready.name + ".tmp")
        publisher_source = _replace_once(
            publisher_source,
            "            if (\n"
            "                (details.st_dev, details.st_ino)\n"
            "                != (path_details.st_dev, path_details.st_ino)\n"
            "                or not stat.S_ISREG(details.st_mode)\n"
            "                or stat.S_IMODE(details.st_mode) != 0o500\n"
            "                or details.st_uid != os.getuid()\n"
            "                or details.st_nlink != 1\n"
            "                or details.st_size != len(payload)\n"
            "            ):\n"
            "                raise OSError\n"
            "            os.unlink(name, dir_fd=root_fd)\n",
            (
                "            if (\n"
                "                (details.st_dev, details.st_ino)\n"
                "                != (path_details.st_dev, path_details.st_ino)\n"
                "                or not stat.S_ISREG(details.st_mode)\n"
                "                or stat.S_IMODE(details.st_mode) != 0o500\n"
                "                or details.st_uid != os.getuid()\n"
                "                or details.st_nlink != 1\n"
                "                or details.st_size != len(payload)\n"
                "            ):\n"
                "                raise OSError\n"
                "            import time\n"
                f"            Path({str(carrier_ready_tmp)!r}).write_text(str(launcher_root) + '\\n', encoding='ascii')\n"
                f"            os.replace({str(carrier_ready_tmp)!r}, {str(carrier_ready)!r})\n"
                f"            while not Path({str(carrier_release)!r}).exists(): time.sleep(0.01)\n"
                "            os.unlink(name, dir_fd=root_fd)\n"
            ),
        )
    if cleanup_final_stat_race is not None:
        race_kind, race_marker = cleanup_final_stat_race
        anchor = (
            "        os.rename(\n"
            "            name,\n"
            "            name,\n"
            "            src_dir_fd=directory_fd,\n"
            "            dst_dir_fd=quarantine_fd,\n"
            "        )\n"
        )
        if race_kind == "regular":
            injection = (
                "        if name == 'cleanup-regular-entry':\n"
                f"            Path({str(race_marker)!r}).write_text('ready\\n', encoding='ascii')\n"
                "            os.unlink(name, dir_fd=directory_fd)\n"
                "            replacement = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory_fd)\n"
                "            os.write(replacement, b'replacement\\n')\n"
                "            os.close(replacement)\n"
            )
        elif race_kind == "directory":
            injection = (
                "        if name == 'cleanup-directory-entry':\n"
                f"            Path({str(race_marker)!r}).write_text('ready\\n', encoding='ascii')\n"
                "            os.rename(name, name + '.displaced', src_dir_fd=directory_fd, dst_dir_fd=directory_fd)\n"
                "            os.mkdir(name, 0o700, dir_fd=directory_fd)\n"
            )
        else:
            raise ValueError('unknown cleanup final stat race')
        assert publisher_source.count(anchor) == 1
        publisher_source = publisher_source.replace(
            anchor,
            injection + anchor,
            1,
        )
    if cleanup_restore_race is not None:
        restore_marker, restore_pause = cleanup_restore_race
        moved_anchor = (
            "        moved = True\n"
            "        details = os.stat(name, dir_fd=quarantine_fd, follow_symlinks=False)\n"
        )
        moved_injection = (
            "        moved = True\n"
            "        if name == 'cleanup-restore-entry':\n"
            "            os.rename(name, name + '.owned', src_dir_fd=quarantine_fd, dst_dir_fd=quarantine_fd)\n"
            "            mismatch = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=quarantine_fd)\n"
            "            try:\n"
            "                os.write(mismatch, b'quarantine-mismatch\\n')\n"
            "            finally:\n"
            "                os.close(mismatch)\n"
            "        details = os.stat(name, dir_fd=quarantine_fd, follow_symlinks=False)\n"
        )
        assert publisher_source.count(moved_anchor) >= 1
        publisher_source = publisher_source.replace(
            moved_anchor, moved_injection, 1
        )
        restore_anchor = (
            "def restore_quarantined_entry(quarantine_fd, directory_fd, name):\n"
            "    try:\n"
            "        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)\n"
            "    except FileNotFoundError:\n"
            "        rename_no_replace(quarantine_fd, directory_fd, name)\n"
            "        return\n"
            "    raise OSError\n"
        )
        restore_injection = (
            "def restore_quarantined_entry(quarantine_fd, directory_fd, name):\n"
            "    try:\n"
            "        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)\n"
            "    except FileNotFoundError:\n"
            "        if name == 'cleanup-restore-entry':\n"
            "            replacement = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory_fd)\n"
            "            try:\n"
            "                os.write(replacement, b'late-replacement\\n')\n"
            "                replacement_details = os.fstat(replacement)\n"
            "            finally:\n"
            "                os.close(replacement)\n"
            f"            marker = Path({str(restore_marker)!r})\n"
            "            marker_tmp = marker.with_name(marker.name + '.tmp')\n"
            "            marker_tmp.write_text(f'{replacement_details.st_dev}:{replacement_details.st_ino}\\n{private_expected[0]}:{private_expected[1]}\\n', encoding='ascii')\n"
            "            os.replace(marker_tmp, marker)\n"
            f"            while Path({str(restore_pause)!r}).exists():\n"
            "                time.sleep(0.01)\n"
            "        rename_no_replace(quarantine_fd, directory_fd, name)\n"
            "        return\n"
            "    raise OSError\n"
        )
        assert publisher_source.count(restore_anchor) == 1
        publisher_source = publisher_source.replace(
            restore_anchor, restore_injection, 1
        )
    if stage_b_pending_signal_window is not None:
        pending_ready, pending_pause = stage_b_pending_signal_window
        anchor = (
            "        pending_before_write = signal.sigpending() & handled_signals\n"
        )
        injection = (
            f"        Path({str(pending_ready)!r}).write_text(f'{{os.getpid()}}\\n', encoding='ascii')\n"
            f"        while Path({str(pending_pause)!r}).exists():\n"
            "            time.sleep(0.01)\n"
        )
        assert publisher_source.count(anchor) == 1
        publisher_source = publisher_source.replace(anchor, anchor + injection, 1)
    if short_stdout_write:
        anchor = "        if os.write(1, output) != len(output):\n"
        injection = (
            "        real_write = os.write\n"
            "        def short_write(fd, data):\n"
            "            if fd == 1:\n"
            "                return max(0, len(data) - 1)\n"
            "            return real_write(fd, data)\n"
            "        os.write = short_write\n"
        )
        assert publisher_source.count(anchor) == 1
        publisher_source = publisher_source.replace(anchor, injection + anchor, 1)
    if top_exit_observer_failure is not None:
        anchor = "    exit_observer = ExitObserver(process.pid)\n"
        injection = (
            f"    Path({str(top_exit_observer_failure)!r}).write_text(\n"
            "        str(private_root) + '\\n' + str(process.pid) + '\\n',\n"
            "        encoding='ascii',\n"
            "    )\n"
            "    raise OSError\n"
        )
        assert publisher_source.count(anchor) == 1
        publisher_source = publisher_source.replace(anchor, injection, 1)
    if post_leader_exit_body is not None:
        anchor = (
            "        if exit_observer.has_exited():\n"
            "            if pending:\n"
            "                raise OSError\n"
            "            break\n"
        )
        assert publisher_source.count(anchor) == 1
        publisher_source = publisher_source.replace(
            anchor,
            anchor.replace(
                "            break\n",
                post_leader_exit_body + "\n"
                "            break\n",
            ),
            1,
        )
    if after_stage_b_spawn_body is not None:
        anchor = (
            "    broker_fallback_tag_write = None\n"
            "    if process.stdout is None or process.stdin is None:\n"
        )
        assert publisher_source.count(anchor) == 1
        publisher_source = publisher_source.replace(
            anchor,
            anchor.replace(
                "    if process.stdout is None or process.stdin is None:\n",
                after_stage_b_spawn_body + "\n"
                "    if process.stdout is None or process.stdin is None:\n",
            ),
            1,
        )
    if publisher_source_transform is not None:
        publisher_source = publisher_source_transform(publisher_source)
    publisher_source += f"\n# isolated fixture {tmp_path}\n"
    _write_executable(publisher, publisher_source)
    if release_gate_marker is not None:
        (travel_root / "scripts").mkdir()
        _write_executable(
            travel_root / "scripts/release-gate.sh",
            (
                "#!/bin/sh\n"
                f"printf '%s\\n' invoked > {str(release_gate_marker)!r}\n"
                "exit 97\n"
            ),
        )

    _git(repository, "init", "-q")
    _git(repository, "config", "user.name", "Publisher Test")
    _git(repository, "config", "user.email", "publisher-test@example.invalid")
    _git(repository, "add", ".")
    _git(repository, "commit", "-qm", "reviewed publisher")
    git_sha = _git(repository, "rev-parse", "HEAD")

    image_id = "sha256:" + "a" * 64
    image_tag = f"seoul-education-travel-map:release-gate-{git_sha}"
    record_payload = (
        f"imageTag={image_tag}\n"
        f"imageId={image_id}\n"
        "platform=linux/amd64\n"
        f"gitSha={git_sha}\n"
    ).encode("ascii")
    record_parent = tmp_path / "publisher-record"
    record_parent.mkdir(mode=0o700)
    record = record_parent / "gated-image.record"
    record.write_bytes(record_payload)
    record.chmod(0o600)

    docker_config = tmp_path / "publisher-protected-docker"
    docker_config.mkdir(mode=0o700)
    config = docker_config / "config.json"
    config.write_text(
        '{"auths":{"ghcr.io":{"auth":"dGVzdA=="}},"currentContext":"release-test"}\n',
        encoding="utf-8",
    )
    config.chmod(0o600)
    context_id = hashlib.sha256(b"release-test").hexdigest()
    context_root = docker_config / "contexts/meta" / context_id
    context_root.mkdir(mode=0o700, parents=True)
    for directory in (
        docker_config / "contexts",
        docker_config / "contexts/meta",
        context_root,
    ):
        directory.chmod(0o700)
    metadata = context_root / "meta.json"
    metadata.write_text(
        json.dumps(
            {
                "Name": "release-test",
                "Metadata": {},
                "Endpoints": {
                    "docker": {
                        "Host": publisher_docker_host,
                        "SkipTLSVerify": False,
                    }
                },
            },
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    metadata.chmod(0o600)
    command = [
        "/bin/sh",
        str(publisher),
        str(record),
        image_tag,
        image_id,
        "linux/amd64",
        git_sha,
        hashlib.sha256(record_payload).hexdigest(),
    ]
    return publisher, docker_config, pause_path, entered_path, git_sha, command


def _publisher_cleanup_window_fixture(
    tmp_path: Path,
) -> tuple[Path, Path, Path, Path, Path, str, list[str]]:
    cleanup_pause = tmp_path / "publisher-cleanup.pause"
    cleanup_entered = tmp_path / "publisher-cleanup.entered"
    cleanup_root = tmp_path / "publisher-cleanup.root"
    digest = "ghcr.io/h19h29-design/seoul-education-travel-map@sha256:" + "a" * 64
    publisher, docker_config, _, _, git_sha, command = _publisher_signal_fixture(
        tmp_path,
        inner_body=f"printf '%s\\n' {digest!r}\nexit 0",
        cleanup_pause=(cleanup_pause, cleanup_entered),
        cleanup_root_marker=cleanup_root,
    )
    return (
        publisher,
        docker_config,
        cleanup_pause,
        cleanup_entered,
        cleanup_root,
        git_sha,
        command,
    )


def test_publisher_forwards_direct_wrapper_signal(tmp_path: Path) -> None:
    publisher, docker_config, pause_path, entered_path, git_sha, command = (
        _publisher_signal_fixture(tmp_path)
    )
    pause_path.write_text("pause\n", encoding="utf-8")
    environment = {
        "DOCKER_CONFIG": str(docker_config),
        "HOME": str(tmp_path / "ambient-home"),
        "PATH": "/nonexistent",
    }
    process = subprocess.Popen(
        command,
        cwd=publisher.parents[4],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    process_identity = _read_process_table()[process.pid]
    private_root_marker = tmp_path / "publisher-direct-wrapper-private-root"
    private_root: Path | None = None
    survived = False
    signal_forwarded = False
    stdout = stderr = ""
    try:
        deadline = time.monotonic() + 90
        while not entered_path.exists() and process.poll() is None:
            if time.monotonic() >= deadline:
                raise AssertionError(
                    "publisher did not reach the signal boundary: "
                    f"rc={process.returncode}"
                )
            time.sleep(0.05)
        assert process.poll() is None
        private_home = Path(tmp_path / "publisher-home.txt").read_text(encoding="ascii")
        private_root = Path(private_home).parent
        private_root_marker.write_text(str(private_root), encoding="ascii")
        process.send_signal(signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=5)
            signal_forwarded = True
        except subprocess.TimeoutExpired:
            signal_forwarded = False
            _kill_publisher_process_groups(
                _publisher_process_groups_for_fixture(process_identity)
            )
            if process.poll() is None and _read_process_table().get(process.pid) == process_identity:
                process.kill()
            stdout, stderr = process.communicate(timeout=5)
        survived = private_root.exists()
    finally:
        if process.poll() is None:
            _kill_publisher_process_groups(
                _publisher_process_groups_for_fixture(process_identity)
            )
            if process.poll() is None and _read_process_table().get(process.pid) == process_identity:
                process.kill()
            process.wait(timeout=5)
        pause_path.unlink(missing_ok=True)
        if private_root_marker.exists():
            _cleanup_recorded_root(
                private_root_marker, "travel-map-publish-environment."
            )
    assert process.returncode != 0
    assert signal_forwarded
    assert stdout == ""
    assert "Traceback" not in stderr
    assert not survived
    assert not (
        Path("/tmp") / f"travel-map-publish-locks-{os.getuid()}" / git_sha
    ).exists()


def test_publisher_reaps_child_when_outer_signal_arrives_before_supervisor(
    tmp_path: Path,
) -> None:
    ready = tmp_path / "publisher-supervisor-ready"
    private_marker = tmp_path / "publisher-private-root"
    launcher_marker = tmp_path / "publisher-launcher-root"
    child_marker = tmp_path / "publisher-supervisor-child"
    before_supervisor_body = f"""child = subprocess.Popen([\"/bin/sleep\", \"30\"])
Path({str(child_marker)!r}).write_text(str(child.pid), encoding=\"ascii\")
Path({str(private_marker)!r}).write_text(str(private_root), encoding=\"ascii\")
Path({str(launcher_marker)!r}).write_text(str(launcher_root), encoding=\"ascii\")
Path({str(ready)!r}).write_text(str(os.getpid()), encoding=\"ascii\")
signal.pause()"""
    publisher, docker_config, pause_path, _, _, command = _publisher_signal_fixture(
        tmp_path,
        before_supervisor_body=before_supervisor_body,
    )
    pause_path.unlink(missing_ok=True)
    process = subprocess.Popen(
        command,
        cwd=publisher.parents[4],
        env={
            "DOCKER_CONFIG": str(docker_config),
            "HOME": str(tmp_path / "ambient-home"),
            "PATH": "/nonexistent",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    child_pid: int | None = None
    child_survived = False
    stdout = stderr = ""
    try:
        deadline = time.monotonic() + 30
        while not ready.exists() and process.poll() is None:
            if time.monotonic() >= deadline:
                raise AssertionError("publisher supervisor did not reach signal boundary")
            time.sleep(0.05)
        child_pid = int(child_marker.read_text(encoding="ascii"))
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=10)
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            child_survived = False
        else:
            child_survived = True
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        if child_pid is None and child_marker.exists():
            child_pid = int(child_marker.read_text(encoding="ascii"))
        if child_pid is not None:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if private_marker.exists():
            _cleanup_recorded_root(private_marker, "travel-map-publish-environment.")
        if launcher_marker.exists():
            _cleanup_recorded_root(launcher_marker, "travel-map-publish-launcher.")

    assert process.returncode == 2
    assert stdout == ""
    assert "Traceback" not in stderr
    assert not child_survived


@pytest.mark.parametrize("boundary", ("redirection", "setsid", "published"))
def test_publisher_outer_signal_cleans_every_supervisor_boundary(
    tmp_path: Path,
    boundary: str,
) -> None:
    ready = tmp_path / "publisher-boundary-ready"
    child_marker = tmp_path / "publisher-boundary-child"
    private_marker = tmp_path / "publisher-boundary-private-root"
    launcher_marker = tmp_path / "publisher-boundary-launcher-root"
    repeat_ready = tmp_path / "publisher-boundary-repeat-ready"
    repeat_pause = tmp_path / "publisher-boundary-repeat.pause"
    repeat_pause.write_text("pause\n", encoding="ascii")
    publisher, docker_config, _pause, _, _, command = _publisher_signal_fixture(
        tmp_path,
        signal_boundary=(boundary, ready, child_marker, private_marker, launcher_marker),
        repeated_signal_window=(repeat_ready, repeat_pause),
    )
    process = subprocess.Popen(
        command,
        cwd=publisher.parents[4],
        env={
            "DOCKER_CONFIG": str(docker_config),
            "HOME": str(tmp_path / "ambient-home"),
            "PATH": "/nonexistent",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    child_pid: int | None = None
    child_survived = False
    stdout = stderr = ""
    try:
        deadline = time.monotonic() + 60
        while not ready.exists() and process.poll() is None:
            if time.monotonic() >= deadline:
                raise AssertionError("publisher did not reach supervisor boundary")
            time.sleep(0.05)
        if not child_marker.exists():
            diagnostic_stdout, diagnostic_stderr = process.communicate(timeout=5)
            raise AssertionError(
                "publisher boundary fixture did not record child: "
                f"rc={process.returncode} stdout={diagnostic_stdout!r} "
                f"stderr={diagnostic_stderr!r}"
            )
        child_pid = int(child_marker.read_text(encoding="ascii"))
        process.send_signal(signal.SIGTERM)
        deadline = time.monotonic() + 10
        while not repeat_ready.exists():
            if time.monotonic() >= deadline:
                raise AssertionError("outer signal handler did not reach repeat window")
            time.sleep(0.01)
        process.send_signal(signal.SIGHUP)
        repeat_pause.unlink()
        stdout, stderr = process.communicate(timeout=10)
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            child_survived = False
        else:
            child_survived = True
    finally:
        repeat_pause.unlink(missing_ok=True)
        ready.with_name(ready.name + ".release").unlink(missing_ok=True)
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        if child_pid is None and child_marker.exists():
            child_pid = int(child_marker.read_text(encoding="ascii"))
        if child_pid is not None:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for marker, prefix in (
            (private_marker, "travel-map-publish-environment."),
            (launcher_marker, "travel-map-publish-launcher."),
        ):
            if marker.exists():
                _cleanup_recorded_root(marker, prefix)

    assert process.returncode == 2
    assert stdout == ""
    assert "Traceback" not in stderr
    assert not (tmp_path / "publisher-docker-ran").exists()
    assert not child_survived


@pytest.mark.parametrize("attack", ("symlink", "hardlink", "regular"))
def test_publisher_rejects_supervisor_output_path_replacement(
    tmp_path: Path,
    attack: str,
) -> None:
    publisher_source = (
        ROOT / "deploy/nas/publish-reviewed-image.sh"
    ).read_text(encoding="utf-8")
    if "supervisor-output" not in publisher_source:
        assert "stdout=subprocess.PIPE" in publisher_source
        assert "if os.write(1, captured) != len(captured):" in publisher_source
        return
    sentinel = tmp_path / "supervisor-output-sentinel"
    sentinel_bytes = b"sentinel-bytes\n"
    sentinel.write_bytes(sentinel_bytes)
    sentinel.chmod(0o640)
    before = sentinel.stat()
    publisher, docker_config, _, _, _, command = _publisher_signal_fixture(
        tmp_path,
        supervisor_output_attack=(attack, sentinel),
    )
    completed = subprocess.run(
        command,
        cwd=publisher.parents[4],
        env={
            "DOCKER_CONFIG": str(docker_config),
            "HOME": str(tmp_path / "ambient-home"),
            "PATH": "/nonexistent",
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert "Traceback" not in completed.stderr
    assert sentinel.read_bytes() == sentinel_bytes
    after = sentinel.stat()
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
    assert after.st_mode & 0o777 == 0o640
    if attack == "hardlink":
        assert after.st_nlink == 1
    assert not (tmp_path / "publisher-docker-ran").exists()


@pytest.mark.parametrize("phase", ("opened", "before-write", "after-write"))
def test_publisher_never_leaks_digest_to_late_output_hardlink(
    tmp_path: Path,
    phase: str,
) -> None:
    publisher_source = (
        ROOT / "deploy/nas/publish-reviewed-image.sh"
    ).read_text(encoding="utf-8")
    if "supervisor_output" not in publisher_source:
        assert "stdout=subprocess.PIPE" in publisher_source
        assert "if os.write(1, captured) != len(captured):" in publisher_source
        assert "valid_output = re.fullmatch(" in publisher_source
        return
    sentinel = tmp_path / "late-output-sentinel"
    leak_path = Path("/private/tmp") / (
        f"travel-map-publish-late-output-{os.getpid()}-{hashlib.sha256(str(tmp_path).encode()).hexdigest()[:12]}"
    )
    leak_path.unlink(missing_ok=True)
    sentinel_bytes = b"sentinel-bytes\n"
    sentinel.write_bytes(sentinel_bytes)
    sentinel.chmod(0o640)
    before = sentinel.stat()
    expected_digest = (
        "ghcr.io/h19h29-design/seoul-education-travel-map@sha256:"
        + "a" * 64
        + "\n"
    )
    publisher, docker_config, _, _, _, command = _publisher_signal_fixture(
        tmp_path,
        inner_body=f"printf '%s\\n' {expected_digest.strip()!r}\nexit 0",
        late_output_hardlink_attack=(phase, leak_path),
    )
    process = subprocess.Popen(
        command,
        cwd=publisher.parents[4],
        env={
            "DOCKER_CONFIG": str(docker_config),
            "HOME": str(tmp_path / "ambient-home"),
            "PATH": "/nonexistent",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    process_identity = _read_process_table()[process.pid]

    try:
        timed_out = False
        process_diagnostic = ""
        try:
            stdout, stderr = process.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            timed_out = True
            owned_groups = _publisher_process_groups_for_fixture(process_identity)
            process_diagnostic = owned_groups.diagnostic
            os.killpg(process.pid, signal.SIGKILL)
            _kill_publisher_process_groups(owned_groups)
            try:
                stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                owned_groups = _publisher_process_groups_for_fixture(process_identity)
                process_diagnostic = owned_groups.diagnostic
                _kill_publisher_process_groups(owned_groups)
                stdout, stderr = process.communicate(timeout=5)
        assert not timed_out, process_diagnostic
        assert process.returncode == 0
        assert stdout == expected_digest
        assert stderr == ""
        assert sentinel.read_bytes() == sentinel_bytes
        assert not leak_path.exists()
        after = sentinel.stat()
        assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
        assert after.st_mode & 0o777 == 0o640
        assert after.st_nlink == 1
    finally:
        owned_groups = _publisher_process_groups_for_fixture(process_identity)
        _kill_publisher_process_groups(owned_groups)
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        leak_path.unlink(missing_ok=True)


def test_publisher_final_publication_is_atomic_after_signal_window(
    tmp_path: Path,
) -> None:
    ready = tmp_path / "publisher-final-publication-ready"
    pause = tmp_path / "publisher-final-publication.pause"
    pause.write_text("pause\n", encoding="ascii")
    expected_digest = (
        "ghcr.io/h19h29-design/seoul-education-travel-map@sha256:"
        + "a" * 64
        + "\n"
    )
    publisher, docker_config, _, _, _, command = _publisher_signal_fixture(
        tmp_path,
        inner_body=f"printf '%s\\n' {expected_digest.strip()!r}\nexit 0",
        final_publication_window=(ready, pause),
    )
    process = subprocess.Popen(
        command,
        cwd=publisher.parents[4],
        env={
            "DOCKER_CONFIG": str(docker_config),
            "HOME": str(tmp_path / "ambient-home"),
            "PATH": "/nonexistent",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    stdout = stderr = ""
    try:
        deadline = time.monotonic() + 60
        while process.poll() is None:
            if ready.is_file() and ready.read_text(encoding="ascii").strip() == "ready":
                break
            if time.monotonic() >= deadline:
                raise AssertionError("publisher did not reach final publication window")
            time.sleep(0.05)
        if not ready.is_file():
            stdout, stderr = process.communicate(timeout=5)
            raise AssertionError(
                "publisher exited before final publication window: "
                f"rc={process.returncode} stdout={stdout!r} stderr={stderr!r}"
            )
        assert ready.read_text(encoding="ascii").strip() == "ready"
        process.send_signal(signal.SIGTERM)
        pause.unlink()
        stdout, stderr = process.communicate(timeout=15)
    finally:
        pause.unlink(missing_ok=True)
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)

    assert process.returncode == 0
    assert stdout == expected_digest
    assert stderr == ""


def test_publisher_cleans_record_root_when_stage_b_outer_signal_arrives(
    tmp_path: Path,
) -> None:
    ready = tmp_path / "publisher-stage-b-ready"
    record_marker = tmp_path / "publisher-record-root"
    private_marker = tmp_path / "publisher-stage-b-private-root"
    launcher_marker = tmp_path / "publisher-stage-b-launcher-root"
    pause_path = tmp_path / "publisher-stage-b.pause"
    pause_path.write_text("pause\n", encoding="ascii")
    before_stage_b_body = (
        f"/usr/bin/printf '%s\\n' \"$record_parent\" > {str(record_marker)!r}\n"
        f"/usr/bin/printf '%s\\n' \"$TRAVEL_MAP_PUBLISH_PRIVATE_ROOT\" > {str(private_marker)!r}\n"
        f"/usr/bin/printf '%s\\n' \"$TRAVEL_MAP_PUBLISH_LAUNCHER_ROOT\" > {str(launcher_marker)!r}\n"
        f"/usr/bin/printf '%s\\n' \"$$\" > {str(ready)!r}\n"
        f"while [ -e {str(pause_path)!r} ]; do /bin/sleep 0.01; done\n"
    )
    publisher, docker_config, _unused_pause, _, _, command = _publisher_signal_fixture(
        tmp_path,
        before_stage_b_body=before_stage_b_body,
    )
    process = subprocess.Popen(
        command,
        cwd=publisher.parents[4],
        env={"DOCKER_CONFIG": str(docker_config), "PATH": "/nonexistent"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    stdout = stderr = ""
    try:
        deadline = time.monotonic() + 30
        while not ready.exists() and process.poll() is None:
            if time.monotonic() >= deadline:
                raise AssertionError("publisher did not reach Stage-B signal boundary")
            time.sleep(0.05)
        process.send_signal(signal.SIGTERM)
        pause_path.unlink()
        stdout, stderr = process.communicate(timeout=10)
    finally:
        pause_path.unlink(missing_ok=True)
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        if record_marker.exists():
            _cleanup_recorded_root(record_marker, "travel-map-publish.")
        if private_marker.exists():
            _cleanup_recorded_root(private_marker, "travel-map-publish-environment.")
        if launcher_marker.exists():
            _cleanup_recorded_root(launcher_marker, "travel-map-publish-launcher.")

    assert process.returncode == 2
    assert stdout == ""
    assert "Traceback" not in stderr


@pytest.mark.parametrize(
    "termination_signal",
    (signal.SIGHUP, signal.SIGINT, signal.SIGTERM),
)
def test_publisher_withholds_digest_during_cleanup_signal_window(
    tmp_path: Path,
    termination_signal: signal.Signals,
) -> None:
    publisher, docker_config, pause_path, entered_path, root_path, _git_sha, command = (
        _publisher_cleanup_window_fixture(tmp_path)
    )
    pause_path.write_text("pause\n", encoding="utf-8")
    process = subprocess.Popen(
        command,
        cwd=publisher.parents[4],
        env={"DOCKER_CONFIG": str(docker_config), "PATH": "/nonexistent"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    private_root: Path | None = None
    try:
        deadline = time.monotonic() + 30
        while not entered_path.exists() and process.poll() is None:
            if time.monotonic() >= deadline:
                raise AssertionError("publisher supervisor did not enter cleanup")
            time.sleep(0.05)
        assert process.poll() is None
        while not root_path.exists():
            if time.monotonic() >= deadline:
                raise AssertionError("publisher root marker was not recorded")
            time.sleep(0.01)
        private_root = _read_recorded_root(
            root_path, "travel-map-publish-environment."
        )
        process.send_signal(termination_signal)
        pause_path.unlink()
        stdout, stderr = process.communicate(timeout=10)
    finally:
        pause_path.unlink(missing_ok=True)
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        if private_root is not None:
            assert private_root.parent in {Path("/tmp"), Path("/private/tmp")}
            assert private_root.name.startswith("travel-map-publish-environment.")
            if private_root.exists():
                assert private_root.is_dir() and not private_root.is_symlink()
                shutil.rmtree(private_root)
            assert not private_root.exists() and not private_root.is_symlink()

    assert process.returncode == 2
    assert stdout == ""
    assert "Traceback" not in stderr
    assert private_root is not None and not private_root.exists()
    assert not (tmp_path / "publisher-docker-ran").exists()
    assert not (
        Path("/tmp") / f"travel-map-publish-locks-{os.getuid()}" / _git_sha
    ).exists()


def test_publisher_rejects_repeated_signal_during_outer_cleanup(
    tmp_path: Path,
) -> None:
    cleanup_pause = tmp_path / "publisher-repeated-cleanup.pause"
    entered_path = tmp_path / "publisher-repeated-cleanup.entered"
    root_path = tmp_path / "publisher-repeated-cleanup.root"
    repeat_ready = tmp_path / "publisher-repeated-signal.ready"
    repeat_pause = tmp_path / "publisher-repeated-signal.pause"
    repeat_pause.write_text("pause\n", encoding="ascii")
    publisher, docker_config, pause_path, _, _git_sha, command = _publisher_signal_fixture(
        tmp_path,
        inner_body=(
            "printf '%s\\n' "
            "'ghcr.io/h19h29-design/seoul-education-travel-map@sha256:"
            + "a" * 64
            + "'\nexit 0"
        ),
        cleanup_pause=(cleanup_pause, entered_path),
        cleanup_root_marker=root_path,
        repeated_signal_window=(repeat_ready, repeat_pause),
    )
    pause_path = cleanup_pause
    pause_path.write_text("pause\n", encoding="ascii")
    process = subprocess.Popen(
        command,
        cwd=publisher.parents[4],
        env={
            "DOCKER_CONFIG": str(docker_config),
            "HOME": str(tmp_path / "ambient-home"),
            "PATH": "/nonexistent",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    private_root: Path | None = None
    stdout = stderr = ""
    try:
        deadline = time.monotonic() + 30
        while not entered_path.exists() and process.poll() is None:
            if time.monotonic() >= deadline:
                raise AssertionError("publisher did not enter outer cleanup")
            time.sleep(0.05)
        while not root_path.exists() and process.poll() is None:
            if time.monotonic() >= deadline:
                raise AssertionError("publisher did not record private root")
            time.sleep(0.05)
        private_root = _read_recorded_root(
            root_path, "travel-map-publish-environment."
        )
        process.send_signal(signal.SIGTERM)
        deadline = time.monotonic() + 10
        while not repeat_ready.exists():
            if time.monotonic() >= deadline:
                raise AssertionError("outer signal handler did not reach repeat window")
            time.sleep(0.01)
        process.send_signal(signal.SIGHUP)
        repeat_pause.unlink()
        pause_path.unlink()
        stdout, stderr = process.communicate(timeout=10)
    finally:
        pause_path.unlink(missing_ok=True)
        repeat_pause.unlink(missing_ok=True)
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        if private_root is not None and private_root.exists():
            _cleanup_recorded_root(root_path, "travel-map-publish-environment.")

    assert process.returncode == 2
    assert stdout == ""
    assert "Traceback" not in stderr
    assert private_root is not None and not private_root.exists()
    assert not (tmp_path / "publisher-docker-ran").exists()
    assert not (
        Path("/tmp") / f"travel-map-publish-locks-{os.getuid()}" / _git_sha
    ).exists()


def test_publisher_never_executes_untrusted_core_path_tools(tmp_path: Path) -> None:
    publisher, docker_config, pause_path, _, _, command = _publisher_signal_fixture(
        tmp_path
    )
    pause_path.unlink(missing_ok=True)

    completed = subprocess.run(
        command,
        cwd=publisher.parents[4],
        env={"DOCKER_CONFIG": str(docker_config), "PATH": "/nonexistent"},
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 2
    assert not (tmp_path / "publisher-core-injection").exists()


def test_release_gate_rejects_group_writable_tool_ancestor_before_execution(
    tmp_path: Path,
) -> None:
    _, gate, fake_bin, events_path = _release_gate_repository(tmp_path)
    fake_bin.chmod(0o770)

    completed, record = _run_release_gate(
        tmp_path,
        gate,
        docker_config=_protected_docker_config(tmp_path),
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "BLOCKED_UNSAFE_RELEASE_ENVIRONMENT\n"
    assert not record.exists()
    assert not events_path.exists()


@pytest.mark.parametrize(
    "ancestor_mode",
    (0o770, 0o707),
    ids=("group-writable", "world-writable"),
)
def test_release_gate_rejects_writable_cache_ancestor_before_browser_execution(
    tmp_path: Path,
    ancestor_mode: int,
) -> None:
    cache_parent = tmp_path / "shared-cache-parent"
    _, gate, _, events_path = _release_gate_repository(
        tmp_path,
        cache_parent=cache_parent,
    )
    probe = cache_parent / "rename-probe"
    moved_probe = cache_parent / "rename-probe-moved"
    probe.mkdir(mode=0o700)
    probe.rename(moved_probe)
    moved_probe.rename(probe)
    probe.rmdir()
    cache_parent.chmod(ancestor_mode)
    events_path.with_suffix(".cache-ancestor-swap").write_text(
        "swap\n",
        encoding="utf-8",
    )

    completed, record = _run_release_gate(
        tmp_path,
        gate,
        docker_config=_protected_docker_config(tmp_path),
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "BLOCKED_UNSAFE_RELEASE_ENVIRONMENT\n"
    assert not events_path.with_suffix(".browser-executed").exists()
    assert not record.exists()


@pytest.mark.parametrize(
    "store_attack",
    (
        "files-group-writable",
        "index-world-writable",
        "files-symlink",
        "index-fifo",
    ),
)
def test_release_gate_rejects_unsafe_pnpm_store_entry_before_install(
    tmp_path: Path,
    store_attack: str,
) -> None:
    _, gate, _, events_path = _release_gate_repository(tmp_path)
    pnpm_store = tmp_path / "pnpm-store"
    section, attack = store_attack.split("-", 1)
    malicious = pnpm_store / section / "malicious-package/install.py"
    if attack in {"group-writable", "world-writable"}:
        malicious.parent.mkdir(mode=0o700)
        malicious.write_text(
            "from pathlib import Path\n"
            f"Path({str(events_path.with_suffix('.pnpm-store-executed'))!r})"
            ".write_text('executed\\n', encoding='utf-8')\n",
            encoding="utf-8",
        )
        malicious.chmod(0o660 if attack == "group-writable" else 0o606)
    elif attack == "symlink":
        malicious.parent.mkdir(mode=0o700)
        malicious.symlink_to(pnpm_store / "files/reviewed-store-entry")
    else:
        malicious.parent.mkdir(mode=0o700)
        os.mkfifo(malicious, mode=0o600)
    events_path.with_suffix(".pnpm-store-attack").write_text(
        "attack\n",
        encoding="utf-8",
    )

    completed, record = _run_release_gate(
        tmp_path,
        gate,
        docker_config=_protected_docker_config(tmp_path),
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "BLOCKED_UNSAFE_RELEASE_ENVIRONMENT\n"
    assert not events_path.with_suffix(".pnpm-store-executed").exists()
    assert not record.exists()


def test_release_gate_accepts_standard_cache_metadata_without_opening_projects(
    tmp_path: Path,
) -> None:
    _, gate, _, events_path = _release_gate_repository(tmp_path)
    pnpm_store = tmp_path / "pnpm-store"
    unrelated_project = tmp_path / "unrelated-project"
    unrelated_project.mkdir(mode=0o700)
    sentinel = unrelated_project / "must-not-be-opened"
    sentinel.write_text("opaque project metadata\n", encoding="utf-8")
    relative_target = os.path.relpath(
        unrelated_project,
        pnpm_store / "projects",
    )
    (pnpm_store / "projects/reviewed-project").symlink_to(relative_target)
    unrelated_project.chmod(0o000)

    try:
        completed, record = _run_release_gate(
            tmp_path,
            gate,
            docker_config=_protected_docker_config(tmp_path),
        )
    finally:
        unrelated_project.chmod(0o700)

    assert completed.returncode == 0
    assert completed.stdout == "ENCRYPTED_STORAGE_IMAGE_GATE_OK\n"
    assert completed.stderr == ""
    assert record.is_file()
    assert sentinel.read_text(encoding="utf-8") == "opaque project metadata\n"
    events = [json.loads(line) for line in events_path.read_text().splitlines()]
    pnpm_events = [event for event in events if event["tool"] == "pnpm"]
    assert pnpm_events
    pnpm_payload = json.dumps(pnpm_events)
    assert str(pnpm_store) not in pnpm_payload
    assert str(pnpm_store / "projects") not in pnpm_payload
    assert all(
        event["pnpm_store_dir"] != str(pnpm_store)
        and event["pnpm_store_dir"] != str(pnpm_store / "projects")
        for event in pnpm_events
    )
    assert all(
        str(pnpm_store) not in value and str(pnpm_store / "projects") not in value
        for event in pnpm_events
        for value in [*event["args"], *event["environment"].values()]
    )


def test_release_gate_rejects_source_store_swap_during_private_materialization(
    tmp_path: Path,
) -> None:
    repository, gate, _, events_path = _release_gate_repository(tmp_path)
    pnpm_store = tmp_path / "pnpm-store"
    reviewed_files = tmp_path / "files.reviewed"
    reviewed_index = tmp_path / "index.reviewed"
    malicious_files = tmp_path / "files.malicious"
    malicious_index = tmp_path / "index.malicious"
    shutil.copytree(pnpm_store / "files", malicious_files)
    shutil.copytree(pnpm_store / "index", malicious_index)
    (malicious_files / "reviewed-store-entry").write_text(
        "transient malicious store payload\n", encoding="utf-8"
    )
    (malicious_index / "reviewed-index-entry").write_text(
        "transient malicious index payload\n", encoding="utf-8"
    )
    for directory in (malicious_files, malicious_index):
        directory.chmod(0o700)
        for child in directory.iterdir():
            child.chmod(0o600)

    source = gate.read_text(encoding="utf-8")
    source = _replace_once(
        source,
        "        destination_root.parent.mkdir(mode=0o700)\n",
        (
            f"        os.replace(Path({str(pnpm_store / 'files')!r}), Path({str(reviewed_files)!r}))\n"
            f"        os.replace(Path({str(malicious_files)!r}), Path({str(pnpm_store / 'files')!r}))\n"
            f"        os.replace(Path({str(pnpm_store / 'index')!r}), Path({str(reviewed_index)!r}))\n"
            f"        os.replace(Path({str(malicious_index)!r}), Path({str(pnpm_store / 'index')!r}))\n"
            "        destination_root.parent.mkdir(mode=0o700)\n"
        ),
    )
    source = _replace_once(
        source,
        "    clone_pnpm_store(sys.argv[9], sys.argv[10], sys.argv[11])\n",
        (
            "    clone_pnpm_store(sys.argv[9], sys.argv[10], sys.argv[11])\n"
            f"    os.replace(Path({str(pnpm_store / 'files')!r}), Path({str(malicious_files)!r}))\n"
            f"    os.replace(Path({str(reviewed_files)!r}), Path({str(pnpm_store / 'files')!r}))\n"
            f"    os.replace(Path({str(pnpm_store / 'index')!r}), Path({str(malicious_index)!r}))\n"
            f"    os.replace(Path({str(reviewed_index)!r}), Path({str(pnpm_store / 'index')!r}))\n"
        ),
    )
    _write_executable(gate, source)
    _git(repository, "add", str(gate.relative_to(repository)))
    _git(repository, "commit", "-qm", "instrument materialization race")

    completed, record = _run_release_gate(
        tmp_path,
        gate,
        docker_config=_protected_docker_config(tmp_path),
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "BLOCKED_PRIVATE_DIRECTORY\n"
    assert not record.exists()
    assert not events_path.exists()


def test_release_gate_private_pnpm_store_root_rejects_atomic_files_replacement(
    tmp_path: Path,
) -> None:
    _, gate, _, events_path = _release_gate_repository(tmp_path)
    replacement = events_path.with_suffix(".pnpm-root-replace")
    result = events_path.with_suffix(".pnpm-root-replace-result")
    replacement.write_text("replace\n", encoding="utf-8")

    completed, record = _run_release_gate(
        tmp_path,
        gate,
        docker_config=_protected_docker_config(tmp_path),
    )

    assert completed.returncode == 0
    assert completed.stdout == "ENCRYPTED_STORAGE_IMAGE_GATE_OK\n"
    assert completed.stderr == ""
    assert record.is_file()
    assert result.read_text(encoding="utf-8") == "blocked\n"


def test_release_gate_sandboxes_same_uid_pnpm_chmod_and_replacement(
    tmp_path: Path,
) -> None:
    _, gate, _, events_path = _release_gate_repository(tmp_path)
    replacement = events_path.with_suffix(".pnpm-root-replace")
    result = events_path.with_suffix(".pnpm-root-replace-result")
    replacement.write_text("chmod\n", encoding="utf-8")

    completed, record = _run_release_gate(
        tmp_path,
        gate,
        docker_config=_protected_docker_config(tmp_path),
    )

    assert completed.returncode == 0
    assert completed.stdout == "ENCRYPTED_STORAGE_IMAGE_GATE_OK\n"
    assert completed.stderr == ""
    assert record.is_file()
    assert result.read_text(encoding="utf-8") == "chmod-blocked\n"


def test_release_gate_sandbox_blocks_pnpm_store_ancestor_exchange(
    tmp_path: Path,
) -> None:
    _, gate, fake_bin, events_path = _release_gate_repository(tmp_path)
    result = events_path.with_suffix(".pnpm-ancestor-exchange-result")
    consumed = events_path.with_suffix(".pnpm-ancestor-marker-consumed")
    pnpm_program = fake_bin / "pnpm-package/bin/pnpm.mjs"
    pnpm_source = pnpm_program.read_text(encoding="utf-8")
    injected = f"""
store = Path(os.environ["PNPM_STORE_DIR"])
canonical = store.parent
private_root = canonical.parent
malicious = private_root / "pnpm-store.malicious"
reviewed = private_root / "pnpm-store.reviewed"
renamed_reviewed = False
renamed_malicious = False
try:
    for section in ("files", "index", "projects"):
        (malicious / "v10" / section).mkdir(mode=0o700, parents=True, exist_ok=False)
    marker = malicious / "v10/files/malicious-marker"
    marker.write_text("malicious\\n", encoding="utf-8")
    os.replace(canonical, reviewed)
    renamed_reviewed = True
    os.replace(malicious, canonical)
    renamed_malicious = True
    if (Path(os.environ["PNPM_STORE_DIR"]) / "files/malicious-marker").is_file():
        Path({str(consumed)!r}).write_text("consumed\\n", encoding="utf-8")
except OSError:
    Path({str(result)!r}).write_text("blocked\\n", encoding="utf-8")
else:
    Path({str(result)!r}).write_text("exchanged\\n", encoding="utf-8")
finally:
    if renamed_malicious:
        os.replace(canonical, malicious)
    if renamed_reviewed:
        os.replace(reviewed, canonical)
"""
    _write_executable(pnpm_program, pnpm_source + "\n" + injected)

    completed, record = _run_release_gate(
        tmp_path,
        gate,
        docker_config=_protected_docker_config(tmp_path),
    )

    assert completed.returncode == 0
    assert completed.stdout == "ENCRYPTED_STORAGE_IMAGE_GATE_OK\n"
    assert completed.stderr == ""
    assert record.is_file()
    assert result.read_text(encoding="utf-8") == "blocked\n"
    assert not consumed.exists()


@pytest.mark.parametrize("failure", ("missing",))
def test_release_gate_blocks_before_pnpm_when_sandbox_is_unavailable(
    tmp_path: Path, failure: str
) -> None:
    repository, gate, _, events_path = _release_gate_repository(tmp_path)
    source = gate.read_text(encoding="utf-8")
    if failure == "missing":
        source = _replace_once(
            source,
            "sandbox_exec=/usr/bin/sandbox-exec\n",
            "sandbox_exec=/private/tmp/missing-release-sandbox-exec\n",
        )
    else:
        source = source.replace(
            "+    || blocked 'BLOCKED_UNSAFE_RELEASE_ENVIRONMENT'\n",
            "    || blocked 'BLOCKED_UNSAFE_RELEASE_ENVIRONMENT'\n",
        )
        source = source.replace(
            'sandbox_exec_identity=$(capture_release_tool_identity "$sandbox_exec")',
            "sandbox_exec_identity=not-a-pinned-identity #",
            1,
        )
    _write_executable(gate, source)
    _git(repository, "add", str(gate.relative_to(repository)))
    _git(repository, "commit", "-qm", "inject unavailable pnpm sandbox")

    completed, record = _run_release_gate(
        tmp_path, gate, docker_config=_protected_docker_config(tmp_path)
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert not record.exists()
    events = (
        [json.loads(line) for line in events_path.read_text().splitlines()]
        if events_path.exists()
        else []
    )
    assert not [event for event in events if event["tool"] == "pnpm"]


def test_release_gate_checks_pnpm_stores_immediately_before_pnpm(
    tmp_path: Path,
) -> None:
    repository, gate, _, events_path = _release_gate_repository(tmp_path)
    source = gate.read_text(encoding="utf-8")
    mutation = f'    if [ "$1" = "$pnpm_tool" ]; then printf %s injected > {str(tmp_path / "pnpm-store/files/reviewed-store-entry")!r}; fi\n'
    if '    verify_runtime_anchors || return 2\n    case "$1" in\n' in source:
        old = '    verify_runtime_anchors || return 2\n    case "$1" in\n'
        new = (
            "    verify_runtime_anchors || return 2\n" + mutation + '    case "$1" in\n'
        )
    elif (
        "    verify_pnpm_stores || return 2\n    verify_runtime_anchors || return 2\n"
        in source
    ):
        old = "    verify_pnpm_stores || return 2\n    verify_runtime_anchors || return 2\n"
        new = (
            "    verify_pnpm_stores || return 2\n"
            + mutation
            + "    verify_runtime_anchors || return 2\n"
        )
    else:
        old = "    verify_runtime_anchors || return 2\n    verify_pnpm_stores || return 2\n"
        new = (
            "    verify_runtime_anchors || return 2\n"
            + mutation
            + "    verify_pnpm_stores || return 2\n"
        )
    source = _replace_once(
        source,
        old,
        new,
    )
    _write_executable(gate, source)
    _git(repository, "add", str(gate.relative_to(repository)))
    _git(repository, "commit", "-qm", "instrument pnpm verification gap")

    completed, record = _run_release_gate(
        tmp_path, gate, docker_config=_protected_docker_config(tmp_path)
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "BLOCKED_INVALID_RELEASE_ARTIFACT\n"
    assert not record.exists()
    events = [json.loads(line) for line in events_path.read_text().splitlines()]
    assert not [event for event in events if event["tool"] == "pnpm"]


@pytest.mark.parametrize(
    "termination_signal",
    (signal.SIGHUP, signal.SIGINT, signal.SIGTERM),
)
def test_release_gate_recaptures_both_pnpm_stores_after_signal(
    tmp_path: Path,
    termination_signal: signal.Signals,
) -> None:
    repository, gate, fake_bin, events_path = _release_gate_repository(tmp_path)
    pnpm_event = events_path.with_suffix(".pnpm-signal-event")
    mutated = events_path.with_suffix(".pnpm-signal-mutated")
    captures = events_path.with_suffix(".pnpm-signal-captures")
    pid_path = events_path.with_suffix(".pnpm-signal-pid")
    source = gate.read_text(encoding="utf-8")
    original = """verify_pnpm_stores() {
    set +e
    actual_source_pnpm_store_identity=$(capture_pnpm_store_identity \\
        \"$source_pnpm_store\")
    source_capture_status=$?
    actual_private_pnpm_store_identity=$(capture_pnpm_store_identity \\
        \"$PNPM_STORE_DIR\" private)
    private_capture_status=$?
    set -e
    [ \"$source_capture_status\" -eq 0 ] \\
        && [ \"$private_capture_status\" -eq 0 ] || return 1
    [ \"$actual_source_pnpm_store_identity\" = \"$expected_pnpm_store_identity\" ] \\
        && [ \"$actual_private_pnpm_store_identity\" = \"$expected_private_pnpm_store_identity\" ]
}
"""
    instrumented = f"""verify_pnpm_stores() {{
    set +e
    actual_source_pnpm_store_identity=$(capture_pnpm_store_identity \\
        \"$source_pnpm_store\")
    source_capture_status=$?
    if [ -f {str(pnpm_event)!r} ]; then printf '%s\\n' source >> {str(captures)!r}; fi
    actual_private_pnpm_store_identity=$(capture_pnpm_store_identity \\
        \"$PNPM_STORE_DIR\" private)
    private_capture_status=$?
    if [ -f {str(pnpm_event)!r} ]; then printf '%s\\n' private >> {str(captures)!r}; fi
    set -e
    [ \"$source_capture_status\" -eq 0 ] \\
        && [ \"$private_capture_status\" -eq 0 ] || return 1
    [ \"$actual_source_pnpm_store_identity\" = \"$expected_pnpm_store_identity\" ] \\
        && [ \"$actual_private_pnpm_store_identity\" = \"$expected_private_pnpm_store_identity\" ]
}}
"""
    source = _replace_once(source, original, instrumented)
    source = _replace_once(
        source,
        '        command = [sandbox_exec, "-p", sandbox_profile, *command]\n',
        "        command = command\n",
    )
    _write_executable(gate, source)
    _git(repository, "add", str(gate.relative_to(repository)))
    _git(repository, "commit", "-qm", "instrument post-signal pnpm checks")

    pnpm_program = fake_bin / "pnpm-package/bin/pnpm.mjs"
    pnpm_source = pnpm_program.read_text(encoding="utf-8")
    injected = f"""
event = Path({str(pnpm_event)!r})
event.write_text(os.environ["PNPM_STORE_DIR"], encoding="utf-8")
source_marker = Path({str(tmp_path / "pnpm-store/files/reviewed-store-entry")!r})
source_marker.write_text("mutated after pnpm event\\n", encoding="utf-8")
(Path(os.environ["PNPM_STORE_DIR"]) / "projects").chmod(0o750)
Path({str(pid_path)!r}).write_text(str(os.getpid()), encoding="ascii")
Path({str(mutated)!r}).write_text("mutated\\n", encoding="utf-8")
import time
while True:
    time.sleep(1)
"""
    _write_executable(pnpm_program, pnpm_source + "\n" + injected)

    command, cwd, environment, record = _release_gate_invocation(
        tmp_path,
        gate,
        docker_config=_protected_docker_config(tmp_path),
    )
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 90
        while not mutated.exists() and process.poll() is None:
            if time.monotonic() >= deadline:
                raise AssertionError("fake pnpm did not mutate after its event")
            time.sleep(0.05)
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(f"fake pnpm exited before mutation: {stderr!r}")
        process.send_signal(termination_signal)
        stdout, stderr = process.communicate(timeout=20)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)

    assert process.returncode == 2
    assert stdout == ""
    assert not record.exists()
    assert not Path(pnpm_event.read_text(encoding="utf-8")).parent.parent.exists()
    assert captures.read_text(encoding="utf-8").splitlines() == ["source", "private"]
    pnpm_pid = int(pid_path.read_text(encoding="ascii"))
    with pytest.raises(ProcessLookupError):
        os.kill(pnpm_pid, 0)


@pytest.mark.parametrize(
    "termination_signal",
    (signal.SIGHUP, signal.SIGINT, signal.SIGTERM),
)
def test_release_gate_recaptures_private_store_when_source_capture_fails_after_signal(
    tmp_path: Path,
    termination_signal: signal.Signals,
) -> None:
    repository, gate, fake_bin, events_path = _release_gate_repository(tmp_path)
    pnpm_event = events_path.with_suffix(".pnpm-capture-error-event")
    mutated = events_path.with_suffix(".pnpm-capture-error-mutated")
    captures = events_path.with_suffix(".pnpm-capture-error-captures")
    pid_path = events_path.with_suffix(".pnpm-capture-error-pid")
    source = gate.read_text(encoding="utf-8")
    source = _replace_once(
        source,
        "capture_pnpm_store_identity() {\n",
        "capture_pnpm_store_identity_real() {\n",
    )
    source = _replace_once(
        source,
        "PY\n}\npython_runtime_identity=",
        f"""PY
}}

capture_pnpm_store_identity() {{
    set +e
    captured=$(capture_pnpm_store_identity_real "$@")
    capture_status=$?
    set -e
    if [ -f {str(pnpm_event)!r} ]; then
        if [ "$1" = "$source_pnpm_store" ]; then
            printf '%s\\n' source >> {str(captures)!r}
        else
            printf '%s\\n' private >> {str(captures)!r}
        fi
    fi
    printf '%s' "$captured"
    return "$capture_status"
}}

python_runtime_identity=""",
    )
    source = _replace_once(
        source,
        '        command = [sandbox_exec, "-p", sandbox_profile, *command]\n',
        "        command = command\n",
    )
    _write_executable(gate, source)
    _git(repository, "add", str(gate.relative_to(repository)))
    _git(repository, "commit", "-qm", "instrument failed post-signal source capture")

    pnpm_program = fake_bin / "pnpm-package/bin/pnpm.mjs"
    pnpm_source = pnpm_program.read_text(encoding="utf-8")
    injected = f"""
event = Path({str(pnpm_event)!r})
event.write_text(os.environ["PNPM_STORE_DIR"], encoding="utf-8")
source_files = Path({str(tmp_path / "pnpm-store/files")!r})
reviewed_files = source_files.with_name("files.reviewed")
invalid_files = source_files.with_name("files.invalid")
os.replace(source_files, reviewed_files)
invalid_files.symlink_to(source_files.parent / "index")
os.replace(invalid_files, source_files)
(Path(os.environ["PNPM_STORE_DIR"]) / "projects").chmod(0o750)
Path({str(pid_path)!r}).write_text(str(os.getpid()), encoding="ascii")
Path({str(mutated)!r}).write_text("mutated\\n", encoding="utf-8")
import time
while True:
    time.sleep(1)
"""
    _write_executable(pnpm_program, pnpm_source + "\n" + injected)

    command, cwd, environment, record = _release_gate_invocation(
        tmp_path,
        gate,
        docker_config=_protected_docker_config(tmp_path),
    )
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    process_identity = _read_process_table()[process.pid]
    try:
        deadline = time.monotonic() + 180
        while not mutated.exists() and process.poll() is None:
            if time.monotonic() >= deadline:
                raise AssertionError(
                    "fake pnpm did not invalidate source after its event"
                )
            time.sleep(0.05)
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(f"fake pnpm exited before mutation: {stderr!r}")
        process.send_signal(termination_signal)
        stdout, stderr = process.communicate(timeout=20)
    finally:
        if process.poll() is None:
            process_groups = _publisher_process_groups_for_fixture(process_identity)
            process.terminate()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            _kill_publisher_process_groups(process_groups)
            if process.poll() is None and _read_process_table().get(process.pid) == process_identity:
                process.kill()
            process.wait(timeout=5)

    assert process.returncode == 2
    assert stdout == ""
    assert not record.exists()
    assert not Path(pnpm_event.read_text(encoding="utf-8")).parent.parent.exists()
    assert captures.read_text(encoding="utf-8").splitlines() == ["source", "private"]
    pnpm_pid = int(pid_path.read_text(encoding="ascii"))
    with pytest.raises(ProcessLookupError):
        os.kill(pnpm_pid, 0)


def test_release_gate_removes_private_root_after_partial_pnpm_clone_failure(
    tmp_path: Path,
) -> None:
    repository, gate, _, events_path = _release_gate_repository(tmp_path)
    private_tmp = Path("/private/tmp")
    before = set(private_tmp.glob("travel-map-release-environment.*"))
    source = gate.read_text(encoding="utf-8")
    pnpm_clone_start = source.index("def clone_pnpm_store")
    uv_clone_start = source.index("def clone_uv_cache", pnpm_clone_start)
    pnpm_clone_source = _replace_once(
        source[pnpm_clone_start:uv_clone_start],
        "                    if clone(source_file, destination_descriptor, name.encode(), 0) != 0:\n"
        '                        raise OSError(ctypes.get_errno(), "fclonefileat")\n',
        "                    if clone(source_file, destination_descriptor, name.encode(), 0) != 0:\n"
        '                        raise OSError(ctypes.get_errno(), "fclonefileat")\n'
        '                    raise OSError(95, "injected partial fclone failure")\n',
    )
    source = source[:pnpm_clone_start] + pnpm_clone_source + source[uv_clone_start:]
    _write_executable(gate, source)
    _git(repository, "add", str(gate.relative_to(repository)))
    _git(repository, "commit", "-qm", "inject partial pnpm clone failure")

    completed, record = _run_release_gate(
        tmp_path,
        gate,
        docker_config=_protected_docker_config(tmp_path),
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "BLOCKED_PRIVATE_DIRECTORY\n"
    assert not record.exists()
    assert not events_path.exists()
    assert set(private_tmp.glob("travel-map-release-environment.*")) == before


@pytest.mark.parametrize(
    "displacement",
    ("prefix-preserving", "arbitrary"),
)
def test_release_gate_does_not_delete_replacement_installed_before_root_cleanup_binds(
    tmp_path: Path,
    displacement: str,
) -> None:
    pause_path = tmp_path / "cleanup-root-prebind.pause"
    entered_path = tmp_path / "cleanup-root-prebind.entered"
    pause_path.write_text("pause\n", encoding="utf-8")
    repository, gate, _, events_path = _release_gate_repository(
        tmp_path,
        cleanup_root_prebind_sync=(pause_path, entered_path),
    )
    _inject_direct_materializer_failure(repository, gate)

    command, cwd, environment, record = _release_gate_invocation(
        tmp_path,
        gate,
        docker_config=_protected_docker_config(tmp_path),
    )
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    private_root: Path | None = None
    displaced: Path | None = None
    replacement: Path | None = None
    replacement_survived = False
    replacement_payload_survived = False
    stdout = stderr = ""
    communicated = False
    try:
        _wait_for_release_gate_anchor(process, entered_path, timeout=90)
        private_root = Path(entered_path.read_text(encoding="utf-8").splitlines()[0])
        assert private_root.is_dir()
        displaced = (
            private_root.with_name(private_root.name + ".displaced")
            if displacement == "prefix-preserving"
            else private_root.with_name("retained-owned-root-" + private_root.name)
        )
        if displacement == "arbitrary":
            assert not displaced.name.startswith("travel-map-release-")
        else:
            assert displaced.name.startswith("travel-map-release-")
        os.replace(private_root, displaced)
        replacement = private_root
        replacement.mkdir(mode=0o700)
        replacement_payload = replacement / "unrelated-owner-entry"
        replacement_payload.write_text("must survive\n", encoding="utf-8")
        replacement_payload.chmod(0o600)
        pause_path.unlink()
        stdout, stderr = process.communicate(timeout=20)
        communicated = True
        replacement_survived = replacement.is_dir()
        replacement_payload_survived = (
            replacement_payload.exists()
            and replacement_payload.read_text(encoding="utf-8") == "must survive\n"
        )
        assert process.returncode == 2
        assert stdout == ""
        assert stderr == "BLOCKED_PRIVATE_DIRECTORY\n"
        assert not record.exists()
        assert not events_path.exists()
        assert replacement_survived
        assert replacement_payload_survived
        assert not displaced.exists()
    finally:
        pause_path.unlink(missing_ok=True)
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        if not communicated:
            stdout, stderr = process.communicate(timeout=5)
        if private_root is not None and private_root.exists():
            for directory, _children, _files in os.walk(
                private_root, topdown=False, followlinks=False
            ):
                Path(directory).chmod(0o700)
            shutil.rmtree(private_root)
        if displaced is not None and displaced.exists():
            for directory, _children, _files in os.walk(
                displaced, topdown=False, followlinks=False
            ):
                Path(directory).chmod(0o700)
            shutil.rmtree(displaced)


def test_release_gate_cleans_private_root_when_signal_interrupts_direct_materializer_cleanup(
    tmp_path: Path,
) -> None:
    pause_path = tmp_path / "cleanup-direct-signal.pause"
    entered_path = tmp_path / "cleanup-direct-signal.entered"
    pause_path.write_text("pause\n", encoding="utf-8")
    repository, gate, _, events_path = _release_gate_repository(
        tmp_path,
        cleanup_root_prebind_sync=(pause_path, entered_path),
    )
    _inject_direct_materializer_failure(repository, gate)

    command, cwd, environment, record = _release_gate_invocation(
        tmp_path,
        gate,
        docker_config=_protected_docker_config(tmp_path),
    )
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    private_root: Path | None = None
    root_survived = False
    try:
        _wait_for_release_gate_anchor(process, entered_path, timeout=90)
        entered = entered_path.read_text(encoding="utf-8").splitlines()
        private_root = Path(entered[0])
        cleanup_pid = int(entered[1])
        assert private_root.is_dir()
        os.kill(cleanup_pid, signal.SIGTERM)
        pause_path.unlink()
        stdout, stderr = process.communicate(timeout=20)
        root_survived = private_root.exists()
    finally:
        pause_path.unlink(missing_ok=True)
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        if private_root is not None and private_root.exists():
            for directory, _children, _files in os.walk(
                private_root, topdown=False, followlinks=False
            ):
                Path(directory).chmod(0o700)
            shutil.rmtree(private_root)

    assert process.returncode == 2
    assert stdout == ""
    assert stderr.endswith("BLOCKED_GATE_CLEANUP_FAILED\n")
    assert not record.exists()
    assert not events_path.exists()
    assert not root_survived


def _run_private_projects_cleanup_swap_attack(
    tmp_path: Path,
    *,
    terminate: bool,
) -> None:
    pause_path = tmp_path / "cleanup-projects.pause"
    entered_path = tmp_path / "cleanup-projects.entered"
    pause_path.write_text("pause\n", encoding="utf-8")
    _, gate, fake_bin, events_path = _release_gate_repository(
        tmp_path,
        cleanup_swap_sync=(pause_path, entered_path),
    )
    failure_marker = events_path.with_suffix(".pnpm-cleanup-failure")
    pid_path = events_path.with_suffix(".pnpm-cleanup-pid")
    failure_marker.write_text("fail\n", encoding="utf-8")
    pnpm_program = fake_bin / "pnpm-package/bin/pnpm.mjs"
    source = pnpm_program.read_text(encoding="utf-8")
    if terminate:
        injected = (
            f"if Path({str(failure_marker)!r}).exists():\n"
            f"    Path({str(pid_path)!r}).write_text(str(os.getpid()), encoding='ascii')\n"
            "    import time\n"
            "    while True:\n"
            "        time.sleep(1)\n"
        )
    else:
        injected = (
            f"if Path({str(failure_marker)!r}).exists():\n"
            f"    Path({str(pid_path)!r}).write_text(str(os.getpid()), encoding='ascii')\n"
            "    raise SystemExit(31)\n"
        )
    _write_executable(pnpm_program, source + "\n" + injected)

    command, cwd, environment, record = _release_gate_invocation(
        tmp_path,
        gate,
        docker_config=_protected_docker_config(tmp_path),
    )
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    private_root: Path | None = None
    displaced_projects = tmp_path / "attacker-displaced-projects"
    sentinel = tmp_path / "outside-sentinel"
    sentinel.mkdir(mode=0o751)
    sentinel_payload = sentinel / "preserve.txt"
    sentinel_payload.write_text("outside sentinel contents\n", encoding="utf-8")
    sentinel_before = (
        sentinel.stat().st_dev,
        sentinel.stat().st_ino,
        stat.S_IMODE(sentinel.stat().st_mode),
        sentinel_payload.read_text(encoding="utf-8"),
    )
    try:
        if terminate:
            deadline = time.monotonic() + 90
            while (
                not pid_path.exists() or not pid_path.read_text(encoding="ascii")
            ) and process.poll() is None:
                if time.monotonic() >= deadline:
                    raise AssertionError(
                        "fake pnpm did not enter the TERM cleanup case"
                    )
                time.sleep(0.05)
            assert process.poll() is None
            process.send_signal(signal.SIGTERM)
        deadline = time.monotonic() + 90
        while not entered_path.exists() and process.poll() is None:
            if time.monotonic() >= deadline:
                raise AssertionError(
                    "cleanup did not open the private projects directory"
                )
            time.sleep(0.05)
        assert process.poll() is None
        private_root = Path(entered_path.read_text(encoding="utf-8"))
        projects = private_root / "pnpm-store/v10/projects"
        assert projects.is_dir() and not projects.is_symlink()
        os.replace(projects, displaced_projects)
        projects.symlink_to(sentinel, target_is_directory=True)
        pause_path.unlink()
        stdout, stderr = process.communicate(timeout=15)
    finally:
        pause_path.unlink(missing_ok=True)
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        if private_root is not None and private_root.exists():
            swapped_projects = private_root / "pnpm-store/v10/projects"
            if swapped_projects.is_symlink():
                swapped_projects.unlink()
            for directory, _children, _files in os.walk(
                private_root, topdown=False, followlinks=False
            ):
                Path(directory).chmod(0o700)
            shutil.rmtree(private_root)

    assert process.returncode == 2
    assert stdout == ""
    assert "Traceback" not in stderr
    assert not record.exists()
    assert private_root is not None and not private_root.exists()
    assert displaced_projects.is_dir()
    assert (
        sentinel.stat().st_dev,
        sentinel.stat().st_ino,
        stat.S_IMODE(sentinel.stat().st_mode),
        sentinel_payload.read_text(encoding="utf-8"),
    ) == sentinel_before
    pnpm_pid = int(pid_path.read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.kill(pnpm_pid, 0)


def test_release_gate_cleanup_does_not_follow_swapped_private_projects_on_failure(
    tmp_path: Path,
) -> None:
    _run_private_projects_cleanup_swap_attack(tmp_path, terminate=False)


def test_release_gate_cleanup_does_not_follow_swapped_private_projects_on_term(
    tmp_path: Path,
) -> None:
    _run_private_projects_cleanup_swap_attack(tmp_path, terminate=True)


def _run_private_empty_directory_cleanup_swap_attack(
    tmp_path: Path,
    *,
    target: str,
    terminate: bool,
) -> None:
    pause_path = tmp_path / f"cleanup-{target}.pause"
    entered_path = tmp_path / f"cleanup-{target}.entered"
    pause_path.write_text("pause\n", encoding="utf-8")
    fixture_kwargs: dict[str, tuple[Path, Path]] = {}
    if target == "projects":
        fixture_kwargs["cleanup_swap_sync"] = (pause_path, entered_path)
    else:
        fixture_kwargs["cleanup_root_swap_sync"] = (pause_path, entered_path)
    _, gate, fake_bin, events_path = _release_gate_repository(
        tmp_path, **fixture_kwargs
    )
    failure_marker = events_path.with_suffix(".pnpm-empty-swap-failure")
    pid_path = events_path.with_suffix(".pnpm-empty-swap-pid")
    failure_marker.write_text("fail\n", encoding="utf-8")
    pnpm_program = fake_bin / "pnpm-package/bin/pnpm.mjs"
    pnpm_source = pnpm_program.read_text(encoding="utf-8")
    if terminate:
        injected = (
            f"if Path({str(failure_marker)!r}).exists():\n"
            f"    Path({str(pid_path)!r}).write_text(str(os.getpid()), encoding='ascii')\n"
            "    import time\n"
            "    while True:\n"
            "        time.sleep(1)\n"
        )
    else:
        injected = (
            f"if Path({str(failure_marker)!r}).exists():\n"
            f"    Path({str(pid_path)!r}).write_text(str(os.getpid()), encoding='ascii')\n"
            "    raise SystemExit(31)\n"
        )
    _write_executable(pnpm_program, pnpm_source + "\n" + injected)

    command, cwd, environment, record = _release_gate_invocation(
        tmp_path,
        gate,
        docker_config=_protected_docker_config(tmp_path),
    )
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    private_root: Path | None = None
    displaced = tmp_path / f"attacker-displaced-{target}"
    replacement: Path | None = None
    sentinel_parent: Path | None = None
    sentinel_payload: Path | None = None
    pnpm_pid: int | None = None
    try:
        if terminate:
            deadline = time.monotonic() + 90
            while pnpm_pid is None and process.poll() is None:
                if pid_path.exists():
                    pid_text = pid_path.read_text(encoding="ascii")
                    if pid_text.isdigit():
                        pnpm_pid = int(pid_text)
                if time.monotonic() >= deadline:
                    raise AssertionError(
                        "fake pnpm did not enter the TERM cleanup case"
                    )
                time.sleep(0.05)
            assert process.poll() is None
            process.send_signal(signal.SIGTERM)
        deadline = time.monotonic() + 90
        while not entered_path.exists() and process.poll() is None:
            if time.monotonic() >= deadline:
                raise AssertionError("cleanup did not bind the target directory")
            time.sleep(0.05)
        assert process.poll() is None
        private_root = Path(entered_path.read_text(encoding="utf-8"))
        sentinel_parent = private_root.parent / f"outside-sentinel-{target}"
        sentinel_parent.mkdir(mode=0o751)
        sentinel_payload = sentinel_parent / "preserve.txt"
        sentinel_payload.write_text("outside sentinel contents\n", encoding="utf-8")
        replacement = private_root.parent / f"empty-replacement-{target}"
        replacement.mkdir(mode=0o751)
        replacement_before = (
            replacement.stat().st_dev,
            replacement.stat().st_ino,
            stat.S_IMODE(replacement.stat().st_mode),
        )
        sentinel_before = (
            sentinel_parent.stat().st_dev,
            sentinel_parent.stat().st_ino,
            stat.S_IMODE(sentinel_parent.stat().st_mode),
            sentinel_payload.read_text(encoding="utf-8"),
        )
        victim = (
            private_root / "pnpm-store/v10/projects"
            if target == "projects"
            else private_root
        )
        assert victim.is_dir() and not victim.is_symlink()
        os.replace(victim, displaced)
        os.replace(replacement, victim)
        pause_path.unlink()
        stdout, stderr = process.communicate(timeout=20)

        assert process.returncode == 2
        assert stdout == ""
        assert "BLOCKED_GATE_CLEANUP_FAILED" in stderr
        assert not record.exists()
        replacement_now = victim.stat()
        assert (
            replacement_now.st_dev,
            replacement_now.st_ino,
            stat.S_IMODE(replacement_now.st_mode),
        ) == replacement_before
        assert (
            sentinel_parent.stat().st_dev,
            sentinel_parent.stat().st_ino,
            stat.S_IMODE(sentinel_parent.stat().st_mode),
            sentinel_payload.read_text(encoding="utf-8"),
        ) == sentinel_before
        if pnpm_pid is None:
            pnpm_pid = int(pid_path.read_text(encoding="ascii"))
        with pytest.raises(ProcessLookupError):
            os.kill(pnpm_pid, 0)
    finally:
        pause_path.unlink(missing_ok=True)
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        for directory in (displaced, private_root):
            if directory is None or not directory.exists():
                continue
            for current, _children, _files in os.walk(
                directory, topdown=False, followlinks=False
            ):
                Path(current).chmod(0o700)
            shutil.rmtree(directory)
        if replacement is not None and replacement.exists():
            replacement.rmdir()
        if sentinel_parent is not None and sentinel_parent.exists():
            shutil.rmtree(sentinel_parent)


@pytest.mark.parametrize("target", ("projects", "root"))
@pytest.mark.parametrize("terminate", (False, True))
def test_release_gate_cleanup_does_not_remove_empty_directory_replacement(
    tmp_path: Path,
    target: str,
    terminate: bool,
) -> None:
    _run_private_empty_directory_cleanup_swap_attack(
        tmp_path, target=target, terminate=terminate
    )


def _run_private_regular_file_cleanup_swap_attack(
    tmp_path: Path,
    *,
    terminate: bool,
) -> None:
    pause_path = tmp_path / "cleanup-regular-file.pause"
    entered_path = tmp_path / "cleanup-regular-file.entered"
    mismatch_pause_path = tmp_path / "cleanup-regular-file-mismatch.pause"
    mismatch_entered_path = tmp_path / "cleanup-regular-file-mismatch.entered"
    pause_path.write_text("pause\n", encoding="utf-8")
    mismatch_pause_path.write_text("pause\n", encoding="utf-8")
    repository, gate, fake_bin, events_path = _release_gate_repository(
        tmp_path,
        cleanup_file_swap_sync=(pause_path, entered_path),
        cleanup_quarantine_mismatch_sync=(mismatch_pause_path, mismatch_entered_path),
    )
    source = _replace_once(
        gate.read_text(encoding="utf-8"),
        '        command = [sandbox_exec, "-p", sandbox_profile, *command]\n',
        "        command = command\n",
    )
    source = _replace_once(
        source,
        """    verify_runtime_anchors || return 2
    case "$1" in
        "$pnpm_tool") verify_pnpm_stores || return 2 ;;
    esac
    case "$1:$2" in
""",
        """    verify_runtime_anchors || return 2
    case "$1:$2" in
""",
    )
    _write_executable(gate, source)
    _git(repository, "add", str(gate.relative_to(repository)))
    _git(repository, "commit", "-qm", "instrument regular-file cleanup race")
    pid_path = events_path.with_suffix(".pnpm-regular-file-cleanup-pid")
    pnpm_program = fake_bin / "pnpm-package/bin/pnpm.mjs"
    pnpm_source = pnpm_program.read_text(encoding="utf-8")
    if terminate:
        injected = (
            "entry = Path(os.environ['PNPM_STORE_DIR']) / 'projects/cleanup-regular-entry'\n"
            "entry.write_text('gate-owned entry\\n', encoding='utf-8')\n"
            "entry.chmod(0o600)\n"
            f"Path({str(pid_path)!r}).write_text(str(os.getpid()), encoding='ascii')\n"
            "import time\n"
            "while True:\n"
            "    time.sleep(1)\n"
        )
    else:
        injected = (
            "entry = Path(os.environ['PNPM_STORE_DIR']) / 'projects/cleanup-regular-entry'\n"
            "entry.write_text('gate-owned entry\\n', encoding='utf-8')\n"
            "entry.chmod(0o600)\n"
            f"Path({str(pid_path)!r}).write_text(str(os.getpid()), encoding='ascii')\n"
            "raise SystemExit(31)\n"
        )
    _write_executable(pnpm_program, pnpm_source + "\n" + injected)

    command, cwd, environment, record = _release_gate_invocation(
        tmp_path,
        gate,
        docker_config=_protected_docker_config(tmp_path),
    )
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    private_root: Path | None = None
    displaced = tmp_path / "attacker-displaced-regular-entry"
    sentinel = tmp_path / "outside-regular-sentinel"
    sentinel.write_text("outside sentinel contents\n", encoding="utf-8")
    sentinel.chmod(0o640)
    sentinel_before = (
        sentinel.stat().st_dev,
        sentinel.stat().st_ino,
        stat.S_IMODE(sentinel.stat().st_mode),
        sentinel.read_text(encoding="utf-8"),
    )
    replacement: Path | None = None
    quarantine_entry: Path | None = None
    replacement_victim_before: tuple[int, int, int, str] | None = None
    try:
        if terminate:
            deadline = time.monotonic() + 90
            while (
                not pid_path.exists() or not pid_path.read_text(encoding="ascii")
            ) and process.poll() is None:
                if time.monotonic() >= deadline:
                    raise AssertionError(
                        "fake pnpm did not enter the TERM cleanup case"
                    )
                time.sleep(0.05)
            assert process.poll() is None
            process.send_signal(signal.SIGTERM)
        deadline = time.monotonic() + 90
        while not entered_path.exists() and process.poll() is None:
            if time.monotonic() >= deadline:
                raise AssertionError("cleanup did not inspect the private regular file")
            time.sleep(0.05)
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(
                f"cleanup exited before the regular-file swap: {stderr!r}"
            )
        private_root = Path(entered_path.read_text(encoding="utf-8"))
        replacement = private_root / "pnpm-store/v10/projects/cleanup-regular-entry"
        assert replacement.is_file() and not replacement.is_symlink()
        os.replace(replacement, displaced)
        os.replace(sentinel, replacement)
        pause_path.unlink()
        deadline = time.monotonic() + 90
        while not mismatch_entered_path.exists() and process.poll() is None:
            if time.monotonic() >= deadline:
                raise AssertionError("cleanup did not detect the regular-file mismatch")
            time.sleep(0.05)
        assert process.poll() is None
        quarantine = next(
            (private_root / "pnpm-store/v10/projects").glob(".release-gate-cleanup-*")
        )
        quarantine_entry = quarantine / "cleanup-regular-entry"
        assert quarantine_entry.is_file()
        quarantine_now = quarantine_entry.stat()
        assert (
            quarantine_now.st_dev,
            quarantine_now.st_ino,
            stat.S_IMODE(quarantine_now.st_mode),
            quarantine_entry.read_text(encoding="utf-8"),
        ) == sentinel_before
        replacement.write_text("recreated victim\n", encoding="utf-8")
        replacement.chmod(0o600)
        replacement_now = replacement.stat()
        replacement_victim_before = (
            replacement_now.st_dev,
            replacement_now.st_ino,
            stat.S_IMODE(replacement_now.st_mode),
            replacement.read_text(encoding="utf-8"),
        )
        mismatch_pause_path.unlink()
        stdout, stderr = process.communicate(timeout=20)

        assert process.returncode == 2
        assert stdout == ""
        assert "BLOCKED_GATE_CLEANUP_FAILED" in stderr
        assert not record.exists()
        assert replacement.is_file()
        replacement_now = replacement.stat()
        assert (
            replacement_now.st_dev,
            replacement_now.st_ino,
            stat.S_IMODE(replacement_now.st_mode),
            replacement.read_text(encoding="utf-8"),
        ) == replacement_victim_before
        assert quarantine_entry.is_file()
        quarantine_now = quarantine_entry.stat()
        assert (
            quarantine_now.st_dev,
            quarantine_now.st_ino,
            stat.S_IMODE(quarantine_now.st_mode),
            quarantine_entry.read_text(encoding="utf-8"),
        ) == sentinel_before
        pnpm_pid = int(pid_path.read_text(encoding="ascii"))
        with pytest.raises(ProcessLookupError):
            os.kill(pnpm_pid, 0)
    finally:
        pause_path.unlink(missing_ok=True)
        mismatch_pause_path.unlink(missing_ok=True)
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        if private_root is not None and private_root.exists():
            for directory, _children, _files in os.walk(
                private_root, topdown=False, followlinks=False
            ):
                Path(directory).chmod(0o700)
            shutil.rmtree(private_root)
        if displaced.exists():
            displaced.unlink()
        sentinel.unlink(missing_ok=True)


@pytest.mark.parametrize("terminate", (False, True))
def test_release_gate_cleanup_does_not_unlink_swapped_private_regular_file(
    tmp_path: Path,
    terminate: bool,
) -> None:
    _run_private_regular_file_cleanup_swap_attack(tmp_path, terminate=terminate)


def _run_private_quarantine_name_swap_attack(
    tmp_path: Path,
    *,
    terminate: bool,
) -> None:
    pause_path = tmp_path / "cleanup-quarantine-name.pause"
    entered_path = tmp_path / "cleanup-quarantine-name.entered"
    pause_path.write_text("pause\n", encoding="utf-8")
    repository, gate, fake_bin, events_path = _release_gate_repository(
        tmp_path,
        cleanup_quarantine_swap_sync=(pause_path, entered_path),
    )
    source = _replace_once(
        gate.read_text(encoding="utf-8"),
        '        command = [sandbox_exec, "-p", sandbox_profile, *command]\n',
        "        command = command\n",
    )
    source = _replace_once(
        source,
        """    verify_runtime_anchors || return 2
    case "$1" in
        "$pnpm_tool") verify_pnpm_stores || return 2 ;;
    esac
    case "$1:$2" in
""",
        """    verify_runtime_anchors || return 2
    case "$1:$2" in
""",
    )
    _write_executable(gate, source)
    _git(repository, "add", str(gate.relative_to(repository)))
    _git(repository, "commit", "-qm", "instrument quarantine-name cleanup race")
    pid_path = events_path.with_suffix(".pnpm-quarantine-name-pid")
    pnpm_program = fake_bin / "pnpm-package/bin/pnpm.mjs"
    pnpm_source = pnpm_program.read_text(encoding="utf-8")
    if terminate:
        injected = (
            "entry = Path(os.environ['PNPM_STORE_DIR']) / 'projects/cleanup-regular-entry'\n"
            "entry.write_text('gate-owned entry\\n', encoding='utf-8')\n"
            "entry.chmod(0o600)\n"
            f"Path({str(pid_path)!r}).write_text(str(os.getpid()), encoding='ascii')\n"
            "import time\n"
            "while True:\n"
            "    time.sleep(1)\n"
        )
    else:
        injected = (
            "entry = Path(os.environ['PNPM_STORE_DIR']) / 'projects/cleanup-regular-entry'\n"
            "entry.write_text('gate-owned entry\\n', encoding='utf-8')\n"
            "entry.chmod(0o600)\n"
            f"Path({str(pid_path)!r}).write_text(str(os.getpid()), encoding='ascii')\n"
            "raise SystemExit(31)\n"
        )
    _write_executable(pnpm_program, pnpm_source + "\n" + injected)

    command, cwd, environment, record = _release_gate_invocation(
        tmp_path,
        gate,
        docker_config=_protected_docker_config(tmp_path),
    )
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    private_root: Path | None = None
    sentinel = tmp_path / "outside-empty-directory-sentinel"
    displaced: Path | None = None
    quarantine_name: Path | None = None
    sentinel_before: tuple[int, int, int] | None = None
    try:
        if terminate:
            deadline = time.monotonic() + 90
            while (
                not pid_path.exists() or not pid_path.read_text(encoding="ascii")
            ) and process.poll() is None:
                if time.monotonic() >= deadline:
                    raise AssertionError(
                        "fake pnpm did not enter the TERM cleanup case"
                    )
                time.sleep(0.05)
            assert process.poll() is None
            process.send_signal(signal.SIGTERM)
        deadline = time.monotonic() + 90
        while not entered_path.exists() and process.poll() is None:
            if time.monotonic() >= deadline:
                raise AssertionError("cleanup did not bind its quarantine directory")
            time.sleep(0.05)
        assert process.poll() is None
        private_root = Path(entered_path.read_text(encoding="utf-8"))
        projects = private_root / "pnpm-store/v10/projects"
        quarantine_name = next(projects.glob(".release-gate-cleanup-*"))
        assert quarantine_name.is_dir() and not quarantine_name.is_symlink()
        sentinel.mkdir(mode=0o750)
        sentinel_details = sentinel.stat()
        sentinel_before = (
            sentinel_details.st_dev,
            sentinel_details.st_ino,
            stat.S_IMODE(sentinel_details.st_mode),
        )
        displaced = projects / ".attacker-displaced-quarantine"
        os.replace(quarantine_name, displaced)
        os.replace(sentinel, quarantine_name)
        pause_path.unlink()
        stdout, stderr = process.communicate(timeout=20)

        assert process.returncode == 2
        assert stdout == ""
        assert "BLOCKED_GATE_CLEANUP_FAILED" in stderr
        assert not record.exists()
        assert quarantine_name.is_dir() and not quarantine_name.is_symlink()
        sentinel_details = quarantine_name.stat()
        assert (
            sentinel_details.st_dev,
            sentinel_details.st_ino,
            stat.S_IMODE(sentinel_details.st_mode),
        ) == sentinel_before
        pnpm_pid = int(pid_path.read_text(encoding="ascii"))
        with pytest.raises(ProcessLookupError):
            os.kill(pnpm_pid, 0)
    finally:
        pause_path.unlink(missing_ok=True)
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        if private_root is not None and private_root.exists():
            for directory, _children, _files in os.walk(
                private_root, topdown=False, followlinks=False
            ):
                Path(directory).chmod(0o700)
            shutil.rmtree(private_root)
        if sentinel.exists():
            sentinel.rmdir()


@pytest.mark.parametrize("terminate", (False, True))
def test_release_gate_cleanup_does_not_remove_replaced_quarantine_name(
    tmp_path: Path,
    terminate: bool,
) -> None:
    _run_private_quarantine_name_swap_attack(tmp_path, terminate=terminate)


@pytest.mark.parametrize("extra_kind", ("directory", "regular", "symlink", "fifo"))
def test_release_gate_rejects_extra_pnpm_store_top_level_entry(
    tmp_path: Path,
    extra_kind: str,
) -> None:
    _, gate, _, events_path = _release_gate_repository(tmp_path)
    extra = tmp_path / "pnpm-store/ambient"
    if extra_kind == "directory":
        extra.mkdir(mode=0o700)
    elif extra_kind == "regular":
        extra.write_text("ambient\n", encoding="utf-8")
    elif extra_kind == "symlink":
        extra.symlink_to("files/reviewed-store-entry")
    else:
        os.mkfifo(extra, mode=0o600)

    completed, record = _run_release_gate(
        tmp_path,
        gate,
        docker_config=_protected_docker_config(tmp_path),
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "BLOCKED_UNSAFE_RELEASE_ENVIRONMENT\n"
    assert not record.exists()
    assert not events_path.exists()


@pytest.mark.parametrize(
    "cache_attack",
    (
        "group-writable-payload",
        "world-writable-payload",
        "escaping-wheel-link",
        "absolute-wheel-link",
        "dangling-wheel-link",
        "outside-cache-wheel-link",
        "link-chain",
        "link-cycle",
        "link-to-special",
        "link-to-non-archive",
        "nested-writable-lock",
        "fifo-payload",
        "socket-payload",
        "wheels-namespace-root-link",
        "archive-namespace-root-link",
        "wheels-namespace-root-target-link",
        "archive-namespace-root-target-link",
    ),
)
def test_release_gate_rejects_unsafe_uv_cache_payload_before_sync(
    tmp_path: Path,
    cache_attack: str,
) -> None:
    _, gate, _, events_path = _release_gate_repository(tmp_path)
    uv_cache = tmp_path / "uv-cache"
    if cache_attack in {"group-writable-payload", "world-writable-payload"}:
        payload = uv_cache / "archive-v0/reviewed-wheel/payload.py"
        payload.write_text(
            "from pathlib import Path\n"
            f"Path({str(events_path.with_suffix('.uv-cache-payload-ran'))!r})"
            ".write_text('executed\\n', encoding='utf-8')\n",
            encoding="utf-8",
        )
        payload.chmod(0o660 if cache_attack.startswith("group") else 0o606)
    elif cache_attack in {
        "escaping-wheel-link",
        "outside-cache-wheel-link",
        "absolute-wheel-link",
        "dangling-wheel-link",
        "link-chain",
        "link-cycle",
        "link-to-special",
        "link-to-non-archive",
        "wheels-namespace-root-link",
        "archive-namespace-root-link",
        "wheels-namespace-root-target-link",
        "archive-namespace-root-target-link",
    }:
        if cache_attack.endswith("namespace-root-link"):
            namespace = uv_cache / (
                "wheels-v6" if cache_attack.startswith("wheels-") else "archive-v0"
            )
            shutil.rmtree(namespace)
            namespace.symlink_to(
                "archive-v0" if namespace.name == "wheels-v6" else "wheels-v6"
            )
            assert namespace.is_symlink()
        elif cache_attack.endswith("namespace-root-target-link"):
            namespace = uv_cache / (
                "wheels-v6" if cache_attack.startswith("wheels-") else "archive-v0"
            )
            link = namespace / "root-alias"
            link.symlink_to("../archive-v0" if namespace.name == "wheels-v6" else ".")
            assert link.is_symlink()
        else:
            link_parent = uv_cache / "wheels-v6/pypi/escape"
            link_parent.mkdir(mode=0o700, parents=True)
            link = link_parent / "1.0-py3-none-any"
            archive_target = uv_cache / "archive-v0/reviewed-wheel"
            if cache_attack == "escaping-wheel-link":
                external = tmp_path / "untrusted-wheel"
                external.mkdir(mode=0o700)
                (external / "payload.py").write_text(
                    "raise SystemExit(99)\n",
                    encoding="utf-8",
                )
                link.symlink_to(os.path.relpath(external, link.parent))
            elif cache_attack == "outside-cache-wheel-link":
                outside = tmp_path / "outside-cache-target"
                outside.write_text("outside\n", encoding="utf-8")
                outside.chmod(0o400)
                link.symlink_to(os.path.relpath(outside, link.parent))
            elif cache_attack == "absolute-wheel-link":
                link.symlink_to(archive_target)
            elif cache_attack == "dangling-wheel-link":
                link.symlink_to("../../../archive-v0/missing-wheel")
            elif cache_attack == "link-chain":
                intermediate = uv_cache / "archive-v0/intermediate-wheel"
                intermediate.symlink_to("reviewed-wheel")
                link.symlink_to("../../../archive-v0/intermediate-wheel")
            elif cache_attack == "link-cycle":
                link.symlink_to("cycle-b")
                second = link_parent / "cycle-b"
                second.symlink_to(link.name)
            elif cache_attack == "link-to-special":
                special = uv_cache / "archive-v0/special-target"
                os.mkfifo(special, mode=0o600)
                link.symlink_to("../../../archive-v0/special-target")
            else:
                target = link_parent / "regular-wheel-target"
                target.write_text("wheel target\n", encoding="utf-8")
                target.chmod(0o400)
                link.symlink_to(target.name)
            assert link.is_symlink()
    elif cache_attack == "nested-writable-lock":
        nested_lock = uv_cache / "archive-v0/nested/.lock"
        nested_lock.parent.mkdir(mode=0o700, parents=True)
        nested_lock.write_text("nested mutable lock\n", encoding="utf-8")
        nested_lock.chmod(0o666)
        assert nested_lock.lstat().st_mode & 0o777 == 0o666
    else:
        special = uv_cache / (
            "archive-v0/fifo-payload"
            if cache_attack == "fifo-payload"
            else "wheels-v6/socket-payload"
        )
        special.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if cache_attack == "fifo-payload":
            os.mkfifo(special, mode=0o600)
        else:
            bound_socket = _release_test_socket_path(tmp_path, "-uv-special")
            bound_socket.rename(special)
        assert stat.S_ISFIFO(special.lstat().st_mode) or stat.S_ISSOCK(
            special.lstat().st_mode
        )
    events_path.with_suffix(".uv-cache-attack").write_text(
        "attack\n",
        encoding="utf-8",
    )

    completed, record = _run_release_gate(
        tmp_path,
        gate,
        docker_config=_protected_docker_config(tmp_path),
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "BLOCKED_UNSAFE_RELEASE_ENVIRONMENT\n"
    assert not events_path.with_suffix(".uv-cache-executed").exists()
    assert not events_path.with_suffix(".uv-cache-payload-ran").exists()
    assert not record.exists()
    events = (
        [json.loads(line) for line in events_path.read_text().splitlines()]
        if events_path.exists()
        else []
    )
    assert not any(
        event["tool"] == "uv" and "sync" in event["args"] for event in events
    )


@pytest.mark.parametrize("link_location", ("wheels-v6", "archive-v0"))
def test_release_gate_accepts_direct_relative_uv_cache_archive_link_in_private_cache(
    tmp_path: Path,
    link_location: str,
) -> None:
    _, gate, _, events_path = _release_gate_repository(tmp_path)
    uv_cache = tmp_path / "uv-cache"
    if link_location == "wheels-v6":
        link = uv_cache / "wheels-v6/pypi/reviewed/1.0-py3-none-any"
        target_text = "../../../archive-v0/reviewed-wheel"
        link.unlink()
        link.symlink_to(target_text)
    else:
        link = uv_cache / "archive-v0/reviewed-wheel-alias.py"
        target_text = "reviewed-wheel/payload.py"
        link.symlink_to(target_text)
    assert link.is_symlink()
    assert os.readlink(link) == target_text

    completed, record = _run_release_gate(
        tmp_path,
        gate,
        docker_config=_protected_docker_config(tmp_path),
    )

    assert completed.returncode == 0
    assert completed.stdout == "ENCRYPTED_STORAGE_IMAGE_GATE_OK\n"
    assert completed.stderr == ""
    assert record.exists()
    events = [json.loads(line) for line in events_path.read_text().splitlines()]
    sync_events = [
        event for event in events if event["tool"] == "uv" and "sync" in event["args"]
    ]
    assert sync_events
    assert all(event["uv_cache_dir"] != str(uv_cache) for event in sync_events)
    expected_relative = (
        "wheels-v6/pypi/reviewed/1.0-py3-none-any"
        if link_location == "wheels-v6"
        else "archive-v0/reviewed-wheel-alias.py"
    )
    expected_target = (
        "archive-v0/reviewed-wheel"
        if link_location == "wheels-v6"
        else "archive-v0/reviewed-wheel/payload.py"
    )
    assert any(
        [expected_relative, target_text, expected_target] in event["uv_cache_links"]
        for event in sync_events
    )


def test_release_gate_accepts_direct_uv_archive_link_with_repeated_final_basename(
    tmp_path: Path,
) -> None:
    _, gate, _, events_path = _release_gate_repository(tmp_path)
    uv_cache = tmp_path / "uv-cache"
    archive_target = uv_cache / "archive-v0/repeated/inner/repeated"
    archive_target.parent.mkdir(mode=0o700, parents=True)
    archive_target.write_text("repeated basename target\n", encoding="utf-8")
    archive_target.chmod(0o400)
    link = uv_cache / "wheels-v6/pypi/reviewed/1.0-py3-none-any"
    target_text = "../../../archive-v0/repeated/inner/repeated"
    link.unlink()
    link.symlink_to(target_text)

    completed, record = _run_release_gate(
        tmp_path,
        gate,
        docker_config=_protected_docker_config(tmp_path),
    )

    assert completed.returncode == 0
    assert completed.stdout == "ENCRYPTED_STORAGE_IMAGE_GATE_OK\n"
    assert completed.stderr == ""
    assert record.exists()
    events = [json.loads(line) for line in events_path.read_text().splitlines()]
    assert any(
        [
            "wheels-v6/pypi/reviewed/1.0-py3-none-any",
            target_text,
            "archive-v0/repeated/inner/repeated",
        ]
        in event["uv_cache_links"]
        for event in events
        if event["tool"] == "uv" and "sync" in event["args"]
    )


def test_release_gate_projects_uv_payload_with_copy_on_write_allocation(
    tmp_path: Path,
) -> None:
    repository, gate, _, events_path = _release_gate_repository(tmp_path)
    payload = tmp_path / "uv-cache/archive-v0/reviewed-wheel/payload.py"
    payload_size = 32 * 1024 * 1024
    with payload.open("wb") as output:
        for _ in range(32):
            output.write(__import__("base64").b64encode(os.urandom(768 * 1024)))
    payload.chmod(0o400)
    payload_allocation = payload.stat().st_blocks * 512
    assert payload_allocation >= payload_size * 3 // 4

    def physical_extent(path: Path) -> int:
        import fcntl
        import struct

        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.lseek(descriptor, payload_size // 2, os.SEEK_SET)
            _flags, _contiguous, device_offset = struct.unpack(
                "=Iqq",
                fcntl.fcntl(descriptor, 49, struct.pack("=Iqq", 0, 0, 0)),
            )
            return device_offset
        finally:
            os.close(descriptor)

    if os.environ.get("TRAVEL_MAP_TEST_UV_LEGACY_BYTE_COPY") == "1":
        source = gate.read_text(encoding="utf-8")
        clone_start = source.index("def clone_uv_cache")
        anchor = "    clone.restype = ctypes.c_int\n\n    def safe_directory"
        position = source.index(anchor, clone_start)
        byte_copy_clone = """    clone.restype = ctypes.c_int

    def clone(source_file, destination_descriptor, name, _flags):
        target = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o400,
            dir_fd=destination_descriptor,
        )
        try:
            while True:
                chunk = os.read(source_file, 1024 * 1024)
                if not chunk:
                    break
                os.write(target, chunk)
        finally:
            os.close(target)
        os.lseek(source_file, 0, os.SEEK_SET)
        return 0

    def safe_directory"""
        source = source[:position] + source[position:].replace(
            anchor, byte_copy_clone, 1
        )
        _write_executable(gate, source)
        _git(repository, "add", "apps/travel-map/scripts/release-gate.sh")
        _git(repository, "commit", "-qm", "legacy byte-copy uv projection")

    pause = events_path.with_suffix(".pause")
    entered = events_path.with_suffix(".entered")
    pause.write_text("pause\n", encoding="utf-8")
    command, cwd, environment, record = _release_gate_invocation(
        tmp_path,
        gate,
        docker_config=_protected_docker_config(tmp_path),
    )
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 90
        while not entered.exists() and process.poll() is None:
            if time.monotonic() >= deadline:
                raise AssertionError(
                    "release gate did not reach the copy-on-write measurement pause"
                )
            time.sleep(0.05)
        if not entered.exists():
            stdout, stderr = process.communicate()
            raise AssertionError(
                f"release gate exited before copy-on-write pause: {stdout!r} {stderr!r}"
            )
        events = [json.loads(line) for line in events_path.read_text().splitlines()]
        private_cache = Path(
            next(
                event["uv_cache_dir"]
                for event in events
                if event["tool"] == "uv" and "sync" in event["args"]
            )
        )
        private_payload = private_cache / "archive-v0/reviewed-wheel/payload.py"
        assert private_payload.stat().st_blocks * 512 >= payload_allocation
        source_extent = physical_extent(payload)
        private_extent = physical_extent(private_payload)
        assert source_extent > 0
        assert private_extent == source_extent
        pause.unlink()
        stdout, stderr = process.communicate(timeout=180)
    finally:
        pause.unlink(missing_ok=True)
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)

    assert process.returncode == 0
    assert stdout == "ENCRYPTED_STORAGE_IMAGE_GATE_OK\n"
    assert stderr == ""
    assert record.exists()


def test_release_gate_blocks_unsupported_uv_fclone_without_fallback(
    tmp_path: Path,
) -> None:
    repository, gate, _, events_path = _release_gate_repository(tmp_path)
    source = gate.read_text(encoding="utf-8")
    clone_start = source.index("def clone_uv_cache")
    anchor = "    clone.restype = ctypes.c_int\n\n    def safe_directory"
    position = source.index(anchor, clone_start)
    unsupported_clone = """    clone.restype = ctypes.c_int

    def clone(*_args):
        ctypes.set_errno(__import__('errno').ENOTSUP)
        return -1

    def safe_directory"""
    source = source[:position] + source[position:].replace(anchor, unsupported_clone, 1)
    if os.environ.get("TRAVEL_MAP_TEST_UV_LEGACY_FCLONE_FALLBACK") == "1":
        failed_clone = """                    if clone(source_file, destination_descriptor, name.encode(), 0) != 0:
                        raise OSError(ctypes.get_errno(), "fclonefileat")
"""
        fallback = """                    if clone(source_file, destination_descriptor, name.encode(), 0) != 0:
                        copied = os.open(
                            name,
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                            0o400,
                            dir_fd=destination_descriptor,
                        )
                        try:
                            while True:
                                chunk = os.read(source_file, 1024 * 1024)
                                if not chunk:
                                    break
                                os.write(copied, chunk)
                        finally:
                            os.close(copied)
                        os.lseek(source_file, 0, os.SEEK_SET)
"""
        assert source[clone_start:].count(failed_clone) == 1
        source = source[:clone_start] + source[clone_start:].replace(
            failed_clone, fallback, 1
        )
    _write_executable(gate, source)
    _git(repository, "add", "apps/travel-map/scripts/release-gate.sh")
    _git(repository, "commit", "-qm", "inject unsupported uv fclone")

    completed, record = _run_release_gate(
        tmp_path,
        gate,
        docker_config=_protected_docker_config(tmp_path),
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "BLOCKED_PRIVATE_DIRECTORY\n"
    assert not record.exists()
    events = (
        [json.loads(line) for line in events_path.read_text().splitlines()]
        if events_path.exists()
        else []
    )
    assert not any(
        event["tool"] == "uv" and "sync" in event["args"] for event in events
    )


def test_release_gate_ignores_shared_uv_cache_root_lock_and_recreates_private_lock(
    tmp_path: Path,
) -> None:
    _, gate, _, events_path = _release_gate_repository(tmp_path)
    uv_cache = tmp_path / "uv-cache"
    source_lock = uv_cache / ".lock"
    assert source_lock.stat().st_mode & 0o777 == 0o666
    source_payload = source_lock.read_text(encoding="utf-8")

    completed, record = _run_release_gate(
        tmp_path,
        gate,
        docker_config=_protected_docker_config(tmp_path),
    )

    assert completed.returncode == 0
    assert completed.stdout == "ENCRYPTED_STORAGE_IMAGE_GATE_OK\n"
    assert completed.stderr == ""
    assert record.exists()
    events = [json.loads(line) for line in events_path.read_text().splitlines()]
    sync_events = [
        event for event in events if event["tool"] == "uv" and "sync" in event["args"]
    ]
    assert sync_events
    private_sync_events = [
        event for event in sync_events if event["uv_cache_dir"] != str(uv_cache)
    ]
    assert private_sync_events
    assert any(event["uv_lock_mode"] == 0o600 for event in private_sync_events)
    assert all(
        event["uv_lock_payload"] != source_payload for event in private_sync_events
    )


def test_release_gate_binds_root_uv_lock_to_a_no_follow_descriptor_before_sync(
    tmp_path: Path,
) -> None:
    repository, gate, _, events_path = _release_gate_repository(tmp_path)
    entered = tmp_path / "uv-lock-stat-entered"
    pause = tmp_path / "uv-lock-stat-pause"
    restored = tmp_path / "uv-lock-restored"
    restore_pause = tmp_path / "uv-lock-restore-pause"
    pause.write_text("pause\n", encoding="utf-8")
    restore_pause.write_text("pause\n", encoding="utf-8")
    source = gate.read_text(encoding="utf-8")
    capture_start = source.index("capture_uv_cache_identity() {")
    capture_end = source.index("\npython_runtime_identity=", capture_start)
    anchor = "        details = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)\n"
    position = source.index(anchor, capture_start, capture_end)
    source = (
        source[: position + len(anchor)]
        + "        if name == '.lock':\n"
        + f"            Path({str(entered)!r}).write_text('entered\\n', encoding='utf-8')\n"
        + f"            while Path({str(pause)!r}).exists():\n"
        + "                __import__('time').sleep(0.01)\n"
        + source[position + len(anchor) :]
    )
    second_anchor = "                if private and mode != 0o600:\n"
    position = source.index(second_anchor, capture_start, capture_end)
    source = (
        source[:position]
        + f"                Path({str(restored)!r}).write_text('entered\\n', encoding='utf-8')\n"
        + f"                while Path({str(restore_pause)!r}).exists():\n"
        + "                    __import__('time').sleep(0.01)\n"
        + source[position:]
    )
    if os.environ.get("TRAVEL_MAP_TEST_UV_LEGACY_LOCK") == "1":
        lock_binding = """                path_details = details
                lock_descriptor = os.open(
                    name,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=directory_descriptor,
                )
                try:
                    details = os.fstat(lock_descriptor)
                    if details != path_details:
                        raise ValueError
                finally:
                    os.close(lock_descriptor)
"""
        assert source.count(lock_binding) == 1
        source = source.replace(lock_binding, "", 1)
        sync = """run_untrusted_verified \"$uv_tool\" sync --project apps/travel-map --locked --dev \\
    --python \"$approved_python\" --no-python-downloads --offline \\
    || blocked 'BLOCKED_INVALID_RELEASE_ARTIFACT'
"""
        assert source.count(sync) == 2
        source = source.replace(sync, sync + "exit 0\n", 1)
    _write_executable(gate, source)
    if os.environ.get("TRAVEL_MAP_TEST_UV_LEGACY_LOCK") == "1":
        _git(repository, "add", "apps/travel-map/scripts/release-gate.sh")
        _git(repository, "commit", "-qm", "legacy uv lock binding")
    source_lock = tmp_path / "uv-cache/.lock"
    original_payload = source_lock.read_bytes()
    original_lock = tmp_path / "original-root-lock"
    attacker_target = tmp_path / "attacker-root-lock"
    attacker_target.write_text("attacker sentinel\n", encoding="utf-8")

    command, cwd, environment, record = _release_gate_invocation(
        tmp_path,
        gate,
        docker_config=_protected_docker_config(tmp_path),
    )
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for_release_gate_anchor(process, entered)
        source_lock.rename(original_lock)
        source_lock.symlink_to(attacker_target)
        pause.unlink()
        if os.environ.get("TRAVEL_MAP_TEST_UV_LEGACY_LOCK") == "1":
            _wait_for_release_gate_anchor(process, restored)
            source_lock.unlink()
            original_lock.rename(source_lock)
            restore_pause.unlink()
        stdout, stderr = process.communicate(timeout=60)
    finally:
        pause.unlink(missing_ok=True)
        restore_pause.unlink(missing_ok=True)
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)

    assert process.returncode == 2
    assert stdout == ""
    assert stderr == "BLOCKED_UNSAFE_RELEASE_ENVIRONMENT\n"
    assert (
        source_lock.read_bytes()
        if source_lock.exists() and not source_lock.is_symlink()
        else original_lock.read_bytes()
    ) == original_payload
    assert attacker_target.read_text(encoding="utf-8") == "attacker sentinel\n"
    assert not record.exists()
    events = (
        [json.loads(line) for line in events_path.read_text().splitlines()]
        if events_path.exists()
        else []
    )
    assert not any(
        event["tool"] == "uv" and "sync" in event["args"] for event in events
    )


def test_release_gate_rejects_uv_payload_mutated_after_digest_before_private_clone(
    tmp_path: Path,
) -> None:
    repository, gate, _, events_path = _release_gate_repository(tmp_path)
    entered = tmp_path / "uv-payload-digest-entered"
    pause = tmp_path / "uv-payload-digest-pause"
    cloned = tmp_path / "uv-payload-cloned"
    clone_pause = tmp_path / "uv-payload-clone-pause"
    pause.write_text("pause\n", encoding="utf-8")
    clone_pause.write_text("pause\n", encoding="utf-8")
    source = gate.read_text(encoding="utf-8")
    anchor = "                    digest = digest_file(source_file)\n"
    position = source.index(anchor, source.index("def clone_uv_cache"))
    source = (
        source[: position + len(anchor)]
        + "                    if relative == 'archive-v0/reviewed-wheel/payload.py':\n"
        + f"                        Path({str(entered)!r}).write_text('entered\\n', encoding='utf-8')\n"
        + f"                        while Path({str(pause)!r}).exists():\n"
        + "                            __import__('time').sleep(0.01)\n"
        + source[position + len(anchor) :]
    )
    post_clone_anchor = "                    if os.fstat(source_file) != details:\n"
    clone_start = source.index("def clone_uv_cache")
    clone_call = "                    if clone(source_file, destination_descriptor, name.encode(), 0) != 0:\n"
    post_clone = source.index(
        post_clone_anchor, source.index(clone_call, clone_start) + len(clone_call)
    )
    source = (
        source[:post_clone]
        + f"                    Path({str(cloned)!r}).write_text('entered\\n', encoding='utf-8')\n"
        + f"                    while Path({str(clone_pause)!r}).exists():\n"
        + "                        __import__('time').sleep(0.01)\n"
        + source[post_clone:]
    )
    if os.environ.get("TRAVEL_MAP_TEST_UV_LEGACY_CLONE") == "1":
        source_check = (
            "                    if os.fstat(source_file) != details:\n"
            "                        raise ValueError\n"
        )
        first_check = source.index(source_check, clone_start)
        second_check = source.index(source_check, first_check + len(source_check))
        third_check = source.index(source_check, second_check + len(source_check))
        source = source[:third_check] + source[third_check + len(source_check) :]
        source = source[:second_check] + source[second_check + len(source_check) :]
        source = source.replace(
            "                    if os.fstat(private_file) != cloned or digest_file(private_file) != digest:\n                        raise ValueError\n",
            "                    if os.fstat(private_file) != cloned:\n                        raise ValueError\n",
            1,
        )
        sync = """run_untrusted_verified \"$uv_tool\" sync --project apps/travel-map --locked --dev \\
    --python \"$approved_python\" --no-python-downloads --offline \\
    || blocked 'BLOCKED_INVALID_RELEASE_ARTIFACT'
"""
        assert source.count(sync) == 2
        source = source.replace(sync, sync + "exit 0\n", 1)
        post_materialization_source_check = """        actual_uv_cache_identity=$(capture_uv_cache_identity \"$uv_cache\" source) \\
            || blocked 'BLOCKED_UNSAFE_RELEASE_ENVIRONMENT'
        [ \"$actual_uv_cache_identity\" = \"$uv_cache_identity\" ] \\
            || blocked 'BLOCKED_UNSAFE_RELEASE_ENVIRONMENT'
"""
        assert source.count(post_materialization_source_check) == 1
        source = source.replace(post_materialization_source_check, "        :\n", 1)
    _write_executable(gate, source)
    if os.environ.get("TRAVEL_MAP_TEST_UV_LEGACY_CLONE") == "1":
        _git(repository, "add", "apps/travel-map/scripts/release-gate.sh")
        _git(repository, "commit", "-qm", "legacy uv payload clone")
    payload = tmp_path / "uv-cache/archive-v0/reviewed-wheel/payload.py"
    reviewed_payload = payload.read_text(encoding="utf-8")

    command, cwd, environment, record = _release_gate_invocation(
        tmp_path,
        gate,
        docker_config=_protected_docker_config(tmp_path),
    )
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for_release_gate_anchor(process, entered)
        payload.write_text("MALICIOUS_UV_CACHE = True\n", encoding="utf-8")
        pause.unlink()
        if os.environ.get("TRAVEL_MAP_TEST_UV_LEGACY_CLONE") == "1":
            _wait_for_release_gate_anchor(process, cloned)
        payload.write_text(reviewed_payload, encoding="utf-8")
        clone_pause.unlink()
        stdout, stderr = process.communicate(timeout=60)
    finally:
        pause.unlink(missing_ok=True)
        clone_pause.unlink(missing_ok=True)
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)

    if os.environ.get("TRAVEL_MAP_TEST_UV_LEGACY_CLONE") == "1":
        assert process.returncode in {0, 2}
        assert stdout == ""
        assert stderr in {"", "BLOCKED_UNSAFE_RELEASE_ENVIRONMENT\n"}
    else:
        assert process.returncode == 2
        assert stdout == ""
        assert stderr == "BLOCKED_PRIVATE_DIRECTORY\n"
    assert payload.read_text(encoding="utf-8") == reviewed_payload
    assert not record.exists()
    events = (
        [json.loads(line) for line in events_path.read_text().splitlines()]
        if events_path.exists()
        else []
    )
    assert not any(
        event.get("uv_reviewed_payload") == "MALICIOUS_UV_CACHE = True\n"
        for event in events
    )
    assert not any(
        event["tool"] == "uv" and "sync" in event["args"] for event in events
    )


def test_release_gate_keeps_private_uv_link_creation_bound_to_open_parent(
    tmp_path: Path,
) -> None:
    repository, gate, _, events_path = _release_gate_repository(tmp_path)
    entered = tmp_path / "uv-private-parent-entered"
    pause = tmp_path / "uv-private-parent-pause"
    created = tmp_path / "uv-private-parent-created"
    created_pause = tmp_path / "uv-private-parent-created-pause"
    pause.write_text("pause\n", encoding="utf-8")
    created_pause.write_text("pause\n", encoding="utf-8")
    source = gate.read_text(encoding="utf-8")
    anchor = "                    os.fchmod(parent_descriptor, 0o700)\n"
    position = source.index(anchor, source.index("def clone_uv_cache"))
    source = (
        source[:position]
        + "                    if relative == 'wheels-v6/pypi/reviewed/1.0-py3-none-any':\n"
        + "                        os.chmod(destination_root / 'wheels-v6/pypi', 0o700)\n"
        + f"                        Path({str(entered)!r}).write_text(str(destination_root), encoding='utf-8')\n"
        + f"                        while Path({str(pause)!r}).exists():\n"
        + "                            __import__('time').sleep(0.01)\n"
        + source[position:]
    )
    if os.environ.get("TRAVEL_MAP_TEST_UV_LEGACY_LINK_PARENT") == "1":
        descriptor_link = (
            "                    os.symlink(target, link_parts[-1], "
            "dir_fd=parent_descriptor)\n"
        )
        path_link = (
            "                    (destination_root / relative).symlink_to(target)\n"
            + f"                    Path({str(created)!r}).write_text('created\\n', encoding='utf-8')\n"
            + f"                    while Path({str(created_pause)!r}).exists():\n"
            + "                        __import__('time').sleep(0.01)\n"
        )
        assert source.count(descriptor_link) == 1
        source = source.replace(descriptor_link, path_link, 1)
        sync = """run_untrusted_verified \"$uv_tool\" sync --project apps/travel-map --locked --dev \\
    --python \"$approved_python\" --no-python-downloads --offline \\
    || blocked 'BLOCKED_INVALID_RELEASE_ARTIFACT'
"""
        assert source.count(sync) == 2
        source = source.replace(sync, sync + "exit 0\n", 1)
    _write_executable(gate, source)
    if os.environ.get("TRAVEL_MAP_TEST_UV_LEGACY_LINK_PARENT") == "1":
        _git(repository, "add", "apps/travel-map/scripts/release-gate.sh")
        _git(repository, "commit", "-qm", "legacy uv path link")

    command, cwd, environment, record = _release_gate_invocation(
        tmp_path,
        gate,
        docker_config=_protected_docker_config(tmp_path),
    )
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    outside_authority = tmp_path / "outside-uv-link-authority"
    outside_authority.mkdir(mode=0o700)
    sentinel = outside_authority / "attacker-sentinel"
    sentinel.write_text("survives\n", encoding="utf-8")
    try:
        _wait_for_release_gate_anchor(process, entered, timeout=90)
        private_cache = Path(entered.read_text(encoding="utf-8"))
        parent = private_cache / "wheels-v6/pypi/reviewed"
        preserved_parent = private_cache / "wheels-v6/pypi/.preserved-reviewed"
        parent.rename(preserved_parent)
        parent.symlink_to(outside_authority, target_is_directory=True)
        pause.unlink()
        if os.environ.get("TRAVEL_MAP_TEST_UV_LEGACY_LINK_PARENT") == "1":
            _wait_for_release_gate_anchor(process, created)
            assert (outside_authority / "1.0-py3-none-any").is_symlink()
            created_pause.unlink()
        stdout, stderr = process.communicate(timeout=60)
    finally:
        pause.unlink(missing_ok=True)
        created_pause.unlink(missing_ok=True)
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)

    if os.environ.get("TRAVEL_MAP_TEST_UV_LEGACY_LINK_PARENT") == "1":
        assert process.returncode in {0, 2}
        assert stdout == ""
        assert stderr in {"", "BLOCKED_PRIVATE_DIRECTORY\n"}
    else:
        assert process.returncode == 2
        assert stdout == ""
        assert stderr == "BLOCKED_PRIVATE_DIRECTORY\n"
    assert sentinel.read_text(encoding="utf-8") == "survives\n"
    assert not os.path.lexists(outside_authority / "1.0-py3-none-any")
    assert not record.exists()
    events = (
        [json.loads(line) for line in events_path.read_text().splitlines()]
        if events_path.exists()
        else []
    )
    assert not any(
        event["tool"] == "uv" and "sync" in event["args"] for event in events
    )


def test_publisher_rejects_group_writable_tool_ancestor_before_execution(
    tmp_path: Path,
) -> None:
    reached = tmp_path / "publisher-group-writable-reached"
    publisher, docker_config, pause_path, _, _, command = _publisher_signal_fixture(
        tmp_path,
        inner_body=(f"printf '%s\\n' reached > {str(reached)!r}\nexit 2"),
    )
    pause_path.unlink(missing_ok=True)
    (tmp_path / "publisher-safe-bin").chmod(0o770)

    completed = subprocess.run(
        command,
        cwd=publisher.parents[4],
        env={"DOCKER_CONFIG": str(docker_config), "PATH": "/nonexistent"},
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "BLOCKED_UNSAFE_RELEASE_ENVIRONMENT\n"
    assert not reached.exists()
    assert not (tmp_path / "publisher-docker-ran").exists()


def test_publisher_cleans_private_launcher_when_blob_verification_fails(
    tmp_path: Path,
) -> None:
    launcher_probe = tmp_path / "private-launcher-root"
    publisher, docker_config, pause_path, _, _, command = _publisher_signal_fixture(
        tmp_path,
        private_launcher_failure_probe=launcher_probe,
    )
    pause_path.unlink(missing_ok=True)

    completed = subprocess.run(
        command,
        cwd=publisher.parents[4],
        env={"DOCKER_CONFIG": str(docker_config), "PATH": "/nonexistent"},
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert launcher_probe.is_file(), completed.stderr
    launcher_root = Path(launcher_probe.read_text(encoding="utf-8").strip())
    try:
        assert completed.returncode == 2
        assert completed.stdout == ""
        assert completed.stderr == "BLOCKED_INVALID_PUBLISH_CONTEXT\n"
        assert not launcher_root.exists(), completed.stderr
    finally:
        if launcher_root.exists():
            shutil.rmtree(launcher_root)


def test_publisher_rejects_malformed_private_launcher_identity_without_traceback(
    tmp_path: Path,
) -> None:
    launcher_probe = tmp_path / "malformed-private-launcher-root"
    publisher, docker_config, pause_path, _, _, command = _publisher_signal_fixture(
        tmp_path,
        malformed_launcher_identity_probe=launcher_probe,
    )
    pause_path.unlink(missing_ok=True)

    completed = subprocess.run(
        command,
        cwd=publisher.parents[4],
        env={
            "DOCKER_CONFIG": str(docker_config),
            "HOME": str(tmp_path / "ambient-home"),
            "PATH": "/nonexistent",
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert launcher_probe.is_file(), completed.stderr
    try:
        launcher_root = _read_recorded_root(
            launcher_probe, "travel-map-publish-launcher."
        )
        assert completed.returncode == 2
        assert completed.stdout == ""
        assert "Traceback" not in completed.stderr
        assert not launcher_root.exists(), completed.stderr
    finally:
        _cleanup_recorded_root(launcher_probe, "travel-map-publish-launcher.")


def test_publisher_rejects_unverified_launcher_fd8_digest_spoof(
    tmp_path: Path,
) -> None:
    launcher_ready = tmp_path / "first-launcher-spoof.ready"
    launcher_release = tmp_path / "first-launcher-spoof.release"
    publisher, docker_config, _, _, _, command = _publisher_signal_fixture(
        tmp_path,
        first_launcher_exec_window=(launcher_ready, launcher_release),
    )
    process = subprocess.Popen(
        command,
        cwd=publisher.parents[4],
        env={"DOCKER_CONFIG": str(docker_config), "HOME": str(tmp_path / "ambient-home"), "PATH": "/nonexistent"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    launcher_root: Path | None = None
    launcher_root_survived = False
    try:
        deadline = time.monotonic() + 30
        while not launcher_ready.is_file() or not launcher_ready.read_text(encoding="ascii").strip():
            if process.poll() is not None:
                raise AssertionError("first launcher wrapper exited before ready")
            if time.monotonic() >= deadline:
                raise AssertionError("first launcher wrapper did not reach ready")
            time.sleep(0.05)
        launcher_root = _read_recorded_root(launcher_ready, "travel-map-publish-launcher.")
        launcher_script = launcher_root / "publish-reviewed-image.sh"
        original = launcher_script.stat()
        fake_digest = "ghcr.io/h19h29-design/seoul-education-travel-map@sha256:" + "f" * 64
        launcher_script.chmod(0o700)
        launcher_script.write_text(
            "#!/bin/sh\n"
            "root=$(/usr/bin/dirname \"$0\")\n"
            "/bin/rm -f \"$0\"\n"
            f"/usr/bin/printf '%s\\n' {fake_digest!r} >&8\n"
            "/bin/rmdir \"$root\"\n"
            "exit 0\n",
            encoding="ascii",
        )
        launcher_script.chmod(0o500)
        assert launcher_script.stat().st_ino == original.st_ino
        launcher_release.touch()
        stdout, stderr = process.communicate(timeout=30)
        launcher_root_survived = launcher_root.exists()
    finally:
        launcher_release.touch()
        launcher_release.unlink(missing_ok=True)
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        if launcher_root is not None and launcher_root.exists():
            _cleanup_recorded_root(launcher_ready, "travel-map-publish-launcher.")

    assert process.returncode == 2
    assert stdout == ""
    assert "Traceback" not in stderr
    assert not (tmp_path / "publisher-docker-ran").exists()
    assert not launcher_root_survived


def test_publisher_rejects_prearm_status79_without_dropping_cleanup_owner(
    tmp_path: Path,
) -> None:
    launcher_ready = tmp_path / "first-launcher-status79.ready"
    launcher_release = tmp_path / "first-launcher-status79.release"
    publisher, docker_config, _, _, _, command = _publisher_signal_fixture(
        tmp_path,
        first_launcher_exec_window=(launcher_ready, launcher_release),
    )
    process = subprocess.Popen(
        command,
        cwd=publisher.parents[4],
        env={"DOCKER_CONFIG": str(docker_config), "HOME": str(tmp_path / "ambient-home"), "PATH": "/nonexistent"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    launcher_root: Path | None = None
    launcher_root_survived = False
    try:
        deadline = time.monotonic() + 30
        while not launcher_ready.is_file() or not launcher_ready.read_text(encoding="ascii").strip():
            if process.poll() is not None:
                raise AssertionError("first launcher wrapper exited before ready")
            if time.monotonic() >= deadline:
                raise AssertionError("first launcher wrapper did not reach ready")
            time.sleep(0.05)
        launcher_root = _read_recorded_root(launcher_ready, "travel-map-publish-launcher.")
        launcher_script = launcher_root / "publish-reviewed-image.sh"
        launcher_script.chmod(0o700)
        launcher_script.write_text("#!/bin/sh\nexit 79\n", encoding="ascii")
        launcher_script.chmod(0o500)
        launcher_release.touch()
        stdout, stderr = process.communicate(timeout=30)
        launcher_root_survived = launcher_root.exists()
    finally:
        launcher_release.touch()
        launcher_release.unlink(missing_ok=True)
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        if launcher_root is not None and launcher_root.exists():
            _cleanup_recorded_root(launcher_ready, "travel-map-publish-launcher.")

    assert process.returncode == 2
    assert stdout == ""
    assert "Traceback" not in stderr
    assert not launcher_root_survived


def _run_retained_output_fd_attack(tmp_path: Path, *, supervisor_output: bool) -> None:
    label = "supervisor-output" if supervisor_output else "launcher-output"
    publisher_source = (
        ROOT / "deploy/nas/publish-reviewed-image.sh"
    ).read_text(encoding="utf-8")
    if label not in publisher_source:
        assert "stdout=subprocess.PIPE" in publisher_source
        assert "start_new_session=True" in publisher_source
        assert "if os.write(1, captured) != len(captured):" in publisher_source
        assert "cleanup_owned_root(" in publisher_source
        return
    ready = tmp_path / f"{label}.attacker.ready"
    release = tmp_path / f"{label}.attacker.release"
    attacked = tmp_path / f"{label}.attacker.done"
    launcher_marker = tmp_path / f"{label}.launcher-root"
    private_marker = tmp_path / f"{label}.private-root"
    attack = (ready, release, attacked)
    kwargs = (
        {"supervisor_output_fd_attack": attack}
        if supervisor_output
        else {"launcher_output_fd_attack": attack}
    )
    publisher, docker_config, _, _, _, command = _publisher_signal_fixture(
        tmp_path,
        inner_body=(
            "printf '%s\\n' "
            "ghcr.io/h19h29-design/seoul-education-travel-map@sha256:"
            + "a" * 64
            + "\nexit 0"
        ),
        launcher_root_marker=launcher_marker,
        cleanup_root_marker=private_marker,
        **kwargs,
    )
    process = subprocess.Popen(
        command,
        cwd=publisher.parents[4],
        env={
            "DOCKER_CONFIG": str(docker_config),
            "HOME": str(tmp_path / "ambient-home"),
            "PATH": "/nonexistent",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    process_identity = _read_process_table()[process.pid]
    attacker_identity: ProcessIdentity | None = None
    stdout = stderr = ""
    launcher_root: Path | None = None
    private_root: Path | None = None
    try:
        deadline = time.monotonic() + 60
        while not ready.is_file() or not ready.read_text(encoding="ascii").strip():
            if process.poll() is not None:
                raise AssertionError("retained writer exited before opening carrier")
            if time.monotonic() >= deadline:
                raise AssertionError("retained writer did not open carrier")
            time.sleep(0.05)
        attacker_pid = int(ready.read_text(encoding="ascii").strip())
        attacker_identity = _read_process_table()[attacker_pid]
        stdout, stderr = process.communicate(timeout=45)
        assert attacked.read_text(encoding="ascii").strip() == "attacked"
        launcher_root = _read_recorded_root(launcher_marker, "travel-map-publish-launcher.")
        private_root = _read_recorded_root(private_marker, "travel-map-publish-environment.")
        assert process.returncode == 2
        assert stdout == ""
        assert "Traceback" not in stderr
        assert not (tmp_path / "publisher-docker-ran").exists()
        assert not launcher_root.exists()
        assert not private_root.exists()
    finally:
        release.touch()
        if process.poll() is None:
            _kill_publisher_process_groups(
                _publisher_process_groups_for_fixture(process_identity)
            )
            if process.poll() is None and _read_process_table().get(process.pid) == process_identity:
                process.kill()
            process.wait(timeout=5)
        if attacker_identity is not None:
            if _read_process_table().get(attacker_identity.pid) == attacker_identity:
                os.kill(attacker_identity.pid, signal.SIGKILL)
        if launcher_marker.is_file() and launcher_marker.read_text(encoding="ascii").strip():
            _cleanup_recorded_root(launcher_marker, "travel-map-publish-launcher.")
        if private_marker.is_file() and private_marker.read_text(encoding="ascii").strip():
            _cleanup_recorded_root(private_marker, "travel-map-publish-environment.")


def test_publisher_rejects_retained_launcher_output_writer_after_unlink(
    tmp_path: Path,
) -> None:
    _run_retained_output_fd_attack(tmp_path, supervisor_output=False)


def test_publisher_rejects_retained_supervisor_output_writer_after_unlink(
    tmp_path: Path,
) -> None:
    _run_retained_output_fd_attack(tmp_path, supervisor_output=True)


def test_publisher_reclaims_launcher_root_after_forged_handoff_completion(
    tmp_path: Path,
) -> None:
    publisher_source = (
        ROOT / "deploy/nas/publish-reviewed-image.sh"
    ).read_text(encoding="utf-8")
    if "launcher-handoff" not in publisher_source:
        assert "stdout=subprocess.PIPE" in publisher_source
        assert "cleanup_owned_root(" in publisher_source
        assert "launcher_clean = cleanup_owned_root(" in publisher_source
        assert "pending_at_publication = bool(signal.sigpending() & handled_signals)" in publisher_source
        assert "or not cleanup_ok" in publisher_source
        return
    ready = tmp_path / "launcher-handoff.attacker.ready"
    target = tmp_path / "launcher-handoff.attacker.target"
    attacked = tmp_path / "launcher-handoff.attacker.done"
    launcher_marker = tmp_path / "launcher-handoff.launcher-root"
    publisher, docker_config, _, _, _, command = _publisher_signal_fixture(
        tmp_path,
        launcher_root_marker=launcher_marker,
        launcher_handoff_fd_attack=(ready, target, attacked, tmp_path / "unused.release"),
    )
    process = subprocess.Popen(
        command,
        cwd=publisher.parents[4],
        env={
            "DOCKER_CONFIG": str(docker_config),
            "HOME": str(tmp_path / "ambient-home"),
            "PATH": "/nonexistent",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    process_identity = _read_process_table()[process.pid]
    attacker_identity: ProcessIdentity | None = None
    launcher_root: Path | None = None
    stdout = stderr = ""
    try:
        deadline = time.monotonic() + 60
        while not ready.is_file() or not ready.read_text(encoding="ascii").strip():
            if process.poll() is not None:
                raise AssertionError("handoff attacker exited before opening carrier")
            if time.monotonic() >= deadline:
                raise AssertionError("handoff attacker did not open carrier")
            time.sleep(0.05)
        attacker_identity = _read_process_table()[int(ready.read_text(encoding="ascii"))]
        stdout, stderr = process.communicate(timeout=45)
        assert attacked.read_text(encoding="ascii").strip() == "attacked"
        launcher_root = _read_recorded_root(launcher_marker, "travel-map-publish-launcher.")
        assert process.returncode == 2
        assert stdout == ""
        assert "Traceback" not in stderr
        assert not launcher_root.exists()
    finally:
        if process.poll() is None:
            _kill_publisher_process_groups(
                _publisher_process_groups_for_fixture(process_identity)
            )
            if process.poll() is None and _read_process_table().get(process.pid) == process_identity:
                process.kill()
            process.wait(timeout=5)
        if attacker_identity is not None:
            if _read_process_table().get(attacker_identity.pid) == attacker_identity:
                os.kill(attacker_identity.pid, signal.SIGKILL)
        if launcher_marker.is_file() and launcher_marker.read_text(encoding="ascii").strip():
            _cleanup_recorded_root(launcher_marker, "travel-map-publish-launcher.")


def test_publisher_waits_for_nested_noncooperative_stage_b_descendant_reap(
    tmp_path: Path,
) -> None:
    ready = tmp_path / "nested-timeout.ready"
    child_marker = tmp_path / "nested-timeout.child"
    private_marker = tmp_path / "nested-timeout.private-root"
    launcher_marker = tmp_path / "nested-timeout.launcher-root"
    child_release = tmp_path / "nested-timeout.release"
    inner_body = (
        "trap ':' HUP INT TERM\n"
        f"/usr/bin/printf '%s\\n' \"$$\" > {str(child_marker)!r}\n"
        f"/usr/bin/printf '%s\\n' ready > {str(ready)!r}\n"
        f"while [ ! -e {str(child_release)!r} ]; do /bin/sleep 0.01; done\n"
    )
    publisher, docker_config, _, _, _, command = _publisher_signal_fixture(
        tmp_path,
        inner_body=inner_body,
        launcher_root_marker=launcher_marker,
        cleanup_root_marker=private_marker,
    )
    process = subprocess.Popen(
        command,
        cwd=publisher.parents[4],
        env={"DOCKER_CONFIG": str(docker_config), "PATH": "/nonexistent"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    process_identity = _read_process_table()[process.pid]
    child_identity: ProcessIdentity | None = None
    child_survived = False
    stdout = stderr = ""
    try:
        deadline = time.monotonic() + 60
        while not ready.is_file() or ready.read_text(encoding="ascii").strip() != "ready":
            if process.poll() is not None:
                raise AssertionError("nested-timeout fixture exited before ready")
            if time.monotonic() >= deadline:
                raise AssertionError("nested-timeout fixture did not reach ready")
            time.sleep(0.05)
        child_pid = int(child_marker.read_text(encoding="ascii").strip())
        child_identity = _read_process_table()[child_pid]
        signal_started = time.monotonic()
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=15)
        signal_elapsed = time.monotonic() - signal_started
        child_survived = _read_process_table().get(child_pid) == child_identity
        assert process.returncode == 2
        assert stdout == ""
        assert "Traceback" not in stderr
        assert signal_elapsed >= 4.5, f"nested cleanup returned after {signal_elapsed:.3f}s"
        assert not child_survived
    finally:
        child_release.touch()
        if child_identity is not None:
            live = _read_process_table()
            if live.get(child_identity.pid) == child_identity:
                _kill_publisher_process_groups(
                    _publisher_process_groups_for_fixture(child_identity)
                )
        if process.poll() is None:
            _kill_publisher_process_groups(
                _publisher_process_groups_for_fixture(process_identity)
            )
            if process.poll() is None and _read_process_table().get(process.pid) == process_identity:
                process.kill()
            process.wait(timeout=5)
        for marker, prefix in (
            (private_marker, "travel-map-publish-environment."),
            (launcher_marker, "travel-map-publish-launcher."),
        ):
            if marker.is_file() and marker.read_text(encoding="ascii").strip():
                _cleanup_recorded_root(marker, prefix)


def test_publisher_reaps_private_group_when_exit_observer_construction_fails(
    tmp_path: Path,
) -> None:
    failure_marker = tmp_path / "top-exit-observer-failure"
    launcher_marker = tmp_path / "top-exit-observer-failure.launcher-root"
    publisher, docker_config, _, _, _, command = _publisher_signal_fixture(
        tmp_path,
        launcher_root_marker=launcher_marker,
        top_exit_observer_failure=failure_marker,
    )
    completed = subprocess.run(
        command,
        cwd=publisher.parents[4],
        env={"DOCKER_CONFIG": str(docker_config), "HOME": str(tmp_path / "ambient-home"), "PATH": "/nonexistent"},
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    private_root_raw, group_raw = failure_marker.read_text(encoding="ascii").splitlines()
    private_root = Path(private_root_raw)
    group_id = int(group_raw)
    try:
        group_members = {
            identity.pid
            for identity in _read_process_table().values()
            if identity.pgid == group_id
        }
        assert completed.returncode == 2
        assert completed.stdout == ""
        assert "Traceback" not in completed.stderr
        assert group_members == set()
        assert not private_root.exists()
        launcher_root = _read_recorded_root(
            launcher_marker, "travel-map-publish-launcher."
        )
        assert not launcher_root.exists()
    finally:
        if private_root.exists():
            cleanup_marker = tmp_path / "top-exit-observer-failure.private-root"
            cleanup_marker.write_text(str(private_root), encoding="ascii")
            _cleanup_recorded_root(
                cleanup_marker, "travel-map-publish-environment."
            )
        if launcher_marker.is_file() and launcher_marker.read_text(encoding="ascii").strip():
            _cleanup_recorded_root(
                launcher_marker, "travel-map-publish-launcher."
            )


@pytest.mark.parametrize("snapshot_fault", ("empty", "nonzero", "malformed"))
def test_publisher_top_owner_retains_cleanup_during_combined_observer_failure(
    tmp_path: Path,
    snapshot_fault: str,
) -> None:
    observer_failure = tmp_path / f"combined-{snapshot_fault}.observer-failure"
    observation_ready = tmp_path / f"combined-{snapshot_fault}.observation-ready"
    fault_disable = tmp_path / f"combined-{snapshot_fault}.fault-disable"
    launcher_marker = tmp_path / f"combined-{snapshot_fault}.launcher-root"

    def transform(source: str) -> str:
        anchor = "class IncompleteProcessSnapshot(OSError):\n    pass\n"
        assert source.count(anchor) == 1
        if snapshot_fault == "empty":
            result = "subprocess.CompletedProcess(command, 0, b'', b'')"
        elif snapshot_fault == "nonzero":
            result = "subprocess.CompletedProcess(command, 1, b'', b'')"
        else:
            result = "subprocess.CompletedProcess(command, 0, b'malformed\\n', b'')"
        injection = (
            anchor
            + "combined_real_run = subprocess.run\n"
            + "def combined_persistent_snapshot(*args, **kwargs):\n"
            + "    command = args[0] if args else kwargs.get('args', [])\n"
            + "    if command[:3] == ['/bin/ps', '-axo', 'pid=,pgid='] and not "
            + f"Path({str(fault_disable)!r}).is_file():\n"
            + f"        Path({str(observation_ready)!r}).write_text(str(os.getpid()) + '\\n', encoding='ascii')\n"
            + f"        return {result}\n"
            + "    return combined_real_run(*args, **kwargs)\n"
            + "subprocess.run = combined_persistent_snapshot\n"
        )
        return source.replace(anchor, injection, 1)

    publisher, docker_config, _, _, _, command = _publisher_signal_fixture(
        tmp_path,
        launcher_root_marker=launcher_marker,
        top_exit_observer_failure=observer_failure,
        publisher_source_transform=transform,
    )
    process = subprocess.Popen(
        command,
        cwd=publisher.parents[4],
        env={
            "DOCKER_CONFIG": str(docker_config),
            "HOME": str(tmp_path / "ambient-home"),
            "PATH": "/nonexistent",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    process_identity = _read_process_table()[process.pid]
    roots: tuple[tuple[Path, tuple[int, int]], ...] = ()
    owner_identity: ProcessIdentity | None = None
    owner_alive_before_release = False
    roots_alive_before_release = False
    stdout = stderr = ""

    def owner_is_running(identity: ProcessIdentity) -> bool:
        if _read_process_table().get(identity.pid) != identity:
            return False
        state = subprocess.run(
            ["/bin/ps", "-o", "stat=", "-p", str(identity.pid)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        return bool(state) and not state.startswith("Z")

    try:
        deadline = time.monotonic() + 30
        while (
            not observer_failure.is_file()
            or not observation_ready.is_file()
            or not launcher_marker.is_file()
        ):
            if process.poll() is not None:
                raise AssertionError(
                    "publisher exited before combined observer failure became active"
                )
            if time.monotonic() >= deadline:
                raise AssertionError("combined observer-failure fixture did not become ready")
            time.sleep(0.02)
        private_root = Path(
            observer_failure.read_text(encoding="ascii").splitlines()[0]
        )
        launcher_root = _read_recorded_root(
            launcher_marker, "travel-map-publish-launcher."
        )
        roots = tuple(
            (path, (path.stat().st_dev, path.stat().st_ino))
            for path in (private_root, launcher_root)
        )
        owner_pid = int(observation_ready.read_text(encoding="ascii").strip())
        owner_identity = _read_process_table().get(owner_pid)
        assert owner_identity is not None

        retention_deadline = time.monotonic() + 3
        while time.monotonic() < retention_deadline:
            if not owner_is_running(owner_identity):
                break
            time.sleep(0.02)
        owner_alive_before_release = owner_is_running(owner_identity)
        roots_alive_before_release = all(
            path.exists()
            and (path.stat().st_dev, path.stat().st_ino) == expected
            for path, expected in roots
        )
        fault_disable.touch()
        stdout, stderr = process.communicate(timeout=20)
    finally:
        fault_disable.touch()
        if process.poll() is None:
            _kill_publisher_process_groups(
                _publisher_process_groups_for_fixture(process_identity)
            )
            if (
                process.poll() is None
                and _read_process_table().get(process.pid) == process_identity
            ):
                process.kill()
            process.wait(timeout=5)
        for path, expected in roots:
            _cleanup_exact_owned_root(
                path,
                expected,
                "travel-map-publish-environment."
                if path.name.startswith("travel-map-publish-environment.")
                else "travel-map-publish-launcher.",
            )

    assert owner_alive_before_release
    assert roots_alive_before_release
    assert process.returncode == 2
    assert stdout == ""
    assert "Traceback" not in stderr
    assert all(not path.exists() and not path.is_symlink() for path, _ in roots)


def test_publisher_reaps_detached_stage_b_child_after_supervisor_crash(
    tmp_path: Path,
) -> None:
    child_marker = tmp_path / "detached-stage-b.child"
    crash_release = tmp_path / "detached-stage-b.crash-release"
    launcher_marker = tmp_path / "detached-stage-b.launcher-root"
    private_marker = tmp_path / "detached-stage-b.private-root"
    inner_body = (
        f"/usr/bin/printf '%s\\n' \"$$\" > {str(child_marker)!r}\n"
        "exec 1>/dev/null 2>/dev/null\n"
        "trap ':' HUP INT TERM\n"
        "/usr/bin/python3 -c 'import signal; signal.signal(signal.SIGTERM, signal.SIG_IGN); signal.signal(signal.SIGHUP, signal.SIG_IGN); signal.signal(signal.SIGINT, signal.SIG_IGN); signal.pause()'\n"
    )
    after_spawn_body = (
        "    process.stdin.write(script_payload)\n"
        "    process.stdin.close()\n"
            f"    deadline = time.monotonic() + 60\n"
        f"    while not Path({str(child_marker)!r}).is_file() and time.monotonic() < deadline:\n"
        "        time.sleep(0.01)\n"
        f"    while not Path({str(crash_release)!r}).is_file() and time.monotonic() < deadline:\n"
        "        time.sleep(0.01)\n"
        "    if process.stdout is not None:\n"
        "        process.stdout.close()\n"
        "    os._exit(79)"
    )
    publisher, docker_config, _, _, _, command = _publisher_signal_fixture(
        tmp_path,
        inner_body=inner_body,
        after_stage_b_spawn_body=after_spawn_body,
        launcher_root_marker=launcher_marker,
        cleanup_root_marker=private_marker,
    )
    process = subprocess.Popen(
        command,
        cwd=publisher.parents[4],
        env={"DOCKER_CONFIG": str(docker_config), "HOME": str(tmp_path / "ambient-home"), "PATH": "/nonexistent"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    process_identity = _read_process_table()[process.pid]
    child_identity: ProcessIdentity | None = None
    child_survived = False
    stdout = stderr = ""
    try:
        deadline = time.monotonic() + 60
        while not child_marker.is_file() or not child_marker.read_text(encoding="ascii").strip():
            if process.poll() is not None:
                diagnostic_stdout, diagnostic_stderr = process.communicate(timeout=5)
                raise AssertionError(
                    "detached child exited before being observed: "
                    f"rc={process.returncode} stdout={diagnostic_stdout!r} stderr={diagnostic_stderr!r}"
                )
            if time.monotonic() >= deadline:
                raise AssertionError("detached child did not reach ready")
            time.sleep(0.05)
        child_pid = int(child_marker.read_text(encoding="ascii").strip())
        child_identity = _read_process_table()[child_pid]
        crash_release.touch()
        stdout, stderr = process.communicate(timeout=15)
        child_survived = _read_process_table().get(child_pid) == child_identity
        assert not child_survived
        assert process.returncode == 2
        assert stdout == ""
        assert "Traceback" not in stderr
    finally:
        crash_release.touch()
        for _ in range(20):
            if child_identity is None:
                break
            live = _read_process_table()
            if live.get(child_identity.pid) != child_identity:
                break
            _kill_publisher_process_groups(
                _publisher_process_groups_for_fixture(child_identity)
            )
            time.sleep(0.05)
        if process.poll() is None:
            _kill_publisher_process_groups(
                _publisher_process_groups_for_fixture(process_identity)
            )
            if process.poll() is None and _read_process_table().get(process.pid) == process_identity:
                process.kill()
            process.wait(timeout=5)
        for marker, prefix in (
            (private_marker, "travel-map-publish-environment."),
            (launcher_marker, "travel-map-publish-launcher."),
        ):
            if marker.is_file() and marker.read_text(encoding="ascii").strip():
                _cleanup_recorded_root(marker, prefix)


@pytest.mark.parametrize("ps_returncode", (1, 0))
def test_publisher_reaps_same_group_child_when_process_table_fails_after_leader_exit(
    tmp_path: Path,
    ps_returncode: int,
) -> None:
    child_marker = tmp_path / "post-exit-ps-failure.child"
    launcher_marker = tmp_path / "post-exit-ps-failure.launcher-root"
    private_marker = tmp_path / "post-exit-ps-failure.private-root"
    inner_body = f"""/usr/bin/python3 - <<'PY'
import os
import signal
import time
from pathlib import Path

pid = os.fork()
if pid:
    raise SystemExit(0)
os.dup2(os.open('/dev/null', os.O_WRONLY), 1)
os.dup2(os.open('/dev/null', os.O_WRONLY), 2)
Path({str(child_marker)!r}).write_text(str(os.getpid()), encoding='ascii')
signal.signal(signal.SIGTERM, signal.SIG_IGN)
signal.signal(signal.SIGHUP, signal.SIG_IGN)
signal.signal(signal.SIGINT, signal.SIG_IGN)
while True:
    time.sleep(1)
PY
exit 0"""
    post_exit_body = (
        "            real_run = subprocess.run\n"
        "            def broken_run(*args, **kwargs):\n"
        "                command = args[0] if args else kwargs.get('args', [])\n"
        "                if command[:2] == ['/bin/ps', '-axo']:\n"
        f"                    return subprocess.CompletedProcess(command, {ps_returncode}, b'garbled\\n', b'')\n"
        "                return real_run(*args, **kwargs)\n"
        "            subprocess.run = broken_run"
    )
    publisher, docker_config, _, _, _, command = _publisher_signal_fixture(
        tmp_path,
        inner_body=inner_body,
        post_leader_exit_body=post_exit_body,
        launcher_root_marker=launcher_marker,
        cleanup_root_marker=private_marker,
    )
    process = subprocess.Popen(
        command,
        cwd=publisher.parents[4],
        env={"DOCKER_CONFIG": str(docker_config), "HOME": str(tmp_path / "ambient-home"), "PATH": "/nonexistent"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    process_identity = _read_process_table()[process.pid]
    child_identity: ProcessIdentity | None = None
    child_survived = False
    stdout = stderr = ""
    try:
        deadline = time.monotonic() + 60
        while not child_marker.is_file() or not child_marker.read_text(encoding="ascii").strip():
            if process.poll() is not None:
                raise AssertionError("same-group child exited before process-table boundary")
            if time.monotonic() >= deadline:
                raise AssertionError("same-group child did not reach post-exit boundary")
            time.sleep(0.05)
        child_pid = int(child_marker.read_text(encoding="ascii").strip())
        child_identity = _read_process_table()[child_pid]
        stdout, stderr = process.communicate(timeout=15)
        child_survived = _read_process_table().get(child_pid) == child_identity
        assert not child_survived
        assert process.returncode == 2
        assert stdout == ""
        assert "Traceback" not in stderr
    finally:
        if child_identity is not None and _read_process_table().get(child_identity.pid) == child_identity:
            _kill_publisher_process_groups(
                _publisher_process_groups_for_fixture(child_identity)
            )
        if process.poll() is None:
            _kill_publisher_process_groups(
                _publisher_process_groups_for_fixture(process_identity)
            )
            if process.poll() is None and _read_process_table().get(process.pid) == process_identity:
                process.kill()
            process.wait(timeout=5)
        for marker, prefix in (
            (private_marker, "travel-map-publish-environment."),
            (launcher_marker, "travel-map-publish-launcher."),
        ):
            if marker.is_file() and marker.read_text(encoding="ascii").strip():
                _cleanup_recorded_root(marker, prefix)


@pytest.mark.parametrize("ps_fault", ("empty", "nonzero", "observer-only"))
def test_publisher_rejects_post_leader_unproven_quiescence(
    tmp_path: Path,
    ps_fault: str,
) -> None:
    child_marker = tmp_path / f"post-leader-{ps_fault}.child"
    fault_enable = tmp_path / f"post-leader-{ps_fault}.fault-enable"
    fault_ready = tmp_path / f"post-leader-{ps_fault}.fault-ready"
    fault_disable = tmp_path / f"post-leader-{ps_fault}.fault-disable"
    cleanup_probe = tmp_path / f"post-leader-{ps_fault}.cleanup"
    private_marker = tmp_path / f"post-leader-{ps_fault}.private-root"
    launcher_marker = tmp_path / f"post-leader-{ps_fault}.launcher-root"
    inner_body = f"""/usr/bin/python3 - <<'PY'
import os
import signal
import time
from pathlib import Path

pid = os.fork()
if pid:
    raise SystemExit(0)
os.dup2(os.open('/dev/null', os.O_WRONLY), 1)
os.dup2(os.open('/dev/null', os.O_WRONLY), 2)
Path({str(child_marker)!r}).write_text(str(os.getpid()), encoding='ascii')
signal.signal(signal.SIGTERM, signal.SIG_IGN)
signal.signal(signal.SIGHUP, signal.SIG_IGN)
signal.signal(signal.SIGINT, signal.SIG_IGN)
while True:
    time.sleep(1)
PY
exit 0"""
    if ps_fault == "observer-only":
        snapshot_result = (
            "                    payload = f'{os.getpid()} {os.getpgrp()}\\n'.encode('ascii')\n"
            "                    return subprocess.CompletedProcess(command, 0, payload, b'')\n"
        )
    else:
        snapshot_result = (
            "                    return subprocess.CompletedProcess(command, "
            f"{0 if ps_fault == 'empty' else 1}, b'', b'')\n"
        )
    post_exit_body = (
        "            real_run = subprocess.run\n"
        "            def broken_run(*args, **kwargs):\n"
        "                command = args[0] if args else kwargs.get('args', [])\n"
        "                if command[:2] == ['/bin/ps', '-axo']:\n"
        f"                    while not Path({str(fault_enable)!r}).is_file():\n"
        "                        time.sleep(0.01)\n"
        f"                    if Path({str(fault_disable)!r}).is_file():\n"
        "                        return real_run(*args, **kwargs)\n"
        f"                    Path({str(fault_ready)!r}).touch()\n"
        + snapshot_result
        + "                return real_run(*args, **kwargs)\n"
        "            subprocess.run = broken_run"
    )
    def transform(publisher_source: str) -> str:
        cleanup_anchor = "def cleanup_owned_root(path, expected, prefix):\n"
        assert publisher_source.count(cleanup_anchor) == 1
        cleanup_probe_code = (
            f"def cleanup_owned_root(path, expected, prefix):\n"
            f"    if not Path({str(cleanup_probe)!r}).exists() and Path({str(child_marker)!r}).is_file():\n"
            f"        child_pid = int(Path({str(child_marker)!r}).read_text(encoding='ascii'))\n"
            "        try:\n"
            "            os.kill(child_pid, 0)\n"
            "        except ProcessLookupError:\n"
            "            child_state = 'gone'\n"
            "        else:\n"
            "            child_state = 'alive'\n"
            f"        Path({str(cleanup_probe)!r}).write_text(child_state + '\\n', encoding='ascii')\n"
        )
        publisher_source = publisher_source.replace(cleanup_anchor, cleanup_probe_code, 1)
        stop_anchor = (
            "    protected: tuple[tuple[int, int, int, str], ...],\n"
            ") -> None:\n"
        )
        assert publisher_source.count(stop_anchor) == 1
        publisher_source = publisher_source.replace(
            stop_anchor, stop_anchor + "    return\n", 1
        )
        emergency_anchor = "def emergency_stop_publisher_group(group_id: int) -> bool:\n"
        assert publisher_source.count(emergency_anchor) == 1
        publisher_source = publisher_source.replace(
            emergency_anchor, emergency_anchor + "    return True\n", 1
        )
        table_anchor = (
            "handled_signals = {signal.SIGHUP, signal.SIGINT, signal.SIGTERM}\n"
            "signal.pthread_sigmask(signal.SIG_BLOCK, handled_signals)\n"
        )
        assert publisher_source.count(table_anchor) == 2
        top_signal_guard = (
            "top_real_killpg = os.killpg\n"
            "def top_hold_killpg(group, signum):\n"
            f"    if signum != 0 and not Path({str(fault_disable)!r}).is_file():\n"
            "        return None\n"
            "    return top_real_killpg(group, signum)\n"
            "os.killpg = top_hold_killpg\n\n"
        )
        return publisher_source.replace(
            table_anchor, table_anchor + top_signal_guard, 1
        )

    publisher, docker_config, _, _, _, command = _publisher_signal_fixture(
        tmp_path,
        inner_body=inner_body,
        post_leader_exit_body=post_exit_body,
        cleanup_root_marker=private_marker,
        launcher_root_marker=launcher_marker,
        publisher_source_transform=transform,
    )

    process = subprocess.Popen(
        command,
        cwd=publisher.parents[4],
        env={
            "DOCKER_CONFIG": str(docker_config),
            "HOME": str(tmp_path / "ambient-home"),
            "PATH": "/nonexistent",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    process_identity = _read_process_table()[process.pid]
    child_identity: ProcessIdentity | None = None
    private_root: Path | None = None
    launcher_root: Path | None = None
    root_identities: tuple[tuple[Path, tuple[int, int]], ...] = ()
    stdout = stderr = ""
    try:
        deadline = time.monotonic() + 90
        while (
            not child_marker.is_file()
            or not child_marker.read_text(encoding="ascii").strip()
            or not private_marker.is_file()
            or not private_marker.read_text(encoding="ascii").strip()
            or not launcher_marker.is_file()
            or not launcher_marker.read_text(encoding="ascii").strip()
        ):
            if process.poll() is not None:
                diagnostic_stdout, diagnostic_stderr = process.communicate()
                raise AssertionError(
                    "publisher exited before post-leader fault: "
                    f"child={child_marker.exists()} private={private_marker.exists()} "
                    f"launcher={launcher_marker.exists()} rc={process.returncode} "
                    f"stdout={diagnostic_stdout!r} stderr={diagnostic_stderr!r}"
                )
            if time.monotonic() >= deadline:
                raise AssertionError("post-leader fault fixture did not become ready")
            time.sleep(0.05)
        child_pid = int(child_marker.read_text(encoding="ascii").strip())
        child_identity = _read_process_table()[child_pid]
        private_root = Path(private_marker.read_text(encoding="ascii").strip())
        launcher_root = Path(launcher_marker.read_text(encoding="ascii").strip())
        root_identities = tuple(
            (path, (path.stat().st_dev, path.stat().st_ino))
            for path in (private_root, launcher_root)
        )
        fault_enable.touch()
        while not fault_ready.exists() and process.poll() is None:
            if time.monotonic() >= deadline:
                raise AssertionError("publisher did not reach post-leader fault")
            time.sleep(0.05)
        assert process.poll() is None, "publisher exited during post-leader fault"
        assert _read_process_table().get(process_identity.pid) == process_identity
        assert not cleanup_probe.exists(), "cleanup entered without quiescence proof"
        for path, expected in root_identities:
            details = path.stat()
            assert (details.st_dev, details.st_ino) == expected
        if child_identity is not None and _read_process_table().get(child_identity.pid) == child_identity:
            os.kill(child_identity.pid, signal.SIGKILL)
        fault_disable.touch()
        stdout, stderr = process.communicate(timeout=20)
        assert cleanup_probe.read_text(encoding="ascii") == "gone\n"
        assert process.returncode == 2
        assert stdout == ""
        assert "Traceback" not in stderr
        for path, expected in root_identities:
            assert not path.exists(), "production did not clean after recovery"
    finally:
        fault_disable.touch()
        if child_identity is not None and _read_process_table().get(child_identity.pid) == child_identity:
            os.kill(child_identity.pid, signal.SIGKILL)
        if process.poll() is None:
            _kill_publisher_process_groups(
                _publisher_process_groups_for_fixture(process_identity)
            )
            if process.poll() is None and _read_process_table().get(process.pid) == process_identity:
                process.kill()
            process.wait(timeout=5)
        for marker, prefix in (
            (private_marker, "travel-map-publish-environment."),
            (launcher_marker, "travel-map-publish-launcher."),
        ):
            if marker.is_file() and marker.read_text(encoding="ascii").strip():
                _cleanup_recorded_root(marker, prefix)


def test_publisher_top_owner_rejects_truncated_group_snapshot_before_cleanup(
    tmp_path: Path,
) -> None:
    """Observer plus leader is not proof that a live same-group descendant is gone."""
    child_marker = tmp_path / "truncated-top.child"
    fault_enable = tmp_path / "truncated-top.fault-enable"
    capture_ready = tmp_path / "truncated-top.capture-ready"
    snapshot_ready = tmp_path / "truncated-top.snapshot-ready"
    snapshot_release = tmp_path / "truncated-top.snapshot-release"
    fault_disable = tmp_path / "truncated-top.fault-disable"
    cleanup_probe = tmp_path / "truncated-top.cleanup"
    private_marker = tmp_path / "truncated-top.private-root"
    launcher_marker = tmp_path / "truncated-top.launcher-root"
    inner_body = f"""/usr/bin/python3 -I -S - <<'PY'
import os
import signal
import time
from pathlib import Path

child_marker = Path({str(child_marker)!r})
pid = os.fork()
if pid:
    raise SystemExit(0)
null = os.open('/dev/null', os.O_WRONLY)
os.dup2(null, 1)
os.dup2(null, 2)
if null > 2:
    os.close(null)
for handled in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
    signal.signal(handled, signal.SIG_IGN)
child_marker.write_text(str(os.getpid()), encoding='ascii')
while True:
    time.sleep(1)
PY
exit 0"""

    def transform(source: str) -> str:
        table_anchor = (
            "handled_signals = {signal.SIGHUP, signal.SIGINT, signal.SIGTERM}\n"
            "signal.pthread_sigmask(signal.SIG_BLOCK, handled_signals)\n"
        )
        assert source.count(table_anchor) == 2
        top_snapshot_injection = (
            "real_subprocess_run = subprocess.run\n"
            "truncated_snapshot_seen = False\n"
            "def run_truncated_top_snapshot(*args, **kwargs):\n"
            "    global truncated_snapshot_seen\n"
            "    command = args[0] if args else kwargs.get('args', [])\n"
            f"    if command[:3] == ['/bin/ps', '-axo', 'pid=,pgid='] and Path({str(child_marker)!r}).is_file():\n"
            f"        if not Path({str(capture_ready)!r}).exists():\n"
            f"            Path({str(capture_ready)!r}).write_text('ready\\n', encoding='ascii')\n"
            f"            while not Path({str(fault_enable)!r}).is_file():\n"
            "                time.sleep(0.01)\n"
            f"        if Path({str(fault_enable)!r}).is_file() and not Path({str(fault_disable)!r}).is_file():\n"
            f"            if not truncated_snapshot_seen:\n"
            "                truncated_snapshot_seen = True\n"
            "                payload = (\n"
            "                    f'{os.getpid()} {os.getpgrp()}\\n'\n"
            "                    f'{process.pid} {process.pid}\\n'\n"
            "                ).encode('ascii')\n"
            f"                Path({str(snapshot_ready)!r}).write_bytes(payload)\n"
            "                return subprocess.CompletedProcess(command, 0, payload, b'')\n"
            f"            while not Path({str(snapshot_release)!r}).is_file():\n"
            "                time.sleep(0.01)\n"
            "    return real_subprocess_run(*args, **kwargs)\n"
            "subprocess.run = run_truncated_top_snapshot\n\n"
        )
        source = source.replace(
            table_anchor,
            table_anchor + top_snapshot_injection,
            1,
        )

        stop_anchor = (
            "    protected: tuple[tuple[int, int, int, str], ...],\n"
            ") -> None:\n"
        )
        assert source.count(stop_anchor) == 1
        source = source.replace(
            stop_anchor,
            stop_anchor + "    return\n",
            1,
        )

        emergency_anchor = "def emergency_stop_publisher_group(group_id: int) -> bool:\n"
        assert source.count(emergency_anchor) == 1
        source = source.replace(
            emergency_anchor,
            emergency_anchor + "    return True\n",
            1,
        )

        cleanup_anchor = "def cleanup_owned_root(path, expected, prefix):\n"
        assert source.count(cleanup_anchor) == 1
        cleanup_probe_code = (
            "def cleanup_owned_root(path, expected, prefix):\n"
            f"    if not Path({str(cleanup_probe)!r}).exists() and Path({str(child_marker)!r}).is_file():\n"
            f"        child_pid = int(Path({str(child_marker)!r}).read_text(encoding='ascii'))\n"
            "        try:\n"
            "            os.kill(child_pid, 0)\n"
            "        except ProcessLookupError:\n"
            "            child_state = 'gone'\n"
            "        else:\n"
            "            child_state = 'alive'\n"
            f"        Path({str(cleanup_probe)!r}).write_text(child_state + '\\n', encoding='ascii')\n"
        )
        return source.replace(cleanup_anchor, cleanup_probe_code, 1)

    publisher, docker_config, _, _, _, command = _publisher_signal_fixture(
        tmp_path,
        inner_body=inner_body,
        cleanup_root_marker=private_marker,
        launcher_root_marker=launcher_marker,
        publisher_source_transform=transform,
    )
    process = subprocess.Popen(
        command,
        cwd=publisher.parents[4],
        env={
            "DOCKER_CONFIG": str(docker_config),
            "HOME": str(tmp_path / "ambient-home"),
            "PATH": "/nonexistent",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    process_identity = _read_process_table()[process.pid]
    child_identity: ProcessIdentity | None = None
    private_root: Path | None = None
    launcher_root: Path | None = None
    root_identities: tuple[tuple[Path, tuple[int, int]], ...] = ()
    owner_identity: ProcessIdentity | None = None
    owner_alive_before_release = False
    command_alive_before_release = False
    child_alive_before_release = False
    roots_alive_before_release = False
    cleanup_entered_before_release = False
    cleanup_state_before_release: str | None = None
    resources_removed_by_production_after_release = False
    release_boundary_error: AssertionError | None = None
    stdout = stderr = ""
    try:
        deadline = time.monotonic() + 90
        while (
            not child_marker.is_file()
            or not child_marker.read_text(encoding="ascii").strip()
            or not private_marker.is_file()
            or not private_marker.read_text(encoding="ascii").strip()
            or not launcher_marker.is_file()
            or not launcher_marker.read_text(encoding="ascii").strip()
        ):
            if process.poll() is not None:
                diagnostic_stdout, diagnostic_stderr = process.communicate()
                raise AssertionError(
                    "publisher exited before truncated top snapshot setup: "
                    f"child={child_marker.exists()} private={private_marker.exists()} "
                    f"launcher={launcher_marker.exists()} rc={process.returncode} "
                    f"stdout={diagnostic_stdout!r} stderr={diagnostic_stderr!r}"
                )
            if time.monotonic() >= deadline:
                raise AssertionError("truncated top snapshot fixture did not become ready")
            time.sleep(0.05)

        child_pid = int(child_marker.read_text(encoding="ascii").strip())
        private_root = Path(private_marker.read_text(encoding="ascii").strip())
        launcher_root = Path(launcher_marker.read_text(encoding="ascii").strip())
        root_identities = tuple(
            (path, (path.stat().st_dev, path.stat().st_ino))
            for path in (private_root, launcher_root)
        )
        assert private_root.parent in {Path("/tmp"), Path("/private/tmp")}
        assert private_root.name.startswith("travel-map-publish-environment.")
        assert launcher_root.parent in {Path("/tmp"), Path("/private/tmp")}
        assert launcher_root.name.startswith("travel-map-publish-launcher.")

        while not capture_ready.is_file():
            if process.poll() is not None:
                raise AssertionError("publisher exited before top snapshot capture boundary")
            if time.monotonic() >= deadline:
                raise AssertionError("top snapshot did not pause for identity capture")
            time.sleep(0.05)

        # The fault is enabled only after the child and both cleanup roots have
        # been identity-captured, and the child has reparented after leader exit.
        child_identity = None
        captured_processes: dict[int, ProcessIdentity] = {}
        child_lookup_deadline = time.monotonic() + 5
        while child_identity is None and time.monotonic() < child_lookup_deadline:
            captured_processes = _read_process_table()
            child_identity = captured_processes.get(child_pid)
            if child_identity is None:
                time.sleep(0.02)
        assert child_identity is not None
        leader_identity = next(
            (
                identity
                for identity in captured_processes.values()
                if identity.pid == identity.pgid == child_identity.pgid
            ),
            None,
        )
        assert leader_identity is not None
        fault_enable.touch()
        while not snapshot_ready.is_file():
            if process.poll() is not None:
                raise AssertionError("publisher exited before truncated top snapshot returned")
            if time.monotonic() >= deadline:
                raise AssertionError("truncated top snapshot did not return")
            time.sleep(0.05)

        rows = snapshot_ready.read_text(encoding="ascii").splitlines()
        assert len(rows) == 2
        owner_pid = int(rows[0].split()[0])
        assert rows[1].split() == [
            str(leader_identity.pid),
            str(leader_identity.pgid),
        ]
        owner_identity = _read_process_table().get(owner_pid)
        assert owner_identity is not None
        observation_deadline = time.monotonic() + 5
        while not cleanup_probe.exists() and process.poll() is None:
            if time.monotonic() >= observation_deadline:
                break
            time.sleep(0.05)
        if cleanup_probe.exists() and process.poll() is None:
            cleanup_exit_deadline = min(time.monotonic() + 0.25, observation_deadline)
            while process.poll() is None and time.monotonic() < cleanup_exit_deadline:
                time.sleep(0.05)
        boundary_processes = _read_process_table()
        owner_alive_before_release = (
            owner_identity is not None
            and boundary_processes.get(owner_identity.pid) == owner_identity
        )
        command_alive_before_release = process.poll() is None
        cleanup_entered_before_release = cleanup_probe.exists()
        cleanup_state_before_release = (
            cleanup_probe.read_text(encoding="ascii")
            if cleanup_entered_before_release
            else None
        )
        child_alive_before_release = (
            _read_process_table().get(child_identity.pid) == child_identity
        )
        roots_alive_before_release = all(
            path.exists()
            and (path.stat().st_dev, path.stat().st_ino) == expected
            for path, expected in root_identities
        )

        if child_alive_before_release:
            try:
                assert owner_alive_before_release, (
                    "top cleanup owner exited before exact-child release boundary"
                )
                assert command_alive_before_release, (
                    "publisher command exited before exact-child release boundary"
                )
            except AssertionError as error:
                release_boundary_error = error
        live_before_signal = _read_process_table()
        if live_before_signal.get(child_identity.pid) == child_identity:
            os.kill(child_identity.pid, signal.SIGKILL)
        fault_disable.touch()
        snapshot_release.touch()
        stdout, stderr = process.communicate(timeout=45)
        resources_removed_by_production_after_release = (
            all(
                not path.exists() and not path.is_symlink()
                for path in (private_root, launcher_root)
            )
            and (
                roots_alive_before_release
                or (
                    not child_alive_before_release
                    and cleanup_state_before_release == "gone\n"
                )
            )
        )
    finally:
        fault_disable.touch()
        snapshot_release.touch()
        if child_identity is not None:
            live = _read_process_table()
            if live.get(child_identity.pid) == child_identity:
                os.kill(child_identity.pid, signal.SIGKILL)
        if process.poll() is None:
            live_tree = _publisher_process_groups_for_fixture(process_identity)
            _kill_publisher_process_groups(live_tree)
            if process.poll() is None and _read_process_table().get(process.pid) == process_identity:
                process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        for path, expected in root_identities:
            _cleanup_exact_owned_root(path, expected, "travel-map-publish-environment." if path == private_root else "travel-map-publish-launcher.")
        if process.poll() is not None:
            try:
                stdout, stderr = process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()

    assert release_boundary_error is None, str(release_boundary_error)
    if child_alive_before_release:
        assert owner_alive_before_release, (
            "top cleanup owner exited while the exact child remained live"
        )
        assert command_alive_before_release, (
            "publisher command exited while the exact child remained live"
        )
        assert roots_alive_before_release
        assert cleanup_state_before_release != "alive\n", (
            "top cleanup entered while truncated snapshot omitted a live child"
        )
    else:
        assert cleanup_state_before_release == "gone\n", (
            "top cleanup did not prove the exact child gone before cleanup"
        )
    assert process.returncode == 2
    assert stdout == ""
    assert "Traceback" not in stderr
    assert resources_removed_by_production_after_release, (
        "top production cleanup did not remove both exact roots after release"
    )


def test_publisher_top_owner_does_not_signal_reused_group_after_observation_loss(
    tmp_path: Path,
) -> None:
    """A reaped leader must not authorize a later numeric-group signal."""
    child_marker = tmp_path / "top-owner-loss.child"
    top_owner_marker = tmp_path / "top-owner-loss.owner"
    fault_enable = tmp_path / "top-owner-loss.fault-enable"
    snapshot_ready = tmp_path / "top-owner-loss.snapshot-ready"
    generic_error = tmp_path / "top-owner-loss.generic-error"
    unsafe_signal = tmp_path / "top-owner-loss.unsafe-signal"
    signal_release = tmp_path / "top-owner-loss.signal-release"
    fault_disable = tmp_path / "top-owner-loss.fault-disable"
    private_marker = tmp_path / "top-owner-loss.private-root"
    launcher_marker = tmp_path / "top-owner-loss.launcher-root"
    inner_body = f"""/usr/bin/python3 -I -S - <<'PY'
import os
import signal
import time
from pathlib import Path

pid = os.fork()
if pid:
    raise SystemExit(0)
null = os.open('/dev/null', os.O_WRONLY)
os.dup2(null, 1)
os.dup2(null, 2)
if null > 2:
    os.close(null)
for handled in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
    signal.signal(handled, signal.SIG_IGN)
Path({str(child_marker)!r}).write_text(str(os.getpid()), encoding='ascii')
while True:
    time.sleep(1)
PY
exit 0"""

    def transform(source: str) -> str:
        table_anchor = (
            "handled_signals = {signal.SIGHUP, signal.SIGINT, signal.SIGTERM}\n"
            "signal.pthread_sigmask(signal.SIG_BLOCK, handled_signals)\n"
        )
        assert source.count(table_anchor) == 2
        top_injection = (
            f"Path({str(top_owner_marker)!r}).write_text(str(os.getpid()), encoding='ascii')\n"
            "top_real_killpg = os.killpg\n"
            "def top_record_killpg(group, signum):\n"
            "    if signum != 0:\n"
            f"        Path({str(unsafe_signal)!r}).write_text(f'{{group}} {{signum}}', encoding='ascii')\n"
            f"        while not Path({str(signal_release)!r}).exists():\n"
            "            time.sleep(0.01)\n"
            "        return None\n"
            "    return top_real_killpg(group, signum)\n"
            "os.killpg = top_record_killpg\n"
            "top_real_subprocess_run = subprocess.run\n"
            "top_snapshot_calls = 0\n"
            "def top_incomplete_snapshot(*args, **kwargs):\n"
            "    global top_snapshot_calls\n"
            "    command = args[0] if args else kwargs.get('args', [])\n"
            f"    if command[:3] == ['/bin/ps', '-axo', 'pid=,pgid='] and Path({str(child_marker)!r}).is_file() and not Path({str(fault_disable)!r}).is_file():\n"
            f"        while not Path({str(fault_enable)!r}).is_file():\n"
            "            time.sleep(0.01)\n"
            "        top_snapshot_calls += 1\n"
            "        if top_snapshot_calls == 1:\n"
            f"            Path({str(snapshot_ready)!r}).write_text('incomplete\\n', encoding='ascii')\n"
            "            payload = (\n"
            "                f'{os.getpid()} {os.getpgrp()}\\n'\n"
            "                f'{process.pid} {process.pid}\\n'\n"
            "            ).encode('ascii')\n"
            "            return subprocess.CompletedProcess(command, 0, payload, b'')\n"
            "        if top_snapshot_calls >= 2:\n"
            f"            Path({str(generic_error)!r}).touch()\n"
            "            return subprocess.CompletedProcess(command, 1, b'', b'')\n"
            "    return top_real_subprocess_run(*args, **kwargs)\n"
            "subprocess.run = top_incomplete_snapshot\n\n"
        )
        source = source.replace(table_anchor, table_anchor + top_injection, 1)
        stop_anchor = (
            "    protected: tuple[tuple[int, int, int, str], ...],\n"
            ") -> None:\n"
        )
        assert source.count(stop_anchor) == 1
        source = source.replace(stop_anchor, stop_anchor + "    return\n", 1)
        emergency_anchor = "def emergency_stop_publisher_group(group_id: int) -> bool:\n"
        assert source.count(emergency_anchor) == 1
        return source.replace(emergency_anchor, emergency_anchor + "    return True\n", 1)

    publisher, docker_config, _, _, _, command = _publisher_signal_fixture(
        tmp_path,
        inner_body=inner_body,
        cleanup_root_marker=private_marker,
        launcher_root_marker=launcher_marker,
        publisher_source_transform=transform,
    )
    process = subprocess.Popen(
        command,
        cwd=publisher.parents[4],
        env={
            "DOCKER_CONFIG": str(docker_config),
            "HOME": str(tmp_path / "ambient-home"),
            "PATH": "/nonexistent",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    process_identity = _read_process_table()[process.pid]
    child_identity: ProcessIdentity | None = None
    top_owner_identity: ProcessIdentity | None = None
    roots: tuple[tuple[Path, tuple[int, int]], ...] = ()
    owner_alive_before_release = False
    child_alive_before_release = False
    roots_alive_before_release = False
    signal_observed = False
    stdout = stderr = ""
    try:
        deadline = time.monotonic() + 90
        while (
            not child_marker.is_file()
            or not private_marker.is_file()
            or not launcher_marker.is_file()
            or not top_owner_marker.is_file()
        ):
            if process.poll() is not None:
                diagnostic_stdout, diagnostic_stderr = process.communicate()
                raise AssertionError(
                    "publisher exited before top owner-loss setup: "
                    f"rc={process.returncode} stdout={diagnostic_stdout!r} "
                    f"stderr={diagnostic_stderr!r}"
                )
            if time.monotonic() >= deadline:
                raise AssertionError("top owner-loss fixture did not become ready")
            time.sleep(0.05)
        child_pid = int(child_marker.read_text(encoding="ascii").strip())
        top_owner_pid = int(top_owner_marker.read_text(encoding="ascii").strip())
        captured = _read_process_table()
        child_identity = captured.get(child_pid)
        top_owner_identity = captured.get(top_owner_pid)
        assert child_identity is not None
        assert top_owner_identity is not None
        private_root = _read_recorded_root(
            private_marker, "travel-map-publish-environment."
        )
        launcher_root = _read_recorded_root(
            launcher_marker, "travel-map-publish-launcher."
        )
        roots = tuple(
            (path, (path.stat().st_dev, path.stat().st_ino))
            for path in (private_root, launcher_root)
        )
        fault_enable.touch()
        while not snapshot_ready.is_file():
            if process.poll() is not None:
                raise AssertionError("publisher exited before incomplete top snapshot")
            if time.monotonic() >= deadline:
                raise AssertionError("top owner-loss snapshot did not become ready")
            time.sleep(0.05)
        while not generic_error.is_file():
            if process.poll() is not None:
                raise AssertionError("publisher exited before generic top observation error")
            if time.monotonic() >= deadline:
                raise AssertionError("top owner-loss generic error did not become ready")
            time.sleep(0.05)
        time.sleep(1.0)
        owner_alive_before_release = (
            _read_process_table().get(top_owner_identity.pid) == top_owner_identity
        )
        child_alive_before_release = (
            _read_process_table().get(child_identity.pid) == child_identity
        )
        roots_alive_before_release = all(
            path.exists()
            and (path.stat().st_dev, path.stat().st_ino) == expected
            for path, expected in roots
        )
        signal_observed = unsafe_signal.is_file()
        if child_alive_before_release:
            os.kill(child_identity.pid, signal.SIGKILL)
        fault_disable.touch()
        signal_release.touch()
        stdout, stderr = process.communicate(timeout=45)
    finally:
        fault_disable.touch()
        signal_release.touch()
        if child_identity is not None:
            live = _read_process_table()
            if live.get(child_identity.pid) == child_identity:
                os.kill(child_identity.pid, signal.SIGKILL)
        if process.poll() is None:
            _kill_publisher_process_groups(
                _publisher_process_groups_for_fixture(process_identity)
            )
            if process.poll() is None and _read_process_table().get(process.pid) == process_identity:
                process.kill()
            process.wait(timeout=5)
        for path, expected in roots:
            _cleanup_exact_owned_root(
                path,
                expected,
                "travel-map-publish-environment."
                if path.name.startswith("travel-map-publish-environment.")
                else "travel-map-publish-launcher.",
            )
        if process.poll() is not None:
            try:
                stdout, stderr = process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()

    assert not signal_observed, "top owner signalled a group after leader identity loss"
    assert owner_alive_before_release
    assert child_alive_before_release
    assert roots_alive_before_release
    assert process.returncode == 2
    assert stdout == ""
    assert "Traceback" not in stderr
    assert all(not path.exists() and not path.is_symlink() for path, _ in roots)


@pytest.mark.parametrize("signal_kind", ("individual", "group"))
def test_publisher_signal_authority_rejects_ppid_only_identity_change(
    signal_kind: str,
) -> None:
    source = (ROOT / "deploy/nas/publish-reviewed-image.sh").read_text(
        encoding="utf-8"
    )
    start = source.index("def broker_stable_identity(")
    end = source.index("\n\ndef broker_process(", start)
    nonzero_signals: list[tuple[int, int]] = []
    fake_os = SimpleNamespace(
        getpgrp=lambda: 9000,
        kill=lambda pid, signum: nonzero_signals.append((pid, signum)),
        killpg=lambda group, signum: (
            nonzero_signals.append((group, signum)) if signum != 0 else None
        ),
    )
    namespace = {
        "os": fake_os,
        "re": __import__("re"),
        "select": __import__("select"),
        "signal": signal,
        "subprocess": subprocess,
        "sys": sys,
        "time": time,
    }
    exec(compile(source[start:end], "publish-reviewed-image.sh", "exec"), namespace)

    expected = (41001, 40001, 41001, "Thu Sep  4 15:00:00 2026")
    ppid_changed = (expected[0], 40002, expected[2], expected[3])
    namespace["broker_process_table"] = lambda: {expected[0]: ppid_changed}

    if signal_kind == "individual":
        namespace["signal_broker_tree"]((expected,), signal.SIGTERM)
    else:
        observations = iter((False, True))
        namespace["wait_for_publisher_group_observation"] = lambda group: next(
            observations
        )
        namespace["publisher_group_exists"] = lambda group: True
        assert namespace["_emergency_stop_publisher_group"](
            expected[0], expected
        )

    assert nonzero_signals == []


@pytest.mark.parametrize("signum", (signal.SIGTERM, signal.SIGKILL))
def test_publisher_broker_tree_never_renews_invalidated_numeric_pid(
    signum: signal.Signals,
) -> None:
    source = (ROOT / "deploy/nas/publish-reviewed-image.sh").read_text(
        encoding="utf-8"
    )
    start = source.index("def broker_stable_identity(")
    end = source.index("\n\ndef stop_broker_shell(", start)
    signals: list[tuple[int, int]] = []
    namespace = {
        "os": SimpleNamespace(kill=lambda pid, sent: signals.append((pid, sent))),
        "re": __import__("re"),
        "select": __import__("select"),
        "signal": signal,
        "subprocess": subprocess,
        "sys": sys,
        "time": time,
    }
    exec(compile(source[start:end], "publish-reviewed-image.sh", "exec"), namespace)

    shell = (42000, 41000, 42000, "Thu Sep  4 16:00:00 2026")
    captured = (42001, shell[0], shell[2], "Thu Sep  4 16:00:01 2026")
    replacement = (captured[0], 41999, captured[2], captured[3])
    snapshots = iter(
        (
            {replacement[0]: replacement},
            {replacement[0]: replacement},
            {replacement[0]: replacement},
        )
    )
    namespace["broker_process_table"] = lambda: next(snapshots)

    extended = namespace["extend_broker_owned_tree"](shell, (captured,), ())
    live = namespace["live_broker_identities"](extended)
    namespace["signal_broker_tree"](live, signum)

    assert signals == []


@pytest.mark.parametrize("signum", (signal.SIGTERM, signal.SIGKILL))
def test_publisher_broker_tree_never_renews_invalidated_shell_from_empty_capture(
    signum: signal.Signals,
) -> None:
    source = (ROOT / "deploy/nas/publish-reviewed-image.sh").read_text(
        encoding="utf-8"
    )
    start = source.index("def broker_stable_identity(")
    end = source.index("\n\ndef stop_broker_shell(", start)
    signals: list[tuple[int, int]] = []
    namespace = {
        "os": SimpleNamespace(kill=lambda pid, sent: signals.append((pid, sent))),
        "re": __import__("re"),
        "select": __import__("select"),
        "signal": signal,
        "subprocess": subprocess,
        "sys": sys,
        "time": time,
    }
    exec(compile(source[start:end], "publish-reviewed-image.sh", "exec"), namespace)

    shell = (43000, 42000, 43000, "Thu Sep  4 17:00:00 2026")
    replacement = (shell[0], 41999, shell[2], shell[3])
    snapshots = iter(
        (
            {replacement[0]: replacement},
            {replacement[0]: replacement},
            {replacement[0]: replacement},
        )
    )
    namespace["broker_process_table"] = lambda: next(snapshots)

    extended = namespace["extend_broker_owned_tree"](shell, (), ())
    live = namespace["live_broker_identities"](extended)
    namespace["signal_broker_tree"](live, signum)

    assert signals == []


@pytest.mark.parametrize(
    "observation_fault",
    (
        "publisher-observer-only",
        "broker-table-partial",
        "publisher-zombie",
        "publisher-nonzero",
        "publisher-malformed",
    ),
)
def test_publisher_broker_owner_rejects_truncated_publisher_snapshot_before_cleanup(
    tmp_path: Path,
    observation_fault: str,
) -> None:
    """Incomplete broker observations cannot authorize resource cleanup."""
    child_marker = tmp_path / "truncated-broker.child"
    zombie_marker = tmp_path / "truncated-broker.zombie"
    fault_enable = tmp_path / "truncated-broker.fault-enable"
    capture_ready = tmp_path / "truncated-broker.capture-ready"
    snapshot_ready = tmp_path / "truncated-broker.snapshot-ready"
    snapshot_release = tmp_path / "truncated-broker.snapshot-release"
    fault_disable = tmp_path / "truncated-broker.fault-disable"
    shell_capture_ready = tmp_path / "truncated-broker.shell-capture-ready"
    cleanup_probe = tmp_path / "truncated-broker.cleanup"
    resource_marker = tmp_path / "truncated-broker.resources"
    boundary_marker = tmp_path / "truncated-broker.boundary"
    snapshot_payload = tmp_path / "truncated-broker.snapshot-payload"
    inner_body = f"""/usr/bin/python3 -I -S - <<'PY'
import os
import signal
import time
from pathlib import Path

child_marker = Path({str(child_marker)!r})
pid = os.fork()
if pid == 0:
    null = os.open('/dev/null', os.O_WRONLY)
    os.dup2(null, 1)
    os.dup2(null, 2)
    if null > 2:
        os.close(null)
    for handled in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        signal.signal(handled, signal.SIG_IGN)
    child_marker.write_text(str(os.getpid()), encoding='ascii')
    while True:
        time.sleep(1)
PY
exit 2"""

    def transform(source: str) -> str:
        arm_anchor = (
            "    (\n"
            "        broker_record_root,\n"
            "        broker_record_identity,\n"
            "        broker_lock_path,\n"
            "        broker_lock_identity,\n"
            "        broker_lock_parent_identity,\n"
            "        broker_lock_parent_created,\n"
            "    ) = arm_resource_broker()\n"
        )
        assert source.count(arm_anchor) == 1
        resource_probe = (
            arm_anchor
            + f"    Path({str(resource_marker)!r}).write_text(\n"
            "        f'{broker_record_root}\\n{broker_record_identity}\\n'\n"
            "        f'{broker_lock_path}\\n{broker_lock_identity}\\n{broker_pid}\\n',\n"
            "        encoding='ascii',\n"
            "    )\n"
        )
        source = source.replace(arm_anchor, resource_probe, 1)
        stop_anchor = (
            "def stop_broker_shell(\n"
            "    shell_identity: tuple[int, int, int, str],\n"
            "    observer: BrokerExitObserver,\n"
            "    protected: tuple[tuple[int, int, int, str], ...],\n"
            ") -> None:\n"
        )
        assert source.count(stop_anchor) == 1
        if observation_fault != "broker-table-partial":
            stop_injection = (
                stop_anchor
                + "    raise OSError('injected broker snapshot boundary')\n"
            )
            source = source.replace(stop_anchor, stop_injection, 1)

        if observation_fault == "publisher-zombie":
            publisher_group_anchor = (
                "    lock_parent_created = precreated_lock_parent_created\n"
                "    tag_armed = False\n"
                "    publisher_group = os.getpgrp()\n"
            )
            assert source.count(publisher_group_anchor) == 1
            zombie_injection = (
                publisher_group_anchor
                + "    zombie_pid = os.fork()\n"
                + "    if zombie_pid == 0:\n"
                + "        os._exit(0)\n"
                + f"    Path({str(zombie_marker)!r}).write_text("
                + "f'{zombie_pid} {publisher_group}\\n', encoding='ascii')\n"
            )
            source = source.replace(publisher_group_anchor, zombie_injection, 1)

        if observation_fault == "broker-table-partial":
            table_anchor = "\ndef broker_stable_identity("
            assert source.count(table_anchor) == 1
            table_injection = (
                "\nreal_broker_process_table = broker_process_table\n"
                "def broker_process_table():\n"
                "    records = real_broker_process_table()\n"
                f"    if Path({str(child_marker)!r}).is_file() and not Path({str(fault_disable)!r}).exists():\n"
                f"        if not Path({str(shell_capture_ready)!r}).exists():\n"
                f"            Path({str(shell_capture_ready)!r}).write_text('ready\\n', encoding='ascii')\n"
                "            return records\n"
                f"        if not Path({str(capture_ready)!r}).exists():\n"
                f"            Path({str(capture_ready)!r}).write_text('ready\\n', encoding='ascii')\n"
                f"            while not Path({str(fault_enable)!r}).is_file():\n"
                "                time.sleep(0.01)\n"
                f"        if Path({str(fault_enable)!r}).is_file() and not Path({str(fault_disable)!r}).exists():\n"
                "            observer = records.get(os.getpid())\n"
                "            if observer is None:\n"
                "                raise OSError\n"
                f"            if not Path({str(snapshot_ready)!r}).exists():\n"
                f"                Path({str(snapshot_ready)!r}).write_text('observer-only\\n', encoding='ascii')\n"
                "            return {os.getpid(): observer}\n"
                "    return records\n"
            )
            source = source.replace(table_anchor, table_injection + table_anchor, 1)
        if observation_fault != "broker-table-partial":
            table_anchor = (
                "handled_signals = {signal.SIGHUP, signal.SIGINT, signal.SIGTERM}\n"
                "signal.pthread_sigmask(signal.SIG_BLOCK, handled_signals)\n"
            )
            assert source.count(table_anchor) == 2
            if observation_fault == "publisher-observer-only":
                snapshot_body = (
                    "            payload = f'{os.getpid()} {os.getpgrp()} S\\n'.encode('ascii')\n"
                )
            elif observation_fault == "publisher-zombie":
                snapshot_body = (
                    f"            zombie_pid, publisher_group = (int(value) for value in Path({str(zombie_marker)!r}).read_text(encoding='ascii').split())\n"
                    "            completed = real_subprocess_run(*args, **kwargs)\n"
                    "            kept = []\n"
                    "            for raw_line in completed.stdout.splitlines():\n"
                    "                fields = raw_line.split()\n"
                    "                if len(fields) == 3 and int(fields[1]) == publisher_group and int(fields[0]) != zombie_pid:\n"
                    "                    continue\n"
                    "                kept.append(raw_line)\n"
                    "            payload = b'\\n'.join(kept) + b'\\n'\n"
                )
            elif observation_fault == "publisher-nonzero":
                snapshot_body = (
                    "            return subprocess.CompletedProcess(command, 1, b'', b'')\n"
                )
            elif observation_fault == "publisher-malformed":
                snapshot_body = (
                    "            return subprocess.CompletedProcess(command, 0, b'malformed\\n', b'')\n"
                )
            else:
                raise AssertionError(f"unknown observation fault: {observation_fault}")
            broker_snapshot_injection = (
                "real_subprocess_run = subprocess.run\n"
                "def run_incomplete_broker_snapshot(*args, **kwargs):\n"
                "    command = args[0] if args else kwargs.get('args', [])\n"
                f"    if command[:3] == ['/bin/ps', '-axo', 'pid=,pgid=,stat='] and Path({str(child_marker)!r}).is_file():\n"
                f"        if not Path({str(capture_ready)!r}).exists():\n"
                f"            Path({str(capture_ready)!r}).write_text('ready\\n', encoding='ascii')\n"
                f"            while not Path({str(fault_enable)!r}).is_file():\n"
                "                time.sleep(0.01)\n"
                f"        if Path({str(fault_enable)!r}).is_file() and not Path({str(fault_disable)!r}).is_file():\n"
                f"            if not Path({str(snapshot_ready)!r}).exists():\n"
                f"                Path({str(snapshot_ready)!r}).write_bytes(b'fault\\n')\n"
                + snapshot_body
                + f"            if 'payload' in locals() and not Path({str(snapshot_payload)!r}).exists():\n"
                f"                Path({str(snapshot_payload)!r}).write_bytes(payload)\n"
                + "            return subprocess.CompletedProcess(command, 0, payload, b'')\n"
                "    return real_subprocess_run(*args, **kwargs)\n"
                "subprocess.run = run_incomplete_broker_snapshot\n\n"
            )
            source = (
                source[: source.rfind(table_anchor)]
                + broker_snapshot_injection
                + source[source.rfind(table_anchor) :]
            )
            if observation_fault in {"publisher-nonzero", "publisher-malformed"}:
                fallback_snapshot_body = (
                    "            return subprocess.CompletedProcess(command, 1, b'', b'')\n"
                    if observation_fault == "publisher-nonzero"
                    else "            return subprocess.CompletedProcess(command, 0, b'malformed\\n', b'')\n"
                )
                fallback_snapshot_injection = (
                    "\nreal_subprocess_run = subprocess.run\n"
                    "def run_persistent_incomplete_snapshot(*args, **kwargs):\n"
                    "    command = args[0] if args else kwargs.get('args', [])\n"
                    f"    if command[:3] == ['/bin/ps', '-axo', 'pid=,pgid=,stat='] and Path({str(child_marker)!r}).is_file() and Path({str(fault_enable)!r}).is_file() and not Path({str(fault_disable)!r}).exists():\n"
                    + fallback_snapshot_body
                    + "    return real_subprocess_run(*args, **kwargs)\n"
                    "subprocess.run = run_persistent_incomplete_snapshot\n\n"
                )
                source = source.replace(
                    table_anchor,
                    fallback_snapshot_injection + table_anchor,
                    1,
                )

        cleanup_anchor = (
            ") -> bool:\n"
            "    if path.parent != parent or not path.name.startswith(prefix):\n"
        )
        assert source.count(cleanup_anchor) == 1
        cleanup_probe_code = (
            ") -> bool:\n"
            f"    if prefix == 'travel-map-publish.' and not Path({str(cleanup_probe)!r}).exists() and Path({str(child_marker)!r}).is_file():\n"
            f"        child_pid = int(Path({str(child_marker)!r}).read_text(encoding='ascii'))\n"
            "        try:\n"
            "            os.kill(child_pid, 0)\n"
            "        except ProcessLookupError:\n"
            "            child_state = 'gone'\n"
            "        else:\n"
            "            child_state = 'alive'\n"
            f"        Path({str(cleanup_probe)!r}).write_text(child_state + '\\n', encoding='ascii')\n"
            "    if path.parent != parent or not path.name.startswith(prefix):\n"
        )
        return source.replace(cleanup_anchor, cleanup_probe_code, 1)

    post_leader_exit_body = None
    if observation_fault == "broker-table-partial":
        post_leader_exit_body = (
            "            real_supervisor_run = subprocess.run\n"
            "            def run_partial_supervisor_snapshot(*args, **kwargs):\n"
            "                command = args[0] if args else kwargs.get('args', [])\n"
            "                if command[:3] == ['/bin/ps', '-axo', 'pid=,pgid=']:\n"
            "                    payload = (\n"
            "                        f'{os.getpid()} {os.getpgrp()}\\n'\n"
            "                        f'{process.pid} {process.pid}\\n'\n"
            "                    ).encode('ascii')\n"
            "                    return subprocess.CompletedProcess(command, 0, payload, b'')\n"
            "                return real_supervisor_run(*args, **kwargs)\n"
            "            subprocess.run = run_partial_supervisor_snapshot"
        )

    publisher, docker_config, _, _, _, command = _publisher_signal_fixture(
        tmp_path,
        inner_body=inner_body,
        post_leader_exit_body=post_leader_exit_body,
        publisher_source_transform=transform,
    )
    process = subprocess.Popen(
        command,
        cwd=publisher.parents[4],
        env={
            "DOCKER_CONFIG": str(docker_config),
            "HOME": str(tmp_path / "ambient-home"),
            "PATH": "/nonexistent",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    process_identity = _read_process_table()[process.pid]
    broker_identity: ProcessIdentity | None = None
    child_identity: ProcessIdentity | None = None
    zombie_identity: ProcessIdentity | None = None
    record_path: Path | None = None
    record_identity: tuple[int, int] | None = None
    lock_path: Path | None = None
    lock_identity: tuple[int, int] | None = None
    owner_identity: ProcessIdentity | None = None
    owner_alive_before_release = False
    command_alive_before_release = False
    child_alive_before_release = False
    resources_alive_before_release = False
    cleanup_entered_before_release = False
    cleanup_state_before_release: str | None = None
    resources_removed_by_production_after_release = False
    release_boundary_error: AssertionError | None = None
    stdout = stderr = ""
    try:
        deadline = time.monotonic() + 90
        while (
            not child_marker.is_file()
            or not resource_marker.is_file()
            or (
                observation_fault == "publisher-zombie"
                and not zombie_marker.is_file()
            )
        ):
            if process.poll() is not None:
                diagnostic_stdout, diagnostic_stderr = process.communicate()
                raise AssertionError(
                    "publisher exited before truncated broker snapshot setup: "
                    f"child={child_marker.exists()} resources={resource_marker.exists()} "
                    f"rc={process.returncode} stdout={diagnostic_stdout!r} "
                    f"stderr={diagnostic_stderr!r}"
                )
            if time.monotonic() >= deadline:
                raise AssertionError("truncated broker snapshot fixture did not become ready")
            time.sleep(0.05)

        fields = resource_marker.read_text(encoding="ascii").splitlines()
        assert len(fields) == 5
        record_path = Path(fields[0])
        record_identity = tuple(int(part) for part in fields[1].split(":"))
        lock_path = Path(fields[2])
        lock_identity = tuple(int(part) for part in fields[3].split(":"))
        broker_pid = int(fields[4])
        assert record_path.parent in {Path("/tmp"), Path("/private/tmp")}
        assert record_path.name.startswith("travel-map-publish.")
        assert lock_path.parent == Path(f"/tmp/travel-map-publish-locks-{os.getuid()}")
        record_details = record_path.lstat()
        lock_details = lock_path.lstat()
        assert (record_details.st_dev, record_details.st_ino) == record_identity
        assert (lock_details.st_dev, lock_details.st_ino) == lock_identity
        assert stat.S_ISDIR(record_details.st_mode)
        assert stat.S_IMODE(record_details.st_mode) == 0o700
        assert record_details.st_uid == os.getuid()
        assert stat.S_ISDIR(lock_details.st_mode)
        assert stat.S_IMODE(lock_details.st_mode) == 0o700
        assert lock_details.st_uid == os.getuid()
        child_pid = int(child_marker.read_text(encoding="ascii").strip())
        zombie_pid: int | None = None
        if observation_fault == "publisher-zombie":
            zombie_fields = zombie_marker.read_text(encoding="ascii").split()
            assert len(zombie_fields) == 2
            zombie_pid, zombie_group = (int(value) for value in zombie_fields)
            assert zombie_pid > 0 and zombie_group > 1
            zombie_lookup_deadline = time.monotonic() + 5
            while zombie_identity is None and time.monotonic() < zombie_lookup_deadline:
                live_processes = _read_process_table()
                candidate = live_processes.get(zombie_pid)
                if candidate is not None:
                    state_result = subprocess.run(
                        ["/bin/ps", "-o", "pid=,state=", "-p", str(zombie_pid)],
                        check=True,
                        capture_output=True,
                        text=True,
                    )
                    state_fields = state_result.stdout.split()
                    if (
                        len(state_fields) == 2
                        and state_fields[0] == str(zombie_pid)
                        and state_fields[1].startswith("Z")
                    ):
                        zombie_identity = candidate
                        break
                time.sleep(0.02)
            assert zombie_identity is not None, "publisher zombie did not become observable"
            assert zombie_identity.pgid == zombie_group

        while not capture_ready.is_file():
            if process.poll() is not None:
                raise AssertionError("publisher exited before broker snapshot capture boundary")
            if time.monotonic() >= deadline:
                raise AssertionError("broker snapshot did not pause for identity capture")
            time.sleep(0.05)

        # The fault is enabled only after the broker resources and process
        # identities are captured, and the child has reparented after leader exit.
        child_lookup_deadline = time.monotonic() + 5
        while (
            (child_identity is None or broker_identity is None)
            and time.monotonic() < child_lookup_deadline
        ):
            live_processes = _read_process_table()
            child_identity = live_processes.get(child_pid)
            broker_identity = live_processes.get(broker_pid)
            if child_identity is None or broker_identity is None:
                time.sleep(0.02)
        assert child_identity is not None
        assert broker_identity is not None
        fault_enable.touch()
        while not snapshot_ready.is_file():
            if process.poll() is not None:
                raise AssertionError("publisher exited before truncated broker snapshot returned")
            if time.monotonic() >= deadline:
                raise AssertionError("truncated broker snapshot did not return")
            time.sleep(0.05)
        if observation_fault in {"publisher-observer-only", "publisher-zombie"}:
            while not snapshot_payload.is_file():
                if process.poll() is not None:
                    raise AssertionError(
                        "publisher exited before truncated broker snapshot payload"
                    )
                if time.monotonic() >= deadline:
                    raise AssertionError(
                        "truncated broker snapshot payload did not return"
                    )
                time.sleep(0.01)

        rows = snapshot_ready.read_text(encoding="ascii").splitlines()
        if observation_fault == "broker-table-partial":
            assert rows == ["observer-only"]
        elif observation_fault == "publisher-observer-only":
            assert rows == ["fault"]
            snapshot_rows = snapshot_payload.read_text(encoding="ascii").splitlines()
            assert len(snapshot_rows) == 1
            assert snapshot_rows[0].split()[:2] == [str(broker_pid), str(broker_pid)]
        elif observation_fault == "publisher-zombie":
            assert rows == ["fault"]
            snapshot_rows = []
            for raw_line in snapshot_payload.read_text(encoding="ascii").splitlines():
                fields = raw_line.split()
                if len(fields) == 3:
                    snapshot_rows.append(fields)
            assert any(
                fields[0] == str(broker_pid)
                and fields[1] == str(broker_pid)
                for fields in snapshot_rows
            )
            assert any(
                fields[0] == str(zombie_pid)
                and fields[1] == str(zombie_group)
                and fields[2].startswith("Z")
                for fields in snapshot_rows
            )
            assert not any(fields[0] == str(child_pid) for fields in snapshot_rows)
        else:
            assert rows == ["fault"]
        owner_identity = _read_process_table().get(broker_identity.pid)
        if owner_identity is None:
            release_boundary_error = AssertionError(
                "broker cleanup owner exited before exact-child release boundary"
            )
        observation_deadline = time.monotonic() + 5
        while not cleanup_probe.exists() and process.poll() is None:
            if time.monotonic() >= observation_deadline:
                break
            time.sleep(0.05)
        if cleanup_probe.exists() and process.poll() is None:
            cleanup_exit_deadline = min(time.monotonic() + 0.25, observation_deadline)
            while process.poll() is None and time.monotonic() < cleanup_exit_deadline:
                time.sleep(0.05)
        boundary_processes = _read_process_table()
        owner_alive_before_release = (
            owner_identity is not None
            and boundary_processes.get(owner_identity.pid) == owner_identity
        )
        command_alive_before_release = process.poll() is None
        cleanup_entered_before_release = cleanup_probe.exists()
        child_alive_before_release = (
            _read_process_table().get(child_identity.pid) == child_identity
        )
        resources_alive_before_release = all(
            path.exists()
            and (path.stat().st_dev, path.stat().st_ino) == expected
            for path, expected in (
                (record_path, record_identity),
                (lock_path, lock_identity),
            )
        )

        if child_alive_before_release:
            try:
                assert owner_alive_before_release, (
                    "broker cleanup owner exited before exact-child release boundary"
                )
                assert command_alive_before_release, (
                    "publisher command exited before exact-child release boundary"
                )
            except AssertionError as error:
                release_boundary_error = error
        cleanup_state_before_release = (
            cleanup_probe.read_text(encoding="ascii")
            if cleanup_entered_before_release
            else None
        )
        live_before_signal = _read_process_table()
        if live_before_signal.get(child_identity.pid) == child_identity:
            os.kill(child_identity.pid, signal.SIGKILL)
        fault_disable.touch()
        snapshot_release.touch()
        stdout, stderr = process.communicate(timeout=45)
        resources_removed_by_production_after_release = (
            all(
                not path.exists() and not path.is_symlink()
                for path in (record_path, lock_path)
            )
            and (
                resources_alive_before_release
                or (
                    not child_alive_before_release
                    and cleanup_state_before_release == "gone\n"
                )
            )
        )
        boundary_marker.write_text(
            json.dumps(
                {
                    "owner_alive_before_release": owner_alive_before_release,
                    "command_alive_before_release": command_alive_before_release,
                    "child_alive_before_release": child_alive_before_release,
                    "resources_alive_before_release": resources_alive_before_release,
                    "cleanup_entered_before_release": cleanup_entered_before_release,
                    "cleanup_state_before_release": cleanup_state_before_release,
                    "resources_removed_by_production_after_release": resources_removed_by_production_after_release,
                    "release_boundary_error": release_boundary_error is not None,
                },
                separators=(",", ":"),
            )
            + "\n",
            encoding="ascii",
        )
    finally:
        fault_disable.touch()
        snapshot_release.touch()
        if child_identity is not None:
            live = _read_process_table()
            if live.get(child_identity.pid) == child_identity:
                os.kill(child_identity.pid, signal.SIGKILL)
        if broker_identity is not None:
            live = _read_process_table()
            if live.get(broker_identity.pid) == broker_identity:
                os.kill(broker_identity.pid, signal.SIGKILL)
        if process.poll() is None:
            _kill_publisher_process_groups(
                _publisher_process_groups_for_fixture(process_identity)
            )
            if process.poll() is None and _read_process_table().get(process.pid) == process_identity:
                process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        if record_path is not None and record_identity is not None:
            _cleanup_exact_owned_root(
                record_path,
                record_identity,
                "travel-map-publish.",
            )
        if lock_path is not None and lock_identity is not None:
            try:
                details = lock_path.lstat()
            except FileNotFoundError:
                pass
            else:
                if (
                    (details.st_dev, details.st_ino) == lock_identity
                    and stat.S_ISDIR(details.st_mode)
                    and stat.S_IMODE(details.st_mode) == 0o700
                    and details.st_uid == os.getuid()
                    and not lock_path.is_symlink()
                    and not any(lock_path.iterdir())
                ):
                    lock_path.rmdir()
        if process.poll() is not None:
            try:
                stdout, stderr = process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()

    boundary = json.loads(boundary_marker.read_text(encoding="ascii"))
    assert not boundary["release_boundary_error"]
    if boundary["child_alive_before_release"]:
        assert boundary["owner_alive_before_release"]
        assert boundary["command_alive_before_release"]
        assert boundary["resources_alive_before_release"]
        assert boundary["cleanup_state_before_release"] != "alive\n", (
            "broker cleanup entered while truncated snapshot omitted a live publisher member"
        )
    else:
        assert boundary["cleanup_state_before_release"] == "gone\n", (
            "broker cleanup did not prove the exact child gone before cleanup"
        )
    assert process.returncode == 2
    assert stdout == ""
    assert "Traceback" not in stderr
    assert boundary["resources_removed_by_production_after_release"], (
        "broker production cleanup did not remove the exact record and lock roots after release"
    )


@pytest.mark.parametrize("owner_path", ("broker", "fallback"))
def test_publisher_persistent_incomplete_observation_keeps_owner_and_resources(
    tmp_path: Path,
    owner_path: str,
) -> None:
    """Uncertain broker/fallback cleanup must not signal or delete by number alone."""
    child_marker = tmp_path / f"persistent-{owner_path}.child"
    resource_marker = tmp_path / f"persistent-{owner_path}.resources"
    fault_enable = tmp_path / f"persistent-{owner_path}.fault-enable"
    observation_ready = tmp_path / f"persistent-{owner_path}.observation-ready"
    unsafe_signal = tmp_path / f"persistent-{owner_path}.unsafe-signal"
    signal_release = tmp_path / f"persistent-{owner_path}.signal-release"
    fault_disable = tmp_path / f"persistent-{owner_path}.fault-disable"
    private_marker = tmp_path / f"persistent-{owner_path}.private-root"
    launcher_marker = tmp_path / f"persistent-{owner_path}.launcher-root"
    inner_body = f"""/usr/bin/python3 -I -S - <<'PY'
import os
import signal
import time
from pathlib import Path

pid = os.fork()
if pid == 0:
    null = os.open('/dev/null', os.O_WRONLY)
    os.dup2(null, 1)
    os.dup2(null, 2)
    if null > 2:
        os.close(null)
    for handled in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        signal.signal(handled, signal.SIG_IGN)
    Path({str(child_marker)!r}).write_text(str(os.getpid()), encoding='ascii')
    while True:
        time.sleep(1)
PY
exit 2"""

    def transform(source: str) -> str:
        arm_anchor = (
            "    (\n"
            "        broker_record_root,\n"
            "        broker_record_identity,\n"
            "        broker_lock_path,\n"
            "        broker_lock_identity,\n"
            "        broker_lock_parent_identity,\n"
            "        broker_lock_parent_created,\n"
            "    ) = arm_resource_broker()\n"
        )
        assert source.count(arm_anchor) == 1
        resource_probe = (
            arm_anchor
            + f"    Path({str(resource_marker)!r}).write_text(\n"
            "        f'{broker_record_root}\\n{broker_record_identity}\\n'\n"
            "        f'{broker_lock_path}\\n{broker_lock_identity}\\n{broker_pid}\\n'\n"
            "        f'{private_root}\\n{launcher_root}\\n',\n"
            "        encoding='ascii',\n"
            "    )\n"
        )
        source = source.replace(arm_anchor, resource_probe, 1)

        table_anchor = (
            "handled_signals = {signal.SIGHUP, signal.SIGINT, signal.SIGTERM}\n"
            "signal.pthread_sigmask(signal.SIG_BLOCK, handled_signals)\n"
        )
        assert source.count(table_anchor) == 2
        outer_injection = (
            "persistent_real_killpg = os.killpg\n"
            "def persistent_record_killpg(group, signum):\n"
            "    if signum != 0:\n"
            f"        Path({str(unsafe_signal)!r}).write_text(f'{{group}} {{signum}}', encoding='ascii')\n"
            f"        while not Path({str(signal_release)!r}).exists():\n"
            "            time.sleep(0.01)\n"
            "        return None\n"
            "    return persistent_real_killpg(group, signum)\n"
            "os.killpg = persistent_record_killpg\n\n"
        )
        table_position = source.rfind(table_anchor)
        assert table_position >= 0
        source = (
            source[:table_position]
            + table_anchor
            + outer_injection
            + source[table_position + len(table_anchor) :]
        )

        quiescent_anchor = "def publisher_group_quiescent(group_id: int) -> bool:\n"
        assert source.count(quiescent_anchor) == 1
        quiescent_injection = (
            f"    while not Path({str(fault_enable)!r}).exists():\n"
            "        time.sleep(0.01)\n"
            f"    if not Path({str(fault_disable)!r}).exists():\n"
            f"        if not Path({str(observation_ready)!r}).exists():\n"
            f"            Path({str(observation_ready)!r}).write_text(f'{{os.getpid()}} {{group_id}}', encoding='ascii')\n"
            "        raise IncompletePublisherGroupSnapshot(\n"
            "            'persistent fixture observation is incomplete'\n"
            "        )\n"
            "    return True\n"
        )
        source = source.replace(
            quiescent_anchor,
            quiescent_anchor + quiescent_injection,
            1,
        )

        if owner_path == "broker":
            stop_anchor = (
                "def stop_broker_shell(\n"
                "    shell_identity: tuple[int, int, int, str],\n"
                "    observer: BrokerExitObserver,\n"
                "    protected: tuple[tuple[int, int, int, str], ...],\n"
                ") -> None:\n"
            )
            assert source.count(stop_anchor) == 1
            source = source.replace(
                stop_anchor,
                stop_anchor + "    raise OSError('persistent broker fixture failure')\n",
                1,
            )
        else:
            poll_anchor = "def poll_resource_broker() -> bool:\n"
            assert source.count(poll_anchor) == 1
            fallback_child = (
                "    if not globals().get('persistent_fallback_child_started', False):\n"
                "        persistent_fallback_child_started = True\n"
                "        fallback_child_pid = os.fork()\n"
                "        if fallback_child_pid == 0:\n"
                "            for handled in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):\n"
                "                signal.signal(handled, signal.SIG_IGN)\n"
                f"            Path({str(child_marker)!r}).write_text(str(os.getpid()), encoding='ascii')\n"
                "            while True:\n"
                "                time.sleep(1)\n"
            )
            source = source.replace(
                poll_anchor,
                poll_anchor
                + fallback_child
                + f"    while not Path({str(fault_enable)!r}).exists():\n"
                + "        time.sleep(0.01)\n",
                1,
            )
            broker_arm_anchor = (
                "        pid_line = read_broker_line(pid_read, time.monotonic() + 90)\n"
            )
            assert source.count(broker_arm_anchor) == 1
            source = source.replace(
                broker_arm_anchor,
                broker_arm_anchor + "        os._exit(2)\n",
                1,
            )
        return source

    publisher, docker_config, _, _, _, command = _publisher_signal_fixture(
        tmp_path,
        inner_body=inner_body,
        cleanup_root_marker=private_marker,
        launcher_root_marker=launcher_marker,
        publisher_source_transform=transform,
    )
    process = subprocess.Popen(
        command,
        cwd=publisher.parents[4],
        env={
            "DOCKER_CONFIG": str(docker_config),
            "HOME": str(tmp_path / "ambient-home"),
            "PATH": "/nonexistent",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    process_identity = _read_process_table()[process.pid]
    child_identity: ProcessIdentity | None = None
    owner_identity: ProcessIdentity | None = None
    resource_paths: tuple[Path, Path] | None = None
    resource_identities: tuple[tuple[int, int], tuple[int, int]] | None = None
    cleanup_roots: tuple[tuple[Path, tuple[int, int]], ...] = ()
    owner_alive_before_release = False
    child_alive_before_release = False
    resources_alive_before_release = False
    roots_alive_before_release = False
    signal_observed = False
    stdout = stderr = ""
    try:
        deadline = time.monotonic() + 90
        while (
            not child_marker.is_file()
            or not resource_marker.is_file()
            or not private_marker.is_file()
            or not launcher_marker.is_file()
        ):
            if process.poll() is not None:
                diagnostic_stdout, diagnostic_stderr = process.communicate()
                raise AssertionError(
                    f"publisher exited before persistent {owner_path} setup: "
                    f"rc={process.returncode} stdout={diagnostic_stdout!r} "
                    f"stderr={diagnostic_stderr!r}"
                )
            if time.monotonic() >= deadline:
                raise AssertionError(
                    f"persistent {owner_path} fixture did not become ready"
                )
            time.sleep(0.05)

        fields = resource_marker.read_text(encoding="ascii").splitlines()
        assert len(fields) == 7
        record_path = Path(fields[0])
        record_identity = tuple(int(value) for value in fields[1].split(":"))
        lock_path = Path(fields[2])
        lock_identity = tuple(int(value) for value in fields[3].split(":"))
        resource_paths = (record_path, lock_path)
        resource_identities = (record_identity, lock_identity)
        private_root = _read_recorded_root(
            private_marker, "travel-map-publish-environment."
        )
        launcher_root = _read_recorded_root(
            launcher_marker, "travel-map-publish-launcher."
        )
        cleanup_roots = tuple(
            (path, (path.stat().st_dev, path.stat().st_ino))
            for path in (private_root, launcher_root)
        )
        child_pid = int(child_marker.read_text(encoding="ascii").strip())
        child_identity = _read_process_table().get(child_pid)
        assert child_identity is not None
        assert all(
            path.exists()
            and (path.stat().st_dev, path.stat().st_ino) == expected
            for path, expected in zip(resource_paths, resource_identities)
        )
        fault_enable.touch()
        while not observation_ready.is_file():
            if process.poll() is not None:
                raise AssertionError(
                    f"publisher exited before persistent {owner_path} observation"
                )
            if time.monotonic() >= deadline:
                raise AssertionError(
                    f"persistent {owner_path} observation did not become ready"
                )
            time.sleep(0.05)
        owner_pid = int(observation_ready.read_text(encoding="ascii").split()[0])
        owner_identity = _read_process_table().get(owner_pid)
        assert owner_identity is not None
        signal_window_deadline = time.monotonic() + 1.0
        while time.monotonic() < signal_window_deadline:
            if process.poll() is not None:
                raise AssertionError(
                    f"publisher exited during persistent {owner_path} signal window"
                )
            time.sleep(0.05)
        owner_alive_before_release = (
            _read_process_table().get(owner_identity.pid) == owner_identity
        )
        child_alive_before_release = (
            _read_process_table().get(child_identity.pid) == child_identity
        )
        resources_alive_before_release = all(
            path.exists()
            and (path.stat().st_dev, path.stat().st_ino) == expected
            for path, expected in zip(resource_paths, resource_identities)
        )
        roots_alive_before_release = all(
            path.exists()
            and (path.stat().st_dev, path.stat().st_ino) == expected
            for path, expected in cleanup_roots
        )
        signal_observed = unsafe_signal.is_file()
        if child_alive_before_release:
            os.kill(child_identity.pid, signal.SIGKILL)
        fault_disable.touch()
        signal_release.touch()
        stdout, stderr = process.communicate(timeout=45)
    finally:
        fault_disable.touch()
        signal_release.touch()
        if child_identity is not None:
            live = _read_process_table()
            if live.get(child_identity.pid) == child_identity:
                os.kill(child_identity.pid, signal.SIGKILL)
        if owner_identity is not None:
            live = _read_process_table()
            if live.get(owner_identity.pid) == owner_identity:
                os.kill(owner_identity.pid, signal.SIGKILL)
        if process.poll() is None:
            _kill_publisher_process_groups(
                _publisher_process_groups_for_fixture(process_identity)
            )
            if process.poll() is None and _read_process_table().get(process.pid) == process_identity:
                process.kill()
            process.wait(timeout=5)
        for path, expected in cleanup_roots:
            _cleanup_exact_owned_root(
                path,
                expected,
                "travel-map-publish-environment."
                if path.name.startswith("travel-map-publish-environment.")
                else "travel-map-publish-launcher.",
            )
        if resource_paths is not None and resource_identities is not None:
            record_path = resource_paths[0]
            record_expected = resource_identities[0]
            _cleanup_exact_owned_root(
                record_path,
                record_expected,
                "travel-map-publish.",
            )
            lock_path = resource_paths[1]
            lock_expected = resource_identities[1]
            try:
                details = lock_path.lstat()
            except FileNotFoundError:
                pass
            else:
                if (
                    (details.st_dev, details.st_ino) == lock_expected
                    and stat.S_ISDIR(details.st_mode)
                    and stat.S_IMODE(details.st_mode) == 0o700
                    and details.st_uid == os.getuid()
                    and not lock_path.is_symlink()
                    and not any(lock_path.iterdir())
                ):
                    lock_path.rmdir()
        if process.poll() is not None:
            try:
                stdout, stderr = process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()

    assert not signal_observed, (
        f"{owner_path} cleanup signalled an unproven numeric process group"
    )
    assert owner_alive_before_release
    assert child_alive_before_release
    assert resources_alive_before_release
    assert roots_alive_before_release
    assert process.returncode == 2
    assert stdout == ""
    assert "Traceback" not in stderr
    assert all(not path.exists() and not path.is_symlink() for path, _ in cleanup_roots)
    assert all(
        not path.exists() and not path.is_symlink()
        for path in resource_paths or ()
    )


def _stage_b_resource_fault_transform(
    *,
    mode: str,
    resource_ready: Path,
    resource_marker: Path,
    child_ready: Path,
    supervisor_marker: Path,
    resource_pause: Path,
    docker_state: Path,
):
    def transform(source: str) -> str:
        tag_anchor = "    owns_tagged=1\n"
        assert source.count(tag_anchor) == 1
        resource_body = textwrap.dedent(
            f"""
            /usr/bin/python3 -I -S - <<'PY' &
            import os
            import signal
            import time
            from pathlib import Path

            for handled in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
                signal.signal(handled, signal.SIG_IGN)
            Path({str(child_ready)!r}).write_text(str(os.getpid()) + "\\n", encoding="ascii")
            while True:
                time.sleep(1)
            PY
            resource_child=$!
            trap '' HUP INT TERM
            resource_ticks=0
            while [ ! -s {str(child_ready)!r} ]; do
                [ "$resource_ticks" -lt 1000 ] || exit 2
                /bin/sleep 0.01
                resource_ticks=$((resource_ticks + 1))
            done
            /usr/bin/python3 -I -S - \
                "$record_parent" "$record_parent_identity" "$lock_directory" \
                "$tagged" "$image_id" "$resource_child" "$$" <<'PY'
            import json
            import os
            import stat
            import sys
            from pathlib import Path

            record = Path(sys.argv[1])
            expected_record = sys.argv[2]
            lock = Path(sys.argv[3])
            tagged = sys.argv[4]
            image_id = sys.argv[5]
            child_pid = int(sys.argv[6])
            shell_pid = int(sys.argv[7])
            record_details = record.lstat()
            lock_details = lock.lstat()
            state = json.loads(Path({str(docker_state)!r}).read_text(encoding="utf-8"))
            if (
                f"{{record_details.st_dev}}:{{record_details.st_ino}}" != expected_record
                or not stat.S_ISDIR(record_details.st_mode)
                or not stat.S_ISDIR(lock_details.st_mode)
                or not state.get("tagged")
                or state.get("current_tag_id") != image_id
            ):
                raise SystemExit(2)
            os.kill(child_pid, 0)
            payload = {{
                "record": str(record),
                "record_identity": [record_details.st_dev, record_details.st_ino],
                "lock": str(lock),
                "lock_identity": [lock_details.st_dev, lock_details.st_ino],
                "tagged": tagged,
                "image_id": image_id,
                "child_pid": child_pid,
                "shell_pid": shell_pid,
            }}
            marker = Path({str(resource_marker)!r})
            temporary = marker.with_name(marker.name + ".tmp")
            temporary.write_text(json.dumps(payload), encoding="ascii")
            os.replace(temporary, marker)
            Path({str(resource_ready)!r}).write_text("ready\\n", encoding="ascii")
            PY
            while [ -e {str(resource_pause)!r} ]; do
                /bin/sleep 0.01
            done
            wait "$resource_child"
            """
        )
        source = source.replace(tag_anchor, tag_anchor + resource_body, 1)

        spawn_anchor = (
            "    broker_fallback_tag_write = None\n"
            "    if process.stdout is None or process.stdin is None:\n"
        )
        assert source.count(spawn_anchor) == 1
        supervisor_body = (
            f"    supervisor_marker = Path({str(supervisor_marker)!r})\n"
            "    supervisor_marker_tmp = supervisor_marker.with_name(supervisor_marker.name + '.tmp')\n"
            "    supervisor_marker_tmp.write_text(str(os.getpid()) + '\\n', encoding='ascii')\n"
            "    os.replace(supervisor_marker_tmp, supervisor_marker)\n"
        )
        if mode == "crash":
            supervisor_body += (
                "    process.stdin.write(script_payload)\n"
                "    process.stdin.close()\n"
                "    deadline = time.monotonic() + 180\n"
                f"    while not Path({str(resource_ready)!r}).is_file():\n"
                "        if time.monotonic() >= deadline:\n"
                "            os._exit(78)\n"
                "        time.sleep(0.01)\n"
                "    os._exit(79)\n"
            )
        elif mode != "signal":
            raise ValueError("unknown Stage-B resource failure mode")
        return source.replace(
            spawn_anchor,
            "    broker_fallback_tag_write = None\n"
            + supervisor_body
            + "    if process.stdout is None or process.stdin is None:\n",
            1,
        )

    return transform


@pytest.mark.parametrize("failure_mode", ("crash", "signal"))
def test_publisher_top_owner_cleans_registered_stage_b_resources(
    tmp_path: Path,
    failure_mode: str,
) -> None:
    image_id = "sha256:" + "a" * 64
    remote_digest = "sha256:" + "b" * 64
    resource_ready = tmp_path / f"stage-b-owner-{failure_mode}.ready"
    resource_marker = tmp_path / f"stage-b-owner-{failure_mode}.json"
    child_ready = tmp_path / f"stage-b-owner-{failure_mode}.child"
    supervisor_marker = tmp_path / f"stage-b-owner-{failure_mode}.supervisor"
    resource_pause = tmp_path / f"stage-b-owner-{failure_mode}.pause"
    docker_state = tmp_path / "docker-state.json"
    resource_pause.write_text("pause\n", encoding="ascii")
    observed: dict[str, object] = {}

    transform = _stage_b_resource_fault_transform(
        mode=failure_mode,
        resource_ready=resource_ready,
        resource_marker=resource_marker,
        child_ready=child_ready,
        supervisor_marker=supervisor_marker,
        resource_pause=resource_pause,
        docker_state=docker_state,
    )

    def runner(
        command: list[str],
        cwd: Path,
        environment: dict[str, str],
        state_path: Path,
    ) -> subprocess.CompletedProcess[str]:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        top_identity = _read_process_table()[process.pid]
        owned_tree: OwnedProcessTree | None = None
        record_path: Path | None = None
        record_identity: tuple[int, int] | None = None
        lock_path: Path | None = None
        lock_identity: tuple[int, int] | None = None
        captured_identities: tuple[ProcessIdentity, ...] = ()
        stdout = stderr = ""
        try:
            deadline = time.monotonic() + 180
            while not supervisor_marker.is_file():
                if process.poll() is not None:
                    raise AssertionError("publisher exited before Stage-B supervisor registration")
                if time.monotonic() >= deadline:
                    raise AssertionError("Stage-B supervisor was not registered")
                time.sleep(0.02)
            supervisor_pid = int(supervisor_marker.read_text(encoding="ascii").strip())
            supervisor_identity = _read_process_table()[supervisor_pid]
            captured_identities = tuple(
                identity
                for identity in (top_identity, supervisor_identity)
                if _read_process_table().get(identity.pid) == identity
            )

            while not resource_ready.is_file():
                if process.poll() is not None:
                    raise AssertionError("publisher exited before owned resources were ready")
                if time.monotonic() >= deadline:
                    raise AssertionError("owned Stage-B resources were not ready")
                time.sleep(0.02)
            resources = json.loads(resource_marker.read_text(encoding="ascii"))
            child_pid = int(resources["child_pid"])
            shell_pid = int(resources["shell_pid"])
            ready_processes = _read_process_table()
            child_identity = ready_processes[child_pid]
            shell_identity = ready_processes[shell_pid]
            captured_identities = tuple(
                identity
                for identity in (
                    top_identity,
                    supervisor_identity,
                    shell_identity,
                    child_identity,
                )
                if ready_processes.get(identity.pid) == identity
            )
            record_path = Path(resources["record"])
            lock_path = Path(resources["lock"])
            record_identity = tuple(resources["record_identity"])
            lock_identity = tuple(resources["lock_identity"])
            assert record_path.parent in {Path("/tmp"), Path("/private/tmp")}
            assert record_path.name.startswith("travel-map-publish.")
            assert lock_path.parent == Path(
                f"/tmp/travel-map-publish-locks-{os.getuid()}"
            )
            actual_image_id = str(resources["image_id"])
            assert resources["tagged"].endswith(
                f"-sha256-{actual_image_id.removeprefix('sha256:')}"
            )
            assert (record_path.stat().st_dev, record_path.stat().st_ino) == record_identity
            assert (lock_path.stat().st_dev, lock_path.stat().st_ino) == lock_identity
            ready_state = json.loads(state_path.read_text(encoding="utf-8"))
            assert ready_state["tagged"] is True
            assert ready_state["current_tag_id"] == actual_image_id
            owned_tree = OwnedProcessTree(
                top_identity if ready_processes.get(top_identity.pid) == top_identity else None,
                captured_identities,
                repr(captured_identities),
            )

            if failure_mode == "signal":
                assert _read_process_table().get(supervisor_pid) == supervisor_identity
                os.kill(supervisor_pid, signal.SIGTERM)
            try:
                stdout, stderr = process.communicate(timeout=60)
            except subprocess.TimeoutExpired:
                observed["timed_out"] = True
            else:
                observed["timed_out"] = False
            live = _read_process_table()
            observed["child_survived"] = live.get(child_pid) == child_identity
            try:
                current_record = record_path.lstat()
                observed["record_survived"] = (
                    current_record.st_dev,
                    current_record.st_ino,
                ) == record_identity
            except FileNotFoundError:
                observed["record_survived"] = False
            try:
                current_lock = lock_path.lstat()
                observed["lock_survived"] = (
                    current_lock.st_dev,
                    current_lock.st_ino,
                ) == lock_identity
            except FileNotFoundError:
                observed["lock_survived"] = False
            final_state = json.loads(state_path.read_text(encoding="utf-8"))
            observed["tag_survived"] = bool(final_state["tagged"])
        finally:
            resource_pause.unlink(missing_ok=True)
            if captured_identities:
                live_processes = _read_process_table()
                refreshed = tuple(
                    identity
                    for identity in captured_identities
                    if live_processes.get(identity.pid) == identity
                )
                _kill_publisher_process_groups(
                    OwnedProcessTree(
                        top_identity
                        if live_processes.get(top_identity.pid) == top_identity
                        else None,
                        refreshed,
                        repr(refreshed),
                    )
                )
            if process.poll() is None:
                current = _read_process_table().get(process.pid)
                if current == top_identity:
                    _kill_publisher_process_groups(
                        _publisher_process_groups_for_fixture(top_identity)
                    )
                if process.poll() is None and _read_process_table().get(process.pid) == top_identity:
                    process.kill()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            if record_path is not None and record_identity is not None:
                _cleanup_exact_owned_root(
                    record_path, record_identity, "travel-map-publish."
                )
            if lock_path is not None and lock_identity is not None:
                try:
                    lock_details = lock_path.lstat()
                except FileNotFoundError:
                    pass
                else:
                    if (
                        (lock_details.st_dev, lock_details.st_ino) == lock_identity
                        and stat.S_ISDIR(lock_details.st_mode)
                        and not lock_path.is_symlink()
                        and not any(lock_path.iterdir())
                    ):
                        lock_path.rmdir()
            if process.poll() is not None:
                try:
                    stdout, stderr = process.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    if process.stdout is not None:
                        process.stdout.close()
                    if process.stderr is not None:
                        process.stderr.close()
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)

    completed = _run_publish_reviewed_image(
        tmp_path,
        image_id=image_id,
        remote_digest=remote_digest,
        root_manifest=_image_manifest(image_id),
        publisher_source_transform=transform,
        publisher_runner=runner,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert "Traceback" not in completed.stderr
    assert observed == {
        "timed_out": False,
        "child_survived": False,
        "record_survived": False,
        "lock_survived": False,
        "tag_survived": False,
    }, completed.stderr


def test_publisher_restore_does_not_replace_late_destination_entry(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "cleanup-restore-race.identity"
    pause = tmp_path / "cleanup-restore-race.pause"
    private_marker = tmp_path / "cleanup-restore-race.private-root"
    launcher_marker = tmp_path / "cleanup-restore-race.launcher-root"
    pause.write_text("pause\n", encoding="ascii")
    publisher, docker_config, _, _, _, command = _publisher_signal_fixture(
        tmp_path,
        inner_body=(
            "/usr/bin/printf '%s\\n' original > "
            '"$HOME/cleanup-restore-entry"\n'
            "exit 2"
        ),
        cleanup_restore_race=(marker, pause),
        cleanup_root_marker=private_marker,
        launcher_root_marker=launcher_marker,
    )
    process = subprocess.Popen(
        command,
        cwd=publisher.parents[4],
        env={
            "DOCKER_CONFIG": str(docker_config),
            "HOME": str(tmp_path / "ambient-home"),
            "PATH": "/nonexistent",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    process_identity = _read_process_table()[process.pid]
    private_root: Path | None = None
    private_identity: tuple[int, int] | None = None
    launcher_root: Path | None = None
    launcher_identity: tuple[int, int] | None = None
    replacement: Path | None = None
    replacement_identity: tuple[int, int] | None = None
    stdout = stderr = ""
    replacement_preserved = False
    original_preserved = False
    try:
        deadline = time.monotonic() + 60
        while not marker.is_file():
            if process.poll() is not None:
                raise AssertionError("publisher exited before restore race boundary")
            if time.monotonic() >= deadline:
                raise AssertionError("publisher did not reach restore race boundary")
            time.sleep(0.02)
        marker_lines = marker.read_text(encoding="ascii").splitlines()
        replacement_identity = tuple(
            int(value) for value in marker_lines[0].split(":")
        )
        private_identity = tuple(
            int(value) for value in marker_lines[1].split(":")
        )
        private_root = _find_exact_owned_root(
            private_identity, "travel-map-publish-environment."
        )
        replacement = _find_identity_under(private_root, replacement_identity)
        assert replacement is not None
        replacement_details = replacement.lstat()
        assert (replacement_details.st_dev, replacement_details.st_ino) == replacement_identity
        assert replacement.read_text(encoding="ascii") == "late-replacement\n"
        if launcher_marker.is_file() and launcher_marker.read_text(encoding="ascii").strip():
            launcher_root = _read_recorded_root(
                launcher_marker, "travel-map-publish-launcher."
            )
            launcher_details = launcher_root.lstat()
            launcher_identity = (launcher_details.st_dev, launcher_details.st_ino)
        pause.unlink()
        stdout, stderr = process.communicate(timeout=15)
        private_root = _find_exact_owned_root(
            private_identity, "travel-map-publish-environment."
        )
        replacement = _find_identity_under(private_root, replacement_identity)
        replacement_preserved = (
            replacement is not None
            and replacement.read_text(encoding="ascii") == "late-replacement\n"
        )
        if private_root.exists():
            for candidate in private_root.rglob("*"):
                try:
                    details = candidate.lstat()
                except FileNotFoundError:
                    continue
                if (
                    stat.S_ISREG(details.st_mode)
                    and not candidate.is_symlink()
                    and candidate.read_bytes() == b"original\n"
                ):
                    original_preserved = True
                    break
    finally:
        pause.unlink(missing_ok=True)
        if process.poll() is None:
            try:
                _kill_publisher_process_groups(
                    _publisher_process_groups_for_fixture(process_identity)
                )
            except PermissionError:
                pass
            if process.poll() is None and _read_process_table().get(process.pid) == process_identity:
                process.kill()
            process.wait(timeout=5)
        if private_root is not None and private_identity is not None:
            _cleanup_exact_owned_root(
                private_root,
                private_identity,
                "travel-map-publish-environment.",
            )
        if launcher_root is not None and launcher_identity is not None:
            _cleanup_exact_owned_root(
                launcher_root,
                launcher_identity,
                "travel-map-publish-launcher.",
            )

    assert process.returncode == 2
    assert stdout == ""
    assert "Traceback" not in stderr
    assert replacement_preserved
    assert original_preserved


@pytest.mark.parametrize("race_kind", ("regular", "directory"))
def test_publisher_rejects_top_cleanup_final_stat_replacement(
    tmp_path: Path,
    race_kind: str,
) -> None:
    marker = tmp_path / f"cleanup-final-stat-{race_kind}.ready"
    launcher_marker = tmp_path / f"cleanup-final-stat-{race_kind}.launcher-root"
    private_marker = tmp_path / f"cleanup-final-stat-{race_kind}.private-root"
    target = (
        "cleanup-regular-entry" if race_kind == "regular" else "cleanup-directory-entry"
    )
    if race_kind == "regular":
        inner_body = f"/usr/bin/printf original > \"$HOME/{target}\"\nexit 2"
    else:
        inner_body = f"/bin/mkdir \"$HOME/{target}\"\nexit 2"
    publisher, docker_config, _, _, _, command = _publisher_signal_fixture(
        tmp_path,
        inner_body=inner_body,
        cleanup_final_stat_race=(race_kind, marker),
        launcher_root_marker=launcher_marker,
        cleanup_root_marker=private_marker,
    )
    process = subprocess.Popen(
        command,
        cwd=publisher.parents[4],
        env={"DOCKER_CONFIG": str(docker_config), "HOME": str(tmp_path / "ambient-home"), "PATH": "/nonexistent"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    process_identity = _read_process_table()[process.pid]
    completed: subprocess.CompletedProcess[str]
    root: Path | None = None
    replacement: Path | None = None
    try:
        try:
            stdout, stderr = process.communicate(timeout=30)
        except subprocess.TimeoutExpired as error:
            groups = _publisher_process_groups_for_fixture(process_identity)
            diagnostic = groups.diagnostic
            try:
                _kill_publisher_process_groups(groups)
            except PermissionError:
                # A diagnostic tree may contain a foreign/reparented member.  Never
                # broaden authority for cleanup; terminate only this exact Popen root.
                if (
                    process.poll() is None
                    and _read_process_table().get(process.pid) == process_identity
                ):
                    process.kill()
            if process.poll() is None and _read_process_table().get(process.pid) == process_identity:
                process.kill()
            try:
                stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()
                stdout = stderr = ""
            raise AssertionError(
                f"cleanup final-stat fixture timed out; process tree: {diagnostic}"
            ) from error
        completed = subprocess.CompletedProcess(
            command, process.returncode, stdout, stderr
        )
        assert marker.is_file() and marker.read_text(encoding="ascii").strip() == "ready"
        root = _read_recorded_root(private_marker, "travel-map-publish-environment.")
        replacement = root / "home" / target
        assert completed.returncode == 2
        assert completed.stdout == ""
        assert "Traceback" not in completed.stderr
        assert replacement.exists()
    finally:
        if process.poll() is None:
            groups = _publisher_process_groups_for_fixture(process_identity)
            try:
                _kill_publisher_process_groups(groups)
            except PermissionError:
                if _read_process_table().get(process.pid) == process_identity:
                    process.kill()
            if process.poll() is None and _read_process_table().get(process.pid) == process_identity:
                process.kill()
            process.wait(timeout=5)
        if private_marker.is_file() and private_marker.read_text(encoding="ascii").strip():
            _cleanup_recorded_root(private_marker, "travel-map-publish-environment.")
        if launcher_marker.is_file() and launcher_marker.read_text(encoding="ascii").strip():
            _cleanup_recorded_root(launcher_marker, "travel-map-publish-launcher.")


def test_publisher_rejects_stage_b_pending_signal_before_public_write(
    tmp_path: Path,
) -> None:
    ready = tmp_path / "stage-b-pending.ready"
    pause = tmp_path / "stage-b-pending.pause"
    pause.write_text("pause\n", encoding="ascii")
    launcher_marker = tmp_path / "stage-b-pending.launcher-root"
    private_marker = tmp_path / "stage-b-pending.private-root"
    digest = "ghcr.io/h19h29-design/seoul-education-travel-map@sha256:" + "a" * 64
    publisher, docker_config, _, _, _, command = _publisher_signal_fixture(
        tmp_path,
        inner_body=f"printf '%s\\n' {digest!r}\nexit 0",
        stage_b_pending_signal_window=(ready, pause),
        launcher_root_marker=launcher_marker,
        cleanup_root_marker=private_marker,
    )
    process = subprocess.Popen(
        command,
        cwd=publisher.parents[4],
        env={"DOCKER_CONFIG": str(docker_config), "HOME": str(tmp_path / "ambient-home"), "PATH": "/nonexistent"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    process_identity = _read_process_table()[process.pid]
    supervisor_identity: ProcessIdentity | None = None
    stdout = stderr = ""
    try:
        deadline = time.monotonic() + 60
        while not ready.is_file() or not ready.read_text(encoding="ascii").strip():
            if process.poll() is not None:
                raise AssertionError("Stage-B supervisor exited before pending boundary")
            if time.monotonic() >= deadline:
                raise AssertionError("Stage-B supervisor did not reach pending boundary")
            time.sleep(0.05)
        supervisor_pid = int(ready.read_text(encoding="ascii").strip())
        supervisor_identity = _read_process_table()[supervisor_pid]
        os.kill(supervisor_pid, signal.SIGTERM)
        pause.unlink()
        stdout, stderr = process.communicate(timeout=15)
        assert process.returncode == 2
        assert stdout == ""
        assert "Traceback" not in stderr
    finally:
        pause.unlink(missing_ok=True)
        if supervisor_identity is not None and _read_process_table().get(supervisor_identity.pid) == supervisor_identity:
            _kill_publisher_process_groups(
                _publisher_process_groups_for_fixture(supervisor_identity)
            )
        if process.poll() is None:
            _kill_publisher_process_groups(
                _publisher_process_groups_for_fixture(process_identity)
            )
            if process.poll() is None and _read_process_table().get(process.pid) == process_identity:
                process.kill()
            process.wait(timeout=5)
        for marker, prefix in (
            (private_marker, "travel-map-publish-environment."),
            (launcher_marker, "travel-map-publish-launcher."),
        ):
            if marker.is_file() and marker.read_text(encoding="ascii").strip():
                _cleanup_recorded_root(marker, prefix)


def test_publisher_fails_closed_when_public_stdout_reader_is_closed(
    tmp_path: Path,
) -> None:
    ready = tmp_path / "stdout-close.ready"
    pause = tmp_path / "stdout-close.pause"
    pause.write_text("pause\n", encoding="ascii")
    publisher, docker_config, _, _, _, command = _publisher_signal_fixture(
        tmp_path,
        inner_body=("printf '%s\\n' "
                    "ghcr.io/h19h29-design/seoul-education-travel-map@sha256:"
                    + "a" * 64 + "\nexit 0"),
        stage_b_pending_signal_window=(ready, pause),
    )
    process = subprocess.Popen(
        command,
        cwd=publisher.parents[4],
        env={"DOCKER_CONFIG": str(docker_config), "HOME": str(tmp_path / "ambient-home"), "PATH": "/nonexistent"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    process_identity = _read_process_table()[process.pid]
    stderr = ""
    try:
        deadline = time.monotonic() + 60
        while not ready.is_file() or not ready.read_text(encoding="ascii").strip():
            if process.poll() is not None:
                raise AssertionError("publisher exited before stdout boundary")
            if time.monotonic() >= deadline:
                raise AssertionError("publisher did not reach stdout boundary")
            time.sleep(0.05)
        assert process.stdout is not None
        process.stdout.close()
        pause.unlink()
        stderr = process.stderr.read() if process.stderr is not None else ""
        process.wait(timeout=15)
    finally:
        pause.unlink(missing_ok=True)
        if process.poll() is None:
            _kill_publisher_process_groups(
                _publisher_process_groups_for_fixture(process_identity)
            )
            process.wait(timeout=5)
    assert process.returncode == 2
    assert "Traceback" not in stderr


@pytest.mark.parametrize("sink_kind", ("regular", "devnull", "pty"))
def test_publisher_rejects_nonpipe_public_stdout_before_registry_mutation(
    tmp_path: Path,
    sink_kind: str,
) -> None:
    image_id = "sha256:" + "a" * 64
    remote_digest = "sha256:" + "b" * 64
    public_output = tmp_path / "public-output.txt"

    def runner(
        command: list[str],
        cwd: Path,
        environment: dict[str, str],
        state_path: Path,
    ) -> subprocess.CompletedProcess[str]:
        master_fd: int | None = None
        if sink_kind == "regular":
            sink_fd = os.open(
                public_output,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                0o600,
            )
        elif sink_kind == "devnull":
            sink_fd = os.open(os.devnull, os.O_WRONLY)
        elif sink_kind == "pty":
            master_fd, sink_fd = pty.openpty()
        else:
            raise ValueError("unknown public stdout sink")
        try:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=environment,
                stdout=sink_fd,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            _, stderr_raw = process.communicate(timeout=30)
        finally:
            os.close(sink_fd)
        if sink_kind == "regular":
            stdout_raw = public_output.read_bytes()
        elif master_fd is None:
            stdout_raw = b""
        else:
            captured = bytearray()
            try:
                while True:
                    chunk = os.read(master_fd, 4096)
                    if not chunk:
                        break
                    captured.extend(chunk)
            except OSError:
                pass
            finally:
                os.close(master_fd)
            stdout_raw = bytes(captured)
        assert not state_path.exists()
        return subprocess.CompletedProcess(
            command,
            process.returncode,
            stdout_raw.decode("utf-8", errors="replace"),
            stderr_raw.decode("utf-8", errors="replace"),
        )

    completed = _run_publish_reviewed_image(
        tmp_path,
        image_id=image_id,
        remote_digest=remote_digest,
        root_manifest=_image_manifest(image_id),
        publisher_runner=runner,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert "Traceback" not in completed.stderr


def test_publisher_fails_closed_on_short_public_stdout_write(
    tmp_path: Path,
) -> None:
    publisher, docker_config, _, _, _, command = _publisher_signal_fixture(
        tmp_path,
        inner_body=("printf '%s\\n' "
                    "ghcr.io/h19h29-design/seoul-education-travel-map@sha256:"
                    + "a" * 64 + "\nexit 0"),
        short_stdout_write=True,
    )
    completed = subprocess.run(
        command,
        cwd=publisher.parents[4],
        env={"DOCKER_CONFIG": str(docker_config), "HOME": str(tmp_path / "ambient-home"), "PATH": "/nonexistent"},
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 2
    assert completed.stdout == ""
    assert "Traceback" not in completed.stderr


def _run_mutable_script_carrier_attack(tmp_path: Path, *, stage_b: bool) -> None:
    publisher_source = (
        ROOT / "deploy/nas/publish-reviewed-image.sh"
    ).read_text(encoding="utf-8")
    declaration = (
        'name = ".stage-b-script"'
        if stage_b
        else 'capture_name = ".launcher-script"'
    )
    pathname_carrier = ".stage-b-script" if stage_b else ".launcher-script"
    if declaration not in publisher_source:
        # Pathless-pipe production removes this pathname carrier and its
        # instrumentation anchor; retain the regression as a no-carrier check.
        assert pathname_carrier not in publisher_source
        return

    carrier_ready = tmp_path / ("stage-b-carrier.ready" if stage_b else "launcher-carrier.ready")
    carrier_release = tmp_path / ("stage-b-carrier.release" if stage_b else "launcher-carrier.release")
    executed = tmp_path / ("stage-b-carrier.executed" if stage_b else "launcher-carrier.executed")
    fixture_kwargs = (
        {"stage_b_script_carrier_window": (carrier_ready, carrier_release)}
        if stage_b
        else {"launcher_script_carrier_window": (carrier_ready, carrier_release)}
    )
    publisher, docker_config, _, _, _, command = _publisher_signal_fixture(
        tmp_path, **fixture_kwargs
    )
    process = subprocess.Popen(
        command,
        cwd=publisher.parents[4],
        env={
            "DOCKER_CONFIG": str(docker_config),
            "HOME": str(tmp_path / "ambient-home"),
            "PATH": "/nonexistent",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    process_identity = _read_process_table()[process.pid]
    launcher_root: Path | None = None
    root_survived = False
    try:
        deadline = time.monotonic() + 60
        while not carrier_ready.is_file() or not carrier_ready.read_text(encoding="ascii").strip():
            if process.poll() is not None:
                raise AssertionError("publisher exited before carrier boundary")
            if time.monotonic() >= deadline:
                raise AssertionError("publisher did not reach carrier boundary")
            time.sleep(0.05)
        launcher_root = _read_recorded_root(carrier_ready, "travel-map-publish-launcher.")
        carrier = launcher_root / (".stage-b-script" if stage_b else ".launcher-script")
        if stage_b:
            fake = (
                "#!/bin/sh\n"
                f"/usr/bin/touch {str(executed)!r}\n"
                "/usr/bin/printf '%s\\n' "
                "ghcr.io/h19h29-design/seoul-education-travel-map@sha256:"
                + "e" * 64
                + "\nexit 0\n"
            )
        else:
            fake = (
                "#!/bin/sh\n"
                f"/usr/bin/touch {str(executed)!r}\n"
                "/usr/bin/printf '%s\\n' "
                "ghcr.io/h19h29-design/seoul-education-travel-map@sha256:"
                + "d" * 64
                + " >&8\n"
                "root=$(/usr/bin/dirname \"$0\")\n"
                "/bin/rm -f \"$0\"\n"
                "/bin/rmdir \"$root\"\n"
                "exit 0\n"
            )
        _rewrite_same_inode(carrier, fake)
        carrier_release.touch()
        stdout, stderr = process.communicate(timeout=30)
        root_survived = launcher_root.exists()
    finally:
        carrier_release.touch()
        carrier_release.unlink(missing_ok=True)
        if process.poll() is None:
            _kill_publisher_process_groups(
                _publisher_process_groups_for_fixture(process_identity)
            )
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
        if launcher_root is not None and launcher_root.exists():
            _cleanup_recorded_root(carrier_ready, "travel-map-publish-launcher.")

    assert executed.is_file()
    assert process.returncode == 2
    assert stdout == ""
    assert "Traceback" not in stderr
    assert not (tmp_path / "publisher-docker-ran").exists()
    assert not root_survived


def test_publisher_rejects_mutated_launcher_script_carrier_after_hash(
    tmp_path: Path,
) -> None:
    _run_mutable_script_carrier_attack(tmp_path, stage_b=False)


def test_publisher_rejects_mutated_stage_b_script_carrier_after_hash(
    tmp_path: Path,
) -> None:
    _run_mutable_script_carrier_attack(tmp_path, stage_b=True)


def test_publisher_has_no_pathname_reachable_script_execution_carriers() -> None:
    source = (ROOT / "deploy/nas/publish-reviewed-image.sh").read_text(
        encoding="utf-8"
    )

    # Static invariant is unavoidable here: both carrier names identify regular files
    # that were previously trusted after pathname-visible hash validation.
    assert 'capture_name = ".launcher-script"' not in source
    assert 'name = ".stage-b-script"' not in source


@pytest.mark.parametrize("termination_signal", (signal.SIGHUP, signal.SIGINT, signal.SIGTERM))
def test_publisher_cleans_launcher_on_exec_handoff_signal(
    tmp_path: Path,
    termination_signal: signal.Signals,
) -> None:
    launcher_probe = tmp_path / "exec-handoff-launcher-root"
    launcher_release = tmp_path / "exec-handoff.release"
    publisher, docker_config, _, _, _, command = _publisher_signal_fixture(
        tmp_path,
        launcher_exec_handoff=(launcher_probe, launcher_release),
    )
    process = subprocess.Popen(
        command,
        cwd=publisher.parents[4],
        env={
            "DOCKER_CONFIG": str(docker_config),
            "HOME": str(tmp_path / "ambient-home"),
            "PATH": "/nonexistent",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    launcher_root: Path | None = None
    stdout = stderr = ""
    try:
        deadline = time.monotonic() + 30
        while process.poll() is None:
            if launcher_probe.is_file() and launcher_probe.read_text(encoding="ascii").strip():
                break
            if time.monotonic() >= deadline:
                raise AssertionError("publisher did not reach launcher exec handoff")
            time.sleep(0.05)
        launcher_root = _read_recorded_root(
            launcher_probe, "travel-map-publish-launcher."
        )
        process.send_signal(termination_signal)
        launcher_release.touch()
        stdout, stderr = process.communicate(timeout=10)
    finally:
        launcher_release.unlink(missing_ok=True)
        launcher_probe_tmp = launcher_probe.with_name(launcher_probe.name + ".tmp")
        launcher_probe_tmp.unlink(missing_ok=True)
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        if launcher_probe.exists() and launcher_probe.read_text(encoding="ascii").strip():
            _cleanup_recorded_root(launcher_probe, "travel-map-publish-launcher.")

    assert process.returncode == 2
    assert stdout == ""
    assert "Traceback" not in stderr
    assert launcher_root is not None and not launcher_root.exists()


@pytest.mark.parametrize(
    ("relative", "resolver_name", "function_name"),
    (
        (
            "scripts/release-gate.sh",
            "resolve_release_tool",
            "capture_release_tool_identity",
        ),
        (
            "deploy/nas/publish-reviewed-image.sh",
            "resolve_publish_tool",
            "capture_publish_tool_identity",
        ),
    ),
)
def test_tool_capture_binds_open_fd_to_the_resolved_path_inode(
    relative: str,
    resolver_name: str,
    function_name: str,
) -> None:
    source = (ROOT / relative).read_text(encoding="utf-8")
    resolver = source.split(f"{resolver_name}() {{\n", 1)[1].split("\n}\n", 1)[0]
    function = source.split(f"{function_name}() {{\n", 1)[1].split("\n}\n", 1)[0]

    assert "descriptor = os.open(" in function
    assert 'getattr(os, "O_NOFOLLOW", 0)' in function
    assert "details = os.fstat(descriptor)" in function
    assert "path_details = path.lstat()" in function
    assert "(details.st_dev, details.st_ino)" in function
    assert "(path_details.st_dev, path_details.st_ino)" in function
    assert "parent_details.st_mode & (stat.S_IWGRP | stat.S_IWOTH)" in resolver


def test_publisher_opens_auth_only_from_verified_private_launcher(
    tmp_path: Path,
) -> None:
    probe = tmp_path / "publisher-auth-open-phases"
    publisher, docker_config, pause_path, _, _, command = _publisher_signal_fixture(
        tmp_path,
        authority_open_probe=probe,
    )
    pause_path.unlink(missing_ok=True)

    completed = subprocess.run(
        command,
        cwd=publisher.parents[4],
        env={"DOCKER_CONFIG": str(docker_config), "PATH": "/nonexistent"},
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    phases = probe.read_text(encoding="ascii").splitlines()
    assert phases
    assert set(phases) == {"1"}
    assert completed.returncode == 2
    assert completed.stdout == ""
    assert "Traceback" not in completed.stderr


def test_publisher_socket_contract_allows_only_valid_root_group_socket_mode() -> None:
    source = (ROOT / "deploy/nas/publish-reviewed-image.sh").read_text(encoding="utf-8")

    assert "allowed_group_socket = (" in source
    assert "socket_details.st_uid == 0" in source
    assert "socket_details.st_gid in {os.getgid(), *os.getgroups()}" in source
    assert "not socket_permissions & 0o017" in source


def test_publisher_stage_b_never_invokes_release_gate(
    tmp_path: Path,
) -> None:
    marker = "publisher-write-packages-marker"
    gate_marker = tmp_path / "publisher-release-gate-ran"
    publisher, docker_config, pause_path, _, _, command = _publisher_signal_fixture(
        tmp_path,
        release_gate_marker=gate_marker,
    )
    pause_path.unlink(missing_ok=True)
    (docker_config / "config.json").write_text(
        (
            f'{{"auths":{{"ghcr.io":{{"auth":"{marker}"}}}},'
            '"currentContext":"release-test"}\n'
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        command,
        cwd=publisher.parents[4],
        env={"DOCKER_CONFIG": str(docker_config), "PATH": "/nonexistent"},
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 2
    assert "release-gate.sh" not in publisher.read_text(encoding="utf-8")
    assert not gate_marker.exists()
    assert marker not in completed.stdout
    assert marker not in completed.stderr


def test_publisher_reaps_descendants_before_releasing_digest(
    tmp_path: Path,
) -> None:
    descendant_pid_path = tmp_path / "publisher-descendant.pid"
    digest = "ghcr.io/h19h29-design/seoul-education-travel-map@sha256:" + "a" * 64
    inner_body = (
        "/bin/sleep 30 &\n"
        f"printf '%s\\n' \"$!\" > {str(descendant_pid_path)!r}\n"
        f"printf '%s\\n' {digest!r}\n"
        "exit 0"
    )
    publisher, docker_config, pause_path, _, _, command = _publisher_signal_fixture(
        tmp_path, inner_body=inner_body
    )
    pause_path.unlink(missing_ok=True)
    descendant_pid: int | None = None
    try:
        completed = subprocess.run(
            command,
            cwd=publisher.parents[4],
            env={"DOCKER_CONFIG": str(docker_config), "PATH": "/nonexistent"},
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        descendant_pid = int(descendant_pid_path.read_text(encoding="ascii"))
        try:
            os.kill(descendant_pid, 0)
        except ProcessLookupError:
            survived = False
        else:
            survived = True
    finally:
        if descendant_pid is None and descendant_pid_path.exists():
            descendant_pid = int(descendant_pid_path.read_text(encoding="ascii"))
        if descendant_pid is not None:
            try:
                os.kill(descendant_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    assert completed.returncode == 0
    assert completed.stdout == digest + "\n"
    assert completed.stderr == ""
    assert not survived


def _forged_private_environment(tmp_path: Path, root_variable: str) -> dict[str, str]:
    private_root = tmp_path / "forged-private-root"
    escaped_root = tmp_path / "escaped-private-state"
    private_root.mkdir(mode=0o700)
    escaped_root.mkdir(mode=0o700)
    (private_root / "escape").symlink_to(escaped_root, target_is_directory=True)
    layout = {
        "HOME": "home",
        "XDG_CONFIG_HOME": "xdg-config",
        "XDG_CACHE_HOME": "xdg-cache",
        "XDG_DATA_HOME": "xdg-data",
        "npm_config_cache": "npm-cache",
        "PNPM_HOME": "pnpm-home",
    }
    environment = {root_variable: str(private_root)}
    for name, leaf in layout.items():
        (escaped_root / leaf).mkdir(mode=0o700)
        environment[name] = str(private_root / "escape" / leaf)
    return environment


def _preseeded_private_environment(
    tmp_path: Path,
    root_variable: str,
) -> dict[str, str]:
    private_root = tmp_path / "preseeded-private-root"
    private_root.mkdir(mode=0o700)
    layout = {
        "HOME": "home",
        "XDG_CONFIG_HOME": "xdg-config",
        "XDG_CACHE_HOME": "xdg-cache",
        "XDG_DATA_HOME": "xdg-data",
        "npm_config_cache": "npm-cache",
        "PNPM_HOME": "pnpm-home",
    }
    environment = {root_variable: str(private_root)}
    for name, leaf in layout.items():
        child = private_root / leaf
        child.mkdir(mode=0o700)
        environment[name] = str(child)
    attacker_shell = tmp_path / "attacker-script-shell"
    _write_executable(
        attacker_shell,
        f"#!/bin/sh\nprintf '%s\\n' invoked > {str(tmp_path / 'script-shell-ran')!r}\nexit 0\n",
    )
    (private_root / "home/.npmrc").write_text(
        f"script-shell={attacker_shell}\n",
        encoding="utf-8",
    )
    return environment


def test_release_gate_rejects_forged_clean_marker_symlink_layout(
    tmp_path: Path,
) -> None:
    repository, gate, _, _ = _release_gate_repository(tmp_path)
    docker_config = _protected_docker_config(tmp_path)
    record_parent = tmp_path / "forged-record"
    record_parent.mkdir(mode=0o700)
    environment = _forged_private_environment(
        tmp_path,
        "TRAVEL_MAP_RELEASE_PRIVATE_ROOT",
    )
    environment.update(
        {
            "PATH": TRUSTED_PATH,
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "CI": "1",
            "TMPDIR": str(Path("/tmp").resolve()),
            "NAS_PLATFORM": "linux/amd64",
            "RELEASE_GATE_IMAGE_RECORD": str(record_parent / "gated-image.record"),
            "DOCKER_CONFIG": str(docker_config),
            "UV_CACHE_DIR": str(tmp_path / "uv-cache"),
            "PLAYWRIGHT_BROWSERS_PATH": str(tmp_path / "playwright-cache"),
            "PNPM_STORE_DIR": str(tmp_path / "pnpm-store"),
            "npm_config_userconfig": "/dev/null",
            "npm_config_globalconfig": "/dev/null",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "TRAVEL_MAP_RELEASE_CLEAN_ENVIRONMENT": "1",
            "TRAVEL_MAP_RELEASE_DOCKER_HOST": (
                "unix://" + str(_release_test_socket_path(tmp_path))
            ),
        }
    )

    completed = subprocess.run(
        ["/bin/sh", str(gate)],
        cwd=repository,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "BLOCKED_UNSAFE_RELEASE_ENVIRONMENT\n"


def test_publisher_rejects_forged_clean_marker_symlink_layout(
    tmp_path: Path,
) -> None:
    publisher, docker_config, pause_path, _, _, command = _publisher_signal_fixture(
        tmp_path
    )
    pause_path.unlink(missing_ok=True)
    environment = _forged_private_environment(
        tmp_path,
        "TRAVEL_MAP_PUBLISH_PRIVATE_ROOT",
    )
    environment.update(
        {
            "PATH": TRUSTED_PATH,
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "CI": "1",
            "TMPDIR": str(Path("/tmp").resolve()),
            "DOCKER_CONFIG": str(docker_config),
            "UV_CACHE_DIR": str(tmp_path / "publisher-uv-cache"),
            "PLAYWRIGHT_BROWSERS_PATH": str(tmp_path / "publisher-playwright-cache"),
            "PNPM_STORE_DIR": str(tmp_path / "publisher-pnpm-store"),
            "npm_config_userconfig": "/dev/null",
            "npm_config_globalconfig": "/dev/null",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "TRAVEL_MAP_PUBLISH_CLEAN_ENVIRONMENT": "1",
            "TRAVEL_MAP_PUBLISH_DOCKER_HOST": "unix:///tmp/forged-publish.sock",
        }
    )

    completed = subprocess.run(
        command,
        cwd=publisher.parents[4],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "BLOCKED_INVALID_PUBLISH_CONTEXT\n"


def test_release_gate_rejects_preseeded_private_package_config(
    tmp_path: Path,
) -> None:
    repository, gate, _, _ = _release_gate_repository(tmp_path)
    docker_config = _protected_docker_config(tmp_path)
    record_parent = tmp_path / "preseeded-record"
    record_parent.mkdir(mode=0o700)
    environment = _preseeded_private_environment(
        tmp_path,
        "TRAVEL_MAP_RELEASE_PRIVATE_ROOT",
    )
    environment.update(
        {
            "PATH": TRUSTED_PATH,
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "CI": "1",
            "TMPDIR": str(Path("/tmp").resolve()),
            "NAS_PLATFORM": "linux/amd64",
            "RELEASE_GATE_IMAGE_RECORD": str(record_parent / "gated-image.record"),
            "DOCKER_CONFIG": str(docker_config),
            "UV_CACHE_DIR": str(tmp_path / "uv-cache"),
            "PLAYWRIGHT_BROWSERS_PATH": str(tmp_path / "playwright-cache"),
            "PNPM_STORE_DIR": str(tmp_path / "pnpm-store"),
            "npm_config_userconfig": "/dev/null",
            "npm_config_globalconfig": "/dev/null",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "TRAVEL_MAP_RELEASE_CLEAN_ENVIRONMENT": "1",
            "TRAVEL_MAP_RELEASE_DOCKER_HOST": (
                "unix://" + str(_release_test_socket_path(tmp_path))
            ),
        }
    )

    completed = subprocess.run(
        ["/bin/sh", str(gate)],
        cwd=repository,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "BLOCKED_UNSAFE_RELEASE_ENVIRONMENT\n"
    assert not (tmp_path / "script-shell-ran").exists()


@pytest.mark.parametrize(
    ("artifact", "mode"),
    (("gate", 0o777), ("materializer", 0o666)),
)
def test_release_gate_binds_bootstrap_artifacts_to_head_mode_and_blob(
    tmp_path: Path,
    artifact: str,
    mode: int,
) -> None:
    _, gate, _, _ = _release_gate_repository(tmp_path)
    target = (
        gate if artifact == "gate" else gate.with_name("materialize-pinned-source.py")
    )
    target.chmod(mode)

    completed, record = _run_release_gate(
        tmp_path,
        gate,
        docker_config=_protected_docker_config(tmp_path),
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "BLOCKED_INVALID_RELEASE_ARTIFACT\n"
    assert not record.exists()


def test_publisher_rejects_preseeded_private_package_config(
    tmp_path: Path,
) -> None:
    publisher, docker_config, pause_path, _, _, command = _publisher_signal_fixture(
        tmp_path
    )
    pause_path.unlink(missing_ok=True)
    environment = _preseeded_private_environment(
        tmp_path,
        "TRAVEL_MAP_PUBLISH_PRIVATE_ROOT",
    )
    environment.update(
        {
            "PATH": TRUSTED_PATH,
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "CI": "1",
            "TMPDIR": str(Path("/tmp").resolve()),
            "DOCKER_CONFIG": str(docker_config),
            "UV_CACHE_DIR": str(tmp_path / "publisher-uv-cache"),
            "PLAYWRIGHT_BROWSERS_PATH": str(tmp_path / "publisher-playwright-cache"),
            "PNPM_STORE_DIR": str(tmp_path / "publisher-pnpm-store"),
            "npm_config_userconfig": "/dev/null",
            "npm_config_globalconfig": "/dev/null",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "TRAVEL_MAP_PUBLISH_CLEAN_ENVIRONMENT": "1",
            "TRAVEL_MAP_PUBLISH_DOCKER_HOST": "unix:///tmp/preseed-publish.sock",
        }
    )

    completed = subprocess.run(
        command,
        cwd=publisher.parents[4],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "BLOCKED_INVALID_PUBLISH_CONTEXT\n"
    assert not (tmp_path / "script-shell-ran").exists()


@pytest.mark.parametrize("index_mode", ("assume", "skip", "dirty"))
def test_release_gate_blocks_dirty_or_hidden_index_state(
    tmp_path: Path,
    index_mode: str,
) -> None:
    repository, gate, _, _ = _release_gate_repository(tmp_path)
    reviewed = "apps/travel-map/app/reviewed.py"
    if index_mode == "assume":
        _git(repository, "update-index", "--assume-unchanged", reviewed)
    elif index_mode == "skip":
        _git(repository, "update-index", "--skip-worktree", reviewed)
    else:
        (repository / reviewed).write_text("dirty tracked source\n")

    completed, record = _run_release_gate(
        tmp_path,
        gate,
        docker_config=_protected_docker_config(tmp_path),
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "BLOCKED_DIRTY_RELEASE_SOURCE\n"
    assert not record.exists()


# Production break caught: ambient global Git config and replace refs can make
# HEAD archive different bytes while the printed commit SHA remains unchanged.
def test_release_gate_ignores_global_git_config_and_replace_refs(
    tmp_path: Path,
) -> None:
    repository, gate, _, events_path = _release_gate_repository(tmp_path)
    reviewed_sha = _git(repository, "rev-parse", "HEAD")
    reviewed = repository / "apps/travel-map/app/reviewed.py"
    reviewed.write_text("ambient replacement\n")
    _git(repository, "add", str(reviewed.relative_to(repository)))
    _git(repository, "commit", "-qm", "ambient replacement")
    replacement_sha = _git(repository, "rev-parse", "HEAD")
    _git(repository, "checkout", "-q", "--detach", reviewed_sha)
    _git(repository, "replace", reviewed_sha, replacement_sha)
    fsmonitor_ran = tmp_path / "ambient-fsmonitor-ran"
    fsmonitor = tmp_path / "ambient-fsmonitor.sh"
    _write_executable(
        fsmonitor,
        f"#!/bin/sh\n/usr/bin/touch {fsmonitor_ran}\nexit 0\n",
    )
    global_config = tmp_path / "ambient.gitconfig"
    global_config.write_text(f"[core]\n\tfsmonitor = {fsmonitor}\n")
    os.environ["GIT_CONFIG_GLOBAL"] = str(global_config)
    try:
        completed, _ = _run_release_gate(
            tmp_path,
            gate,
            docker_config=_protected_docker_config(tmp_path),
        )
    finally:
        os.environ.pop("GIT_CONFIG_GLOBAL", None)

    assert completed.returncode == 0
    events = [json.loads(line) for line in events_path.read_text().splitlines()]
    first_uv = next(event for event in events if event["tool"] == "uv")
    assert first_uv["reviewed"] == "reviewed\n"
    assert not fsmonitor_ran.exists()


@pytest.mark.parametrize(
    ("directory_mode", "config_mode"),
    ((0o755, 0o600), (0o700, 0o644)),
)
def test_release_gate_rejects_unprotected_docker_config(
    tmp_path: Path,
    directory_mode: int,
    config_mode: int,
) -> None:
    _, gate, _, _ = _release_gate_repository(tmp_path)
    docker_config = _protected_docker_config(tmp_path)
    docker_config.chmod(directory_mode)
    (docker_config / "config.json").chmod(config_mode)

    completed, record = _run_release_gate(
        tmp_path,
        gate,
        docker_config=docker_config,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "BLOCKED_INVALID_DOCKER_CONFIG\n"
    assert not record.exists()


def test_release_gate_requires_explicit_protected_docker_config(
    tmp_path: Path,
) -> None:
    repository, gate, _, _ = _release_gate_repository(tmp_path)

    completed = subprocess.run(
        ["/bin/sh", str(gate)],
        cwd=repository,
        env={"PATH": "/nonexistent", "NAS_PLATFORM": "linux/amd64"},
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "BLOCKED_INVALID_DOCKER_CONFIG\n"


def test_publisher_requires_explicit_protected_docker_config(
    tmp_path: Path,
) -> None:
    publisher, _, pause_path, _, _, command = _publisher_signal_fixture(tmp_path)
    pause_path.unlink(missing_ok=True)

    completed = subprocess.run(
        command,
        cwd=publisher.parents[4],
        env={"PATH": "/nonexistent"},
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "BLOCKED_INVALID_DOCKER_CONFIG\n"


def test_release_documentation_requires_explicit_protected_docker_config() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "DOCKER_CONFIG=/protected/path/docker-local-authless" in readme
    assert "RELEASE_GATE_IMAGE_RECORD=" in readme
    assert "imageTag=seoul-education-travel-map:release-gate-" in readme
    assert "recordSha256" in readme
    assert "action-time approval" in readme
    assert "DOCKER_CONFIG=/protected/path/docker-ghcr-inline-auth" in readme
    assert "directory must be `0700`" in readme
    assert "`config.json` must be `0600`" in readme
    assert "<approved-image-tag>" in readme
    assert "<approved-record-sha256>" in readme
    assert "do not derive the" in readme
    assert "expected arguments from the record at publish time" in readme
    assert "record path is transport only" in readme
    assert "five-field content tuple" in readme
    assert "relocating identical record bytes" in readme
    assert "<git-sha>-sha256-<local-image-id>" in readme
    assert "content-addressed registry tag" in readme
    assert "not a cryptographic provenance token" in readme
    assert "hostile same-UID code" in readme
    assert "creation-to-first-descriptor-binding" in readme
    assert "mkdirat/openat replacement window for record/staging/lock resources" in readme
    assert "invoking UID must not be shared with untrusted concurrent code" in readme
    assert "separate UID/sandbox or inaccessible pre-provisioned parent is the upgrade path" in readme
