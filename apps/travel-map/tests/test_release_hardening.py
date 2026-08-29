import hashlib
import io
import json
import os
import shutil
import signal
import socket
import stat
import subprocess
import tarfile
import time
from pathlib import Path

import pytest

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
        _wait_for_release_gate_anchor(process, entered)
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
        deadline = time.monotonic() + 30
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
    release_gate_marker: Path | None = None,
    authority_open_probe: Path | None = None,
    private_launcher_failure_probe: Path | None = None,
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
            '                    TRAVEL_MAP_PUBLISH_LAUNCHER_SHA256="$publisher_launcher_hash" \\\n',
            (
                "                    TRAVEL_MAP_PUBLISH_LAUNCHER_SHA256="
                + '"'
                + "0" * 64
                + '" \\\n'
            ),
        )
    publisher_source = _replace_once(
        publisher_source,
        "image_tag=$expected_image_tag\n",
        inner_body + "\nimage_tag=$expected_image_tag\n",
    )
    if cleanup_pause is not None:
        cleanup_pause_path, cleanup_entered = cleanup_pause
        publisher_source = _replace_once(
            publisher_source,
            "    shutil.rmtree(private_root)\n",
            (
                f"    Path({str(cleanup_entered)!r}).write_text(\n"
                '        str(private_root), encoding="utf-8"\n'
                "    )\n"
                f"    while Path({str(cleanup_pause_path)!r}).exists():\n"
                "        time.sleep(0.01)\n"
                "    shutil.rmtree(private_root)\n"
            ),
        )
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
) -> tuple[Path, Path, Path, Path, str, list[str]]:
    cleanup_pause = tmp_path / "publisher-cleanup.pause"
    cleanup_entered = tmp_path / "publisher-cleanup.entered"
    digest = "ghcr.io/h19h29-design/seoul-education-travel-map@sha256:" + "a" * 64
    publisher, docker_config, _, _, git_sha, command = _publisher_signal_fixture(
        tmp_path,
        inner_body=f"printf '%s\\n' {digest!r}\nexit 0",
        cleanup_pause=(cleanup_pause, cleanup_entered),
    )
    return (
        publisher,
        docker_config,
        cleanup_pause,
        cleanup_entered,
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
    private_root: Path | None = None
    survived = False
    try:
        deadline = time.monotonic() + 30
        while not entered_path.exists() and process.poll() is None:
            if time.monotonic() >= deadline:
                raise AssertionError("publisher did not reach the signal boundary")
            time.sleep(0.05)
        assert process.poll() is None
        private_home = Path(tmp_path / "publisher-home.txt").read_text()
        private_root = Path(private_home).parent
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
    assert not (
        Path("/tmp") / f"travel-map-publish-locks-{os.getuid()}" / git_sha
    ).exists()


@pytest.mark.parametrize(
    "termination_signal",
    (signal.SIGHUP, signal.SIGINT, signal.SIGTERM),
)
def test_publisher_withholds_digest_during_cleanup_signal_window(
    tmp_path: Path,
    termination_signal: signal.Signals,
) -> None:
    publisher, docker_config, pause_path, entered_path, _git_sha, command = (
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
        private_root = Path(entered_path.read_text(encoding="utf-8"))
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
    assert stdout == ""
    assert "Traceback" not in stderr
    assert private_root is not None and not private_root.exists()


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
    try:
        deadline = time.monotonic() + 90
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
        _wait_for_release_gate_anchor(process, entered_path)
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
        _wait_for_release_gate_anchor(process, entered_path)
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
        _wait_for_release_gate_anchor(process, entered)
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
        assert not launcher_root.exists()
    finally:
        if launcher_root.exists():
            shutil.rmtree(launcher_root)


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
