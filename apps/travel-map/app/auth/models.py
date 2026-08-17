"""Public OAuth and OIDC value objects with no plaintext provider values."""

from dataclasses import dataclass
from datetime import datetime


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
class VerifiedSubject:
    subject_hmac: bytes

    def __post_init__(self) -> None:
        if type(self.subject_hmac) is not bytes or len(self.subject_hmac) != 32:
            raise ValueError("subject_hmac must be exactly 32 bytes")
