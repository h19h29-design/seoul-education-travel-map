"""Atomic persistence boundary for user and session HMAC records."""

import sqlite3
from datetime import datetime

from app.storage.database import SqliteDatabase
from app.storage.models import (
    SessionRecord,
    StorageIntegrityError,
    UserRecord,
    format_storage_timestamp,
    parse_storage_timestamp,
)


class UserSessionRepository:
    def __init__(self, database: SqliteDatabase) -> None:
        self._database = database

    async def upsert_user_and_insert_session(
        self,
        *,
        subject_hmac: bytes,
        token_hmac: bytes,
        csrf_hmac: bytes,
        now: datetime,
        expires_at: datetime,
    ) -> UserRecord:
        _require_digest(subject_hmac)
        _require_digest(token_hmac)
        _require_digest(csrf_hmac)
        now_text = _format_input_timestamp(now)
        expires_text = _format_input_timestamp(expires_at)
        if expires_text <= now_text:
            raise StorageIntegrityError("storage input is invalid")

        def operation(connection: sqlite3.Connection) -> UserRecord:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "INSERT INTO users(kakao_subject_hmac, created_at, last_login_at) "
                    "VALUES (?, ?, ?) ON CONFLICT(kakao_subject_hmac) DO UPDATE "
                    "SET last_login_at=excluded.last_login_at",
                    (subject_hmac, now_text, now_text),
                )
                row = connection.execute(
                    "SELECT id, created_at, last_login_at FROM users "
                    "WHERE kakao_subject_hmac=?",
                    (subject_hmac,),
                ).fetchone()
                if row is None:
                    raise StorageIntegrityError("storage row is invalid")
                user_record = _user_record_from_row(row)
                connection.execute(
                    "INSERT INTO sessions(token_hash, user_id, csrf_token_hash, "
                    "created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
                    (token_hmac, row[0], csrf_hmac, now_text, expires_text),
                )
                connection.commit()
            except sqlite3.Error:
                connection.rollback()
                raise StorageIntegrityError("storage input is invalid") from None
            except BaseException:
                connection.rollback()
                raise
            return user_record

        return await self._database.write(operation)

    async def resolve_session(
        self, *, token_hmac: bytes, now: datetime
    ) -> SessionRecord | None:
        _require_digest(token_hmac)
        now_text = _format_input_timestamp(now)

        def operation(connection: sqlite3.Connection) -> SessionRecord | None:
            row = connection.execute(
                "SELECT user_id, token_hash, csrf_token_hash, created_at, expires_at "
                "FROM sessions WHERE token_hash=? AND expires_at > ?",
                (token_hmac, now_text),
            ).fetchone()
            if row is None:
                return None
            try:
                return SessionRecord(
                    user_id=row[0],
                    token_hmac=row[1],
                    csrf_hmac=row[2],
                    created_at=parse_storage_timestamp(row[3]),
                    expires_at=parse_storage_timestamp(row[4]),
                )
            except (TypeError, ValueError, StorageIntegrityError):
                raise StorageIntegrityError("storage row is invalid") from None

        return await self._database.read(operation)

    async def revoke_session(self, *, token_hmac: bytes) -> bool:
        _require_digest(token_hmac)

        def operation(connection: sqlite3.Connection) -> bool:
            return (
                connection.execute(
                    "DELETE FROM sessions WHERE token_hash=?", (token_hmac,)
                ).rowcount
                > 0
            )

        return await self._database.write(operation)

    async def revoke_all_sessions(self, *, user_id: int) -> int:
        _require_user_id(user_id)

        def operation(connection: sqlite3.Connection) -> int:
            return connection.execute(
                "DELETE FROM sessions WHERE user_id=?", (user_id,)
            ).rowcount

        return await self._database.write(operation)

    async def delete_user(self, *, user_id: int) -> bool:
        _require_user_id(user_id)

        def operation(connection: sqlite3.Connection) -> bool:
            return (
                connection.execute("DELETE FROM users WHERE id=?", (user_id,)).rowcount
                > 0
            )

        return await self._database.write(operation)


def _require_digest(value: object) -> None:
    if type(value) is not bytes or len(value) != 32:
        raise StorageIntegrityError("storage input is invalid")


def _require_user_id(value: object) -> None:
    if type(value) is not int or value <= 0:
        raise StorageIntegrityError("storage input is invalid")


def _format_input_timestamp(value: object) -> str:
    if type(value) is not datetime:
        raise StorageIntegrityError("storage input is invalid")
    try:
        return format_storage_timestamp(value)
    except StorageIntegrityError:
        raise StorageIntegrityError("storage input is invalid") from None


def _user_record_from_row(row: tuple[object, ...]) -> UserRecord:
    try:
        user_id = row[0]
        created_at = row[1]
        last_login_at = row[2]
        if (
            type(user_id) is not int
            or type(created_at) is not str
            or type(last_login_at) is not str
        ):
            raise TypeError
        return UserRecord(
            id=user_id,
            created_at=parse_storage_timestamp(created_at),
            last_login_at=parse_storage_timestamp(last_login_at),
        )
    except (IndexError, TypeError, ValueError, StorageIntegrityError):
        raise StorageIntegrityError("storage row is invalid") from None
