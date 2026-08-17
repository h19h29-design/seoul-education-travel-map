"""Authenticated account state, deletion, and encrypted settings endpoints."""

import sqlite3
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from app.api.auth import (
    auth_unavailable_response,
    clear_all_auth_cookies,
    require_mutating_principal,
)
from app.api.common import dependencies_for
from app.auth.models import SessionPrincipal, UserServices
from app.contracts import (
    InstitutionSearchItemResponse,
    MeResponse,
    UserSettingsInput,
    UserSettingsResponse,
)
from app.dependencies import AppDependencies
from app.storage.crypto import UserDataUnavailableError
from app.storage.models import DEFAULT_USER_SETTINGS, StorageIntegrityError

router = APIRouter(tags=["me"])

_SESSION_COOKIE = "__Host-travel_session"


def require_user_services(dependencies: AppDependencies) -> UserServices:
    """Narrow the optional private-service bundle once for a full request."""

    services = dependencies.user_services
    if type(services) is not UserServices:
        raise HTTPException(status_code=503, detail="AUTH_UNAVAILABLE")
    return services


async def require_session_principal(
    request: Request, services: UserServices
) -> SessionPrincipal:
    raw_token = request.cookies.get(_SESSION_COOKIE)
    if raw_token is None:
        raise HTTPException(status_code=401, detail="UNAUTHENTICATED")
    principal = await services.sessions.resolve(
        raw_token=raw_token, now=datetime.now(UTC)
    )
    if principal is None:
        raise HTTPException(status_code=401, detail="UNAUTHENTICATED")
    return principal


def _captured_dependencies_and_services(
    request: Request,
) -> tuple[AppDependencies, UserServices] | JSONResponse:
    try:
        dependencies = dependencies_for(request)
        return dependencies, require_user_services(dependencies)
    except HTTPException as exc:
        if exc.status_code == 503:
            return auth_unavailable_response(clear_attempt=False)
        raise


def _private_error_response(exc: HTTPException) -> JSONResponse:
    detail = exc.detail if isinstance(exc.detail, str) else "REQUEST_FAILED"
    return JSONResponse(
        {"error": {"code": detail}},
        status_code=exc.status_code,
        headers={"Cache-Control": "no-store"},
    )


def _settings_response(
    *,
    settings: UserSettingsInput,
    source: Literal["DEFAULT", "SAVED"],
    resolved_default_origin: InstitutionSearchItemResponse | None,
    warnings: tuple[str, ...],
) -> UserSettingsResponse:
    return UserSettingsResponse(
        settings=settings,
        source=source,
        resolved_default_origin=resolved_default_origin,
        warnings=warnings,
    )


@router.get("/me", response_model=MeResponse, response_model_exclude_none=True)
async def me(request: Request) -> JSONResponse:
    captured = _captured_dependencies_and_services(request)
    if isinstance(captured, JSONResponse):
        return captured
    _dependencies, services = captured
    raw_token = request.cookies.get(_SESSION_COOKIE)
    try:
        principal = (
            await services.sessions.resolve(raw_token=raw_token, now=datetime.now(UTC))
            if raw_token is not None
            else None
        )
    except (StorageIntegrityError, sqlite3.Error):
        return auth_unavailable_response(clear_attempt=False)
    payload = MeResponse(
        authenticated=principal is not None,
        session_expires_at=principal.expires_at if principal is not None else None,
    )
    response = JSONResponse(
        payload.model_dump(mode="json", by_alias=True, exclude_none=True)
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.delete("/me/data", status_code=204)
async def delete_my_data(request: Request) -> Response:
    captured = _captured_dependencies_and_services(request)
    if isinstance(captured, JSONResponse):
        return captured
    _dependencies, services = captured
    try:
        principal = await require_mutating_principal(request, services=services)
        deleted = await services.sessions.delete_user(principal=principal)
    except (StorageIntegrityError, sqlite3.Error):
        return auth_unavailable_response(clear_attempt=False)
    if not deleted:
        raise HTTPException(status_code=401, detail="UNAUTHENTICATED")
    response = Response(status_code=204)
    clear_all_auth_cookies(response)
    return response


@router.get("/me/settings", response_model=UserSettingsResponse)
async def get_settings(
    request: Request, response: Response
) -> UserSettingsResponse | JSONResponse:
    captured = _captured_dependencies_and_services(request)
    if isinstance(captured, JSONResponse):
        return captured
    dependencies, services = captured
    try:
        principal = await require_session_principal(request, services)
        stored = await services.settings.get(user_id=principal.user_id)
    except HTTPException as exc:
        return _private_error_response(exc)
    except (StorageIntegrityError, UserDataUnavailableError, sqlite3.Error):
        return auth_unavailable_response(clear_attempt=False)
    value = stored or DEFAULT_USER_SETTINGS
    resolved = (
        dependencies.institutions.get_search_item(value.default_origin_site_id)
        if value.default_origin_site_id is not None
        else None
    )
    response.headers["Cache-Control"] = "no-store"
    return _settings_response(
        settings=UserSettingsInput.from_stored(value),
        source="SAVED" if stored is not None else "DEFAULT",
        resolved_default_origin=(
            InstitutionSearchItemResponse.from_domain(resolved)
            if resolved is not None
            else None
        ),
        warnings=(
            ("DEFAULT_ORIGIN_UNAVAILABLE",)
            if value.default_origin_site_id is not None and resolved is None
            else ()
        ),
    )


@router.put("/me/settings", response_model=UserSettingsResponse)
async def put_settings(
    request: Request,
    value: UserSettingsInput,
    response: Response,
) -> UserSettingsResponse | JSONResponse:
    captured = _captured_dependencies_and_services(request)
    if isinstance(captured, JSONResponse):
        return captured
    dependencies, services = captured
    try:
        principal = await require_mutating_principal(request, services=services)
        resolved = (
            dependencies.institutions.get_search_item(value.default_origin_site_id)
            if value.default_origin_site_id is not None
            else None
        )
        if value.default_origin_site_id is not None and resolved is None:
            raise HTTPException(status_code=422, detail="DEFAULT_ORIGIN_INVALID")
        await services.settings.replace(
            user_id=principal.user_id,
            settings=value.to_stored(),
        )
    except HTTPException as exc:
        return _private_error_response(exc)
    except (StorageIntegrityError, UserDataUnavailableError, sqlite3.Error):
        return auth_unavailable_response(clear_attempt=False)
    response.headers["Cache-Control"] = "no-store"
    return _settings_response(
        settings=value,
        source="SAVED",
        resolved_default_origin=(
            InstitutionSearchItemResponse.from_domain(resolved)
            if resolved is not None
            else None
        ),
        warnings=(),
    )
