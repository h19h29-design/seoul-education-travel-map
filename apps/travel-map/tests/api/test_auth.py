from datetime import UTC, datetime
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from app.auth.models import OidcInternalError, UserServices, VerifiedSubject
from app.auth.oauth import OAuthAttemptRepository
from app.auth.session import SessionService
from app.settings import Settings
from app.storage.database import SqliteDatabase
from app.storage.users import UserSessionRepository

pytest_plugins = ("tests.api.conftest",)

_PUBLIC_ORIGIN = "https://travel.h19h19.com"
_AUTHORIZATION_URL = "https://kauth.kakao.com/oauth/authorize?provider=fake"


class FakeOidcClient:
    def __init__(self) -> None:
        self.exchange_calls = 0
        self.internal_error = False

    def authorization_url(self, *, state: str, nonce: str) -> str:
        return f"{_AUTHORIZATION_URL}&state={state}&nonce={nonce}"

    async def exchange_and_verify(
        self, *, code: str, expected_nonce_hash: bytes
    ) -> VerifiedSubject:
        self.exchange_calls += 1
        assert code == "test-callback-code"
        assert len(expected_nonce_hash) == 32
        if self.internal_error:
            raise OidcInternalError()
        return VerifiedSubject(subject_hmac=b"u" * 32)


def _cookie_value(response, name: str) -> str:
    for header in response.headers.get_list("set-cookie"):
        parsed = SimpleCookie()
        parsed.load(header)
        if name in parsed:
            return parsed[name].value
    raise AssertionError(f"missing {name} cookie")


def _auth_services(client, tmp_path: Path) -> tuple[SessionService, FakeOidcClient]:
    database = SqliteDatabase(tmp_path / "private" / "travel-map.sqlite3")
    database.migrate()
    sessions = SessionService(UserSessionRepository(database), hmac_key=b"s" * 32)
    oidc = FakeOidcClient()
    dependencies = client.app.state.dependencies
    dependencies.settings = Settings(
        environment="test",
        public_base_url=_PUBLIC_ORIGIN,
        user_database_path="/data/travel-map.sqlite3",
        kakao_oidc_client_id="test-login-client-id",
        kakao_oidc_client_secret="test-only-oidc-secret",
        session_hmac_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        kakao_subject_hmac_key="AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE",
        data_encryption_key_v1="AgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgI",
        trusted_proxy_cidrs=("1.1.1.1/32",),
        _env_file=None,
    )
    dependencies.user_services = UserServices(
        oauth_attempts=OAuthAttemptRepository(database, hmac_key=b"s" * 32),
        sessions=sessions,
        history=None,
        settings=None,
        retention_cleaner=None,
        oidc_client=oidc,
    )
    return sessions, oidc


