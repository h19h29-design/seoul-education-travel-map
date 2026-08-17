import os
import sqlite3
import stat
import subprocess
import sys
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from app.storage import database as database_module
from app.storage.database import SqliteDatabase
from app.storage.models import (
    StorageIntegrityError,
    format_storage_timestamp,
    parse_storage_timestamp,
)


# Break caught: a migration creates a database with permissive modes or accepts
# noncanonical timestamp forms that would make lexicographic retention unsafe.
def test_v1_migration_is_idempotent_private_and_rejects_mixed_timestamps(
    tmp_path: Path,
) -> None:
    path = tmp_path / "private" / "travel-map.sqlite3"
    database = SqliteDatabase(path)

    assert database.migrate() == 1
    assert database.migrate() == 1
    database.verify_current_schema()

    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(StorageIntegrityError, match="timestamp"):
        parse_storage_timestamp("2026-08-17T00:00:00Z")
    with pytest.raises(StorageIntegrityError, match="timestamp"):
        parse_storage_timestamp("2026-08-17T09:00:00.000000+09:00")
    with pytest.raises(StorageIntegrityError, match="timezone"):
        format_storage_timestamp(datetime(2026, 8, 17, tzinfo=UTC).replace(tzinfo=None))


def test_v1_schema_is_fixed_and_enforces_digest_version_and_foreign_keys(
    tmp_path: Path,
) -> None:
    path = tmp_path / "private" / "travel-map.sqlite3"
    database = SqliteDatabase(path)
    database.migrate()

    expected_columns = {
        "schema_migrations": ("version", "applied_at"),
        "users": ("id", "kakao_subject_hmac", "created_at", "last_login_at"),
        "oauth_login_attempts": (
            "attempt_hash",
            "state_hash",
            "nonce_hash",
            "created_at",
            "expires_at",
            "consumed_at",
        ),
        "sessions": (
            "token_hash",
            "user_id",
            "csrf_token_hash",
            "created_at",
            "expires_at",
        ),
        "calculation_history": (
            "id",
            "user_id",
            "created_at",
            "expires_at",
            "encrypted_input",
            "encrypted_summary",
            "encryption_version",
        ),
        "user_settings": (
            "user_id",
            "encrypted_payload",
            "encryption_version",
            "updated_at",
        ),
    }
    expected_indexes = {
        "idx_oauth_login_attempts_expires_at",
        "idx_sessions_expires_at",
        "idx_calculation_history_expires_at",
        "idx_calculation_history_user_created_id",
    }

    with sqlite3.connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert tables == set(expected_columns)
        for table, columns in expected_columns.items():
            assert (
                tuple(
                    row[1] for row in connection.execute(f"PRAGMA table_info({table})")
                )
                == columns
            )
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name NOT LIKE 'sqlite_autoindex%'"
            )
        }
        assert indexes == expected_indexes

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO users(kakao_subject_hmac, created_at, last_login_at) "
                "VALUES (?, ?, ?)",
                (
                    b"h" * 31,
                    "2026-08-17T00:00:00.000000Z",
                    "2026-08-17T00:00:00.000000Z",
                ),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO calculation_history("
                "id, user_id, created_at, expires_at, encrypted_input, "
                "encrypted_summary, encryption_version) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "history",
                    1,
                    "2026-08-17T00:00:00.000000Z",
                    "2026-08-24T00:00:00.000000Z",
                    b"input",
                    b"summary",
                    0,
                ),
            )


def test_v1_migration_rolls_back_a_failed_schema_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "private" / "travel-map.sqlite3"
    statements = database_module._MIGRATION_STATEMENTS + (
        "CREATE TABLE users (duplicate_column INTEGER)",
    )
    monkeypatch.setattr(database_module, "_MIGRATION_STATEMENTS", statements)

    with pytest.raises(sqlite3.OperationalError, match="already exists"):
        SqliteDatabase(path).migrate()

    with sqlite3.connect(path) as connection:
        assert not tuple(
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        )


def test_migration_rejects_a_preexisting_database_containing_only_a_view(
    tmp_path: Path,
) -> None:
    private_directory = tmp_path / "private"
    private_directory.mkdir(mode=0o700)
    path = private_directory / "travel-map.sqlite3"
    path.touch(mode=0o600)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE VIEW untrusted_view AS SELECT 1 AS value")

    with pytest.raises(StorageIntegrityError, match="storage schema is invalid"):
        SqliteDatabase(path).migrate()


def test_schema_verify_rejects_noninternal_views_and_triggers(tmp_path: Path) -> None:
    path = tmp_path / "private" / "travel-map.sqlite3"
    database = SqliteDatabase(path)
    database.migrate()
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE VIEW untrusted_view AS SELECT id FROM users")
        connection.execute(
            "CREATE TRIGGER untrusted_trigger AFTER INSERT ON users BEGIN SELECT 1; END"
        )

    with pytest.raises(StorageIntegrityError, match="storage schema is invalid"):
        database.verify_current_schema()


