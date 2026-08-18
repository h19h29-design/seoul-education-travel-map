"""Owner-scoped encrypted-history API contract."""

import json
from base64 import urlsafe_b64encode
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import ANY, AsyncMock, call

import pytest
from app.auth.models import SessionPrincipal, UserServices
from app.routing.models import TravelMode
from app.storage.crypto import UserDataUnavailableError
from app.storage.models import (
    HistoryCursor,
    HistoryDetail,
    HistoryListItem,
    HistoryMetadata,
    HistoryPage,
    HistoryRecalculationDraft,
    HistoryRouteLegSummary,
    HistorySummary,
    format_storage_timestamp,
)
from app.trips.models import RouteDirection, TripPattern
from fastapi.testclient import TestClient

_SESSION_COOKIE = "__Host-travel_session=session-token"
_PUBLIC_ORIGIN = "https://travel.h19h19.com"
_HISTORY_ID = "a" * 22
_NEXT_HISTORY_ID = "b" * 22
_CALCULATED_AT = datetime(2026, 8, 18, 1, 2, 3, 4, tzinfo=UTC)


def _principal() -> SessionPrincipal:
    return SessionPrincipal(
        user_id=73,
        token_hmac=b"s" * 32,
        csrf_hmac=b"c" * 32,
        expires_at=datetime(2099, 1, 1, tzinfo=UTC),
    )


def _services(*, sessions: AsyncMock, history: AsyncMock) -> UserServices:
    return UserServices(
        oauth_attempts=AsyncMock(),
        sessions=sessions,
        history=history,
        settings=AsyncMock(),
        retention_cleaner=AsyncMock(),
        oidc_client=AsyncMock(),
    )


def _configure_mutating_origin(client: TestClient) -> None:
    client.app.state.dependencies.settings = (
        client.app.state.dependencies.settings.model_copy(
            update={"public_base_url": _PUBLIC_ORIGIN}
        )
    )


def _detail(
    *,
    history_id: str = _HISTORY_ID,
    origin_site_id: str = "test-neis:B10:SEMWATER-ES:main",
) -> HistoryDetail:
    metadata = HistoryMetadata(
        id=history_id,
        user_id=73,
        created_at=_CALCULATED_AT,
        expires_at=_CALCULATED_AT + timedelta(hours=168),
    )
    return HistoryDetail(
        metadata=metadata,
        draft=HistoryRecalculationDraft(
            origin_site_id=origin_site_id,
            origin_name="stored origin label",
            destination_name="stored destination label",
            destination_address="stored destination address",
            trip_pattern=TripPattern.ROUND_TRIP,
            starts_at=datetime(2026, 8, 18, 9, 0, tzinfo=UTC),
            ends_at=datetime(2026, 8, 18, 13, 0, tzinfo=UTC),
        ),
        summary=HistorySummary(
            classification="LOCAL",
            allowance_status="ELIGIBLE",
            allowance_krw=20_000,
            route_legs=(
                HistoryRouteLegSummary(
                    direction=RouteDirection.OUTBOUND,
                    mode=TravelMode.TRANSIT,
                    duration_seconds=900,
                    distance_meters=4_000,
                    mobility_cost_krw=1_550,
                ),
            ),
            rule_set_id="seoul-2026",
            effective_from="2026-01-01",
        ),
    )


def _list_item(detail: HistoryDetail) -> HistoryListItem:
    return HistoryListItem(
        metadata=detail.metadata,
        origin_name=detail.draft.origin_name,
        destination_name=detail.draft.destination_name,
        trip_pattern=detail.draft.trip_pattern,
        classification=detail.summary.classification,
        allowance_status=detail.summary.allowance_status,
        allowance_krw=detail.summary.allowance_krw,
    )


