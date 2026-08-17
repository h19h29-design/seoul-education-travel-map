from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from app.storage.crypto import PayloadCipher
from app.storage.database import SqliteDatabase
from app.storage.history import (
    HistoryRecalculationDraft,
    HistoryRepository,
    HistorySummary,
)
from app.storage.models import (
    DEFAULT_USER_SETTINGS,
    CleanupCounts,
    format_storage_timestamp,
)
from app.storage.retention import RetentionCleaner
from app.storage.user_settings import UserSettingsRepository
from app.storage.users import UserSessionRepository
from app.trips.models import TripPattern


# Break caught: cleanup leaves expired OAuth/session/history records behind,
# removes a user who still has settings, or skips the physical WAL truncate.
@pytest.mark.asyncio
async def test_retention_removes_expired_records_orphans_and_truncates_wal(
    tmp_path: Path,
) -> None:
    path = tmp_path / "private" / "travel-map.sqlite3"
    issued_at = datetime(2026, 8, 17, tzinfo=UTC)
    cleanup_at = issued_at + timedelta(days=7)
    database = SqliteDatabase(path)
    database.migrate()
    users = UserSessionRepository(database)
    cipher = PayloadCipher(keys={1: b"e" * 32})
    orphan = await users.upsert_user_and_insert_session(
        subject_hmac=b"a" * 32,
        token_hmac=b"b" * 32,
        csrf_hmac=b"c" * 32,
        now=issued_at,
        expires_at=cleanup_at,
    )
    retained = await users.upsert_user_and_insert_session(
        subject_hmac=b"d" * 32,
        token_hmac=b"e" * 32,
        csrf_hmac=b"f" * 32,
        now=issued_at,
        expires_at=cleanup_at,
    )
    await UserSettingsRepository(database, cipher).replace(
        user_id=retained.id, settings=DEFAULT_USER_SETTINGS
    )
    history = HistoryRepository(database, cipher, clock=lambda: issued_at)
    await history.create(
        user_id=orphan.id,
        draft=HistoryRecalculationDraft(
            origin_site_id="safe-origin",
            origin_name="safe origin",
            destination_name="safe destination",
            destination_address="safe address",
            trip_pattern=TripPattern.ROUND_TRIP,
            starts_at=issued_at,
            ends_at=issued_at + timedelta(hours=5),
        ),
        summary=HistorySummary(
            classification="LOCAL",
            allowance_status="ESTIMATED",
            allowance_krw=20_000,
            route_legs=(),
            rule_set_id="2025-local-travel",
            effective_from="2025-01-01",
        ),
    )
    timestamp = format_storage_timestamp(issued_at)
    expiry = format_storage_timestamp(cleanup_at)
    await database.write(
        lambda connection: connection.execute(
            "INSERT INTO oauth_login_attempts("
            "attempt_hash, state_hash, nonce_hash, created_at, expires_at, consumed_at) "
            "VALUES (?, ?, ?, ?, ?, NULL)",
            (b"o" * 32, b"p" * 32, b"q" * 32, timestamp, expiry),
        )
    )

    counts = await RetentionCleaner(database, clock=lambda: cleanup_at).run_once(
        now=cleanup_at
    )

    assert counts == CleanupCounts(oauth_attempts=1, sessions=2, history=1, users=1)
    assert (
        await database.read(
            lambda connection: connection.execute(
                "SELECT COUNT(*) FROM oauth_login_attempts"
            ).fetchone()[0]
        )
        == 0
    )
    assert (
        await database.read(
            lambda connection: connection.execute(
                "SELECT COUNT(*) FROM sessions"
            ).fetchone()[0]
        )
        == 0
    )
    assert (
        await database.read(
            lambda connection: connection.execute(
                "SELECT COUNT(*) FROM calculation_history"
            ).fetchone()[0]
        )
        == 0
    )
    assert (
        await database.read(
            lambda connection: connection.execute(
                "SELECT COUNT(*) FROM users"
            ).fetchone()[0]
        )
        == 1
    )
    wal = Path(f"{path}-wal")
    assert not wal.exists() or wal.stat().st_size == 0
