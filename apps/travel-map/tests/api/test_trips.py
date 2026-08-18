import hmac
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from app.auth.models import SessionPrincipal, UserServices
from app.cache import WALK_TTL_SECONDS
from app.routing.models import Coordinate, TravelMode
from app.storage.crypto import UserDataUnavailableError
from app.storage.models import HistoryMetadata, StorageIntegrityError
from tests.api.conftest import trip_payload
from tests.institutions.test_store import load_store_with_main_site_name

ORIGIN = Coordinate(latitude=37.5501, longitude=126.9801)
DESTINATION = Coordinate(latitude=37.5662952, longitude=126.9779451)
STARTS_AT = datetime.fromisoformat("2026-08-10T09:00:00+09:00")
ENDS_AT = datetime.fromisoformat("2026-08-10T13:00:00+09:00")


def route_for(leg: dict[str, object], mode: str) -> dict[str, object]:
    routes = leg["routes"]
    assert isinstance(routes, list)
    return next(route for route in routes if route["mode"] == mode)


def test_authenticated_preview_writes_only_minimal_history_draft(client) -> None:
    sessions = AsyncMock()
    sessions.resolve.return_value = SessionPrincipal(
        user_id=73,
        token_hmac=b"s" * 32,
        csrf_hmac=hmac.digest(b"h" * 32, b"csrf-token", "sha256"),
        expires_at=datetime(2099, 1, 1, tzinfo=UTC),
    )
    sessions.verify_csrf.return_value = True
    history = AsyncMock()
    history.create.return_value = HistoryMetadata(
        id="A" * 22,
        user_id=73,
        created_at=datetime(2026, 8, 17, tzinfo=UTC),
        expires_at=datetime(2026, 8, 24, tzinfo=UTC),
    )
    dependencies = client.app.state.dependencies
    dependencies.settings = dependencies.settings.model_copy(
        update={"public_base_url": "https://travel.example.test"}
    )
    dependencies.user_services = UserServices(
        oauth_attempts=AsyncMock(),
        sessions=sessions,
        history=history,
        settings=AsyncMock(),
        retention_cleaner=AsyncMock(),
        oidc_client=AsyncMock(),
    )

    response = client.post(
        "/api/v1/trips/preview",
        json=trip_payload(),
        headers={
            "Cookie": "__Host-travel_session=session-token; "
            "__Host-travel_csrf=csrf-token",
            "Origin": "https://travel.example.test",
            "X-CSRF-Token": "csrf-token",
        },
    )

    assert response.status_code == 200
    draft = history.create.await_args.kwargs["draft"]
    assert history.create.await_args.kwargs["user_id"] == 73
    assert set(type(draft).__dataclass_fields__) == {
        "origin_site_id",
        "origin_name",
        "destination_name",
        "destination_address",
        "trip_pattern",
        "starts_at",
        "ends_at",
    }
    summary = history.create.await_args.kwargs["summary"]
    assert set(type(summary).__dataclass_fields__) == {
        "classification",
        "allowance_status",
        "allowance_krw",
        "route_legs",
        "rule_set_id",
        "effective_from",
    }
    assert [item.mode for item in summary.route_legs] == [
        TravelMode.CAR,
        TravelMode.CAR,
    ]
    assert [item.duration_seconds for item in summary.route_legs] == [900, 1_000]


