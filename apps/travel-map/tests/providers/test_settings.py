import math
from base64 import urlsafe_b64decode, urlsafe_b64encode
from ipaddress import ip_network
from pathlib import Path

import pytest
from app.settings import Settings
from pydantic import SecretStr, ValidationError
from pydantic_settings import SettingsError


def production_values() -> dict[str, object]:
    return {
        "environment": "production",
        "kakao_rest_api_key": SecretStr("kakao-secret"),
        "seoul_transit_service_key": SecretStr("seoul-secret"),
        "opinet_cert_key": SecretStr("opinet-secret"),
        "allowed_hosts": ("travel.h19h19.com",),
        "allowed_origins": ("https://travel.h19h19.com",),
        "provider_timeout_seconds": 4.0,
        "route_max_concurrency": 4,
        **auth_storage_values(),
    }


def _base64url_key(seed: int) -> str:
    return urlsafe_b64encode(bytes([seed]) * 32).decode("ascii").rstrip("=")


def _noncanonical_base64url_alias() -> str:
    canonical = _base64url_key(1)
    decoded = urlsafe_b64decode(canonical + "=")
    for (
        replacement
    ) in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_":
        candidate = canonical[:-1] + replacement
        if candidate != canonical and urlsafe_b64decode(candidate + "=") == decoded:
            return candidate
    raise AssertionError("expected a noncanonical base64url alias")


def auth_storage_values() -> dict[str, object]:
    return {
        "public_base_url": "https://travel.h19h19.com",
        "user_database_path": "/data/travel-map.sqlite3",
        "kakao_oidc_client_id": "login-only-client-id",
        "kakao_oidc_client_secret": SecretStr("oidc-test-secret"),
        "session_hmac_key": SecretStr(_base64url_key(1)),
        "kakao_subject_hmac_key": SecretStr(_base64url_key(2)),
        "data_encryption_key_v1": SecretStr(_base64url_key(3)),
        "trusted_proxy_cidrs": (ip_network("1.1.1.1/32"),),
    }


# Break caught: starting the public production server without required upstreams.
def test_production_settings_require_all_provider_keys_hosts_and_origins() -> None:
    with pytest.raises(ValidationError):
        Settings(environment="production", _env_file=None)

    settings = Settings(**production_values(), _env_file=None)

    assert settings.environment == "production"
    assert "kakao-secret" not in repr(settings)
    assert "seoul-secret" not in repr(settings)
    assert "opinet-secret" not in repr(settings)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider_timeout_seconds", True),
        ("provider_timeout_seconds", math.inf),
        ("provider_timeout_seconds", 0.0),
        ("route_max_concurrency", True),
        ("route_max_concurrency", 0),
        ("allowed_hosts", ["travel.example.kr"]),
        ("allowed_hosts", ("*",)),
        ("allowed_origins", ("*",)),
        ("allowed_origins", ("http://travel.example.kr",)),
    ],
)
def test_production_settings_reject_noncanonical_or_unsafe_values(
    field: str,
    value: object,
) -> None:
    values = production_values()
    values[field] = value

    with pytest.raises(ValidationError):
        Settings(**values, _env_file=None)


def test_development_settings_allow_missing_credentials_but_not_bad_limits() -> None:
    settings = Settings(_env_file=None)

    assert settings.environment == "development"
    assert settings.kakao_rest_api_key is None
    assert settings.seoul_transit_service_key is None
    assert settings.opinet_cert_key is None
    assert settings.kakao_oidc_client_id is None
    assert settings.data_encryption_key_v1 is None
    with pytest.raises(ValidationError):
        Settings(provider_timeout_seconds=float("nan"), _env_file=None)


# Break caught: copying the documented development env template makes startup fail.
def test_development_env_example_treats_blank_optional_credentials_as_missing() -> None:
    settings = Settings(_env_file=Path("apps/travel-map/.env.example"))

    assert settings.environment == "development"
    assert settings.kakao_rest_api_key is None
    assert settings.seoul_transit_service_key is None
    assert settings.opinet_cert_key is None
    assert settings.kakao_oidc_client_id is None
    assert settings.kakao_oidc_client_secret is None
    assert settings.session_hmac_key is None
    assert settings.kakao_subject_hmac_key is None
    assert settings.data_encryption_key_v1 is None
    assert settings.trusted_proxy_cidrs is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider_timeout_seconds", 5),
        ("route_max_concurrency", 4.0),
        ("allowed_hosts", ("localhost",)),
        ("allowed_origins", ("https://travel.example.kr/",)),
    ],
)
def test_settings_reject_wrong_exact_types_and_noncanonical_origin(
    field: str,
    value: object,
) -> None:
    if field == "allowed_hosts":

        class TupleSubclass(tuple[str, ...]):
            pass

        value = TupleSubclass(value)
    values: dict[str, object] = {field: value}
    with pytest.raises(ValidationError):
        Settings(**values, _env_file=None)


def test_create_app_fails_startup_for_invalid_production_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.main import create_app

    monkeypatch.setenv("ENVIRONMENT", "production")
    for name in (
        "KAKAO_REST_API_KEY",
        "SEOUL_TRANSIT_SERVICE_KEY",
        "OPINET_CERT_KEY",
        "ALLOWED_HOSTS",
        "ALLOWED_ORIGINS",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ValidationError):
        create_app()


