from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from app.storage.crypto import EncryptedPayload, PayloadCipher, UserDataUnavailableError
from app.storage.database import SqliteDatabase
from app.storage.history import (
    HistoryRepository,
)
from app.storage.models import (
    HistoryRecalculationDraft,
    HistorySummary,
    StorageIntegrityError,
    format_storage_timestamp,
)
from app.storage.retention import RetentionCleaner
from app.storage.users import UserSessionRepository
from app.trips.models import TripPattern


# Break caught: using a request trip time for retention, an inclusive expiry
# comparison, or an offset pagination cap loses user history at the seven-day
# boundary.
@pytest.mark.asyncio
async def test_history_expires_exactly_at_168_hours_and_pages_all_rows(
    tmp_path: Path,
) -> None:
    clock = [datetime(2026, 8, 17, tzinfo=UTC)]
    database = SqliteDatabase(tmp_path / "private" / "travel-map.sqlite3")
    database.migrate()
    user = await UserSessionRepository(database).upsert_user_and_insert_session(
        subject_hmac=b"s" * 32,
        token_hmac=b"t" * 32,
        csrf_hmac=b"c" * 32,
        now=clock[0],
        expires_at=clock[0] + timedelta(days=7),
    )
    history_repository = HistoryRepository(
        database,
        PayloadCipher(keys={1: b"e" * 32}),
        clock=lambda: clock[0],
    )
    draft = HistoryRecalculationDraft(
        origin_site_id="neis:B10:7010057:main",
        origin_name="서울샘물초등학교",
        destination_name="서울시청",
        destination_address="서울특별시 중구 세종대로 110",
        trip_pattern=TripPattern.ROUND_TRIP,
        starts_at=clock[0] + timedelta(hours=1),
        ends_at=clock[0] + timedelta(hours=6),
    )
    summary = HistorySummary(
        classification="LOCAL",
        allowance_status="ESTIMATED",
        allowance_krw=20_000,
        route_legs=(),
        rule_set_id="2025-local-travel",
        effective_from="2025-01-01",
    )
    created = await history_repository.create(
        user_id=user.id, draft=draft, summary=summary
    )

    clock[0] = created.expires_at - timedelta(microseconds=1)
    assert (
        await history_repository.get(user_id=user.id, history_id=created.id) is not None
    )
    clock[0] = created.expires_at
    assert await history_repository.get(user_id=user.id, history_id=created.id) is None

    for _ in range(101):
        clock[0] += timedelta(microseconds=1)
        await history_repository.create(user_id=user.id, draft=draft, summary=summary)
    first = await history_repository.list_page(user_id=user.id, before=None, limit=100)
    second = await history_repository.list_page(
        user_id=user.id, before=first.next_cursor, limit=100
    )
    assert len(first.items) == 100
    assert len(second.items) == 1


# Break caught: request-supplied starts/ends accidentally determine the
# retention deadline instead of the repository's trusted clock.
@pytest.mark.asyncio
async def test_history_retention_uses_trusted_clock_not_trip_times(
    tmp_path: Path,
) -> None:
    clock = [datetime(2026, 8, 17, 12, tzinfo=UTC)]
    database = SqliteDatabase(tmp_path / "private" / "travel-map.sqlite3")
    database.migrate()
    user = await _create_user(database, now=clock[0], marker=b"a")
    repository = HistoryRepository(
        database, PayloadCipher(keys={1: b"e" * 32}), clock=lambda: clock[0]
    )
    past_trip = HistoryRecalculationDraft(
        origin_site_id="safe-origin",
        origin_name="past origin",
        destination_name="past destination",
        destination_address="past address",
        trip_pattern=TripPattern.ROUND_TRIP,
        starts_at=datetime(2000, 1, 1, tzinfo=UTC),
        ends_at=datetime(2000, 1, 2, tzinfo=UTC),
    )

    created = await repository.create(
        user_id=user.id, draft=past_trip, summary=_summary()
    )

    assert created.created_at == datetime(2026, 8, 17, 12, tzinfo=UTC)
    assert created.expires_at == datetime(2026, 8, 24, 12, tzinfo=UTC)


