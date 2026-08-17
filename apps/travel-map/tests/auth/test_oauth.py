import asyncio
import hashlib
import hmac
from base64 import urlsafe_b64encode
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from app.auth.models import OidcInternalError, OidcLoginFailed
from app.auth.oauth import AuthRejected, KakaoOidcClient, OAuthAttemptRepository
from app.storage.database import SqliteDatabase
from app.storage.models import format_storage_timestamp
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import SecretStr

_AUTHORIZATION_ENDPOINT = "https://kauth.kakao.com/oauth/authorize"
_TOKEN_ENDPOINT = "https://kauth.kakao.com/oauth/token"
_JWKS_ENDPOINT = "https://kauth.kakao.com/.well-known/jwks.json"
_REDIRECT_URI = "https://travel.h19h19.com/auth/kakao/callback"
_CLIENT_ID = "test-login-client-id"
_TEST_SECRET = "test-only-oidc-client-secret"
_CALLBACK_CODE = "test-callback-code"
_NONCE = "test-oidc-nonce"
_SUBJECT = "test-oidc-subject"


def _base64url(value: bytes) -> str:
    return urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _jwk(private_key: rsa.RSAPrivateKey, kid: str) -> dict[str, str]:
    numbers = private_key.public_key().public_numbers()
    return {
        "kty": "RSA",
        "kid": kid,
        "n": _base64url(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8)),
        "e": _base64url(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8)),
    }


def _signed_id_token(
    private_key: rsa.RSAPrivateKey,
    *,
    kid: str = "test-kid",
    nonce: str = _NONCE,
    subject: str = _SUBJECT,
    issuer: str = "https://kauth.kakao.com",
    audience: object = _CLIENT_ID,
    expires_at: int | None = None,
    issued_at: object | None = None,
) -> str:
    now = int(datetime.now(UTC).timestamp())
    return jwt.encode(
        {
            "iss": issuer,
            "aud": audience,
            "sub": subject,
            "nonce": nonce,
            "iat": now if issued_at is None else issued_at,
            "exp": now + 60 if expires_at is None else expires_at,
        },
        private_key,
        algorithm="RS256",
        headers={"kid": kid},
    )


def _oidc_client(http: httpx.AsyncClient) -> KakaoOidcClient:
    return KakaoOidcClient(
        client_id=_CLIENT_ID,
        client_secret=SecretStr(_TEST_SECRET),
        subject_hmac_key=bytes(range(32)),
        http=http,
        timeout_seconds=5.0,
    )


# Break caught: a consumed OAuth attempt, or its plaintext state/nonce, remains
# usable or persistent after the ten-minute login window.
@pytest.mark.asyncio
async def test_login_attempt_is_single_use_hmac_only_and_expires_at_ten_minutes(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 17, tzinfo=UTC)
    path = tmp_path / "private" / "travel-map.sqlite3"
    database = SqliteDatabase(path)
    database.migrate()
    attempts = OAuthAttemptRepository(database, hmac_key=b"a" * 32)

    issued = await attempts.create(now=now)

    assert issued.expires_at == datetime(2026, 8, 17, 0, 10, tzinfo=UTC)
    nonce_hash = await attempts.consume(
        attempt_token=issued.attempt_token, state=issued.state, now=now
    )
    assert nonce_hash == hashlib.sha256(issued.nonce.encode()).digest()
    with pytest.raises(AuthRejected, match="^INVALID_OAUTH_ATTEMPT$"):
        await attempts.consume(
            attempt_token=issued.attempt_token, state=issued.state, now=now
        )

    await database.checkpoint_truncate()
    for artifact in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        if artifact.exists():
            raw = artifact.read_bytes()
            assert issued.attempt_token.encode() not in raw
            assert issued.state.encode() not in raw
            assert issued.nonce.encode() not in raw