def test_environment_json_lists_become_exact_host_and_origin_tuples(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALLOWED_HOSTS", '["travel.example.kr"]')
    monkeypatch.setenv("ALLOWED_ORIGINS", '["https://travel.example.kr"]')

    settings = Settings(_env_file=None)

    assert type(settings.allowed_hosts) is tuple
    assert settings.allowed_hosts == ("travel.example.kr",)
    assert type(settings.allowed_origins) is tuple
    assert settings.allowed_origins == ("https://travel.example.kr",)


def test_production_rejects_empty_environment_host_instead_of_using_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("KAKAO_REST_API_KEY", "kakao")
    monkeypatch.setenv("SEOUL_TRANSIT_SERVICE_KEY", "seoul")
    monkeypatch.setenv("OPINET_CERT_KEY", "opinet")
    monkeypatch.setenv("ALLOWED_HOSTS", "")
    monkeypatch.setenv("ALLOWED_ORIGINS", '["https://travel.example.kr"]')

    with pytest.raises((SettingsError, ValidationError)):
        Settings(_env_file=None)


def test_production_requires_explicit_host_and_origin_allowlists() -> None:
    values = production_values()
    values.pop("allowed_hosts")

    with pytest.raises(ValidationError):
        Settings(**values, _env_file=None)


@pytest.mark.parametrize(
    "host",
    ("*.example.kr", "Travel.Example.Kr", "example.kr:bad"),
)
def test_allowed_hosts_are_exact_canonical_names(host: str) -> None:
    with pytest.raises(ValidationError):
        Settings(allowed_hosts=(host,), _env_file=None)


# Break caught: production starts an unauthenticated user-data subsystem by accident.
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("public_base_url", "https://travel.h19h19.com/path"),
        ("public_base_url", "http://travel.h19h19.com"),
        ("public_base_url", "https://other.example"),
        ("user_database_path", "/tmp/travel-map.sqlite3"),
        ("allowed_origins", ("https://other.example",)),
        ("trusted_proxy_cidrs", ("10.0.0.1/32",)),
        ("trusted_proxy_cidrs", ("1.1.1.0/24",)),
    ],
)
def test_production_requires_exact_auth_storage_settings(
    field: str, value: object
) -> None:
    values = production_values()
    values[field] = value

    with pytest.raises(ValidationError):
        Settings(**values, _env_file=None)


@pytest.mark.parametrize(
    "hosts",
    [
        ("travel.h19h19.com", "legacy.example"),
        ("127.0.0.1", "localhost"),
    ],
)
def test_production_allows_only_the_canonical_public_host(
    hosts: tuple[str, ...],
) -> None:
    values = production_values()
    values["allowed_hosts"] = hosts

    with pytest.raises(ValidationError):
        Settings(**values, _env_file=None)


@pytest.mark.parametrize(
    "field",
    tuple(auth_storage_values()),
)
def test_production_rejects_every_missing_auth_storage_setting(field: str) -> None:
    values = production_values()
    values.pop(field)

    with pytest.raises(ValidationError):
        Settings(**values, _env_file=None)


# Break caught: a routing credential is inadvertently reused as the login client ID.
def test_oidc_client_id_must_not_equal_provider_rest_key() -> None:
    values = production_values()
    values["kakao_oidc_client_id"] = "kakao-secret"

    with pytest.raises(ValidationError):
        Settings(**values, _env_file=None)


@pytest.mark.parametrize("environment", ("development", "test"))
def test_partial_auth_settings_are_rejected_in_development(environment: str) -> None:
    with pytest.raises(ValidationError):
        Settings(
            environment=environment,
            kakao_oidc_client_id="login-only-client-id",
            _env_file=None,
        )

    values = auth_storage_values()
    settings = Settings(environment=environment, **values, _env_file=None)

    assert settings.user_database_path == "/data/travel-map.sqlite3"


def test_environment_trusted_proxy_list_requires_one_exact_global_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://travel.h19h19.com")
    monkeypatch.setenv("USER_DATABASE_PATH", "/data/travel-map.sqlite3")
    monkeypatch.setenv("KAKAO_OIDC_CLIENT_ID", "login-only-client-id")
    monkeypatch.setenv("KAKAO_OIDC_CLIENT_SECRET", "oidc-test-secret")
    monkeypatch.setenv("SESSION_HMAC_KEY", _base64url_key(1))
    monkeypatch.setenv("KAKAO_SUBJECT_HMAC_KEY", _base64url_key(2))
    monkeypatch.setenv("DATA_ENCRYPTION_KEY_V1", _base64url_key(3))
    monkeypatch.setenv("TRUSTED_PROXY_CIDRS", '["1.1.1.1/32"]')

    settings = Settings(_env_file=None)

    assert settings.trusted_proxy_cidrs == (ip_network("1.1.1.1/32"),)


@pytest.mark.parametrize(
    "malformed_key",
    [
        _base64url_key(1) + "=",
        _base64url_key(1) + "==",
        _base64url_key(1)[:20] + "=" + _base64url_key(1)[20:],
        _base64url_key(1) + " ",
        _base64url_key(1)[:-1] + "한",
        _base64url_key(1)[:-1] + "+",
        _base64url_key(1)[:-1],
        _base64url_key(1) + "A",
        _noncanonical_base64url_alias(),
    ],
)
def test_auth_storage_keys_require_canonical_unpadded_base64url(
    malformed_key: str,
) -> None:
    values = auth_storage_values()
    values["session_hmac_key"] = SecretStr(malformed_key)

    with pytest.raises(ValidationError) as raised:
        Settings(environment="test", **values, _env_file=None)

    assert malformed_key not in str(raised.value)
