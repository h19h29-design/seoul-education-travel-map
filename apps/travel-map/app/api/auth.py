"""Top-level Kakao login and versioned opaque-session endpoints."""

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from app.api.common import dependencies_for
from app.auth.models import (
    AuthRejected,
    OidcInternalError,
    OidcLoginFailed,
    SessionPrincipal,
    UserServices,
)
from app.auth.session import SessionService
from app.storage.models import StorageIntegrityError

oauth_router = APIRouter(prefix="/auth", tags=["auth"])
session_router = APIRouter(prefix="/auth", tags=["auth"])

_HOME_LOCATION = "/"
_OAUTH_ATTEMPT_COOKIE = "__Host-travel_oauth"
_SESSION_COOKIE = "__Host-travel_session"
_CSRF_COOKIE = "__Host-travel_csrf"
_OAUTH_ATTEMPT_SECONDS = 600
_SESSION_SECONDS = 7 * 24 * 60 * 60


def user_services_for(request: Request) -> UserServices:
    services = dependencies_for(request).user_services
    if type(services) is not UserServices:
        raise HTTPException(status_code=503, detail="AUTH_UNAVAILABLE")
    return services


@oauth_router.get("/kakao/start")
async def kakao_start(request: Request) -> Response:
    services = _oauth_services_or_unavailable(request, clear_attempt=False)
    if isinstance(services, JSONResponse):
        return services
    try:
        issued = await services.oauth_attempts.create(now=datetime.now(UTC))
        response = _redirect(
            services.oidc_client.authorization_url(
                state=issued.state,
                nonce=issued.nonce,
            )
        )
        _set_attempt_cookie(response, issued.attempt_token)
        return response
    except (AuthRejected, StorageIntegrityError):
        return _redirect(_HOME_LOCATION)


@oauth_router.get("/kakao/callback")
async def kakao_callback(
    request: Request,
    state: str | None = None,
    code: str | None = None,
    error: str | None = None,
) -> Response:
    services = _oauth_services_or_unavailable(request, clear_attempt=True)
    if isinstance(services, JSONResponse):
        return services
    attempt_token = request.cookies.get(_OAUTH_ATTEMPT_COOKIE)
    if error is not None or attempt_token is None or state is None or code is None:
        return _cleared_attempt_redirect()
    try:
        nonce_hash = await services.oauth_attempts.consume(
            attempt_token=attempt_token,
            state=state,
            now=datetime.now(UTC),
        )
        verified = await services.oidc_client.exchange_and_verify(
            code=code,
            expected_nonce_hash=nonce_hash,
        )
        issued = await services.sessions.issue_for_subject(
            subject_hmac=verified.subject_hmac,
            now=datetime.now(UTC),
        )
    except (AuthRejected, OidcLoginFailed, StorageIntegrityError):
        return _cleared_attempt_redirect()
    except OidcInternalError:
        return _cleared_internal_oidc_error()
    response = _redirect(_HOME_LOCATION)
    _set_session_cookies(response, issued.raw_token, issued.raw_csrf)
    _clear_attempt_cookie(response)
    return response


@session_router.post("/logout", status_code=204)
async def logout(request: Request) -> Response:
    services = user_services_for(request)
    await require_mutating_principal(request, services=services)
    raw_token = request.cookies.get(_SESSION_COOKIE)
    if raw_token is None:
        raise HTTPException(status_code=401, detail="UNAUTHENTICATED")
    await services.sessions.revoke(raw_token=raw_token)
    response = Response(status_code=204)
    _clear_all_auth_cookies(response)
    return response


async def require_mutating_principal(
    request: Request,
    *,
    services: UserServices | None = None,
) -> SessionPrincipal:
    active_services = services if services is not None else user_services_for(request)
    raw_token = request.cookies.get(_SESSION_COOKIE)
    principal = await _principal_for_token(active_services.sessions, raw_token)
    if principal is None:
        raise HTTPException(status_code=401, detail="UNAUTHENTICATED")
    await _require_same_origin_csrf(request, principal, active_services.sessions)
    return principal


async def _principal_for_token(
    service: SessionService, raw_token: str | None
) -> SessionPrincipal | None:
    if raw_token is None:
        return None
    return await service.resolve(raw_token=raw_token, now=datetime.now(UTC))


async def _require_same_origin_csrf(
    request: Request,
    principal: SessionPrincipal,
    sessions: SessionService,
) -> None:
    dependencies = dependencies_for(request)
    if request.headers.get("origin") != dependencies.settings.public_base_url:
        raise HTTPException(status_code=403, detail="INVALID_ORIGIN")
    raw_csrf = request.headers.get("x-csrf-token")
    if raw_csrf is None or not await sessions.verify_csrf(
        principal=principal,
        raw_csrf=raw_csrf,
    ):
        raise HTTPException(status_code=403, detail="CSRF_FAILED")


def _redirect(location: str) -> RedirectResponse:
    response = RedirectResponse(location, status_code=302)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return response


def _oauth_services_or_unavailable(
    request: Request, *, clear_attempt: bool
) -> UserServices | JSONResponse:
    try:
        return user_services_for(request)
    except HTTPException as exc:
        if exc.status_code != 503 or exc.detail != "AUTH_UNAVAILABLE":
            raise
        response = JSONResponse(
            {"error": {"code": "AUTH_UNAVAILABLE"}}, status_code=503
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        if clear_attempt:
            _clear_attempt_cookie(response)
        return response


def _cleared_attempt_redirect() -> RedirectResponse:
    response = _redirect(_HOME_LOCATION)
    _clear_attempt_cookie(response)
    return response


def _cleared_internal_oidc_error() -> JSONResponse:
    response = JSONResponse({"error": {"code": "OIDC_INTERNAL_ERROR"}}, status_code=500)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    _clear_attempt_cookie(response)
    return response


def _set_attempt_cookie(response: Response, value: str) -> None:
    response.set_cookie(
        _OAUTH_ATTEMPT_COOKIE,
        value,
        max_age=_OAUTH_ATTEMPT_SECONDS,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )


def _set_session_cookies(response: Response, raw_token: str, raw_csrf: str) -> None:
    response.set_cookie(
        _SESSION_COOKIE,
        raw_token,
        max_age=_SESSION_SECONDS,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )
    response.set_cookie(
        _CSRF_COOKIE,
        raw_csrf,
        max_age=_SESSION_SECONDS,
        path="/",
        secure=True,
        httponly=False,
        samesite="lax",
    )


def _clear_attempt_cookie(response: Response) -> None:
    response.delete_cookie(
        _OAUTH_ATTEMPT_COOKIE,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )


def clear_all_auth_cookies(response: Response) -> None:
    """Clear the three host-only auth cookies with their original attributes."""

    _clear_all_auth_cookies(response)


def _clear_all_auth_cookies(response: Response) -> None:
    _clear_attempt_cookie(response)
    response.delete_cookie(
        _SESSION_COOKIE,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )
    response.delete_cookie(
        _CSRF_COOKIE,
        path="/",
        secure=True,
        httponly=False,
        samesite="lax",
    )
