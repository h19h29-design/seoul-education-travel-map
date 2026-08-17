import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from app.storage.crypto import PayloadCipher, UserDataUnavailableError
from app.storage.database import SqliteDatabase
from app.storage.models import (
    DEFAULT_USER_SETTINGS,
    SessionRecord,
    StorageIntegrityError,
    StoredUserSettings,
)
from app.storage.user_settings import UserSettingsRepository
from app.storage.users import UserSessionRepository
from app.trips.models import TripPattern

UTC_NOW = datetime(2026, 8, 17, tzinfo=UTC)


# Break caught: a login may issue a second user or persist user settings as
# plaintext, exposing data beyond the encrypted seven-day storage boundary.
@pytest.mark.asyncio
async def test_atomic_login_reuses_user_and_settings_are_ciphertext(
    tmp_path: Path,
) -> None:
    path = tmp_path / "private" / "travel-map.sqlite3"
    database = SqliteDatabase(path)
    database.migrate()
    cipher = PayloadCipher(keys={1: b"e" * 32})
    users = UserSessionRepository(database)
    settings = UserSettingsRepository(database, cipher)

    first = await users.upsert_user_and_insert_session(
        subject_hmac=b"s" * 32,
        token_hmac=b"1" * 32,
        csrf_hmac=b"a" * 32,
        now=UTC_NOW,
        expires_at=UTC_NOW + timedelta(days=7),
    )
    second = await users.upsert_user_and_insert_session(
        subject_hmac=b"s" * 32,
        token_hmac=b"2" * 32,
        csrf_hmac=b"b" * 32,
        now=UTC_NOW + timedelta(seconds=1),
        expires_at=UTC_NOW + timedelta(days=7, seconds=1),
    )

    assert first.id == second.id
    await settings.replace(user_id=first.id, settings=DEFAULT_USER_SETTINGS)
    assert await settings.get(user_id=first.id) == DEFAULT_USER_SETTINGS

    await database.checkpoint_truncate()
    for artifact in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        if artifact.exists():
            assert b"ROUND_TRIP" not in artifact.read_bytes()


@pytest.mark.asyncio
async def test_session_issue_rolls_back_a_new_user_when_session_insert_fails(
    tmp_path: Path,
) -> None:
    database = SqliteDatabase(tmp_path / "private" / "travel-map.sqlite3")
    database.migrate()
    users = UserSessionRepository(database)
    shared_token_hmac = b"t" * 32
    await users.upsert_user_and_insert_session(
        subject_hmac=b"a" * 32,
        token_hmac=shared_token_hmac,
        csrf_hmac=b"c" * 32,
        now=UTC_NOW,
        expires_at=UTC_NOW + timedelta(days=7),
    )

    with pytest.raises(StorageIntegrityError, match="storage input is invalid"):
        await users.upsert_user_and_insert_session(
            subject_hmac=b"b" * 32,
            token_hmac=shared_token_hmac,
            csrf_hmac=b"d" * 32,
            now=UTC_NOW + timedelta(seconds=1),
            expires_at=UTC_NOW + timedelta(days=7, seconds=1),
        )

    assert (
        await database.read(
            lambda connection: connection.execute(
                "SELECT COUNT(*) FROM users"
            ).fetchone()[0]
        )
        == 1
    )
    assert (
        await database.read(
            lambda connection: connection.execute(
                "SELECT COUNT(*) FROM sessions"
            ).fetchone()[0]
        )
        == 1
    )


@pytest.mark.asyncio
async def test_session_issue_rolls_back_when_the_return_user_record_is_invalid(
    tmp_path: Path,
) -> None:
    path = tmp_path / "private" / "travel-map.sqlite3"
    database = SqliteDatabase(path)
    database.migrate()
    subject_hmac = b"z" * 32
    original_last_login = "2026-08-17T00:00:00.000000Z"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO users(id, kakao_subject_hmac, created_at, last_login_at) "
            "VALUES (?, ?, ?, ?)",
            (0, subject_hmac, original_last_login, original_last_login),
        )

    with pytest.raises(StorageIntegrityError, match="storage row is invalid"):
        await UserSessionRepository(database).upsert_user_and_insert_session(
            subject_hmac=subject_hmac,
            token_hmac=b"t" * 32,
            csrf_hmac=b"c" * 32,
            now=UTC_NOW + timedelta(seconds=1),
            expires_at=UTC_NOW + timedelta(days=7, seconds=1),
        )

    assert (
        await database.read(
            lambda connection: connection.execute(
                "SELECT last_login_at FROM users WHERE id=0"
            ).fetchone()[0]
        )
        == original_last_login
    )
    assert (
        await database.read(
            lambda connection: connection.execute(
                "SELECT COUNT(*) FROM sessions WHERE user_id=0"
            ).fetchone()[0]
        )
        == 0
    )