def _canonical_cursor(cursor: HistoryCursor) -> str:
    raw = json.dumps(
        {
            "createdAt": format_storage_timestamp(cursor.created_at),
            "historyId": cursor.history_id,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _encoded_cursor_payload(payload: object) -> str:
    return (
        urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        .rstrip(b"=")
        .decode("ascii")
    )


def test_history_list_is_scoped_to_authenticated_user(client: TestClient) -> None:
    sessions = AsyncMock()
    sessions.resolve.return_value = _principal()
    history = AsyncMock()
    history.list_page.return_value = HistoryPage(items=(), next_cursor=None)
    client.app.state.dependencies.user_services = _services(
        sessions=sessions, history=history
    )

    response = client.get(
        "/api/v1/me/history",
        headers={"Cookie": _SESSION_COOKIE},
    )

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == {"items": [], "nextCursor": None}
    history.list_page.assert_awaited_once_with(user_id=73, before=None, limit=50)
    sessions.resolve.assert_awaited_once_with(raw_token="session-token", now=ANY)


def test_history_list_uses_only_canonical_opaque_cursor_and_owner(
    client: TestClient,
) -> None:
    sessions = AsyncMock()
    sessions.resolve.return_value = _principal()
    history = AsyncMock()
    detail = _detail()
    cursor = HistoryCursor(
        created_at=_CALCULATED_AT - timedelta(microseconds=1),
        history_id=_NEXT_HISTORY_ID,
    )
    history.list_page.side_effect = (
        HistoryPage(items=(_list_item(detail),), next_cursor=cursor),
        HistoryPage(items=(), next_cursor=None),
    )
    client.app.state.dependencies.user_services = _services(
        sessions=sessions, history=history
    )

    first = client.get("/api/v1/me/history", headers={"Cookie": _SESSION_COOKIE})
    expected_cursor = _canonical_cursor(cursor)
    second = client.get(
        "/api/v1/me/history",
        params={"cursor": expected_cursor, "limit": "1"},
        headers={"Cookie": _SESSION_COOKIE},
    )

    assert first.status_code == second.status_code == 200
    assert (
        first.headers["Cache-Control"] == second.headers["Cache-Control"] == "no-store"
    )
    assert first.json()["nextCursor"] == expected_cursor
    assert "=" not in expected_cursor
    assert second.json() == {"items": [], "nextCursor": None}
    history.list_page.assert_has_awaits(
        (
            call(user_id=73, before=None, limit=50),
            call(user_id=73, before=cursor, limit=1),
        )
    )


@pytest.mark.parametrize(
    "params",
    (
        {"limit": "0"},
        {"limit": "101"},
        {"limit": "true"},
        {"limit": "1.0"},
        {"limit": "01"},
        {"cursor": "not=canonical"},
        {"cursor": "bm90LWpzb24"},
        {"cursor": urlsafe_b64encode(b"\xff").rstrip(b"=").decode("ascii")},
        {
            "cursor": _encoded_cursor_payload(
                {
                    "createdAt": "2026-08-18T01:02:03.000004+00:00",
                    "historyId": _HISTORY_ID,
                }
            )
        },
        {
            "cursor": _encoded_cursor_payload(
                {
                    "historyId": _HISTORY_ID,
                    "createdAt": format_storage_timestamp(_CALCULATED_AT),
                }
            )
        },
    ),
)
def test_history_list_rejects_invalid_query_before_repository(
    client: TestClient, params: dict[str, str]
) -> None:
    sessions = AsyncMock()
    sessions.resolve.return_value = _principal()
    history = AsyncMock()
    client.app.state.dependencies.user_services = _services(
        sessions=sessions, history=history
    )

    response = client.get(
        "/api/v1/me/history", params=params, headers={"Cookie": _SESSION_COOKIE}
    )

    assert response.status_code == 422
    assert response.json() == {"error": {"code": "VALIDATION_ERROR"}}
    assert response.headers["Cache-Control"] == "no-store"
    history.list_page.assert_not_awaited()


def test_history_detail_serializes_only_approved_minimal_data_and_current_origin(
    client: TestClient,
) -> None:
    sessions = AsyncMock()
    sessions.resolve.return_value = _principal()
    history = AsyncMock()
    history.get.return_value = _detail()
    client.app.state.dependencies.user_services = _services(
        sessions=sessions, history=history
    )

    response = client.get(
        f"/api/v1/me/history/{_HISTORY_ID}", headers={"Cookie": _SESSION_COOKIE}
    )

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    body = response.json()
    assert set(body["recalculationDraft"]) == {
        "originSiteId",
        "originName",
        "destinationName",
        "destinationAddress",
        "tripPattern",
        "startsAt",
        "endsAt",
    }
    assert set(body["routeSummary"][0]) == {
        "direction",
        "mode",
        "durationSeconds",
        "distanceMeters",
        "mobilityCostKrw",
    }
    assert body["resolvedOrigin"]["siteId"] == "test-neis:B10:SEMWATER-ES:main"
    assert body["warnings"] == []
    history.get.assert_awaited_once_with(user_id=73, history_id=_HISTORY_ID)


def test_history_detail_preserves_labels_but_marks_inactive_origin_unavailable(
    client: TestClient,
) -> None:
    sessions = AsyncMock()
    sessions.resolve.return_value = _principal()
    history = AsyncMock()
    history.get.return_value = _detail(origin_site_id="test-neis:B10:CLOSED:main")
    client.app.state.dependencies.user_services = _services(
        sessions=sessions, history=history
    )

    response = client.get(
        f"/api/v1/me/history/{_HISTORY_ID}", headers={"Cookie": _SESSION_COOKIE}
    )

    assert response.status_code == 200
    assert response.json()["recalculationDraft"]["originName"] == "stored origin label"
    assert response.json()["resolvedOrigin"] is None
    assert response.json()["warnings"] == ["HISTORY_ORIGIN_UNAVAILABLE"]


def test_history_detail_fails_closed_if_repository_returns_another_owner(
    client: TestClient,
) -> None:
    sessions = AsyncMock()
    sessions.resolve.return_value = _principal()
    history = AsyncMock()
    detail = _detail()
    history.get.return_value = replace(
        detail, metadata=replace(detail.metadata, user_id=74)
    )
    client.app.state.dependencies.user_services = _services(
        sessions=sessions, history=history
    )

    response = client.get(
        f"/api/v1/me/history/{_HISTORY_ID}", headers={"Cookie": _SESSION_COOKIE}
    )

    assert response.status_code == 503
    assert response.json() == {"error": {"code": "AUTH_UNAVAILABLE"}}
    assert response.headers["Cache-Control"] == "no-store"
    assert "stored destination label" not in response.text


def test_history_delete_requires_owner_origin_and_csrf_before_mutation(
    client: TestClient,
) -> None:
    sessions = AsyncMock()
    sessions.resolve.return_value = _principal()
    sessions.verify_csrf.return_value = True
    history = AsyncMock()
    history.delete.return_value = True
    client.app.state.dependencies.user_services = _services(
        sessions=sessions, history=history
    )
    _configure_mutating_origin(client)
    headers = {"Cookie": _SESSION_COOKIE, "X-CSRF-Token": "csrf-token"}

    wrong_origin = client.delete(
        f"/api/v1/me/history/{_HISTORY_ID}",
        headers={**headers, "Origin": "https://attacker.example"},
    )
    missing_csrf = client.delete(
        f"/api/v1/me/history/{_HISTORY_ID}",
        headers={"Cookie": _SESSION_COOKIE, "Origin": _PUBLIC_ORIGIN},
    )
    good = client.delete(
        f"/api/v1/me/history/{_HISTORY_ID}",
        headers={**headers, "Origin": _PUBLIC_ORIGIN},
    )

    assert wrong_origin.status_code == 403
    assert wrong_origin.headers["Cache-Control"] == "no-store"
    assert missing_csrf.status_code == 403
    assert missing_csrf.headers["Cache-Control"] == "no-store"
    assert good.status_code == 204
    assert good.headers["Cache-Control"] == "no-store"
    history.delete.assert_awaited_once_with(user_id=73, history_id=_HISTORY_ID)


def test_history_delete_all_is_owner_scoped_and_storage_errors_fail_closed(
    client: TestClient,
) -> None:
    sessions = AsyncMock()
    sessions.resolve.return_value = _principal()
    sessions.verify_csrf.return_value = True
    history = AsyncMock()
    history.delete_all.return_value = 2
    client.app.state.dependencies.user_services = _services(
        sessions=sessions, history=history
    )
    _configure_mutating_origin(client)

    deleted = client.delete(
        "/api/v1/me/history",
        headers={
            "Cookie": _SESSION_COOKIE,
            "Origin": _PUBLIC_ORIGIN,
            "X-CSRF-Token": "csrf-token",
        },
    )
    history.list_page.side_effect = UserDataUnavailableError()
    unavailable = client.get("/api/v1/me/history", headers={"Cookie": _SESSION_COOKIE})

    assert deleted.status_code == 204
    assert deleted.headers["Cache-Control"] == "no-store"
    history.delete_all.assert_awaited_once_with(user_id=73)
    assert unavailable.status_code == 503
    assert unavailable.json() == {"error": {"code": "AUTH_UNAVAILABLE"}}
    assert unavailable.headers["Cache-Control"] == "no-store"