# Break caught: a history ID reused or omitted from AAD allows encrypted data
# to be moved between records without a fixed unavailable-data failure.
@pytest.mark.asyncio
async def test_history_ids_are_128_bit_url_safe_and_aad_binds_the_record(
    tmp_path: Path,
) -> None:
    clock = [datetime(2026, 8, 17, tzinfo=UTC)]
    database = SqliteDatabase(tmp_path / "private" / "travel-map.sqlite3")
    database.migrate()
    user = await _create_user(database, now=clock[0], marker=b"b")
    cipher = PayloadCipher(keys={1: b"e" * 32})
    repository = HistoryRepository(database, cipher, clock=lambda: clock[0])

    first = await repository.create(
        user_id=user.id, draft=_draft(clock[0]), summary=_summary()
    )
    second = await repository.create(
        user_id=user.id, draft=_draft(clock[0]), summary=_summary()
    )
    assert first.id != second.id
    assert len(first.id) == 22
    assert all(
        character.isascii() and (character.isalnum() or character in "-_")
        for character in first.id
    )

    swapped_input = cipher.encrypt_json(
        purpose="history-input",
        owner_id=f"{user.id}:{second.id}",
        payload=_draft_payload(clock[0]),
    )
    await database.write(
        lambda connection: connection.execute(
            "UPDATE calculation_history SET encrypted_input=?, encryption_version=? "
            "WHERE id=?",
            (swapped_input.ciphertext, swapped_input.encryption_version, first.id),
        )
    )
    with pytest.raises(UserDataUnavailableError, match="ENCRYPTED_PAYLOAD_INVALID"):
        await repository.get(user_id=user.id, history_id=first.id)


# Break caught: accepting a loose decrypted mapping lets unapproved history
# content through the persistence boundary or leaks parser details to callers.
@pytest.mark.asyncio
async def test_history_rejects_unknown_or_wrongly_typed_encrypted_payloads(
    tmp_path: Path,
) -> None:
    clock = [datetime(2026, 8, 17, tzinfo=UTC)]
    database = SqliteDatabase(tmp_path / "private" / "travel-map.sqlite3")
    database.migrate()
    user = await _create_user(database, now=clock[0], marker=b"c")
    cipher = PayloadCipher(keys={1: b"e" * 32})
    repository = HistoryRepository(database, cipher, clock=lambda: clock[0])
    created = await repository.create(
        user_id=user.id, draft=_draft(clock[0]), summary=_summary()
    )
    payload_with_unknown = {**_draft_payload(clock[0]), "coordinate": [37.5, 127.0]}
    encrypted_unknown = cipher.encrypt_json(
        purpose="history-input",
        owner_id=f"{user.id}:{created.id}",
        payload=payload_with_unknown,
    )
    await _replace_input(database, created.id, encrypted_unknown)
    with pytest.raises(UserDataUnavailableError, match="ENCRYPTED_PAYLOAD_INVALID"):
        await repository.get(user_id=user.id, history_id=created.id)

    wrong_type_payload = {**_draft_payload(clock[0]), "starts_at": True}
    encrypted_wrong_type = cipher.encrypt_json(
        purpose="history-input",
        owner_id=f"{user.id}:{created.id}",
        payload=wrong_type_payload,
    )
    await _replace_input(database, created.id, encrypted_wrong_type)
    with pytest.raises(UserDataUnavailableError, match="ENCRYPTED_PAYLOAD_INVALID"):
        await repository.get(user_id=user.id, history_id=created.id)

    summary_history = await repository.create(
        user_id=user.id, draft=_draft(clock[0]), summary=_summary()
    )
    encrypted_summary_unknown = cipher.encrypt_json(
        purpose="history-summary",
        owner_id=f"{user.id}:{summary_history.id}",
        payload={**_summary_payload(), "unexpected_summary_field": "value"},
    )
    await database.write(
        lambda connection: connection.execute(
            "UPDATE calculation_history SET encrypted_summary=?, encryption_version=? "
            "WHERE id=?",
            (
                encrypted_summary_unknown.ciphertext,
                encrypted_summary_unknown.encryption_version,
                summary_history.id,
            ),
        )
    )
    with pytest.raises(UserDataUnavailableError, match="ENCRYPTED_PAYLOAD_INVALID"):
        await repository.get(user_id=user.id, history_id=summary_history.id)


