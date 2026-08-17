"""Public OAuth and OIDC value objects with no plaintext provider values."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.auth.oauth import KakaoOidcClient, OAuthAttemptRepository
    from app.auth.session import SessionService
    from app.storage.history import HistoryRepository
    from app.storage.retention import RetentionCleaner
    from app.storage.user_settings import UserSettingsRepository


class AuthRejected(RuntimeError):
    """An invalid, expired, or already-consumed OAuth attempt."""

    def __init__(self, code: str = "INVALID_OAUTH_ATTEMPT") -> None:
        if code != "INVALID_OAUTH_ATTEMPT":
            raise ValueError("invalid auth rejection code")
        super().__init__(code)


class OidcLoginFailed(RuntimeError):
    """A fixed safe OIDC rejection."""

    def __init__(self, code: str = "OIDC_LOGIN_FAILED") -> None:
        if code != "OIDC_LOGIN_FAILED":
            raise ValueError("invalid OIDC login failure code")
        super().__init__(code)


class OidcInternalError(RuntimeError):
    """A fixed safe unexpected OIDC failure."""

    def __init__(self, code: str = "OIDC_INTERNAL_ERROR") -> None:
        if code != "OIDC_INTERNAL_ERROR":
            raise ValueError("invalid OIDC internal failure code")
        super().__init__(code)


@dataclass(frozen=True)
class IssuedOAuthAttempt:
    attempt_token: str
    state: str
    nonce: str
    expires_at: datetime

    def __post_init__(self) -> None:
        if any(
            type(value) is not str or not value
            for value in (self.attempt_token, self.state, self.nonce)
        ):
            raise ValueError("OAuth attempt values must be nonblank strings")
        if type(self.expires_at) is not datetime:
            raise TypeError("OAuth expiry must be a datetime")


@dataclass(frozen=True)
class SessionPrincipal:
    user_id: int
    token_hmac: bytes
    csrf_hmac: bytes
    expires_at: datetime

    def __post_init__(self) -> None:
        _require_positive_id(self.user_id)
        _require_32_byte_value(self.token_hmac, "token_hmac")
        _require_32_byte_value(self.csrf_hmac, "csrf_hmac")
        if type(self.expires_at) is not datetime:
            raise TypeError("session expiry must be a datetime")


@dataclass(frozen=True)
class IssuedSession:
    user_id: int
    raw_token: str = field(repr=False)
    raw_csrf: str = field(repr=False)
    expires_at: datetime

    def __post_init__(self) -> None:
        _require_positive_id(self.user_id)
        if type(self.raw_token) is not str or not self.raw_token:
            raise ValueError("raw_token must be a nonblank string")
        if type(self.raw_csrf) is not str or not self.raw_csrf:
            raise ValueError("raw_csrf must be a nonblank string")
        if type(self.expires_at) is not datetime:
            raise TypeError("session expiry must be a datetime")


@dataclass(frozen=True)
class VerifiedSubject:
    subject_hmac: bytes

    def __post_init__(self) -> None:
        if type(self.subject_hmac) is not bytes or len(self.subject_hmac) != 32:
            raise ValueError("subject_hmac must be exactly 32 bytes")


@dataclass(frozen=True)
class UserServices:
    """The private authenticated-service boundary owned by app dependencies."""

    oauth_attempts: OAuthAttemptRepository
    sessions: SessionService
    history: HistoryRepository
    settings: UserSettingsRepository
    retention_cleaner: RetentionCleaner
    oidc_client: KakaoOidcClient


def _require_positive_id(value: object) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError("user_id must be a positive integer")


def _require_32_byte_value(value: object, name: str) -> None:
    if type(value) is not bytes or len(value) != 32:
        raise ValueError(f"{name} must be exactly 32 bytes")
