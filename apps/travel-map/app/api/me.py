"""Authenticated account state and deletion endpoints."""

import sqlite3
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from app.api.auth import (
    auth_unavailable_response,
    clear_all_auth_cookies,
    require_mutating_principal,
    user_services_for,
)
from app.contracts import MeResponse
from app.storage.models import StorageIntegrityError

router = APIRouter(tags=["me"])

_SESSION_COOKIE = "__Host-travel_session"


@router.get("/me", response_model=MeResponse, response_model_exclude_none=True)
async def me(request: Request) -> JSONResponse:
    try:
        services = user_services_for(request)
    except HTTPException as exc:
        if exc.status_code == 503 and exc.detail == "AUTH_UNAVAILABLE":
            return auth_unavailable_response(clear_attempt=False)
        raise
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
    services = user_services_for(request)
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
