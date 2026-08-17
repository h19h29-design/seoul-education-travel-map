from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from app.auth.session import SessionService
from app.storage.database import SqliteDatabase
from app.storage.users import UserSessionRepository


# Break caught: issuing a second login for one subject creates a different user
# or allows that session's CSRF token to authorize a different session.
@pytest.mark.asyncio
async def test_session_issue_reuses_user_and_csrf_is_bound_to_that_session(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 17, tzinfo=UTC)
    database = SqliteDatabase(tmp_path / "private" / "travel-map.sqlite3")
    database.migrate()
    service = SessionService(UserSessionRepository(database), hmac_key=b"h" * 32)

    first = await service.issue_for_subject(subject_hmac=b"s" * 32, now=now)
    second = await service.issue_for_subject(
        subject_hmac=b"s" * 32,
        now=now + timedelta(seconds=1),
    )
    principal = await service.resolve(raw_token=first.raw_token, now=now)

    assert principal is not None
    assert first.user_id == second.user_id == principal.user_id
    assert await service.verify_csrf(principal=principal, raw_csrf=first.raw_csrf)
    assert not await service.verify_csrf(principal=principal, raw_csrf=second.raw_csrf)
    assert first.expires_at == now + timedelta(days=7)


# Break caught: a session remains valid at or after its fixed seven-day expiry,
# raw bearer/CSRF values reach SQLite artifacts, or a failed resolve slides time.
@pytest.mark.asyncio
async def test_session_is_opaque_non_sliding_and_expires_at_exact_seven_day_boundary(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 17, tzinfo=UTC)
    path = tmp_path / "private" / "travel-map.sqlite3"
    database = SqliteDatabase(path)
    database.migrate()
    service = SessionService(UserSessionRepository(database), hmac_key=b"h" * 32)

    issued = await service.issue_for_subject(subject_hmac=b"s" * 32, now=now)
    before_expiry = await service.resolve(
        raw_token=issued.raw_token,
        now=now + timedelta(days=7) - timedelta(microseconds=1),
    )
    at_expiry = await service.resolve(raw_token=issued.raw_token, now=issued.expires_at)

    assert before_expiry is not None
    assert before_expiry.expires_at == now + timedelta(days=7)
    assert at_expiry is None
    assert issued.raw_token not in repr(issued)
    assert issued.raw_csrf not in repr(issued)
    await database.checkpoint_truncate()
    for artifact in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        if artifact.exists():
            raw = artifact.read_bytes()
            assert issued.raw_token.encode() not in raw
            assert issued.raw_csrf.encode() not in raw


# Break caught: revocation or account deletion accepts another user's identity,
# leaves a deleted owner's sessions usable, or deletes an unrelated user.
@pytest.mark.asyncio
async def test_session_revoke_and_principal_scoped_delete_preserve_other_user(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 17, tzinfo=UTC)
    database = SqliteDatabase(tmp_path / "private" / "travel-map.sqlite3")
    database.migrate()
    service = SessionService(UserSessionRepository(database), hmac_key=b"h" * 32)
    owner = await service.issue_for_subject(subject_hmac=b"a" * 32, now=now)
    owner_second = await service.issue_for_subject(
        subject_hmac=b"a" * 32,
        now=now + timedelta(seconds=1),
    )
    other = await service.issue_for_subject(subject_hmac=b"b" * 32, now=now)

    await service.revoke(raw_token=owner_second.raw_token)
    assert (await service.resolve(raw_token=owner_second.raw_token, now=now)) is None
    owner_principal = await service.resolve(raw_token=owner.raw_token, now=now)
    assert owner_principal is not None
    await service.revoke_all(user_id=owner_principal.user_id)
    assert await service.resolve(raw_token=owner.raw_token, now=now) is None
    renewed_owner = await service.issue_for_subject(
        subject_hmac=b"a" * 32,
        now=now + timedelta(seconds=2),
    )
    renewed_principal = await service.resolve(
        raw_token=renewed_owner.raw_token,
        now=now + timedelta(seconds=2),
    )
    assert renewed_principal is not None
    assert await service.delete_user(principal=renewed_principal)

    assert (await service.resolve(raw_token=owner.raw_token, now=now)) is None
    assert (
        await service.resolve(
            raw_token=renewed_owner.raw_token,
            now=now + timedelta(seconds=2),
        )
    ) is None
    assert await service.resolve(raw_token=other.raw_token, now=now) is not None
