"""Opaque session issuance and validation boundary."""

import hmac
import secrets
from datetime import datetime, timedelta

from app.auth.models import IssuedSession, SessionPrincipal
from app.storage.models import (
    StorageIntegrityError,
    format_storage_timestamp,
    parse_storage_timestamp,
)
from app.storage.users import UserSessionRepository

_SESSION_PREFIX = b"travel-map:session\0"
_CSRF_PREFIX = b"travel-map:csrf\0"
_SESSION_TTL = timedelta(days=7)


class SessionService:
    """Issue opaque session and CSRF values through one atomic repository call."""

    def __init__(self, repository: UserSessionRepository, *, hmac_key: bytes) -> None:
        if type(repository) is not UserSessionRepository:
            raise TypeError("repository must be UserSessionRepository")
        _require_digest(hmac_key)
        self._repository = repository
        self._hmac_key = hmac_key

    async def issue_for_subject(
        self, *, subject_hmac: bytes, now: datetime
    ) -> IssuedSession:
        _require_digest(subject_hmac)
        issued_at = _canonical_timestamp(now)
        expires_at = issued_at + _SESSION_TTL
        raw_token = secrets.token_urlsafe(32)
        raw_csrf = secrets.token_urlsafe(32)
        user = await self._repository.upsert_user_and_insert_session(
            subject_hmac=subject_hmac,
            token_hmac=self._digest(_SESSION_PREFIX, raw_token),
            csrf_hmac=self._digest(_CSRF_PREFIX, raw_csrf),
            now=issued_at,
            expires_at=expires_at,
        )
        return IssuedSession(
            user_id=user.id,
            raw_token=raw_token,
            raw_csrf=raw_csrf,
            expires_at=expires_at,
        )

    async def resolve(
        self, *, raw_token: str, now: datetime
    ) -> SessionPrincipal | None:
        if type(raw_token) is not str or not raw_token:
            return None
        resolved_at = _canonical_timestamp(now)
        record = await self._repository.resolve_session(
            token_hmac=self._digest(_SESSION_PREFIX, raw_token),
            now=resolved_at,
        )
        if record is None:
            return None
        return SessionPrincipal(
            user_id=record.user_id,
            token_hmac=record.token_hmac,
            csrf_hmac=record.csrf_hmac,
            expires_at=record.expires_at,
        )

    async def verify_csrf(self, *, principal: SessionPrincipal, raw_csrf: str) -> bool:
        if type(principal) is not SessionPrincipal:
            raise TypeError("principal must be SessionPrincipal")
        if type(raw_csrf) is not str or not raw_csrf:
            return False
        return hmac.compare_digest(
            principal.csrf_hmac,
            self._digest(_CSRF_PREFIX, raw_csrf),
        )

    async def revoke(self, *, raw_token: str) -> None:
        if type(raw_token) is not str or not raw_token:
            return
        await self._repository.revoke_session(
            token_hmac=self._digest(_SESSION_PREFIX, raw_token)
        )

    async def revoke_all(self, *, user_id: int) -> None:
        _require_user_id(user_id)
        await self._repository.revoke_all_sessions(user_id=user_id)

    async def delete_user(self, *, principal: SessionPrincipal) -> bool:
        if type(principal) is not SessionPrincipal:
            raise TypeError("principal must be SessionPrincipal")
        return await self._repository.delete_user(user_id=principal.user_id)

    def _digest(self, prefix: bytes, value: str) -> bytes:
        return hmac.digest(self._hmac_key, prefix + value.encode("utf-8"), "sha256")


def _require_digest(value: object) -> None:
    if type(value) is not bytes or len(value) != 32:
        raise ValueError("HMAC key must be exactly 32 bytes")


def _require_user_id(value: object) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError("user_id must be a positive integer")


def _canonical_timestamp(value: object) -> datetime:
    if type(value) is not datetime:
        raise StorageIntegrityError("session input is invalid")
    try:
        return parse_storage_timestamp(format_storage_timestamp(value))
    except StorageIntegrityError:
        raise StorageIntegrityError("session input is invalid") from None
