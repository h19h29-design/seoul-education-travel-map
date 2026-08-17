"""Private SQLite lifecycle and the fixed version-one schema."""

import asyncio
import os
import sqlite3
import stat
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

from app.storage.models import (
    StorageIntegrityError,
    format_storage_timestamp,
    parse_storage_timestamp,
)

SCHEMA_VERSION = 1
_T = TypeVar("_T")
_PRIVATE_UMASK_LOCK = threading.RLock()

_EXPECTED_TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
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

_EXPECTED_INDEXES: dict[str, tuple[str, ...]] = {
    "idx_oauth_login_attempts_expires_at": ("expires_at",),
    "idx_sessions_expires_at": ("expires_at",),
    "idx_calculation_history_expires_at": ("expires_at",),
    "idx_calculation_history_user_created_id": ("user_id", "created_at", "id"),
}

_TABLE_INFO_QUERIES = (
    ("schema_migrations", "PRAGMA table_info(schema_migrations)"),
    ("users", "PRAGMA table_info(users)"),
    ("oauth_login_attempts", "PRAGMA table_info(oauth_login_attempts)"),
    ("sessions", "PRAGMA table_info(sessions)"),
    ("calculation_history", "PRAGMA table_info(calculation_history)"),
    ("user_settings", "PRAGMA table_info(user_settings)"),
)

_INDEX_XINFO_QUERIES = (
    (
        "idx_oauth_login_attempts_expires_at",
        "PRAGMA index_xinfo(idx_oauth_login_attempts_expires_at)",
    ),
    ("idx_sessions_expires_at", "PRAGMA index_xinfo(idx_sessions_expires_at)"),
    (
        "idx_calculation_history_expires_at",
        "PRAGMA index_xinfo(idx_calculation_history_expires_at)",
    ),
    (
        "idx_calculation_history_user_created_id",
        "PRAGMA index_xinfo(idx_calculation_history_user_created_id)",
    ),
)

_FOREIGN_KEY_QUERIES = (
    "PRAGMA foreign_key_list(sessions)",
    "PRAGMA foreign_key_list(calculation_history)",
    "PRAGMA foreign_key_list(user_settings)",
)

_TIMESTAMP_VALUE_QUERIES = (
    "SELECT applied_at FROM schema_migrations",
    "SELECT created_at, last_login_at FROM users",
    "SELECT created_at, expires_at, consumed_at FROM oauth_login_attempts",
    "SELECT created_at, expires_at FROM sessions",
    "SELECT created_at, expires_at FROM calculation_history",
    "SELECT updated_at FROM user_settings",
)