def test_authenticated_preview_requires_origin_and_csrf_before_calculation_or_save(
    client,
    fake_route_providers,
) -> None:
    sessions = AsyncMock()
    sessions.resolve.return_value = SessionPrincipal(
        user_id=73,
        token_hmac=b"s" * 32,
        csrf_hmac=b"c" * 32,
        expires_at=datetime(2099, 1, 1, tzinfo=UTC),
    )
    sessions.verify_csrf.return_value = False
    history = AsyncMock()
    dependencies = client.app.state.dependencies
    dependencies.settings = dependencies.settings.model_copy(
        update={"public_base_url": "https://travel.example.test"}
    )
    dependencies.user_services = UserServices(
        oauth_attempts=AsyncMock(),
        sessions=sessions,
        history=history,
        settings=AsyncMock(),
        retention_cleaner=AsyncMock(),
        oidc_client=AsyncMock(),
    )

    missing_origin = client.post(
        "/api/v1/trips/preview",
        json=trip_payload(),
        headers={"Cookie": "__Host-travel_session=session-token"},
    )
    wrong_csrf = client.post(
        "/api/v1/trips/preview",
        json=trip_payload(),
        headers={
            "Cookie": "__Host-travel_session=session-token",
            "Origin": "https://travel.example.test",
            "X-CSRF-Token": "wrong-csrf",
        },
    )

    assert missing_origin.status_code == 403
    assert missing_origin.json() == {"error": {"code": "INVALID_ORIGIN"}}
    assert wrong_csrf.status_code == 403
    assert wrong_csrf.json() == {"error": {"code": "CSRF_FAILED"}}
    assert all(provider.call_count == 0 for provider in fake_route_providers.values())
    history.create.assert_not_awaited()


def test_history_save_failure_keeps_public_preview_with_fixed_warning(client) -> None:
    sessions = AsyncMock()
    sessions.resolve.return_value = SessionPrincipal(
        user_id=73,
        token_hmac=b"s" * 32,
        csrf_hmac=b"c" * 32,
        expires_at=datetime(2099, 1, 1, tzinfo=UTC),
    )
    sessions.verify_csrf.return_value = True
    history = AsyncMock()
    history.create.side_effect = UserDataUnavailableError()
    dependencies = client.app.state.dependencies
    dependencies.settings = dependencies.settings.model_copy(
        update={"public_base_url": "https://travel.example.test"}
    )
    dependencies.user_services = UserServices(
        oauth_attempts=AsyncMock(),
        sessions=sessions,
        history=history,
        settings=AsyncMock(),
        retention_cleaner=AsyncMock(),
        oidc_client=AsyncMock(),
    )

    response = client.post(
        "/api/v1/trips/preview",
        json=trip_payload(),
        headers={
            "Cookie": "__Host-travel_session=session-token",
            "Origin": "https://travel.example.test",
            "X-CSRF-Token": "csrf-token",
        },
    )

    assert response.status_code == 200
    assert "HISTORY_NOT_SAVED" in response.json()["warnings"]
    history.create.assert_awaited_once()


@pytest.mark.parametrize(
    "failure",
    [
        StorageIntegrityError("storage input is invalid"),
        sqlite3.OperationalError("storage unavailable"),
    ],
)
def test_history_storage_write_failure_keeps_public_preview_with_fixed_warning(
    client,
    failure: Exception,
) -> None:
    sessions = AsyncMock()
    sessions.resolve.return_value = SessionPrincipal(
        user_id=73,
        token_hmac=b"s" * 32,
        csrf_hmac=b"c" * 32,
        expires_at=datetime(2099, 1, 1, tzinfo=UTC),
    )
    sessions.verify_csrf.return_value = True
    history = AsyncMock()
    history.create.side_effect = failure
    dependencies = client.app.state.dependencies
    dependencies.settings = dependencies.settings.model_copy(
        update={"public_base_url": "https://travel.example.test"}
    )
    dependencies.user_services = UserServices(
        oauth_attempts=AsyncMock(),
        sessions=sessions,
        history=history,
        settings=AsyncMock(),
        retention_cleaner=AsyncMock(),
        oidc_client=AsyncMock(),
    )

    response = client.post(
        "/api/v1/trips/preview",
        json=trip_payload(),
        headers={
            "Cookie": "__Host-travel_session=session-token",
            "Origin": "https://travel.example.test",
            "X-CSRF-Token": "csrf-token",
        },
    )

    assert response.status_code == 200
    assert "HISTORY_NOT_SAVED" in response.json()["warnings"]
    assert str(failure) not in response.text