# Break caught: plaintext draft text can survive in the main database or WAL
# even when a repository returns an encrypted-looking row.
@pytest.mark.asyncio
async def test_history_draft_and_summary_remain_ciphertext_in_database_artifacts(
    tmp_path: Path,
) -> None:
    path = tmp_path / "private" / "travel-map.sqlite3"
    clock = [datetime(2026, 8, 17, tzinfo=UTC)]
    database = SqliteDatabase(path)
    database.migrate()
    user = await _create_user(database, now=clock[0], marker=b"d")
    repository = HistoryRepository(
        database, PayloadCipher(keys={1: b"e" * 32}), clock=lambda: clock[0]
    )
    draft = HistoryRecalculationDraft(
        origin_site_id="history-sentinel-origin",
        origin_name="history-sentinel-origin-name",
        destination_name="history-sentinel-destination-name",
        destination_address="history-sentinel-destination-address",
        trip_pattern=TripPattern.ROUND_TRIP,
        starts_at=clock[0],
        ends_at=clock[0] + timedelta(hours=5),
    )
    await repository.create(
        user_id=user.id,
        draft=draft,
        summary=HistorySummary(
            classification="LOCAL",
            allowance_status="ESTIMATED",
            allowance_krw=20_000,
            route_legs=(),
            rule_set_id="history-sentinel-rule",
            effective_from="2025-01-01",
        ),
    )
    await database.checkpoint_truncate()

    for artifact in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        if artifact.exists():
            raw = artifact.read_bytes()
            assert b"history-sentinel-origin" not in raw
            assert b"history-sentinel-destination-address" not in raw
            assert b"history-sentinel-rule" not in raw


# Break caught: a caller can bypass the HTTP layer with bool/zero/oversize
# limits, a stale owner can read another user's row, or expiry cleanup crosses
# an owner boundary.
@pytest.mark.asyncio
async def test_history_limits_owner_scope_and_expired_deletion_are_strict(
    tmp_path: Path,
) -> None:
    clock = [datetime(2026, 8, 17, tzinfo=UTC)]
    database = SqliteDatabase(tmp_path / "private" / "travel-map.sqlite3")
    database.migrate()
    first_user = await _create_user(database, now=clock[0], marker=b"e")
    second_user = await _create_user(database, now=clock[0], marker=b"f")
    repository = HistoryRepository(
        database, PayloadCipher(keys={1: b"e" * 32}), clock=lambda: clock[0]
    )
    first = await repository.create(
        user_id=first_user.id, draft=_draft(clock[0]), summary=_summary()
    )
    second = await repository.create(
        user_id=second_user.id, draft=_draft(clock[0]), summary=_summary()
    )

    for invalid_limit in (True, 0, 101):
        with pytest.raises(StorageIntegrityError, match="storage input is invalid"):
            await repository.list_page(
                user_id=first_user.id, before=None, limit=invalid_limit
            )
    assert await repository.get(user_id=second_user.id, history_id=first.id) is None
    assert await repository.delete(user_id=second_user.id, history_id=first.id) is False

    clock[0] = first.expires_at
    assert (await repository.list_page(user_id=first_user.id, before=None)).items == ()
    assert (
        await database.read(
            lambda connection: connection.execute(
                "SELECT COUNT(*) FROM calculation_history WHERE id=?", (first.id,)
            ).fetchone()[0]
        )
        == 0
    )
    assert (
        await database.read(
            lambda connection: connection.execute(
                "SELECT COUNT(*) FROM calculation_history WHERE id=?", (second.id,)
            ).fetchone()[0]
        )
        == 1
    )
    clock[0] += timedelta(microseconds=1)
    active = await repository.create(
        user_id=first_user.id, draft=_draft(clock[0]), summary=_summary()
    )
    assert await repository.delete_all(user_id=first_user.id) == 1
    assert await repository.get(user_id=first_user.id, history_id=active.id) is None


