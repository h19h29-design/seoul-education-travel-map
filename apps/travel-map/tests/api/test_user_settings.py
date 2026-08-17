"""Authenticated encrypted workplace settings API contract."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from app.auth.models import SessionPrincipal, UserServices
from app.policy.models import VehicleUse
from app.routing.models import FuelType
from app.settings import Settings
from app.storage.models import StoredUserSettings
from app.trips.models import TripPattern
from fastapi.testclient import TestClient
from pydantic import SecretStr

_SESSION_COOKIE = "__Host-travel_session=session-token"
_PUBLIC_ORIGIN = "https://travel.h19h19.com"


def _principal() -> SessionPrincipal:
    return SessionPrincipal(
        user_id=41,
        token_hmac=b"s" * 32,
        csrf_hmac=b"c" * 32,
        expires_at=datetime(2099, 1, 1, tzinfo=UTC),
    )


def _services(*, sessions: AsyncMock, settings: AsyncMock) -> UserServices:
    return UserServices(
        oauth_attempts=AsyncMock(),
        sessions=sessions,
        history=AsyncMock(),
        settings=settings,
        retention_cleaner=AsyncMock(),
        oidc_client=AsyncMock(),
    )


def _settings_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "defaultOriginSiteId": "test-neis:B10:SEMWATER-ES:main",
        "defaultTripPattern": "OUTBOUND_ONLY_END_AFTER_SCHEDULE",
        "defaultDurationMinutes": 420,
        "vehicleUse": "PRIVATE",
        "fuelType": "DIESEL",
        "efficiencyKmPerLiter": 14.5,
        "parkingCostKrw": 3_000,
        "routeSort": "cost",
    }
    payload.update(overrides)
    return payload


def _configure_authenticated_settings(client: TestClient) -> None:
    client.app.state.dependencies.settings = Settings(
        environment="test",
        kakao_javascript_key=SecretStr("test-public-map-key"),
        kakao_rest_api_key=SecretStr("test-provider-rest-key"),
        seoul_transit_service_key=SecretStr("test-transit-key"),
        opinet_cert_key=SecretStr("test-fuel-key"),
        public_base_url=_PUBLIC_ORIGIN,
        user_database_path="/data/travel-map.sqlite3",
        kakao_oidc_client_id="test-login-client-id",
        kakao_oidc_client_secret=SecretStr("test-login-client-secret"),
        session_hmac_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        kakao_subject_hmac_key="AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE",
        data_encryption_key_v1="AgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgI",
        trusted_proxy_cidrs=("127.0.0.1/32",),
        allowed_hosts=("testserver",),
        allowed_origins=(_PUBLIC_ORIGIN,),
    )


def test_first_login_without_settings_returns_exact_non_null_defaults(
    client: TestClient,
) -> None:
    sessions = AsyncMock()
    sessions.resolve.return_value = _principal()
    settings = AsyncMock()
    settings.get.return_value = None
    dependencies = client.app.state.dependencies
    dependencies.user_services = _services(sessions=sessions, settings=settings)

    response = client.get(
        "/api/v1/me/settings",
        headers={"Cookie": "__Host-travel_session=session-token"},
    )

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == {
        "settings": {
            "defaultOriginSiteId": None,
            "defaultTripPattern": "ROUND_TRIP",
            "defaultDurationMinutes": 300,
            "vehicleUse": "NONE",
            "fuelType": "GASOLINE",
            "efficiencyKmPerLiter": 10.0,
            "parkingCostKrw": 0,
            "routeSort": "time",
        },
        "source": "DEFAULT",
        "resolvedDefaultOrigin": None,
        "warnings": [],
    }


def test_saved_active_default_origin_is_resolved_from_current_store(
    client: TestClient,
) -> None:
    sessions = AsyncMock()
    sessions.resolve.return_value = _principal()
    settings = AsyncMock()
    settings.get.return_value = StoredUserSettings(
        default_origin_site_id="test-neis:B10:SEMWATER-ES:main",
        default_trip_pattern=TripPattern.ROUND_TRIP,
        default_duration_minutes=300,
        vehicle_use=VehicleUse.NONE,
        fuel_type=FuelType.GASOLINE,
        efficiency_km_per_liter=10.0,
        parking_cost_krw=0,
        route_sort="time",
    )
    client.app.state.dependencies.user_services = _services(
        sessions=sessions, settings=settings
    )

    response = client.get("/api/v1/me/settings", headers={"Cookie": _SESSION_COOKIE})

    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "SAVED"
    assert body["resolvedDefaultOrigin"]["siteId"] == "test-neis:B10:SEMWATER-ES:main"
    assert body["warnings"] == []
    settings.get.assert_awaited_once_with(user_id=41)


def test_unavailable_saved_default_origin_is_preserved_with_warning(
    client: TestClient,
) -> None:
    sessions = AsyncMock()
    sessions.resolve.return_value = _principal()
    settings = AsyncMock()
    settings.get.return_value = StoredUserSettings(
        default_origin_site_id="test-neis:B10:CLOSED:main",
        default_trip_pattern=TripPattern.ROUND_TRIP,
        default_duration_minutes=300,
        vehicle_use=VehicleUse.NONE,
        fuel_type=FuelType.GASOLINE,
        efficiency_km_per_liter=10.0,
        parking_cost_krw=0,
        route_sort="time",
    )
    client.app.state.dependencies.user_services = _services(
        sessions=sessions, settings=settings
    )

    response = client.get("/api/v1/me/settings", headers={"Cookie": _SESSION_COOKIE})

    assert response.status_code == 200
    assert (
        response.json()["settings"]["defaultOriginSiteId"]
        == "test-neis:B10:CLOSED:main"
    )
    assert response.json()["resolvedDefaultOrigin"] is None
    assert response.json()["warnings"] == ["DEFAULT_ORIGIN_UNAVAILABLE"]


def test_get_settings_requires_a_current_session_without_reading_storage(
    client: TestClient,
) -> None:
    sessions = AsyncMock()
    sessions.resolve.return_value = None
    settings = AsyncMock()
    client.app.state.dependencies.user_services = _services(
        sessions=sessions, settings=settings
    )

    response = client.get("/api/v1/me/settings", headers={"Cookie": _SESSION_COOKIE})

    assert response.status_code == 401
    assert response.json() == {"error": {"code": "UNAUTHENTICATED"}}
    assert response.headers["Cache-Control"] == "no-store"
    settings.get.assert_not_awaited()


def test_put_settings_requires_session_origin_and_csrf_before_replacing(
    client: TestClient,
) -> None:
    sessions = AsyncMock()
    sessions.resolve.return_value = _principal()
    sessions.verify_csrf.return_value = True
    settings = AsyncMock()
    client.app.state.dependencies.user_services = _services(
        sessions=sessions, settings=settings
    )
    _configure_authenticated_settings(client)

    response = client.put(
        "/api/v1/me/settings",
        json=_settings_payload(),
        headers={
            "Cookie": _SESSION_COOKIE,
            "Origin": _PUBLIC_ORIGIN,
            "X-CSRF-Token": "csrf-token",
        },
    )

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["source"] == "SAVED"
    assert response.json()["resolvedDefaultOrigin"]["siteId"] == (
        "test-neis:B10:SEMWATER-ES:main"
    )
    settings.replace.assert_awaited_once_with(
        user_id=41,
        settings=StoredUserSettings(
            default_origin_site_id="test-neis:B10:SEMWATER-ES:main",
            default_trip_pattern=TripPattern.OUTBOUND_ONLY_END_AFTER_SCHEDULE,
            default_duration_minutes=420,
            vehicle_use=VehicleUse.PRIVATE,
            fuel_type=FuelType.DIESEL,
            efficiency_km_per_liter=14.5,
            parking_cost_krw=3_000,
            route_sort="cost",
        ),
    )


def test_put_settings_rejects_missing_origin_or_csrf_before_storage(
    client: TestClient,
) -> None:
    sessions = AsyncMock()
    sessions.resolve.return_value = _principal()
    sessions.verify_csrf.return_value = False
    settings = AsyncMock()
    client.app.state.dependencies.user_services = _services(
        sessions=sessions, settings=settings
    )
    _configure_authenticated_settings(client)

    missing_origin = client.put(
        "/api/v1/me/settings",
        json=_settings_payload(),
        headers={"Cookie": _SESSION_COOKIE, "X-CSRF-Token": "csrf-token"},
    )
    wrong_csrf = client.put(
        "/api/v1/me/settings",
        json=_settings_payload(),
        headers={
            "Cookie": _SESSION_COOKIE,
            "Origin": _PUBLIC_ORIGIN,
            "X-CSRF-Token": "wrong-csrf",
        },
    )

    assert missing_origin.status_code == 403
    assert missing_origin.json() == {"error": {"code": "INVALID_ORIGIN"}}
    assert missing_origin.headers["Cache-Control"] == "no-store"
    assert wrong_csrf.status_code == 403
    assert wrong_csrf.json() == {"error": {"code": "CSRF_FAILED"}}
    assert wrong_csrf.headers["Cache-Control"] == "no-store"
    settings.replace.assert_not_awaited()


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    (
        ("defaultDurationMinutes", True),
        ("parkingCostKrw", True),
        ("efficiencyKmPerLiter", True),
        ("defaultDurationMinutes", "420"),
        ("parkingCostKrw", "3000"),
        ("efficiencyKmPerLiter", "14.5"),
    ),
)
def test_put_settings_rejects_boolean_and_string_numeric_fields_before_storage(
    client: TestClient, field: str, invalid_value: object
) -> None:
    sessions = AsyncMock()
    sessions.resolve.return_value = _principal()
    sessions.verify_csrf.return_value = True
    settings = AsyncMock()
    client.app.state.dependencies.user_services = _services(
        sessions=sessions, settings=settings
    )
    _configure_authenticated_settings(client)

    response = client.put(
        "/api/v1/me/settings",
        json=_settings_payload(**{field: invalid_value}),
        headers={
            "Cookie": _SESSION_COOKIE,
            "Origin": _PUBLIC_ORIGIN,
            "X-CSRF-Token": "csrf-token",
        },
    )

    assert response.status_code == 422
    assert response.json() == {"error": {"code": "VALIDATION_ERROR"}}
    assert response.headers["Cache-Control"] == "no-store"
    settings.replace.assert_not_awaited()


def test_put_settings_accepts_whole_number_efficiency_and_normalizes_to_float(
    client: TestClient,
) -> None:
    sessions = AsyncMock()
    sessions.resolve.return_value = _principal()
    sessions.verify_csrf.return_value = True
    settings = AsyncMock()
    client.app.state.dependencies.user_services = _services(
        sessions=sessions, settings=settings
    )
    _configure_authenticated_settings(client)

    response = client.put(
        "/api/v1/me/settings",
        json=_settings_payload(efficiencyKmPerLiter=10),
        headers={
            "Cookie": _SESSION_COOKIE,
            "Origin": _PUBLIC_ORIGIN,
            "X-CSRF-Token": "csrf-token",
        },
    )

    assert response.status_code == 200
    assert response.json()["settings"]["efficiencyKmPerLiter"] == 10.0
    assert (
        settings.replace.await_args.kwargs["settings"].efficiency_km_per_liter == 10.0
    )


def test_put_settings_rejects_inactive_origin_and_undocumented_fields(
    client: TestClient,
) -> None:
    sessions = AsyncMock()
    sessions.resolve.return_value = _principal()
    sessions.verify_csrf.return_value = True
    settings = AsyncMock()
    client.app.state.dependencies.user_services = _services(
        sessions=sessions, settings=settings
    )
    _configure_authenticated_settings(client)
    headers = {
        "Cookie": _SESSION_COOKIE,
        "Origin": _PUBLIC_ORIGIN,
        "X-CSRF-Token": "csrf-token",
    }

    inactive = client.put(
        "/api/v1/me/settings",
        json=_settings_payload(defaultOriginSiteId="test-neis:B10:CLOSED:main"),
        headers=headers,
    )
    destination = client.put(
        "/api/v1/me/settings",
        json=_settings_payload(destination={"name": "must-not-store"}),
        headers=headers,
    )
    concrete_date = client.put(
        "/api/v1/me/settings",
        json=_settings_payload(startsAt="2026-08-18T09:00:00+09:00"),
        headers=headers,
    )

    assert inactive.status_code == 422
    assert inactive.headers["Cache-Control"] == "no-store"
    assert destination.status_code == 422
    assert destination.headers["Cache-Control"] == "no-store"
    assert concrete_date.status_code == 422
    assert concrete_date.headers["Cache-Control"] == "no-store"
    settings.replace.assert_not_awaited()