# Break caught: a callback at the exact expiration boundary, a concurrent
# replay, or an attempt flood can bypass the atomic one-use attempt boundary.
@pytest.mark.asyncio
async def test_oauth_attempt_boundary_concurrency_and_active_cap(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 17, tzinfo=UTC)
    database = SqliteDatabase(tmp_path / "private" / "travel-map.sqlite3")
    database.migrate()
    attempts = OAuthAttemptRepository(database, hmac_key=b"a" * 32)
    issued = await attempts.create(now=now)

    results = await asyncio.gather(
        *(
            attempts.consume(
                attempt_token=issued.attempt_token, state=issued.state, now=now
            )
            for _ in range(2)
        ),
        return_exceptions=True,
    )
    assert sum(type(result) is bytes for result in results) == 1
    assert sum(type(result) is AuthRejected for result in results) == 1

    expired = await attempts.create(now=now)
    with pytest.raises(AuthRejected, match="^INVALID_OAUTH_ATTEMPT$"):
        await attempts.consume(
            attempt_token=expired.attempt_token,
            state=expired.state,
            now=expired.expires_at,
        )

    created_at = format_storage_timestamp(now)
    expires_at = format_storage_timestamp(now + timedelta(minutes=10))

    def fill_active_attempts(connection) -> None:
        rows = []
        for index in range(9_998):
            encoded = str(index).encode("ascii")
            rows.append(
                (
                    hmac.digest(
                        b"a" * 32, b"travel-map:oauth-attempt\0" + encoded, "sha256"
                    ),
                    hmac.digest(
                        b"a" * 32, b"travel-map:oauth-state\0" + encoded, "sha256"
                    ),
                    hashlib.sha256(encoded).digest(),
                    created_at,
                    expires_at,
                )
            )
        connection.executemany(
            "INSERT INTO oauth_login_attempts("
            "attempt_hash, state_hash, nonce_hash, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            rows,
        )

    await database.write(fill_active_attempts)
    with pytest.raises(AuthRejected, match="^INVALID_OAUTH_ATTEMPT$"):
        await attempts.create(now=now)


# Break caught: an authorization redirect drifting to a non-OIDC endpoint,
# using a provider REST credential, or omitting state/nonce binding inputs.
def test_authorization_url_has_exact_redirect_scope_state_and_nonce() -> None:
    client = KakaoOidcClient(
        client_id=_CLIENT_ID,
        client_secret=SecretStr(_TEST_SECRET),
        subject_hmac_key=bytes(range(32)),
        timeout_seconds=5.0,
    )

    parsed = urlsplit(
        client.authorization_url(state="state-value", nonce="nonce-value")
    )

    assert (parsed.scheme, parsed.netloc, parsed.path) == (
        "https",
        "kauth.kakao.com",
        "/oauth/authorize",
    )
    assert parse_qs(parsed.query, strict_parsing=True) == {
        "response_type": ["code"],
        "client_id": [_CLIENT_ID],
        "redirect_uri": [_REDIRECT_URI],
        "scope": ["openid"],
        "state": ["state-value"],
        "nonce": ["nonce-value"],
    }
    assert "rest" not in parsed.query.lower()


# Break caught: token exchange sending a Basic credential, a mutable provider
# endpoint, an incomplete form, or returning a raw Kakao subject to callers.
@pytest.mark.asyncio
async def test_token_exchange_uses_exact_form_and_returns_only_subject_hmac() -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    id_token = _signed_id_token(private_key)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if str(request.url) == _TOKEN_ENDPOINT:
            assert request.method == "POST"
            assert request.headers["content-type"] == (
                "application/x-www-form-urlencoded;charset=utf-8"
            )
            assert "authorization" not in request.headers
            assert parse_qs(request.content.decode("ascii"), strict_parsing=True) == {
                "grant_type": ["authorization_code"],
                "client_id": [_CLIENT_ID],
                "redirect_uri": [_REDIRECT_URI],
                "code": [_CALLBACK_CODE],
                "client_secret": [_TEST_SECRET],
            }
            return httpx.Response(
                200,
                json={
                    "access_token": "test-access-token",
                    "refresh_token": "test-refresh-token",
                    "id_token": id_token,
                },
            )
        if str(request.url) == _JWKS_ENDPOINT:
            assert request.method == "GET"
            return httpx.Response(200, json={"keys": [_jwk(private_key, "test-kid")]})
        raise AssertionError("OIDC contacted an unpinned endpoint")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        verified = await _oidc_client(http).exchange_and_verify(
            code=_CALLBACK_CODE,
            expected_nonce_hash=hashlib.sha256(_NONCE.encode()).digest(),
        )

    assert verified.subject_hmac == hmac.digest(
        bytes(range(32)), _SUBJECT.encode(), "sha256"
    )
    assert _SUBJECT not in repr(verified)
    assert {str(request.url) for request in requests} == {
        _TOKEN_ENDPOINT,
        _JWKS_ENDPOINT,
    }