@pytest.mark.asyncio
async def test_async_connection_lifecycle_enables_every_required_pragma(
    tmp_path: Path,
) -> None:
    path = tmp_path / "private" / "travel-map.sqlite3"
    database = SqliteDatabase(path)
    database.migrate()
    captured: list[sqlite3.Connection] = []

    def inspect(connection: sqlite3.Connection) -> dict[str, int | str]:
        captured.append(connection)
        return {
            "foreign_keys": connection.execute("PRAGMA foreign_keys").fetchone()[0],
            "journal_mode": connection.execute("PRAGMA journal_mode").fetchone()[0],
            "busy_timeout": connection.execute("PRAGMA busy_timeout").fetchone()[0],
            "secure_delete": connection.execute("PRAGMA secure_delete").fetchone()[0],
            "synchronous": connection.execute("PRAGMA synchronous").fetchone()[0],
            "trusted_schema": connection.execute("PRAGMA trusted_schema").fetchone()[0],
        }

    assert await database.read(inspect) == {
        "foreign_keys": 1,
        "journal_mode": "wal",
        "busy_timeout": 5000,
        "secure_delete": 1,
        "synchronous": 1,
        "trusted_schema": 0,
    }
    with pytest.raises(sqlite3.ProgrammingError, match="thread|closed"):
        captured[0].execute("SELECT 1")


@pytest.mark.asyncio
async def test_async_writes_enforce_foreign_keys_and_keep_wal_artifacts_private(
    tmp_path: Path,
) -> None:
    path = tmp_path / "private" / "travel-map.sqlite3"
    database = SqliteDatabase(path)
    database.migrate()

    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        await database.write(
            lambda connection: connection.execute(
                "INSERT INTO sessions("
                "token_hash, user_id, csrf_token_hash, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    b"t" * 32,
                    999,
                    b"c" * 32,
                    "2026-08-17T00:00:00.000000Z",
                    "2026-08-24T00:00:00.000000Z",
                ),
            )
        )

    await database.write(
        lambda connection: connection.execute(
            "INSERT INTO users(kakao_subject_hmac, created_at, last_login_at) "
            "VALUES (?, ?, ?)",
            (
                b"h" * 32,
                "2026-08-17T00:00:00.000000Z",
                "2026-08-17T00:00:00.000000Z",
            ),
        )
    )
    for artifact in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        if artifact.exists():
            assert stat.S_IMODE(artifact.stat().st_mode) & 0o077 == 0


def test_runtime_verify_rejects_missing_future_and_altered_schema(
    tmp_path: Path,
) -> None:
    path = tmp_path / "private" / "travel-map.sqlite3"
    database = SqliteDatabase(path)

    with pytest.raises(StorageIntegrityError, match="schema"):
        database.verify_current_schema()

    database.migrate()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (2, ?)",
            ("2026-08-17T00:00:00.000000Z",),
        )
    with pytest.raises(StorageIntegrityError, match="schema"):
        database.verify_current_schema()

    path.unlink()
    database = SqliteDatabase(path)
    database.migrate()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP INDEX idx_sessions_expires_at")
    with pytest.raises(StorageIntegrityError, match="schema"):
        database.verify_current_schema()


def test_schema_verify_rejects_an_untrusted_index_name_with_fixed_error(
    tmp_path: Path,
) -> None:
    path = tmp_path / "private" / "travel-map.sqlite3"
    database = SqliteDatabase(path)
    database.migrate()
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE INDEX "untrusted index" ON users(created_at)')

    with pytest.raises(StorageIntegrityError, match="storage schema is invalid"):
        database.verify_current_schema()


@pytest.mark.parametrize(
    ("statement", "parameters"),
    (
        (
            (
                "INSERT INTO users(kakao_subject_hmac, created_at, last_login_at) "
                "VALUES (?, ?, ?)"
            ),
            (
                "h" * 32,
                "2026-08-17T00:00:00.000000Z",
                "2026-08-17T00:00:00.000000Z",
            ),
        ),
        (
            (
                "INSERT INTO calculation_history("
                "id, user_id, created_at, expires_at, encrypted_input, "
                "encrypted_summary, encryption_version) VALUES (?, ?, ?, ?, ?, ?, ?)"
            ),
            (
                "history",
                1,
                "2026-08-17T00:00:00.000000Z",
                "2026-08-24T00:00:00.000000Z",
                b"input",
                b"summary",
                1.5,
            ),
        ),
    ),
)
def test_schema_rejects_non_blob_digests_and_noninteger_encryption_versions(
    tmp_path: Path, statement: str, parameters: tuple[object, ...]
) -> None:
    path = tmp_path / "private" / "travel-map.sqlite3"
    SqliteDatabase(path).migrate()

    with sqlite3.connect(path) as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute(statement, parameters)


