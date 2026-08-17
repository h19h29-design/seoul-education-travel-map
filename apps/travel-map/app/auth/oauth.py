"""Persistent OAuth attempts and a pinned, fail-closed Kakao OIDC client."""

import asyncio
import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from collections.abc import Coroutine, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import isfinite
from typing import Any, cast
from urllib.parse import urlencode

import httpx
import jwt
from pydantic import SecretStr

from app.auth.models import (
    AuthRejected,
    IssuedOAuthAttempt,
    OidcInternalError,
    OidcLoginFailed,
    VerifiedSubject,
)
from app.storage.database import SqliteDatabase
from app.storage.models import StorageIntegrityError, format_storage_timestamp

_AUTHORIZATION_ENDPOINT = "https://kauth.kakao.com/oauth/authorize"
_TOKEN_ENDPOINT = "https://kauth.kakao.com/oauth/token"
_JWKS_ENDPOINT = "https://kauth.kakao.com/.well-known/jwks.json"
_ISSUER = "https://kauth.kakao.com"
_REDIRECT_URI = "https://travel.h19h19.com/auth/kakao/callback"
_ATTEMPT_PREFIX = b"travel-map:oauth-attempt\0"
_STATE_PREFIX = b"travel-map:oauth-state\0"
_ATTEMPT_TTL = timedelta(minutes=10)
_MAX_ACTIVE_ATTEMPTS = 10_000
_MAX_RESPONSE_BYTES = 256 * 1024
_JWKS_CACHE_SECONDS = 300.0
_MAX_JWKS_KEYS = 64


class _OidcTransportFailure(RuntimeError):
    """Private transport boundary failure without external context."""


class _OidcSchemaFailure(RuntimeError):
    """Private provider payload shape failure without external context."""


class _OidcJwtFailure(RuntimeError):
    """Private signature or claims verification failure without context."""


@dataclass(frozen=True)
class _OidcWorkerOutcome:
    """The only normal value permitted to cross the sensitive worker boundary."""

    subject: VerifiedSubject | None
    failed: bool


class OAuthAttemptRepository:
    """Store only one-use attempt digests in the private SQLite database."""

    def __init__(self, database: SqliteDatabase, *, hmac_key: bytes) -> None:
        if type(database) is not SqliteDatabase:
            raise TypeError("database must be SqliteDatabase")
        _require_digest(hmac_key)
        self._database = database
        self._hmac_key = hmac_key

    async def create(self, *, now: datetime) -> IssuedOAuthAttempt:
        created_at = _canonical_now(now)
        expires_at = created_at + _ATTEMPT_TTL
        created_at_text = format_storage_timestamp(created_at)
        expires_at_text = format_storage_timestamp(expires_at)
        attempt_token = secrets.token_urlsafe(32)
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        attempt_hash = self._digest(_ATTEMPT_PREFIX, attempt_token)
        state_hash = self._digest(_STATE_PREFIX, state)
        nonce_hash = hashlib.sha256(nonce.encode("utf-8")).digest()

        def operation(connection: Any) -> None:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "DELETE FROM oauth_login_attempts WHERE expires_at<=?",
                    (created_at_text,),
                )
                active_row = connection.execute(
                    "SELECT COUNT(*) FROM oauth_login_attempts WHERE expires_at>?",
                    (created_at_text,),
                ).fetchone()
                if active_row is None or type(active_row[0]) is not int:
                    raise _OidcSchemaFailure
                if active_row[0] >= _MAX_ACTIVE_ATTEMPTS:
                    raise AuthRejected()
                connection.execute(
                    "INSERT INTO oauth_login_attempts("
                    "attempt_hash, state_hash, nonce_hash, created_at, expires_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        attempt_hash,
                        state_hash,
                        nonce_hash,
                        created_at_text,
                        expires_at_text,
                    ),
                )
                connection.commit()
            except AuthRejected:
                connection.rollback()
                raise
            except (sqlite3.Error, _OidcSchemaFailure):
                connection.rollback()
                raise AuthRejected() from None

        await self._database.write(operation)
        return IssuedOAuthAttempt(
            attempt_token=attempt_token,
            state=state,
            nonce=nonce,
            expires_at=expires_at,
        )

    async def consume(self, *, attempt_token: str, state: str, now: datetime) -> bytes:
        now_text = format_storage_timestamp(_canonical_now(now))
        if type(attempt_token) is not str or not attempt_token:
            raise AuthRejected()
        if type(state) is not str or not state:
            raise AuthRejected()
        attempt_hash = self._digest(_ATTEMPT_PREFIX, attempt_token)
        state_hash = self._digest(_STATE_PREFIX, state)

        def operation(connection: Any) -> bytes:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT nonce_hash FROM oauth_login_attempts "
                    "WHERE attempt_hash=? AND state_hash=? AND expires_at>? "
                    "AND consumed_at IS NULL",
                    (attempt_hash, state_hash, now_text),
                ).fetchone()
                if row is None or type(row[0]) is not bytes or len(row[0]) != 32:
                    raise AuthRejected()
                changed = connection.execute(
                    "UPDATE oauth_login_attempts SET consumed_at=? "
                    "WHERE attempt_hash=? AND state_hash=? AND expires_at>? "
                    "AND consumed_at IS NULL",
                    (now_text, attempt_hash, state_hash, now_text),
                ).rowcount
                if changed != 1:
                    raise AuthRejected()
                nonce_hash = row[0]
                connection.commit()
                return nonce_hash
            except AuthRejected:
                connection.rollback()
                raise
            except sqlite3.Error:
                connection.rollback()
                raise AuthRejected() from None

        return await self._database.write(operation)

    def _digest(self, prefix: bytes, value: str) -> bytes:
        return hmac.digest(self._hmac_key, prefix + value.encode("utf-8"), "sha256")