@pytest.mark.asyncio
async def test_session_lifecycle_requires_exact_hmacs_and_cascades_user_data(
    tmp_path: Path,
) -> None:
    database = SqliteDatabase(tmp_path / "private" / "travel-map.sqlite3")
    database.migrate()
    users = UserSessionRepository(database)
    settings = UserSettingsRepository(database, PayloadCipher(keys={1: b"e" * 32}))

    with pytest.raises(StorageIntegrityError, match="storage input is invalid"):
        await users.upsert_user_and_insert_session(
            subject_hmac=b"raw-subject",
            token_hmac=b"t" * 32,
            csrf_hmac=b"c" * 32,
            now=UTC_NOW,
            expires_at=UTC_NOW + timedelta(days=7),
        )

    user = await users.upsert_user_and_insert_session(
        subject_hmac=b"s" * 32,
        token_hmac=b"t" * 32,
        csrf_hmac=b"c" * 32,
        now=UTC_NOW,
        expires_at=UTC_NOW + timedelta(days=7),
    )
    resolved = await users.resolve_session(token_hmac=b"t" * 32, now=UTC_NOW)
    assert resolved == SessionRecord(
        user_id=user.id,
        token_hmac=b"t" * 32,
        csrf_hmac=b"c" * 32,
        created_at=UTC_NOW,
        expires_at=UTC_NOW + timedelta(days=7),
    )

    await settings.replace(user_id=user.id, settings=DEFAULT_USER_SETTINGS)
    assert await users.revoke_session(token_hmac=b"t" * 32) is True
    assert await users.revoke_session(token_hmac=b"t" * 32) is False
    assert await users.resolve_session(token_hmac=b"t" * 32, now=UTC_NOW) is None

    await users.upsert_user_and_insert_session(
        subject_hmac=b"s" * 32,
        token_hmac=b"u" * 32,
        csrf_hmac=b"v" * 32,
        now=UTC_NOW + timedelta(seconds=1),
        expires_at=UTC_NOW + timedelta(days=7, seconds=1),
    )
    assert await users.revoke_all_sessions(user_id=user.id) == 1
    assert await users.delete_user(user_id=user.id) is True
    assert await users.delete_user(user_id=user.id) is False
    assert (
        await database.read(
            lambda connection: connection.execute(
                "SELECT COUNT(*) FROM user_settings"
            ).fetchone()[0]
        )
        == 0
    )


@pytest.mark.asyncio
async def test_settings_rejects_tampered_aad_and_unknown_payload_shape(
    tmp_path: Path,
) -> None:
    database = SqliteDatabase(tmp_path / "private" / "travel-map.sqlite3")
    database.migrate()
    cipher = PayloadCipher(keys={1: b"e" * 32})
    user = await UserSessionRepository(database).upsert_user_and_insert_session(
        subject_hmac=b"s" * 32,
        token_hmac=b"t" * 32,
        csrf_hmac=b"c" * 32,
        now=UTC_NOW,
        expires_at=UTC_NOW + timedelta(days=7),
    )
    payload = cipher.encrypt_json(
        purpose="user-settings",
        owner_id=str(user.id),
        payload={
            "default_origin_site_id": None,
            "default_trip_pattern": "ROUND_TRIP",
            "default_duration_minutes": 300,
            "vehicle_use": "NONE",
            "fuel_type": "GASOLINE",
            "efficiency_km_per_liter": 10.0,
            "parking_cost_krw": 0,
            "route_sort": "time",
            "unknown": True,
        },
    )
    with pytest.raises(UserDataUnavailableError, match="ENCRYPTED_PAYLOAD_INVALID"):
        cipher.decrypt_json(
            purpose="user-settings",
            owner_id=str(user.id + 1),
            ciphertext=payload.ciphertext,
            encryption_version=payload.encryption_version,
        )
    await database.write(
        lambda connection: connection.execute(
            "INSERT INTO user_settings("
            "user_id, encrypted_payload, encryption_version, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (
                user.id,
                payload.ciphertext,
                payload.encryption_version,
                "2026-08-17T00:00:00.000000Z",
            ),
        )
    )

    with pytest.raises(UserDataUnavailableError, match="ENCRYPTED_PAYLOAD_INVALID"):
        await UserSettingsRepository(database, cipher).get(user_id=user.id)


@pytest.mark.asyncio
async def test_settings_replace_is_a_full_encrypted_replacement_with_strict_values(
    tmp_path: Path,
) -> None:
    database = SqliteDatabase(tmp_path / "private" / "travel-map.sqlite3")
    database.migrate()
    cipher = PayloadCipher(keys={1: b"e" * 32})
    user = await UserSessionRepository(database).upsert_user_and_insert_session(
        subject_hmac=b"s" * 32,
        token_hmac=b"t" * 32,
        csrf_hmac=b"c" * 32,
        now=UTC_NOW,
        expires_at=UTC_NOW + timedelta(days=7),
    )
    settings = UserSettingsRepository(database, cipher)
    customized = StoredUserSettings(
        default_origin_site_id="safe-test-site",
        default_trip_pattern=TripPattern.OUTBOUND_ONLY_END_AFTER_SCHEDULE,
        default_duration_minutes=360,
        vehicle_use=DEFAULT_USER_SETTINGS.vehicle_use,
        fuel_type=DEFAULT_USER_SETTINGS.fuel_type,
        efficiency_km_per_liter=12.5,
        parking_cost_krw=1_000,
        route_sort="cost",
    )

    assert await settings.get(user_id=user.id) is None
    await settings.replace(user_id=user.id, settings=DEFAULT_USER_SETTINGS)
    await settings.replace(user_id=user.id, settings=customized)
    assert await settings.get(user_id=user.id) == customized
    encrypted_payload = await database.read(
        lambda connection: connection.execute(
            "SELECT encrypted_payload FROM user_settings WHERE user_id=?", (user.id,)
        ).fetchone()[0]
    )
    assert b"safe-test-site" not in encrypted_payload
    assert b"OUTBOUND_ONLY_END_AFTER_SCHEDULE" not in encrypted_payload

    with pytest.raises((TypeError, ValueError)):
        StoredUserSettings(
            default_origin_site_id=None,
            default_trip_pattern=TripPattern.ROUND_TRIP,
            default_duration_minutes=True,
            vehicle_use=DEFAULT_USER_SETTINGS.vehicle_use,
            fuel_type=DEFAULT_USER_SETTINGS.fuel_type,
            efficiency_km_per_liter=10.0,
            parking_cost_krw=0,
            route_sort="time",
        )