def test_session_cipher_failure_stays_anonymous_without_saving_or_private_leak(
    client,
) -> None:
    sessions = AsyncMock()
    sessions.resolve.side_effect = UserDataUnavailableError()
    history = AsyncMock()
    dependencies = client.app.state.dependencies
    dependencies.user_services = UserServices(
        oauth_attempts=AsyncMock(),
        sessions=sessions,
        history=history,
        settings=AsyncMock(),
        retention_cleaner=AsyncMock(),
        oidc_client=AsyncMock(),
    )
    opaque_token = "opaque-session-sentinel"

    response = client.post(
        "/api/v1/trips/preview",
        json=trip_payload(),
        headers={"Cookie": f"__Host-travel_session={opaque_token}"},
    )

    assert response.status_code == 200
    assert "HISTORY_NOT_SAVED" in response.json()["warnings"]
    assert opaque_token not in response.text
    history.create.assert_not_awaited()


# Production mutation caught: planning only the outbound leg, swapping a leg's
# endpoints/time, or letting display and classification providers plan independently.
def test_round_trip_queries_both_display_and_classification_legs_at_exact_times(
    client,
    fake_route_providers,
    fake_classification_provider,
) -> None:
    response = client.post("/api/v1/trips/preview", json=trip_payload())

    assert response.status_code == 200
    body = response.json()
    assert [leg["direction"] for leg in body["routeLegs"]] == ["OUTBOUND", "RETURN"]
    for mode, provider in fake_route_providers.items():
        assert len(provider.queries) == 2
        outbound, returning = provider.queries
        assert (outbound.origin, outbound.destination, outbound.depart_at) == (
            ORIGIN,
            DESTINATION,
            STARTS_AT,
        )
        assert (returning.origin, returning.destination, returning.depart_at) == (
            DESTINATION,
            ORIGIN,
            ENDS_AT,
        )
        assert outbound.mode is returning.mode is mode
    assert [
        (query.origin, query.destination, query.depart_at, query.mode)
        for query in fake_classification_provider.queries
    ] == [
        (ORIGIN, DESTINATION, STARTS_AT, TravelMode.CAR),
        (DESTINATION, ORIGIN, ENDS_AT, TravelMode.CAR),
    ]


# Production mutation caught: issuing a hidden return provider request or adding
# a return-leg fastest cost to an outbound-only preview.
def test_outbound_only_never_queries_return_providers_or_counts_return_cost(
    client,
    fake_route_providers,
    fake_classification_provider,
) -> None:
    response = client.post(
        "/api/v1/trips/preview",
        json=trip_payload(tripPattern="OUTBOUND_ONLY_END_AFTER_SCHEDULE"),
    )

    assert response.status_code == 200
    body = response.json()
    assert [leg["direction"] for leg in body["routeLegs"]] == ["OUTBOUND"]
    assert body["mobilityCost"] == {
        "status": "ESTIMATED",
        "amountKrw": 2_000,
        "warnings": [],
    }
    for mode, provider in fake_route_providers.items():
        assert len(provider.queries) == 1
        query = provider.queries[0]
        assert (query.origin, query.destination, query.depart_at, query.mode) == (
            ORIGIN,
            DESTINATION,
            STARTS_AT,
            mode,
        )
    assert len(fake_classification_provider.queries) == 1
    assert fake_classification_provider.queries[0].origin == ORIGIN


# Production mutation caught: treating return-only as an outbound departure or
# querying it at startsAt instead of endsAt.
def test_return_only_queries_destination_to_workplace_at_ends_at_only(
    client,
    fake_route_providers,
    fake_classification_provider,
) -> None:
    response = client.post(
        "/api/v1/trips/preview",
        json=trip_payload(
            tripPattern="RETURN_ONLY_DIRECT_TO_DESTINATION",
            carAssumptions={
                "fuelType": "GASOLINE",
                "efficiencyKmPerLiter": 10.0,
                "parkingCostKrw": 700,
            },
        ),
    )

    assert response.status_code == 200
    body = response.json()
    assert [leg["direction"] for leg in body["routeLegs"]] == ["RETURN"]
    assert body["mobilityCost"]["amountKrw"] == 2_700
    for mode, provider in fake_route_providers.items():
        assert len(provider.queries) == 1
        query = provider.queries[0]
        assert (query.origin, query.destination, query.depart_at, query.mode) == (
            DESTINATION,
            ORIGIN,
            ENDS_AT,
            mode,
        )
    assert [
        (query.origin, query.destination, query.depart_at, query.mode)
        for query in fake_classification_provider.queries
    ] == [(DESTINATION, ORIGIN, ENDS_AT, TravelMode.CAR)]


