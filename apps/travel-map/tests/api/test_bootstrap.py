import json

from app.api import create_router
from app.main import create_app
from app.settings import Settings
from fastapi import FastAPI
from pydantic import SecretStr


def _paths(router) -> set[str]:
    app = FastAPI()
    app.include_router(router)
    return set(app.openapi()["paths"])


# Break caught: exposing the server credential instead of only the browser-restricted key.
def test_bootstrap_exposes_only_domain_restricted_javascript_key(client) -> None:
    response = client.get("/api/v1/bootstrap")

    assert response.status_code == 200
    body = response.json()
    serialized = json.dumps(body)
    assert body["map"]["javascriptKey"] == "public-js-key"
    assert "rest-secret" not in serialized
    assert "seoul-secret" not in serialized


def test_bootstrap_marks_private_features_disabled_in_stateless_beta(client) -> None:
    client.app.state.dependencies.settings = Settings(
        environment="test",
        stateless_beta=True,
        kakao_javascript_key=SecretStr("public-js-key"),
        allowed_hosts=("testserver",),
        allowed_origins=("https://travel.example.test",),
    )

    response = client.get("/api/v1/bootstrap")

    assert response.status_code == 200
    assert response.json()["privateFeaturesEnabled"] is False


def test_stateless_app_registers_only_public_api_and_no_oauth_routes() -> None:
    app = create_app(Settings(environment="test", stateless_beta=True))
    route_paths = set(app.openapi()["paths"])

    assert "/api/v1/bootstrap" in route_paths
    assert "/api/v1/trips/preview" in route_paths
    assert "/api/v1/me" not in route_paths
    assert "/api/v1/me/history" not in route_paths
    assert "/auth/kakao/start" not in route_paths


def test_public_router_can_exclude_private_endpoints() -> None:
    route_paths = _paths(create_router(include_private=False))

    assert "/api/v1/bootstrap" in route_paths
    assert "/api/v1/me" not in route_paths
    assert "/api/v1/me/history" not in route_paths
