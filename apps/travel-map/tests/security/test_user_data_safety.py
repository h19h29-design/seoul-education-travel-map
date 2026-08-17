import asyncio
import sqlite3
import threading
from base64 import urlsafe_b64encode

import app.main as main_module
from app import dependencies as dependency_module
from app.auth.models import UserServices
from app.dependencies import _optional_user_services
from app.main import create_app
from app.settings import Settings
from app.storage.models import StorageIntegrityError
from fastapi.testclient import TestClient

pytest_plugins = ("tests.api.conftest",)

from tests.api.conftest import trip_payload


def test_user_storage_absence_keeps_anonymous_public_endpoints_available(
    client,
) -> None:
    dependencies = client.app.state.dependencies
    dependencies.user_services = None

    health = client.get("/healthz")
    places = client.get("/api/v1/places", params={"q": "서울시청"})
    preview = client.post("/api/v1/trips/preview", json=trip_payload())

    assert health.json() == {"status": "ok"}
    assert places.status_code == preview.status_code == 200


# Break caught: a schema/open failure escaping optional assembly and preventing
# the public application boundary from starting without a private store.
def test_optional_user_service_schema_failure_is_contained(
    monkeypatch,
) -> None:
    test_key = urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("=")
    settings = Settings(
        environment="test",
        public_base_url="https://travel.h19h19.com",
        user_database_path="/data/travel-map.sqlite3",
        kakao_oidc_client_id="test-login-client",
        kakao_oidc_client_secret="test-only-secret",
        session_hmac_key=test_key,
        kakao_subject_hmac_key=test_key,
        data_encryption_key_v1=test_key,
        trusted_proxy_cidrs=("1.1.1.1/32",),
        _env_file=None,
    )

    def unavailable(_: Settings) -> UserServices:
        raise StorageIntegrityError("storage schema is invalid")

    monkeypatch.setattr(dependency_module, "_build_user_services", unavailable)

    assert _optional_user_services(settings) is None


# Break caught: a raw SQLite open/PRAGMA failure escaping the optional boundary
# and stopping the otherwise public application from assembling.
def test_optional_user_service_raw_sqlite_failure_is_contained(monkeypatch) -> None:
    test_key = urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("=")
    settings = Settings(
        environment="test",
        public_base_url="https://travel.h19h19.com",
        user_database_path="/data/travel-map.sqlite3",
        kakao_oidc_client_id="test-login-client",
        kakao_oidc_client_secret="test-only-secret",
        session_hmac_key=test_key,
        kakao_subject_hmac_key=test_key,
        data_encryption_key_v1=test_key,
        trusted_proxy_cidrs=("1.1.1.1/32",),
        _env_file=None,
    )

    def unavailable(_: Settings) -> UserServices:
        raise sqlite3.OperationalError("sqlite unavailable")

    monkeypatch.setattr(dependency_module, "_build_user_services", unavailable)

    assert _optional_user_services(settings) is None


class _UnavailableSessions:
    async def resolve(self, *, raw_token: str, now: object) -> None:
        raise StorageIntegrityError("storage unavailable")


# Break caught: a browser presenting a session during a private-storage outage
# gets an anonymous preview without the fixed history warning (or exposes the
# session value while trying to resolve it).
def test_presented_session_storage_outage_keeps_preview_public_and_warns(
    client,
) -> None:
    dependencies = client.app.state.dependencies
    dependencies.user_services = UserServices(
        oauth_attempts=None,
        sessions=_UnavailableSessions(),
        history=None,
        settings=None,
        retention_cleaner=None,
        oidc_client=None,
    )
    opaque_token = "opaque-browser-session"

    response = client.post(
        "/api/v1/trips/preview",
        json=trip_payload(),
        headers={"Cookie": f"__Host-travel_session={opaque_token}"},
    )

    assert response.status_code == 200
    assert "HISTORY_NOT_SAVED" in response.json()["warnings"]
    assert opaque_token not in response.text


class _RawSqliteUnavailableSessions:
    async def resolve(self, *, raw_token: str, now: object) -> None:
        raise sqlite3.OperationalError("sqlite unavailable")


# Break caught: a raw SQLite read/PRAGMA error escaping a presented-session
# resolution and turning a public preview into a 500 instead of the fixed
# anonymous-history warning.
def test_presented_session_raw_sqlite_outage_degrades_to_public_preview(client) -> None:
    dependencies = client.app.state.dependencies
    dependencies.user_services = UserServices(
        oauth_attempts=None,
        sessions=_RawSqliteUnavailableSessions(),
        history=None,
        settings=None,
        retention_cleaner=None,
        oidc_client=None,
    )

    response = client.post(
        "/api/v1/trips/preview",
        json=trip_payload(),
        headers={"Cookie": "__Host-travel_session=opaque-browser-session"},
    )

    assert response.status_code == 200
    assert "HISTORY_NOT_SAVED" in response.json()["warnings"]


class _RetryingCleaner:
    def __init__(self) -> None:
        self.calls = 0
        self.running = threading.Event()
        self.cancelled = threading.Event()

    async def run_forever(self) -> None:
        self.calls += 1
        if self.calls == 1:
            raise StorageIntegrityError("storage unavailable")
        self.running.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


class _CloseAfterRetention:
    def __init__(self, cleaner: _RetryingCleaner) -> None:
        self._cleaner = cleaner
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True
        assert self._cleaner.cancelled.is_set()


class _RawSqliteRetryingCleaner:
    def __init__(self) -> None:
        self.calls = 0
        self.running = threading.Event()
        self.cancelled = threading.Event()

    async def run_forever(self) -> None:
        self.calls += 1
        if self.calls == 1:
            raise sqlite3.OperationalError("sqlite unavailable")
        self.running.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


# Break caught: the private cleanup task is never supervised, does not retry a
# fixed storage availability failure, or is left running while its OIDC client
# is closed during application shutdown.
def test_lifespan_retries_retention_then_cancels_before_oidc_close(
    client, monkeypatch
) -> None:
    monkeypatch.setattr(main_module, "_RETENTION_RETRY_SECONDS", 0.0, raising=False)
    cleaner = _RetryingCleaner()
    oidc = _CloseAfterRetention(cleaner)
    dependencies = client.app.state.dependencies
    dependencies.user_services = UserServices(
        oauth_attempts=None,
        sessions=None,
        history=None,
        settings=None,
        retention_cleaner=cleaner,
        oidc_client=oidc,
    )

    app = create_app(dependencies.settings, dependencies)
    with TestClient(app):
        assert cleaner.running.wait(timeout=1.0)

    assert cleaner.calls >= 2
    assert cleaner.cancelled.is_set()
    assert oidc.closed


# Break caught: an unwrapped SQLite cleanup error permanently ends the only
# retention task instead of producing the fixed safe retry behavior.
def test_lifespan_retries_raw_sqlite_retention_failure(client, monkeypatch) -> None:
    monkeypatch.setattr(main_module, "_RETENTION_RETRY_SECONDS", 0.0, raising=False)
    cleaner = _RawSqliteRetryingCleaner()
    oidc = _CloseAfterRetention(cleaner)
    dependencies = client.app.state.dependencies
    dependencies.user_services = UserServices(
        oauth_attempts=None,
        sessions=None,
        history=None,
        settings=None,
        retention_cleaner=cleaner,
        oidc_client=oidc,
    )

    app = create_app(dependencies.settings, dependencies)
    with TestClient(app):
        assert cleaner.running.wait(timeout=1.0)

    assert cleaner.calls >= 2
    assert cleaner.cancelled.is_set()
    assert oidc.closed