class KakaoOidcClient:
    """Use only pinned Kakao OIDC endpoints and sanitize all failure paths."""

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: SecretStr,
        subject_hmac_key: bytes,
        http: httpx.AsyncClient | None = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        if (
            type(client_id) is not str
            or not client_id
            or client_id != client_id.strip()
        ):
            raise ValueError("client_id must be a canonical nonblank string")
        if type(client_secret) is not SecretStr:
            raise TypeError("client_secret must be SecretStr")
        _require_digest(subject_hmac_key)
        if http is not None and type(http) is not httpx.AsyncClient:
            raise TypeError("http must be an exact AsyncClient or None")
        if (
            type(timeout_seconds) is not float
            or not isfinite(timeout_seconds)
            or not 0.0 < timeout_seconds <= 30.0
        ):
            raise ValueError("timeout_seconds must be finite and in (0, 30]")
        self._client_id = client_id
        self._client_secret = client_secret
        self._subject_hmac_key = subject_hmac_key
        self._http = (
            http if http is not None else httpx.AsyncClient(follow_redirects=False)
        )
        self._owns_http = http is None
        self._timeout_seconds = timeout_seconds
        self._jwks_by_kid: dict[str, dict[str, str]] = {}
        self._jwks_expires_at = 0.0
        self._closed = False
        self._close_lock = asyncio.Lock()

    def authorization_url(self, *, state: str, nonce: str) -> str:
        if type(state) is not str or not state:
            raise AuthRejected()
        if type(nonce) is not str or not nonce:
            raise AuthRejected()
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self._client_id,
                "redirect_uri": _REDIRECT_URI,
                "scope": "openid",
                "state": state,
                "nonce": nonce,
            }
        )
        return f"{_AUTHORIZATION_ENDPOINT}?{query}"

    async def exchange_and_verify(
        self, *, code: str, expected_nonce_hash: bytes
    ) -> VerifiedSubject:
        code_holder = code
        worker: Coroutine[object, object, _OidcWorkerOutcome] | None = None
        task: asyncio.Task[_OidcWorkerOutcome] | None = None
        outcome: _OidcWorkerOutcome | None = None
        internal_failed = False
        expected_failed = False
        try:
            if (
                type(code_holder) is not str
                or not code_holder
                or type(expected_nonce_hash) is not bytes
                or len(expected_nonce_hash) != 32
            ):
                expected_failed = True
            else:
                worker = self._exchange_verify_worker(
                    code=code_holder,
                    expected_nonce_hash=expected_nonce_hash,
                )
                code = ""
                code_holder = ""
                try:
                    task = asyncio.create_task(worker)
                except Exception:
                    worker.close()
                    worker = None
                    raise
                worker = None
                outcome = await task
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - unexpected worker failures are sanitized.
            internal_failed = True
        finally:
            code = ""
            code_holder = ""
            if worker is not None:
                worker.close()
            task = None
        if internal_failed:
            raise OidcInternalError().with_traceback(None) from None
        if expected_failed or outcome is None or outcome.subject is None:
            raise OidcLoginFailed().with_traceback(None) from None
        return outcome.subject

    async def aclose(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            if self._owns_http:
                await self._http.aclose()
            self._closed = True

    async def _exchange_verify_worker(
        self, *, code: str, expected_nonce_hash: bytes
    ) -> _OidcWorkerOutcome:
        form: dict[str, str] = {}
        token_response: dict[str, object] = {}
        claims: dict[str, object] = {}
        raw_id_token: object = None
        raw_value: object = None
        response: object = None
        client_secret = ""
        id_token = ""
        raw_subject = ""
        try:
            client_secret = self._client_secret.get_secret_value()
            form = self._token_form(code=code, client_secret=client_secret)
            response = await self._exchange_code(form)
            if type(response) is not dict:
                raise _OidcSchemaFailure
            token_response = response
            response = None
            raw_id_token = token_response.pop("id_token", None)
            if not isinstance(raw_id_token, str) or not raw_id_token:
                raise _OidcSchemaFailure
            id_token = raw_id_token
            claims = await self._verify_rs256_id_token(
                id_token,
                issuer=_ISSUER,
                audience=self._client_id,
                expected_nonce_hash=expected_nonce_hash,
            )
            raw_value = claims.pop("sub", None)
            if not isinstance(raw_value, str) or not raw_value:
                raise _OidcSchemaFailure
            raw_subject = raw_value
            subject_hmac = hmac.digest(
                self._subject_hmac_key, raw_subject.encode("utf-8"), "sha256"
            )
            return _OidcWorkerOutcome(
                subject=VerifiedSubject(subject_hmac=subject_hmac), failed=False
            )
        except asyncio.CancelledError:
            raise
        except (_OidcTransportFailure, _OidcSchemaFailure, _OidcJwtFailure):
            return _OidcWorkerOutcome(subject=None, failed=True)
        finally:
            code = ""
            client_secret = ""
            form.clear()
            raw_id_token = None
            raw_value = None
            if isinstance(response, dict):
                response.clear()
            raw_subject = ""
            id_token = ""
            claims.clear()
            token_response.clear()

    def _token_form(self, *, code: str, client_secret: str) -> dict[str, str]:
        return {
            "grant_type": "authorization_code",
            "client_id": self._client_id,
            "redirect_uri": _REDIRECT_URI,
            "code": code,
            "client_secret": client_secret,
        }

    async def _exchange_code(self, form: dict[str, str]) -> dict[str, object]:
        return await self._request_json(
            method="POST",
            url=_TOKEN_ENDPOINT,
            form=form,
        )

    async def _verify_rs256_id_token(
        self,
        id_token: str,
        *,
        issuer: str,
        audience: str,
        expected_nonce_hash: bytes,
    ) -> dict[str, object]:
        try:
            header = jwt.get_unverified_header(id_token)
            if (
                type(header) is not dict
                or header.get("alg") != "RS256"
                or type(header.get("kid")) is not str
                or not cast(str, header["kid"])
            ):
                raise _OidcJwtFailure
            signing_jwk = await self._jwk_for_kid(cast(str, header["kid"]))
            signing_key = jwt.PyJWK.from_dict(signing_jwk).key
            decoded = jwt.decode(
                id_token,
                signing_key,
                algorithms=["RS256"],
                issuer=issuer,
                audience=audience,
                options={"require": ["sub", "iat", "exp", "nonce"]},
            )
            if type(decoded) is not dict:
                raise _OidcJwtFailure
            claims = cast(dict[str, object], decoded)
            if (
                type(claims.get("iat")) is not int
                or type(claims.get("exp")) is not int
                or type(claims.get("nonce")) is not str
                or claims.get("aud") != audience
            ):
                raise _OidcJwtFailure
            nonce_hash = hashlib.sha256(
                cast(str, claims["nonce"]).encode("utf-8")
            ).digest()
            if not hmac.compare_digest(nonce_hash, expected_nonce_hash):
                raise _OidcJwtFailure
            return claims
        except asyncio.CancelledError:
            raise
        except _OidcJwtFailure:
            raise
        except (jwt.PyJWTError, jwt.PyJWKError, KeyError, TypeError, ValueError):
            raise _OidcJwtFailure from None

    async def _jwk_for_kid(self, kid: str) -> dict[str, str]:
        now = time.monotonic()
        if now >= self._jwks_expires_at:
            await self._refresh_jwks()
        key = self._jwks_by_kid.get(kid)
        if key is None:
            await self._refresh_jwks()
            key = self._jwks_by_kid.get(kid)
        if key is None:
            raise _OidcJwtFailure
        return key

    async def _refresh_jwks(self) -> None:
        response: dict[str, object] = {}
        raw_keys: object = None
        try:
            response = await self._request_json(method="GET", url=_JWKS_ENDPOINT)
            raw_keys = response.pop("keys", None)
            if type(raw_keys) is not list or not 1 <= len(raw_keys) <= _MAX_JWKS_KEYS:
                raise _OidcSchemaFailure
            parsed_keys: dict[str, dict[str, str]] = {}
            for raw_key in raw_keys:
                if type(raw_key) is not dict:
                    raise _OidcSchemaFailure
                kid = raw_key.get("kid")
                if (
                    type(kid) is not str
                    or not kid
                    or raw_key.get("kty") != "RSA"
                    or type(raw_key.get("n")) is not str
                    or type(raw_key.get("e")) is not str
                    or kid in parsed_keys
                ):
                    raise _OidcSchemaFailure
                try:
                    jwt.PyJWK.from_dict(raw_key)
                except (jwt.PyJWKError, TypeError, ValueError):
                    raise _OidcSchemaFailure from None
                parsed_keys[kid] = {
                    "kty": "RSA",
                    "kid": kid,
                    "n": cast(str, raw_key["n"]),
                    "e": cast(str, raw_key["e"]),
                }
            self._jwks_by_kid = parsed_keys
            self._jwks_expires_at = time.monotonic() + _JWKS_CACHE_SECONDS
        finally:
            if type(raw_keys) is list:
                raw_keys.clear()
            response.clear()

    async def _request_json(
        self,
        *,
        method: str,
        url: str,
        form: Mapping[str, str] | None = None,
    ) -> dict[str, object]:
        if (method, url) not in {
            ("POST", _TOKEN_ENDPOINT),
            ("GET", _JWKS_ENDPOINT),
        }:
            raise _OidcTransportFailure
        headers = (
            {"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"}
            if form is not None
            else {}
        )
        body = bytearray()
        try:
            async with self._http.stream(
                method,
                url,
                data=form,
                headers=headers,
                auth=None,
                timeout=self._timeout_seconds,
                follow_redirects=False,
            ) as response:
                content_type = response.headers.get("content-type", "").split(";", 1)[0]
                if (
                    response.status_code != 200
                    or content_type.lower() != "application/json"
                ):
                    raise _OidcTransportFailure
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > _MAX_RESPONSE_BYTES:
                        raise _OidcTransportFailure
                    body.extend(chunk)
            parsed = json.loads(body)
            if type(parsed) is not dict:
                raise _OidcSchemaFailure
            return cast(dict[str, object], parsed)
        except asyncio.CancelledError:
            raise
        except (_OidcTransportFailure, _OidcSchemaFailure):
            raise
        except (httpx.HTTPError, UnicodeDecodeError, json.JSONDecodeError, TypeError):
            raise _OidcTransportFailure from None
        finally:
            body[:] = b"\x00" * len(body)
            body.clear()


def _require_digest(value: object) -> None:
    if type(value) is not bytes or len(value) != 32:
        raise ValueError("HMAC key must be exactly 32 bytes")


def _canonical_now(value: object) -> datetime:
    if type(value) is not datetime:
        raise AuthRejected()
    try:
        text = format_storage_timestamp(value)
        return datetime.fromisoformat(text).astimezone(UTC)
    except (StorageIntegrityError, ValueError):
        raise AuthRejected() from None
