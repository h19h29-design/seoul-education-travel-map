import json
import logging
from ipaddress import ip_network
from pathlib import Path

import pytest
from app.settings import Settings
from pydantic import SecretStr, ValidationError

pytest_plugins = ("tests.api.conftest",)


# Break caught: a public API payload containing a REST or Seoul transit credential.
def test_public_responses_do_not_expose_server_credentials(client) -> None:
    response = client.get("/api/v1/bootstrap")

    assert response.status_code == 200
    serialized = json.dumps(response.json())
    assert "rest-secret" not in serialized
    assert "seoul-secret" not in serialized
    assert "opinet-secret" not in serialized


# Break caught: a configuration validation/logging path renders a user-data secret.
def test_auth_secret_values_never_appear_in_repr_errors_or_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secrets = {
        "oidc-secret-value",
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE",
        "AgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgI",
    }
    settings = Settings(
        public_base_url="https://travel.h19h19.com",
        user_database_path="/data/travel-map.sqlite3",
        kakao_oidc_client_id="login-only-client-id",
        kakao_oidc_client_secret=SecretStr("oidc-secret-value"),
        session_hmac_key=SecretStr("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"),
        kakao_subject_hmac_key=SecretStr("AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE"),
        data_encryption_key_v1=SecretStr("AgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgI"),
        trusted_proxy_cidrs=(ip_network("1.1.1.1/32"),),
        _env_file=None,
    )
    with caplog.at_level(logging.INFO):
        logging.getLogger("travel-map-test").info("settings=%r", settings)
    with pytest.raises(ValidationError) as raised:
        Settings(
            kakao_oidc_client_secret=SecretStr("oidc-secret-value"),
            _env_file=None,
        )

    rendered = "\n".join((repr(settings), str(raised.value), caplog.text))
    assert all(value not in rendered for value in secrets)
    browser_source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in Path("apps/travel-map/app/static").glob("**/*")
        if path.is_file()
    )
    assert all(value not in browser_source for value in secrets)


def test_invalid_environment_auth_secret_does_not_appear_in_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_secret = "runtime-auth-secret-value"
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://travel.h19h19.com")
    monkeypatch.setenv("USER_DATABASE_PATH", "/data/travel-map.sqlite3")
    monkeypatch.setenv("KAKAO_OIDC_CLIENT_ID", "login-only-client-id")
    monkeypatch.setenv("KAKAO_OIDC_CLIENT_SECRET", "oidc-test-secret")
    monkeypatch.setenv("SESSION_HMAC_KEY", raw_secret)
    monkeypatch.setenv(
        "KAKAO_SUBJECT_HMAC_KEY", "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE"
    )
    monkeypatch.setenv(
        "DATA_ENCRYPTION_KEY_V1", "AgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgI"
    )
    monkeypatch.setenv("TRUSTED_PROXY_CIDRS", '["1.1.1.1/32"]')

    with pytest.raises(ValidationError) as raised:
        Settings(_env_file=None)

    assert raw_secret not in str(raised.value)
