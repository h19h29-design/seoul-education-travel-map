pytest_plugins = ("tests.api.conftest",)

from datetime import datetime, timedelta

from app.auth.models import (
    IssuedOAuthAttempt,
    IssuedSession,
    UserServices,
    VerifiedSubject,
)
from app.rate_limit import FixedWindowRateLimiter


# Break caught: an unauthenticated places endpoint accepting more than its fixed-window budget.
def test_rate_limit_returns_retry_after(client) -> None:
    for _ in range(10):
        assert client.get("/api/v1/places", params={"q": "서울시청"}).status_code in {
            200,
            503,
        }
    blocked = client.get("/api/v1/places", params={"q": "서울시청"})

    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) >= 1


# Break caught: fixed-window buckets persisting after their window expires and
# allowing rate-limit state to grow without a time-based bound.
def test_rate_limiter_discards_stale_windows() -> None:
    clock = [0.0]
    limiter = FixedWindowRateLimiter(
        limits={"places": (10, 60.0)}, now=lambda: clock[0]
    )

    for moment in (0.0, 60.0, 120.0):
        clock[0] = moment
        assert limiter.check("places", "198.51.100.10").allowed

    assert len(limiter._counts) == 1


# Break caught: a client fan-out filling an active fixed window without a hard
# bucket capacity, creating an unbounded in-process memory commitment.
def test_rate_limiter_rejects_new_clients_when_bucket_capacity_is_reached() -> None:
    limiter = FixedWindowRateLimiter(
        limits={"places": (10, 60.0)},
        max_buckets=2,
    )

    assert limiter.check("places", "198.51.100.1").allowed
    assert limiter.check("places", "198.51.100.2").allowed
    rejected = limiter.check("places", "198.51.100.3")

    assert not rejected.allowed
    assert rejected.retry_after_seconds is not None


class _CountingAttempts:
    def __init__(self) -> None:
        self.create_calls = 0

    async def create(self, *, now: datetime) -> IssuedOAuthAttempt:
        self.create_calls += 1
        return IssuedOAuthAttempt(
            attempt_token=f"attempt-{self.create_calls}",
            state=f"state-{self.create_calls}",
            nonce=f"nonce-{self.create_calls}",
            expires_at=now + timedelta(minutes=10),
        )


class _AuthorizationOnlyOidc:
    def authorization_url(self, *, state: str, nonce: str) -> str:
        return f"https://kauth.kakao.com/oauth/authorize?state={state}&nonce={nonce}"


# Break caught: auth-start rate limiting happens after OAuth-attempt persistence,
# allowing a client to exceed the fixed ten-per-minute storage-write budget.
def test_auth_start_rate_limit_runs_before_creating_oauth_attempts(client) -> None:
    attempts = _CountingAttempts()
    dependencies = client.app.state.dependencies
    dependencies.user_services = UserServices(
        oauth_attempts=attempts,
        sessions=None,
        history=None,
        settings=None,
        retention_cleaner=None,
        oidc_client=_AuthorizationOnlyOidc(),
    )

    for _ in range(10):
        assert (
            client.get("/auth/kakao/start", follow_redirects=False).status_code == 302
        )
    blocked = client.get("/auth/kakao/start", follow_redirects=False)

    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) >= 1
    assert blocked.headers["Cache-Control"] == "no-store"
    assert blocked.headers["Pragma"] == "no-cache"
    assert attempts.create_calls == 10


class _CountingCallbackAttempts:
    def __init__(self) -> None:
        self.consume_calls = 0

    async def consume(self, *, attempt_token: str, state: str, now: datetime) -> bytes:
        self.consume_calls += 1
        return b"n" * 32


class _CountingCallbackOidc:
    def __init__(self) -> None:
        self.exchange_calls = 0

    async def exchange_and_verify(
        self, *, code: str, expected_nonce_hash: bytes
    ) -> VerifiedSubject:
        self.exchange_calls += 1
        return VerifiedSubject(subject_hmac=b"s" * 32)


class _CountingCallbackSessions:
    def __init__(self) -> None:
        self.issue_calls = 0

    async def issue_for_subject(
        self, *, subject_hmac: bytes, now: datetime
    ) -> IssuedSession:
        self.issue_calls += 1
        return IssuedSession(
            user_id=1,
            raw_token="opaque-session",
            raw_csrf="opaque-csrf",
            expires_at=now + timedelta(days=7),
        )


# Break caught: callback rate limiting running after attempt consumption or the
# token exchange, so a flood can invoke private storage/provider work above
# the fixed twenty-per-minute budget.
def test_auth_callback_rate_limit_runs_before_attempt_or_provider_work(client) -> None:
    attempts = _CountingCallbackAttempts()
    oidc = _CountingCallbackOidc()
    sessions = _CountingCallbackSessions()
    dependencies = client.app.state.dependencies
    dependencies.user_services = UserServices(
        oauth_attempts=attempts,
        sessions=sessions,
        history=None,
        settings=None,
        retention_cleaner=None,
        oidc_client=oidc,
    )
    callback = "/auth/kakao/callback?state=state&code=authorization-code"

    for _ in range(20):
        response = client.get(
            callback,
            headers={"Cookie": "__Host-travel_oauth=attempt-token"},
            follow_redirects=False,
        )
        assert response.status_code == 302
    blocked = client.get(
        callback,
        headers={"Cookie": "__Host-travel_oauth=attempt-token"},
        follow_redirects=False,
    )

    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) >= 1
    assert blocked.headers["Cache-Control"] == "no-store"
    assert blocked.headers["Pragma"] == "no-cache"
    assert attempts.consume_calls == oidc.exchange_calls == sessions.issue_calls == 20
