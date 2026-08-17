"""Hourly expiry cleanup for private user storage."""

import asyncio
import sqlite3
from collections.abc import Callable
from datetime import datetime

from app.storage.database import SqliteDatabase
from app.storage.models import (
    CleanupCounts,
    StorageIntegrityError,
    expected_history_expiry_timestamp,
    format_storage_timestamp,
    parse_storage_timestamp,
)


class RetentionCleaner:
    def __init__(self, database: SqliteDatabase, clock: Callable[[], datetime]) -> None:
        self._database = database
        self._clock = clock

    async def run_once(self, *, now: datetime) -> CleanupCounts:
        now_text = _cleanup_timestamp(now)

        def operation(connection: sqlite3.Connection) -> CleanupCounts:
            connection.execute("BEGIN IMMEDIATE")
            try:
                oauth_attempts = connection.execute(
                    "DELETE FROM oauth_login_attempts WHERE expires_at<=?", (now_text,)
                ).rowcount
                sessions = connection.execute(
                    "DELETE FROM sessions WHERE expires_at<=?", (now_text,)
                ).rowcount
                history = _delete_invalid_or_expired_history(connection, now_text)
                users = connection.execute(
                    "DELETE FROM users WHERE NOT EXISTS("
                    "SELECT 1 FROM user_settings WHERE user_settings.user_id=users.id"
                    ") AND NOT EXISTS("
                    "SELECT 1 FROM sessions WHERE sessions.user_id=users.id"
                    ") AND NOT EXISTS("
                    "SELECT 1 FROM calculation_history "
                    "WHERE calculation_history.user_id=users.id)",
                ).rowcount
                connection.commit()
                return CleanupCounts(
                    oauth_attempts=oauth_attempts,
                    sessions=sessions,
                    history=history,
                    users=users,
                )
            except sqlite3.Error:
                connection.rollback()
                raise StorageIntegrityError("storage cleanup is invalid") from None
            except BaseException:
                connection.rollback()
                raise

        counts = await self._database.write(operation)
        await self._database.checkpoint_truncate()
        return counts

    async def run_forever(self, *, interval_seconds: int = 3600) -> None:
        if type(interval_seconds) is not int or interval_seconds <= 0:
            raise StorageIntegrityError("storage input is invalid")
        while True:
            await self.run_once(now=_trusted_now(self._clock))
            await asyncio.sleep(interval_seconds)


def _cleanup_timestamp(value: object) -> str:
    if type(value) is not datetime:
        raise StorageIntegrityError("storage input is invalid")
    try:
        return format_storage_timestamp(value)
    except StorageIntegrityError:
        raise StorageIntegrityError("storage input is invalid") from None


def _trusted_now(clock: Callable[[], datetime]) -> datetime:
    try:
        value = clock()
        if type(value) is not datetime:
            raise TypeError
        return parse_storage_timestamp(format_storage_timestamp(value))
    except (TypeError, ValueError, StorageIntegrityError):
        raise StorageIntegrityError("storage input is invalid") from None


def _delete_invalid_or_expired_history(
    connection: sqlite3.Connection, now_text: str
) -> int:
    rows = connection.execute(
        "SELECT rowid, created_at, expires_at FROM calculation_history"
    ).fetchall()
    deleted = 0
    for rowid, created_at, expires_at in rows:
        try:
            expected_expiry = (
                expected_history_expiry_timestamp(created_at)
                if type(created_at) is str
                else None
            )
        except StorageIntegrityError:
            expected_expiry = None
        if (
            type(expires_at) is not str
            or expected_expiry is None
            or expires_at != expected_expiry
            or expected_expiry <= now_text
        ):
            deleted += connection.execute(
                "DELETE FROM calculation_history WHERE rowid=?", (rowid,)
            ).rowcount
    return deleted