# Break caught: login start does not set a host-only one-use attempt cookie or
# lets a redirect carrying state be cached by a browser or intermediary.
def test_auth_start_sets_exact_attempt_cookie_and_no_store(
    client, tmp_path: Path
) -> None:
    _, _ = _auth_services(client, tmp_path)

    response = client.get("/auth/kakao/start", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"].startswith(_AUTHORIZATION_URL)
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    cookies = response.headers.get_list("set-cookie")
    assert len(cookies) == 1
    assert "__Host-travel_oauth=" in cookies[0]
    assert "HttpOnly" in cookies[0]
    assert "Secure" in cookies[0]
    assert "SameSite=lax" in cookies[0]
    assert "Path=/" in cookies[0]
    assert "Max-Age=600" in cookies[0]
    assert "Domain=" not in cookies[0]


# Break caught: unavailable private storage lets state-bearing auth responses be
# cached or leaves the browser's one-use attempt cookie after a terminal error.
def test_auth_unavailable_responses_are_no_store_and_clear_callback_attempt(
    client,
) -> None:
    start = client.get("/auth/kakao/start", follow_redirects=False)
    callback = client.get(
        "/auth/kakao/callback?state=unused&code=unused",
        headers={"Cookie": "__Host-travel_oauth=unused"},
        follow_redirects=False,
    )

    assert start.status_code == 503
    assert start.json() == {"error": {"code": "AUTH_UNAVAILABLE"}}
    assert start.headers["cache-control"] == "no-store"
    assert start.headers["pragma"] == "no-cache"
    assert callback.status_code == 503
    assert callback.json() == {"error": {"code": "AUTH_UNAVAILABLE"}}
    assert callback.headers["cache-control"] == "no-store"
    assert callback.headers["pragma"] == "no-cache"
    cleared = callback.headers.get_list("set-cookie")
    assert len(cleared) == 1
    assert "__Host-travel_oauth=" in cleared[0]
    assert "Max-Age=0" in cleared[0]


def _logged_in(
    client, tmp_path: Path
) -> tuple[SessionService, FakeOidcClient, str, str, object]:
    sessions, oidc = _auth_services(client, tmp_path)
    start = client.get("/auth/kakao/start", follow_redirects=False)
    attempt = _cookie_value(start, "__Host-travel_oauth")
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    callback = client.get(
        f"/auth/kakao/callback?state={state}&code=test-callback-code",
        headers={"Cookie": f"__Host-travel_oauth={attempt}"},
        follow_redirects=False,
    )
    assert callback.status_code == 302
    assert callback.headers["location"] == "/"
    return (
        sessions,
        oidc,
        _cookie_value(callback, "__Host-travel_session"),
        _cookie_value(callback, "__Host-travel_csrf"),
        callback,
    )


# Break caught: an OAuth callback leaves the attempt cookie present, omits one
# session cookie attribute, or accepts provider error input as a token exchange.
def test_callback_sets_session_and_csrf_and_clears_attempt_on_all_terminal_paths(
    client,
    tmp_path: Path,
) -> None:
    _, oidc, _, _, callback = _logged_in(client, tmp_path)
    assert oidc.exchange_calls == 1
    assert callback.headers["cache-control"] == "no-store"
    assert callback.headers["pragma"] == "no-cache"
    success_cookies = callback.headers.get_list("set-cookie")
    assert len(success_cookies) == 3
    session_cookie = next(
        header for header in success_cookies if "__Host-travel_session=" in header
    )
    csrf_cookie = next(
        header for header in success_cookies if "__Host-travel_csrf=" in header
    )
    cleared_attempt = next(
        header for header in success_cookies if "__Host-travel_oauth=" in header
    )
    for header in (session_cookie, csrf_cookie):
        assert "Secure" in header
        assert "SameSite=lax" in header
        assert "Path=/" in header
        assert "Max-Age=604800" in header
        assert "Domain=" not in header
    assert "HttpOnly" in session_cookie
    assert "HttpOnly" not in csrf_cookie
    assert "Max-Age=0" in cleared_attempt

    session_response = client.get("/auth/kakao/start", follow_redirects=False)
    attempt = _cookie_value(session_response, "__Host-travel_oauth")
    failed = client.get(
        "/auth/kakao/callback?error=access_denied",
        headers={"Cookie": f"__Host-travel_oauth={attempt}"},
        follow_redirects=False,
    )

    assert failed.status_code == 302
    assert failed.headers["location"] == "/"
    assert failed.headers["cache-control"] == "no-store"
    assert failed.headers["pragma"] == "no-cache"
    cleared = failed.headers.get_list("set-cookie")
    assert len(cleared) == 1
    assert "__Host-travel_oauth=" in cleared[0]
    assert "Max-Age=0" in cleared[0]
    assert "HttpOnly" in cleared[0]
    assert "Secure" in cleared[0]
    assert "SameSite=lax" in cleared[0]
    assert "Path=/" in cleared[0]
    assert "Domain=" not in cleared[0]
    assert oidc.exchange_calls == 1


# Break caught: an unexpected OIDC worker/task failure looks like a normal user
# cancellation instead of reaching the fixed non-cacheable internal-error alert.
def test_callback_preserves_fixed_internal_oidc_error_after_clearing_attempt(
    client,
    tmp_path: Path,
) -> None:
    _, oidc = _auth_services(client, tmp_path)
    oidc.internal_error = True
    start = client.get("/auth/kakao/start", follow_redirects=False)
    attempt = _cookie_value(start, "__Host-travel_oauth")
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]

    response = client.get(
        f"/auth/kakao/callback?state={state}&code=test-callback-code",
        headers={"Cookie": f"__Host-travel_oauth={attempt}"},
        follow_redirects=False,
    )

    assert response.status_code == 500
    assert response.json() == {"error": {"code": "OIDC_INTERNAL_ERROR"}}
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    cleared = response.headers.get_list("set-cookie")
    assert len(cleared) == 1
    assert "__Host-travel_oauth=" in cleared[0]
    assert "Max-Age=0" in cleared[0]