def test_migration_prepares_directories_and_database_under_private_umask(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "nested" / "private" / "travel-map.sqlite3"
    active_umask_contexts = 0
    original_mkdir = Path.mkdir
    original_open = database_module.os.open

    @contextmanager
    def tracked_private_umask() -> object:
        nonlocal active_umask_contexts
        active_umask_contexts += 1
        try:
            yield
        finally:
            active_umask_contexts -= 1

    def checked_mkdir(
        directory: Path,
        mode: int = 0o777,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        if directory == path.parent:
            assert active_umask_contexts > 0
        original_mkdir(directory, mode=mode, parents=parents, exist_ok=exist_ok)

    def checked_open(
        name: os.PathLike[str] | str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if Path(name) == path:
            assert active_umask_contexts > 0
        return original_open(name, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(database_module, "_private_umask", tracked_private_umask)
    monkeypatch.setattr(Path, "mkdir", checked_mkdir)
    monkeypatch.setattr(database_module.os, "open", checked_open)

    assert SqliteDatabase(path).migrate() == 1


def test_schema_verify_rejects_invalid_calendar_timestamp_rows(tmp_path: Path) -> None:
    path = tmp_path / "private" / "travel-map.sqlite3"
    database = SqliteDatabase(path)
    database.migrate()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO users(kakao_subject_hmac, created_at, last_login_at) "
            "VALUES (?, ?, ?)",
            (
                b"h" * 32,
                "2026-99-99T99:99:99.000000Z",
                "2026-08-17T00:00:00.000000Z",
            ),
        )

    with pytest.raises(StorageIntegrityError, match="storage timestamp is invalid"):
        database.verify_current_schema()


def test_migration_refuses_insecure_existing_directory_or_database(
    tmp_path: Path,
) -> None:
    insecure_directory = tmp_path / "insecure-directory"
    insecure_directory.mkdir(mode=0o700)
    insecure_directory.chmod(0o755)
    with pytest.raises(StorageIntegrityError, match="private"):
        SqliteDatabase(insecure_directory / "travel-map.sqlite3").migrate()

    private_directory = tmp_path / "private"
    private_directory.mkdir(mode=0o700)
    path = private_directory / "travel-map.sqlite3"
    path.touch(mode=0o600)
    path.chmod(0o644)
    with pytest.raises(StorageIntegrityError, match="private"):
        SqliteDatabase(path).migrate()


@pytest.mark.parametrize(
    "value",
    (
        "2026-08-17T00:00:00Z",
        "2026-08-17T00:00:00.00000Z",
        "2026-08-17T00:00:00.0000000Z",
        "2026-08-17T00:00:00.000000+00:00",
        "2026-08-17T09:00:00.000000+09:00",
        "2026-08-17 00:00:00.000000Z",
        "2026-08-17T00:00:00.000000z",
        "not-a-timestamp",
        1,
    ),
)
def test_timestamp_parser_rejects_all_noncanonical_forms(value: object) -> None:
    with pytest.raises(StorageIntegrityError, match="timestamp"):
        parse_storage_timestamp(value)  # type: ignore[arg-type]


def test_timestamp_formatter_normalizes_to_canonical_utc_microseconds() -> None:
    value = datetime.fromisoformat("2026-08-17T09:00:00+09:00")

    assert format_storage_timestamp(value) == "2026-08-17T00:00:00.000000Z"
    assert parse_storage_timestamp("2026-08-17T00:00:00.000000Z").tzinfo is not None


def test_migration_cli_prints_only_status_and_schema_version(tmp_path: Path) -> None:
    path = tmp_path / "private" / "travel-map.sqlite3"
    project_directory = Path(__file__).parents[2]
    environment = {**os.environ, "PYTHONWARNINGS": "error"}

    migrate = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.storage.migrations",
            "migrate",
            "--database",
            str(path),
        ],
        capture_output=True,
        check=False,
        cwd=project_directory,
        env=environment,
        text=True,
    )
    assert migrate.returncode == 0, migrate.stderr
    assert migrate.stdout == "migrate: schema version 1\n"
    assert migrate.stderr == ""

    verify = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.storage.migrations",
            "verify",
            "--database",
            str(path),
        ],
        capture_output=True,
        check=False,
        cwd=project_directory,
        env=environment,
        text=True,
    )
    assert verify.returncode == 0, verify.stderr
    assert verify.stdout == "verify: schema version 1\n"
    assert verify.stderr == ""