_MIGRATION_STATEMENTS = (
    """
    CREATE TABLE schema_migrations (
        version INTEGER PRIMARY KEY,
        applied_at TEXT NOT NULL CHECK(
            length(applied_at) = 27 AND applied_at GLOB
            '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'
        )
    )
    """,
    """
    CREATE TABLE users (
        id INTEGER PRIMARY KEY,
        kakao_subject_hmac BLOB UNIQUE NOT NULL CHECK(
            typeof(kakao_subject_hmac) = 'blob' AND length(kakao_subject_hmac) = 32
        ),
        created_at TEXT NOT NULL CHECK(
            length(created_at) = 27 AND created_at GLOB
            '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'
        ),
        last_login_at TEXT NOT NULL CHECK(
            length(last_login_at) = 27 AND last_login_at GLOB
            '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'
        )
    )
    """,
    """
    CREATE TABLE oauth_login_attempts (
        attempt_hash BLOB PRIMARY KEY CHECK(
            typeof(attempt_hash) = 'blob' AND length(attempt_hash) = 32
        ),
        state_hash BLOB NOT NULL CHECK(
            typeof(state_hash) = 'blob' AND length(state_hash) = 32
        ),
        nonce_hash BLOB NOT NULL CHECK(
            typeof(nonce_hash) = 'blob' AND length(nonce_hash) = 32
        ),
        created_at TEXT NOT NULL CHECK(
            length(created_at) = 27 AND created_at GLOB
            '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'
        ),
        expires_at TEXT NOT NULL CHECK(
            length(expires_at) = 27 AND expires_at GLOB
            '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'
        ),
        consumed_at TEXT CHECK(
            consumed_at IS NULL OR (length(consumed_at) = 27 AND consumed_at GLOB
            '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z')
        )
    )
    """,
    """
    CREATE TABLE sessions (
        token_hash BLOB PRIMARY KEY CHECK(
            typeof(token_hash) = 'blob' AND length(token_hash) = 32
        ),
        user_id INTEGER NOT NULL,
        csrf_token_hash BLOB NOT NULL CHECK(
            typeof(csrf_token_hash) = 'blob' AND length(csrf_token_hash) = 32
        ),
        created_at TEXT NOT NULL CHECK(
            length(created_at) = 27 AND created_at GLOB
            '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'
        ),
        expires_at TEXT NOT NULL CHECK(
            length(expires_at) = 27 AND expires_at GLOB
            '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'
        ),
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE calculation_history (
        id TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        created_at TEXT NOT NULL CHECK(
            length(created_at) = 27 AND created_at GLOB
            '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'
        ),
        expires_at TEXT NOT NULL CHECK(
            length(expires_at) = 27 AND expires_at GLOB
            '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'
        ),
        encrypted_input BLOB NOT NULL,
        encrypted_summary BLOB NOT NULL,
        encryption_version INTEGER NOT NULL CHECK(
            typeof(encryption_version) = 'integer' AND encryption_version > 0
        ),
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE user_settings (
        user_id INTEGER PRIMARY KEY,
        encrypted_payload BLOB NOT NULL,
        encryption_version INTEGER NOT NULL CHECK(
            typeof(encryption_version) = 'integer' AND encryption_version > 0
        ),
        updated_at TEXT NOT NULL CHECK(
            length(updated_at) = 27 AND updated_at GLOB
            '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'
        ),
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX idx_oauth_login_attempts_expires_at ON oauth_login_attempts(expires_at)",
    "CREATE INDEX idx_sessions_expires_at ON sessions(expires_at)",
    "CREATE INDEX idx_calculation_history_expires_at ON calculation_history(expires_at)",
    (
        "CREATE INDEX idx_calculation_history_user_created_id "
        "ON calculation_history(user_id, created_at DESC, id DESC)"
    ),
)
_SCHEMA_OBJECT_NAMES = (
    "schema_migrations",
    "users",
    "oauth_login_attempts",
    "sessions",
    "calculation_history",
    "user_settings",
    "idx_oauth_login_attempts_expires_at",
    "idx_sessions_expires_at",
    "idx_calculation_history_expires_at",
    "idx_calculation_history_user_created_id",
)


def _normalize_ddl(value: str) -> str:
    return "".join(value.split()).upper()


_EXPECTED_SCHEMA_DDL = {
    name: _normalize_ddl(statement)
    for name, statement in zip(_SCHEMA_OBJECT_NAMES, _MIGRATION_STATEMENTS, strict=True)
}


@contextmanager
def _private_umask() -> Generator[None, None, None]:
    """Apply the required process-global umask without concurrent leakage."""
    with _PRIVATE_UMASK_LOCK:
        previous = os.umask(0o077)
        try:
            yield
        finally:
            os.umask(previous)


class SqliteDatabase:
    """Own SQLite connections and keep its schema/modes fail-closed."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def migrate(self) -> int:
        """Apply the only supported schema version as an explicit operator action."""
        self._prepare_path(create=True)
        with self._connection(require_existing=True) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                if self._schema_object_names(connection):
                    self._verify_schema(connection)
                else:
                    for statement in _MIGRATION_STATEMENTS:
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                        (SCHEMA_VERSION, format_storage_timestamp(datetime.now(UTC))),
                    )
                    self._verify_schema(connection)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return SCHEMA_VERSION

    def verify_current_schema(self) -> None:
        """Verify, but never alter, the existing runtime database schema."""
        self._prepare_path(create=False)
        with self._connection(require_existing=True) as connection:
            self._verify_schema(connection)

    async def read(self, operation: Callable[[sqlite3.Connection], _T]) -> _T:
        """Run a read callback with a fresh, private connection in a worker thread."""
        return await asyncio.to_thread(self._read_in_thread, operation)

    async def write(self, operation: Callable[[sqlite3.Connection], _T]) -> _T:
        """Run a write callback with a fresh, private connection in a worker thread."""
        return await asyncio.to_thread(self._write_in_thread, operation)

    async def checkpoint_truncate(self) -> None:
        """Checkpoint user-data WAL pages after a completed maintenance action."""
        await asyncio.to_thread(self._checkpoint_truncate_in_thread)

    def _read_in_thread(self, operation: Callable[[sqlite3.Connection], _T]) -> _T:
        with self._connection(require_existing=True) as connection:
            self._verify_schema(connection)
            return operation(connection)

    def _write_in_thread(self, operation: Callable[[sqlite3.Connection], _T]) -> _T:
        with self._connection(require_existing=True) as connection:
            self._verify_schema(connection)
            try:
                result = operation(connection)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            return result

    def _checkpoint_truncate_in_thread(self) -> None:
        with self._connection(require_existing=True) as connection:
            self._verify_schema(connection)
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()

    @contextmanager
    def _connection(
        self, *, require_existing: bool
    ) -> Generator[sqlite3.Connection, None, None]:
        self._prepare_path(create=not require_existing)
        with _private_umask():
            connection = sqlite3.connect(self._path)
            try:
                self._configure_connection(connection)
                self._validate_private_artifacts()
                yield connection
            finally:
                try:
                    self._validate_private_artifacts()
                finally:
                    connection.close()

    def _prepare_path(self, *, create: bool) -> None:
        with _private_umask():
            self._prepare_path_with_private_umask(create=create)

    def _prepare_path_with_private_umask(self, *, create: bool) -> None:
        parent = self._path.parent
        if not parent.exists():
            try:
                parent.mkdir(mode=0o700, parents=True)
                os.chmod(parent, 0o700)
            except OSError as error:
                raise StorageIntegrityError(
                    "storage private directory is invalid"
                ) from error
        self._validate_private_directory(parent)

        try:
            path_status = os.lstat(self._path)
        except FileNotFoundError:
            if not create:
                raise StorageIntegrityError("storage schema is missing") from None
            try:
                descriptor = os.open(
                    self._path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
            except OSError as error:
                raise StorageIntegrityError(
                    "storage private database is invalid"
                ) from error
            else:
                os.close(descriptor)
                os.chmod(self._path, 0o600)
        else:
            self._validate_private_file(self._path, path_status)

    @staticmethod
    def _validate_private_directory(path: Path) -> None:
        try:
            status = os.lstat(path)
        except OSError as error:
            raise StorageIntegrityError(
                "storage private directory is invalid"
            ) from error
        mode = stat.S_IMODE(status.st_mode)
        if (
            stat.S_ISLNK(status.st_mode)
            or not stat.S_ISDIR(status.st_mode)
            or mode != 0o700
        ):
            raise StorageIntegrityError("storage private directory is invalid")

    @staticmethod
    def _validate_private_file(
        path: Path, status: os.stat_result | None = None
    ) -> None:
        try:
            resolved_status = status if status is not None else os.lstat(path)
        except OSError as error:
            raise StorageIntegrityError(
                "storage private database is invalid"
            ) from error
        if (
            stat.S_ISLNK(resolved_status.st_mode)
            or not stat.S_ISREG(resolved_status.st_mode)
            or stat.S_IMODE(resolved_status.st_mode) & 0o077
        ):
            raise StorageIntegrityError("storage private database is invalid")

    def _validate_private_artifacts(self) -> None:
        self._validate_private_file(self._path)
        for suffix in ("-wal", "-shm"):
            artifact = Path(f"{self._path}{suffix}")
            try:
                artifact_status = os.lstat(artifact)
            except FileNotFoundError:
                continue
            self._validate_private_file(artifact, artifact_status)

    @staticmethod
    def _configure_connection(connection: sqlite3.Connection) -> None:
        connection.execute("PRAGMA foreign_keys=ON")
        journal_mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()
        if journal_mode is None or journal_mode[0] != "wal":
            raise StorageIntegrityError("storage pragma configuration is invalid")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA secure_delete=ON")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA trusted_schema=OFF")

    @staticmethod
    def _schema_object_names(connection: sqlite3.Connection) -> tuple[str, ...]:
        return tuple(
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type IN ('table', 'index', 'view', 'trigger') "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        )

    @staticmethod
    def _verify_schema(connection: sqlite3.Connection) -> None:
        if set(SqliteDatabase._schema_object_names(connection)) != set(
            _SCHEMA_OBJECT_NAMES
        ):
            raise StorageIntegrityError("storage schema is invalid")

        versions = tuple(
            row[0]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        )
        if versions != (SCHEMA_VERSION,):
            raise StorageIntegrityError("storage schema is invalid")

        for table, statement in _TABLE_INFO_QUERIES:
            columns = tuple(row[1] for row in connection.execute(statement))
            if columns != _EXPECTED_TABLE_COLUMNS[table]:
                raise StorageIntegrityError("storage schema is invalid")

        index_names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name NOT LIKE 'sqlite_autoindex%'"
            )
        }
        if index_names != set(_EXPECTED_INDEXES):
            raise StorageIntegrityError("storage schema is invalid")

        indexes = {
            index_name: tuple(
                item[2] for item in connection.execute(statement) if item[5] == 1
            )
            for index_name, statement in _INDEX_XINFO_QUERIES
        }
        if indexes != _EXPECTED_INDEXES:
            raise StorageIntegrityError("storage schema is invalid")

        for statement in _FOREIGN_KEY_QUERIES:
            foreign_keys = tuple(connection.execute(statement))
            if foreign_keys != (
                (0, 0, "users", "user_id", "id", "NO ACTION", "CASCADE", "NONE"),
            ):
                raise StorageIntegrityError("storage schema is invalid")

        schema_ddl = {
            row[0]: _normalize_ddl(row[1])
            for row in connection.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type IN ('table', 'index') AND sql IS NOT NULL"
            )
        }
        if schema_ddl != _EXPECTED_SCHEMA_DDL:
            raise StorageIntegrityError("storage schema is invalid")

        for statement in _TIMESTAMP_VALUE_QUERIES:
            for row in connection.execute(statement):
                for value in row:
                    if value is not None:
                        parse_storage_timestamp(value)