# Production mutation caught: flattening both directions into one route collection
# or exposing one direction's fastest amount as the whole-trip total.
def test_route_legs_keep_directional_routes_and_costs_separate(client) -> None:
    response = client.post("/api/v1/trips/preview", json=trip_payload())

    assert response.status_code == 200
    body = response.json()
    outbound, returning = body["routeLegs"]
    assert outbound["departAt"] == "2026-08-10T09:00:00+09:00"
    assert returning["departAt"] == "2026-08-10T13:00:00+09:00"
    assert outbound["mobilityCost"]["amountKrw"] == 2_000
    assert returning["mobilityCost"]["amountKrw"] == 2_500
    assert outbound["best"]["fastestRouteId"] == route_for(outbound, "CAR")["id"]
    assert returning["best"]["fastestRouteId"] == route_for(returning, "CAR")["id"]
    assert body["mobilityCost"]["amountKrw"] == 4_500
    assert "routes" not in body
    assert "best" not in body


# Production mutation caught: summing only known directional fastest costs and
# presenting a partial trip total instead of withholding the aggregate amount.
def test_unknown_fastest_leg_cost_makes_aggregate_mobility_cost_partial(
    client,
    fake_route_providers,
) -> None:
    fake_route_providers[TravelMode.CAR].return_unknown_cost_on_call(2)

    response = client.post("/api/v1/trips/preview", json=trip_payload())

    assert response.status_code == 200
    body = response.json()
    assert [leg["mobilityCost"] for leg in body["routeLegs"]] == [
        {"status": "ESTIMATED", "amountKrw": 2_000, "warnings": []},
        {"status": "UNKNOWN", "amountKrw": None, "warnings": []},
    ]
    assert body["mobilityCost"] == {
        "status": "UNKNOWN",
        "amountKrw": None,
        "warnings": ["PARTIAL_MOBILITY_COST"],
    }


# Production mutation caught: forwarding the configured parking assumption into
# both round-trip car queries instead of only the outbound car leg.
def test_round_trip_applies_parking_cost_exactly_once(
    client,
    fake_route_providers,
) -> None:
    response = client.post(
        "/api/v1/trips/preview",
        json=trip_payload(
            carAssumptions={
                "fuelType": "GASOLINE",
                "efficiencyKmPerLiter": 10.0,
                "parkingCostKrw": 3_000,
            }
        ),
    )

    assert response.status_code == 200
    body = response.json()
    cars = [route_for(leg, "CAR") for leg in body["routeLegs"]]
    assert [car["costBreakdown"]["parkingKrw"] for car in cars] == [3_000, 0]
    assert sum(car["costBreakdown"]["parkingKrw"] for car in cars) == 3_000
    assert body["mobilityCost"]["amountKrw"] == 7_500
    car_queries = fake_route_providers[TravelMode.CAR].queries
    assert [query.car_assumptions.parking_cost_krw for query in car_queries] == [
        3_000,
        0,
    ]