# Break caught: a malformed, redirected, over-large, or invalidly signed OIDC
# response being accepted or exposing provider/JWT details to an auth caller.
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("token_factory", "jwks_factory", "expect_unknown_kid_refresh"),
    [
        pytest.param(
            lambda key: _signed_id_token(key, issuer="https://wrong.example"),
            lambda key: {"keys": [_jwk(key, "test-kid")]},
            False,
            id="wrong-issuer",
        ),
        pytest.param(
            lambda key: _signed_id_token(key, audience="wrong-audience"),
            lambda key: {"keys": [_jwk(key, "test-kid")]},
            False,
            id="wrong-audience",
        ),
        pytest.param(
            lambda key: _signed_id_token(key, audience=[_CLIENT_ID, "other-client"]),
            lambda key: {"keys": [_jwk(key, "test-kid")]},
            False,
            id="multiple-audiences",
        ),
        pytest.param(
            lambda key: _signed_id_token(key, nonce="wrong-nonce"),
            lambda key: {"keys": [_jwk(key, "test-kid")]},
            False,
            id="wrong-nonce",
        ),
        pytest.param(
            lambda key: _signed_id_token(
                key, expires_at=int(datetime.now(UTC).timestamp()) - 1
            ),
            lambda key: {"keys": [_jwk(key, "test-kid")]},
            False,
            id="expired",
        ),
        pytest.param(
            lambda key: _signed_id_token(key, issued_at="not-an-integer"),
            lambda key: {"keys": [_jwk(key, "test-kid")]},
            False,
            id="invalid-iat",
        ),
        pytest.param(
            lambda key: _signed_id_token(key),
            lambda key: {"keys": [_jwk(key, "other-kid")]},
            True,
            id="unknown-kid",
        ),
    ],
)
async def test_oidc_claim_or_jwk_failures_use_only_fixed_login_error(
    token_factory: Callable[[rsa.RSAPrivateKey], str],
    jwks_factory: Callable[[rsa.RSAPrivateKey], dict[str, object]],
    expect_unknown_kid_refresh: bool,
) -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    id_token = token_factory(private_key)
    jwks_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal jwks_calls
        if str(request.url) == _TOKEN_ENDPOINT:
            return httpx.Response(200, json={"id_token": id_token})
        if str(request.url) == _JWKS_ENDPOINT:
            jwks_calls += 1
            return httpx.Response(200, json=jwks_factory(private_key))
        raise AssertionError("OIDC contacted an unpinned endpoint")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(OidcLoginFailed, match="^OIDC_LOGIN_FAILED$") as raised:
            await _oidc_client(http).exchange_and_verify(
                code=_CALLBACK_CODE,
                expected_nonce_hash=hashlib.sha256(_NONCE.encode()).digest(),
            )

    rendered = repr(raised.value) + str(raised.value)
    assert all(
        value not in rendered
        for value in (_CALLBACK_CODE, _TEST_SECRET, id_token, _SUBJECT)
    )
    if expect_unknown_kid_refresh:
        assert jwks_calls == 2