# Break caught: a modified database expiry extends the seven-day retention
# window, exposing the row after the immutable created_at-derived deadline.
@pytest.mark.asyncio
async def test_history_expiry_is_recomputed_from_created_at_for_reads_and_cleanup(
    tmp_path: Path,
) -> None:
    clock = [datetime(2026, 8, 17, tzinfo=UTC)]
    database = SqliteDatabase(tmp_path / "private" / "travel-map.sqlite3")
    database.migrate()
    read_user = await _create_user(database, now=clock[0], marker=b"g")
    cleanup_user = await _create_user(database, now=clock[0], marker=b"h")
    repository = HistoryRepository(
        database, PayloadCipher(keys={1: b"e" * 32}), clock=lambda: clock[0]
    )
    read_history = await repository.create(
        user_id=read_user.id, draft=_draft(clock[0]), summary=_summary()
    )
    cleanup_history = await repository.create(
        user_id=cleanup_user.id, draft=_draft(clock[0]), summary=_summary()
    )
    tampered_expiry = format_storage_timestamp(
        read_history.created_at + timedelta(hours=169)
    )
    await database.write(
        lambda connection: connection.execute(
            "UPDATE calculation_history SET expires_at=? WHERE id IN (?, ?)",
            (tampered_expiry, read_history.id, cleanup_history.id),
        )
    )

    clock[0] = read_history.created_at + timedelta(hours=168)
    assert (
        await repository.get(user_id=read_user.id, history_id=read_history.id) is None
    )
    assert (await repository.list_page(user_id=read_user.id, before=None)).items == ()

    counts = await RetentionCleaner(database, clock=lambda: clock[0]).run_once(
        now=clock[0]
    )
    assert counts.history == 1
    assert (
        await database.read(
            lambda connection: connection.execute(
                "SELECT COUNT(*) FROM calculation_history WHERE id=?",
                (cleanup_history.id,),
            ).fetchone()[0]
        )
        == 0
    )


async def _create_user(database: SqliteDatabase, *, now: datetime, marker: bytes):
    return await UserSessionRepository(database).upsert_user_and_insert_session(
        subject_hmac=marker * 32,
        token_hmac=(marker + b"t") * 16,
        csrf_hmac=(marker + b"c") * 16,
        now=now,
        expires_at=now + timedelta(days=7),
    )


def _draft(now: datetime) -> HistoryRecalculationDraft:
    return HistoryRecalculationDraft(
        origin_site_id="safe-origin",
        origin_name="safe origin",
        destination_name="safe destination",
        destination_address="safe address",
        trip_pattern=TripPattern.ROUND_TRIP,
        starts_at=now + timedelta(hours=1),
        ends_at=now + timedelta(hours=6),
    )


def _summary() -> HistorySummary:
    return HistorySummary(
        classification="LOCAL",
        allowance_status="ESTIMATED",
        allowance_krw=20_000,
        route_legs=(),
        rule_set_id="2025-local-travel",
        effective_from="2025-01-01",
    )


def _draft_payload(now: datetime) -> dict[str, object]:
    return {
        "origin_site_id": "safe-origin",
        "origin_name": "safe origin",
        "destination_name": "safe destination",
        "destination_address": "safe address",
        "trip_pattern": "ROUND_TRIP",
        "starts_at": format_storage_timestamp(now + timedelta(hours=1)),
        "ends_at": format_storage_timestamp(now + timedelta(hours=6)),
    }


def _summary_payload() -> dict[str, object]:
    return {
        "classification": "LOCAL",
        "allowance_status": "ESTIMATED",
        "allowance_krw": 20_000,
        "route_legs": [],
        "rule_set_id": "2025-local-travel",
        "effective_from": "2025-01-01",
    }


async def _replace_input(
    database: SqliteDatabase, history_id: str, encrypted: EncryptedPayload
) -> None:
    await database.write(
        lambda connection: connection.execute(
            "UPDATE calculation_history SET encrypted_input=?, encryption_version=? "
            "WHERE id=?",
            (encrypted.ciphertext, encrypted.encryption_version, history_id),
        )
    )