# Production mutation caught: using coverage to suppress a supported one-way route,
# or paying an outside trip from one-way lower-bound evidence.
def test_outside_one_way_returns_its_actual_route_leg_but_requires_allowance_review(
    client,
    fake_route_providers,
) -> None:
    response = client.post(
        "/api/v1/trips/preview",
        json=trip_payload(
            tripPattern="OUTBOUND_ONLY_END_AFTER_SCHEDULE",
            destination={
                "name": "부산광역시청",
                "address": "부산광역시 연제구 중앙대로 1001",
                "latitude": 35.1798159,
                "longitude": 129.0750222,
            },
        ),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["coverage"]["status"] == "OUT_OF_COVERAGE"
    assert [leg["direction"] for leg in body["routeLegs"]] == ["OUTBOUND"]
    assert len(body["routeLegs"][0]["routes"]) == 3
    assert all(provider.call_count == 1 for provider in fake_route_providers.values())
    assert body["classificationDistanceMeters"] == 1_500
    assert body["classificationDistanceBasis"] == "ONE_WAY_LOWER_BOUND"
    assert body["classification"] == "REVIEW_REQUIRED"
    assert body["allowance"] == {
        "status": "REVIEW_REQUIRED",
        "amountKrw": None,
        "warnings": ["TRIP_PATTERN_DISTANCE_RULE_UNVERIFIED"],
    }


# Production mutation caught: treating a BUFFER destination as provider-ineligible
# or converting its one-way distance into a fabricated exact round trip.
def test_buffer_one_way_returns_its_actual_route_leg_but_requires_allowance_review(
    client,
    fake_route_providers,
) -> None:
    response = client.post(
        "/api/v1/trips/preview",
        json=trip_payload(
            tripPattern="RETURN_ONLY_DIRECT_TO_DESTINATION",
            destination={
                "name": "서울 북쪽 지원영역",
                "address": "경기도 고양시 덕양구",
                "latitude": 37.61,
                "longitude": 126.98,
            },
        ),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["coverage"]["status"] == "BUFFER"
    assert [leg["direction"] for leg in body["routeLegs"]] == ["RETURN"]
    assert len(body["routeLegs"][0]["routes"]) == 3
    assert all(provider.call_count == 1 for provider in fake_route_providers.values())
    assert body["classificationDistanceBasis"] == "ONE_WAY_LOWER_BOUND"
    assert body["allowance"]["status"] == "REVIEW_REQUIRED"
    assert body["allowance"]["amountKrw"] is None
    assert body["allowance"]["warnings"] == ["TRIP_PATTERN_DISTANCE_RULE_UNVERIFIED"]


# Production mutation caught: treating a short Seoul one-way lower bound as proof
# that the inclusive two-kilometre rule has been resolved.
def test_short_seoul_one_way_requires_distance_rule_review(client) -> None:
    response = client.post(
        "/api/v1/trips/preview",
        json=trip_payload(tripPattern="OUTBOUND_ONLY_END_AFTER_SCHEDULE"),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["classification"] == "LOCAL"
    assert body["classificationDistanceMeters"] == 1_500
    assert body["classificationDistanceBasis"] == "ONE_WAY_LOWER_BOUND"
    assert body["allowance"] == {
        "status": "REVIEW_REQUIRED",
        "amountKrw": None,
        "warnings": ["TRIP_PATTERN_DISTANCE_RULE_UNVERIFIED"],
    }


# Production mutation caught: substituting zero or a claimed basis when any
# classification leg required by the selected pattern is missing.
def test_missing_classification_leg_has_no_distance_basis_and_requires_review(
    client,
    fake_classification_provider,
) -> None:
    fake_classification_provider.return_no_route_on_call(2)

    response = client.post("/api/v1/trips/preview", json=trip_payload())

    assert response.status_code == 200
    body = response.json()
    assert body["routeLegs"]
    assert body["classification"] == "REVIEW_REQUIRED"
    assert body["classificationDistanceMeters"] is None
    assert body["classificationDistanceBasis"] is None
    assert body["classificationPath"] is None
    assert body["allowance"] == {
        "status": "REVIEW_REQUIRED",
        "amountKrw": None,
        "warnings": ["DISTANCE_EVIDENCE_UNAVAILABLE"],
    }
    assert "DISTANCE_EVIDENCE_UNAVAILABLE" in body["warnings"]


# Production mutation caught: retaining either removed request field as an alias
# or allowing callers to choose the server-owned policy scope.
@pytest.mark.parametrize(
    ("legacy_field", "value"),
    [
        ("returnsAt", "2026-08-10T13:00:00+09:00"),
        ("policyProfile", "NONPUBLIC_OR_UNKNOWN"),
    ],
)
def test_preview_rejects_legacy_returns_at_and_caller_policy_profile(
    client,
    legacy_field: str,
    value: str,
) -> None:
    payload = trip_payload()
    payload[legacy_field] = value

    response = client.post("/api/v1/trips/preview", json=payload)

    assert response.status_code == 422


# Production mutation caught: using naive wall time, a non-positive/one-minute
# interval, or a duration longer than the public 24-hour contract.
@pytest.mark.parametrize(
    "overrides",
    [
        {"startsAt": "2026-08-10T09:00:00"},
        {"endsAt": "2026-08-10T13:00:00"},
        {"endsAt": "2026-08-10T08:59:00+09:00"},
        {"endsAt": "2026-08-10T09:01:00+09:00"},
        {"endsAt": "2026-08-11T09:00:01+09:00"},
    ],
)
def test_preview_rejects_naive_reversed_one_minute_and_over_twenty_four_hour_intervals(
    client,
    overrides: dict[str, object],
) -> None:
    response = client.post(
        "/api/v1/trips/preview",
        json=trip_payload(**overrides),
    )

    assert response.status_code == 422


# Production mutation caught: loosening the canonical site-id grammar or either
# side of the previous-allowance public bound.
@pytest.mark.parametrize(
    "overrides",
    [
        {"originSiteId": "BAD SITE ID"},
        {"previousAllowanceKrw": -1},
        {"previousAllowanceKrw": 20_001},
    ],
)
def test_preview_rejects_invalid_origin_id_and_previous_allowance_bounds(
    client,
    overrides: dict[str, object],
) -> None:
    response = client.post(
        "/api/v1/trips/preview",
        json=trip_payload(**overrides),
    )

    assert response.status_code == 422


# Production mutation caught: deriving policy scope from request/login state or
# returning anything other than the fixed public Seoul education profile.
@pytest.mark.parametrize(
    "trip_pattern",
    [
        "ROUND_TRIP",
        "OUTBOUND_ONLY_END_AFTER_SCHEDULE",
        "RETURN_ONLY_DIRECT_TO_DESTINATION",
    ],
)
def test_preview_always_reports_seoul_education_policy_scope(
    client,
    trip_pattern: str,
) -> None:
    response = client.post(
        "/api/v1/trips/preview",
        json=trip_payload(tripPattern=trip_pattern),
    )

    assert response.status_code == 200
    assert response.json()["policyScope"] == "SEOUL_EDU_PUBLIC_OFFICIAL_CONFIRMED"


# Production mutation caught: showing the source site token (including `main`)
# as a trip origin despite the canonical picker/search display-name rule.
def test_main_site_uses_official_name_in_search_and_trip_origin(
    client,
    tmp_path: Path,
) -> None:
    client.app.state.dependencies.institutions = load_store_with_main_site_name(
        tmp_path
    )
    search = client.get("/api/v1/institutions", params={"q": "샘물초등학교"})
    preview = client.post("/api/v1/trips/preview", json=trip_payload())

    assert search.status_code == preview.status_code == 200
    searched = next(
        item
        for item in search.json()["items"]
        if item["siteId"] == "test-neis:B10:SEMWATER-ES:main"
    )
    assert searched["displayName"] == "샘물초등학교"
    assert preview.json()["origin"]["name"] == "샘물초등학교"


# Production mutation caught: trusting caller coordinates for the origin or
# merging mobility cost and allowance into one amount.
def test_trip_preview_resolves_origin_by_site_id_and_separates_costs(client) -> None:
    response = client.post("/api/v1/trips/preview", json=trip_payload())

    assert response.status_code == 200
    body = response.json()
    assert body["origin"]["siteId"] == "test-neis:B10:SEMWATER-ES:main"
    assert body["routeLegs"][0]["best"]["fastestRouteId"]
    assert body["mobilityCost"] != body["allowance"]
    assert body["allowance"]["amountKrw"] == 20_000


def test_trip_preview_rejects_caller_supplied_origin_coordinates(client) -> None:
    payload = trip_payload()
    payload["origin"] = {"latitude": 35.0, "longitude": 129.0}

    response = client.post("/api/v1/trips/preview", json=payload)

    assert response.status_code == 422


def test_trip_preview_rejects_snake_case_request_fields(client) -> None:
    payload = trip_payload()
    payload["previous_allowance_krw"] = payload.pop("previousAllowanceKrw")

    response = client.post("/api/v1/trips/preview", json=payload)

    assert response.status_code == 422


def test_trip_preview_accepts_camel_case_request_fields(client) -> None:
    response = client.post("/api/v1/trips/preview", json=trip_payload())

    assert response.status_code == 200


def test_same_day_remaining_allowance_never_exceeds_the_daily_cap(client) -> None:
    response = client.post(
        "/api/v1/trips/preview",
        json=trip_payload(
            hasOtherLocalTripsToday=True,
            previousAllowanceKrw=10_000,
        ),
    )

    assert response.status_code == 200
    assert response.json()["allowance"]["amountKrw"] == 10_000

    exhausted = client.post(
        "/api/v1/trips/preview",
        json=trip_payload(
            hasOtherLocalTripsToday=True,
            previousAllowanceKrw=20_000,
        ),
    )

    assert exhausted.status_code == 200
    assert exhausted.json()["allowance"]["amountKrw"] == 0


# Production mutation caught: repeating an identical preview invokes either
# direction's display providers inside their five-minute cache window.
def test_trip_preview_caches_display_routes(client, fake_provider) -> None:
    first = client.post("/api/v1/trips/preview", json=trip_payload())
    second = client.post("/api/v1/trips/preview", json=trip_payload())

    assert first.status_code == second.status_code == 200
    assert fake_provider.call_count == 2


# Production mutation caught: a combined five-minute display cache expires walk
# results with car/transit instead of retaining each direction's seven-day walk entry.
def test_trip_preview_keeps_walk_cached_after_car_and_transit_expire(
    client,
    cache_clock,
) -> None:
    first = client.post("/api/v1/trips/preview", json=trip_payload())
    cache_clock[0] += 301.0
    second = client.post("/api/v1/trips/preview", json=trip_payload())

    assert first.status_code == second.status_code == 200
    first_ids = {
        (leg["direction"], route["mode"]): route["id"]
        for leg in first.json()["routeLegs"]
        for route in leg["routes"]
    }
    second_ids = {
        (leg["direction"], route["mode"]): route["id"]
        for leg in second.json()["routeLegs"]
        for route in leg["routes"]
    }
    for direction in ("OUTBOUND", "RETURN"):
        assert second_ids[(direction, "WALK")] == first_ids[(direction, "WALK")]
        assert second_ids[(direction, "CAR")] != first_ids[(direction, "CAR")]
        assert second_ids[(direction, "TRANSIT")] != first_ids[(direction, "TRANSIT")]

    cache_clock[0] = WALK_TTL_SECONDS + 1.0
    expired = client.post("/api/v1/trips/preview", json=trip_payload())
    expired_ids = {
        (leg["direction"], route["mode"]): route["id"]
        for leg in expired.json()["routeLegs"]
        for route in leg["routes"]
    }
    assert expired.status_code == 200
    for direction in ("OUTBOUND", "RETURN"):
        assert expired_ids[(direction, "WALK")] != first_ids[(direction, "WALK")]


# Production mutation caught: using display distance or one direction only for
# the legal two-kilometre branch of a round trip.
def test_seoul_destination_uses_two_directional_distance_for_two_km_branch(
    client,
    fake_classification_provider,
) -> None:
    fake_classification_provider.set_directional_distances(900, 1_100)

    response = client.post("/api/v1/trips/preview", json=trip_payload())

    assert response.status_code == 200
    body = response.json()
    assert body["classification"] == "LOCAL"
    assert body["classificationDistanceMeters"] == 2_000
    assert body["classificationDistanceBasis"] == "ROUND_TRIP_EXACT"
    assert body["allowance"]["status"] == "REVIEW_REQUIRED"
    assert body["allowance"]["amountKrw"] is None
    assert [query.origin for query in fake_classification_provider.queries] == [
        fake_classification_provider.site_coordinate,
        fake_classification_provider.destination_coordinate,
    ]