# Break caught: redirect/oversize transport responses receiving provider follow-up
# handling rather than one fixed OIDC rejection through the response boundary.
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(302, headers={"location": "https://wrong.example"}),
        httpx.Response(200, content=b"x" * (256 * 1024 + 1)),
    ],
)
async def test_oidc_transport_boundary_never_follows_or_accepts_oversize_response(
    response: httpx.Response,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == _TOKEN_ENDPOINT
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(OidcLoginFailed, match="^OIDC_LOGIN_FAILED$"):
            await _oidc_client(http).exchange_and_verify(
                code=_CALLBACK_CODE,
                expected_nonce_hash=hashlib.sha256(_NONCE.encode()).digest(),
            )


def _traceback_string_locals(error: BaseException) -> tuple[str, ...]:
    values: list[str] = []
    traceback = error.__traceback__
    while traceback is not None:
        values.extend(
            value
            for value in traceback.tb_frame.f_locals.values()
            if type(value) is str
        )
        traceback = traceback.tb_next
    return tuple(values)


# Break caught: worker, transport, schema, task-creation, unexpected, or
# cancellation paths retaining raw credential/token/subject values after return.
@pytest.mark.asyncio
async def test_oidc_failure_clears_sensitive_holders_and_uses_fixed_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: None)
    ) as http:
        client = _oidc_client(http)
        response_holder: dict[str, object] = {"access_token": "test-access-token"}

        async def schema_failure(_form: dict[str, str]) -> dict[str, object]:
            return response_holder

        monkeypatch.setattr(client, "_exchange_code", schema_failure)
        with pytest.raises(OidcLoginFailed, match="^OIDC_LOGIN_FAILED$") as schema:
            await client.exchange_and_verify(
                code=_CALLBACK_CODE,
                expected_nonce_hash=hashlib.sha256(_NONCE.encode()).digest(),
            )
        assert response_holder == {}
        assert all(
            value not in _traceback_string_locals(schema.value)
            for value in (_CALLBACK_CODE, _TEST_SECRET, "test-access-token", _SUBJECT)
        )

        claims_holder: dict[str, object] = {"iss": "test-only-claim"}

        async def token_with_id(_form: dict[str, str]) -> dict[str, object]:
            return {"id_token": "test-id-token"}

        async def missing_subject(
            _id_token: str, **_kwargs: object
        ) -> dict[str, object]:
            return claims_holder

        monkeypatch.setattr(client, "_exchange_code", token_with_id)
        monkeypatch.setattr(client, "_verify_rs256_id_token", missing_subject)
        with pytest.raises(OidcLoginFailed, match="^OIDC_LOGIN_FAILED$"):
            await client.exchange_and_verify(
                code=_CALLBACK_CODE,
                expected_nonce_hash=hashlib.sha256(_NONCE.encode()).digest(),
            )
        assert claims_holder == {}

        async def unexpected(_form: dict[str, str]) -> dict[str, object]:
            raise RuntimeError("unexpected worker failure")

        monkeypatch.setattr(client, "_exchange_code", unexpected)
        with pytest.raises(OidcInternalError, match="^OIDC_INTERNAL_ERROR$"):
            await client.exchange_and_verify(
                code=_CALLBACK_CODE,
                expected_nonce_hash=hashlib.sha256(_NONCE.encode()).digest(),
            )

        captured_workers: list[Any] = []

        def cannot_create(worker: Any) -> Any:
            captured_workers.append(worker)
            raise RuntimeError("task creation failure")

        monkeypatch.setattr(asyncio, "create_task", cannot_create)
        with pytest.raises(OidcInternalError, match="^OIDC_INTERNAL_ERROR$"):
            await client.exchange_and_verify(
                code=_CALLBACK_CODE,
                expected_nonce_hash=hashlib.sha256(_NONCE.encode()).digest(),
            )
        assert len(captured_workers) == 1
        assert captured_workers[0].cr_frame is None

    monkeypatch.undo()

    release = asyncio.Event()
    entered = asyncio.Event()
    forms: list[dict[str, str]] = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: None)
    ) as http:
        client = _oidc_client(http)

        async def suspended(form: dict[str, str]) -> dict[str, object]:
            forms.append(form)
            entered.set()
            await release.wait()
            raise AssertionError("cancelled worker continued")

        monkeypatch.setattr(client, "_exchange_code", suspended)
        task = asyncio.create_task(
            client.exchange_and_verify(
                code=_CALLBACK_CODE,
                expected_nonce_hash=hashlib.sha256(_NONCE.encode()).digest(),
            )
        )
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert forms == [{}]


# Break caught: a malformed provider response object retaining raw token values
# because only exact dict response holders were cleared in the worker finally.
@pytest.mark.asyncio
async def test_oidc_worker_clears_rejected_noncanonical_response_holder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NoncanonicalResponse(dict[str, object]):
        pass

    holder = NoncanonicalResponse({"id_token": "test-id-token"})
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: None)
    ) as http:
        client = _oidc_client(http)

        async def noncanonical_response(_form: dict[str, str]) -> dict[str, object]:
            return cast(dict[str, object], holder)

        monkeypatch.setattr(client, "_exchange_code", noncanonical_response)
        with pytest.raises(OidcLoginFailed, match="^OIDC_LOGIN_FAILED$"):
            await client.exchange_and_verify(
                code=_CALLBACK_CODE,
                expected_nonce_hash=hashlib.sha256(_NONCE.encode()).digest(),
            )

    assert holder == {}