# Break caught: a missing/wrong Origin or CSRF header performs an authenticated
# mutation, or deleting one session principal removes another user's account.
@pytest.mark.asyncio
async def test_me_logout_and_delete_require_origin_csrf_and_preserve_other_user(
    client,
    tmp_path: Path,
) -> None:
    sessions, _, raw_token, raw_csrf, _ = _logged_in(client, tmp_path)
    now = datetime.now(UTC)
    other_issued = await sessions.issue_for_subject(subject_hmac=b"o" * 32, now=now)

    cookie = f"__Host-travel_session={raw_token}"
    anonymous = client.get("/api/v1/me")
    me = client.get("/api/v1/me", headers={"Cookie": cookie})
    missing_origin = client.post("/api/v1/auth/logout", headers={"Cookie": cookie})
    wrong_origin = client.post(
        "/api/v1/auth/logout",
        headers={
            "Cookie": cookie,
            "Origin": "https://not-travel.h19h19.com",
            "X-CSRF-Token": raw_csrf,
        },
    )
    missing_csrf = client.post(
        "/api/v1/auth/logout",
        headers={"Cookie": cookie, "Origin": _PUBLIC_ORIGIN},
    )
    wrong_csrf = client.post(
        "/api/v1/auth/logout",
        headers={
            "Cookie": cookie,
            "Origin": _PUBLIC_ORIGIN,
            "X-CSRF-Token": "wrong-csrf",
        },
    )

    assert anonymous.json() == {"authenticated": False}
    assert anonymous.headers["cache-control"] == "no-store"
    assert me.json()["authenticated"] is True
    assert me.headers["cache-control"] == "no-store"
    assert missing_origin.status_code == 403
    assert missing_origin.json()["error"]["code"] == "INVALID_ORIGIN"
    assert wrong_origin.status_code == 403
    assert wrong_origin.json()["error"]["code"] == "INVALID_ORIGIN"
    assert missing_csrf.status_code == 403
    assert missing_csrf.json()["error"]["code"] == "CSRF_FAILED"
    assert missing_csrf.headers.get_list("set-cookie") == []
    assert wrong_csrf.status_code == 403
    assert wrong_csrf.json()["error"]["code"] == "CSRF_FAILED"
    assert wrong_csrf.headers.get_list("set-cookie") == []
    assert await sessions.resolve(raw_token=raw_token, now=now) is not None

    deleted = client.delete(
        "/api/v1/me/data",
        headers={
            "Cookie": cookie,
            "Origin": _PUBLIC_ORIGIN,
            "X-CSRF-Token": raw_csrf,
        },
    )

    assert deleted.status_code == 204
    assert len(deleted.headers.get_list("set-cookie")) == 3
    assert await sessions.resolve(raw_token=raw_token, now=now) is None
    assert await sessions.resolve(raw_token=other_issued.raw_token, now=now) is not None
    assert "access-control-allow-credentials" not in deleted.headers


# Break caught: a successful logout fails to revoke the server-side session,
# while anonymous/invalid/cross-origin requests can clear it without the
# session-bound Origin+CSRF proof.
@pytest.mark.asyncio
async def test_logout_revokes_session_and_clears_all_host_only_cookies(
    client, tmp_path: Path
) -> None:
    sessions, _, raw_token, raw_csrf, _ = _logged_in(client, tmp_path)
    cookie = f"__Host-travel_session={raw_token}"
    missing = client.post("/api/v1/auth/logout")
    invalid = client.post(
        "/api/v1/auth/logout",
        headers={
            "Cookie": "__Host-travel_session=invalid",
            "Origin": _PUBLIC_ORIGIN,
            "X-CSRF-Token": "invalid",
        },
    )
    cross_origin = client.post(
        "/api/v1/auth/logout",
        headers={
            "Cookie": cookie,
            "Origin": "https://not-travel.h19h19.com",
            "X-CSRF-Token": raw_csrf,
        },
    )

    assert missing.status_code == 401
    assert missing.headers.get_list("set-cookie") == []
    assert invalid.status_code == 401
    assert invalid.headers.get_list("set-cookie") == []
    assert cross_origin.status_code == 403
    assert cross_origin.headers.get_list("set-cookie") == []
    assert (
        await sessions.resolve(raw_token=raw_token, now=datetime.now(UTC)) is not None
    )

    response = client.post(
        "/api/v1/auth/logout",
        headers={
            "Cookie": cookie,
            "Origin": _PUBLIC_ORIGIN,
            "X-CSRF-Token": raw_csrf,
        },
    )

    assert response.status_code == 204
    cleared = response.headers.get_list("set-cookie")
    assert len(cleared) == 3
    for name in (
        "__Host-travel_oauth",
        "__Host-travel_session",
        "__Host-travel_csrf",
    ):
        header = next(header for header in cleared if f"{name}=" in header)
        assert "Max-Age=0" in header
        assert "Secure" in header
        assert "SameSite=lax" in header
        assert "Path=/" in header
        assert "Domain=" not in header
    session_header = next(
        header for header in cleared if "__Host-travel_session=" in header
    )
    csrf_header = next(header for header in cleared if "__Host-travel_csrf=" in header)
    assert "HttpOnly" in session_header
    assert "HttpOnly" not in csrf_header
    assert "access-control-allow-credentials" not in response.headers

    assert client.get("/api/v1/me", headers={"Cookie": cookie}).json() == {
        "authenticated": False
    }
