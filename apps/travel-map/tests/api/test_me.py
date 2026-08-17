"""Account-route private-storage boundary regressions."""

from fastapi.testclient import TestClient


def test_settings_without_private_services_fails_closed_without_cache(
    client: TestClient,
) -> None:
    response = client.get(
        "/api/v1/me/settings",
        headers={"Cookie": "__Host-travel_session=opaque-token"},
    )

    assert response.status_code == 503
    assert response.json() == {"error": {"code": "AUTH_UNAVAILABLE"}}
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Pragma"] == "no-cache"
