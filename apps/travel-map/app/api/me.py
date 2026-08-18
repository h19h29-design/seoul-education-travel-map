"""Authenticated account state, deletion, and encrypted settings endpoints."""

import base64
import json
import re
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
    HistoryDetailResponse,
    HistoryListItemResponse,
    HistoryPageResponse,
    InstitutionSearchItemResponse,
    MeResponse,
    UserSettingsInput,
    UserSettingsResponse,
)
from app.dependencies import AppDependencies
from app.storage.crypto import UserDataUnavailableError
from app.storage.models import (
    DEFAULT_USER_SETTINGS,
    HistoryCursor,
    HistoryDetail,
    HistoryListItem,
    StorageIntegrityError,
    format_storage_timestamp,
    parse_storage_timestamp,
)

router = APIRouter(tags=["me"])

_SESSION_COOKIE = "__Host-travel_session"
_HISTORY_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{22}$")


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


def encode_history_cursor(cursor: HistoryCursor) -> str:
    """Encode the sole accepted opaque history cursor representation."""

    try:
        if type(cursor) is not HistoryCursor:
            raise ValueError
        history_id = _validated_history_id(cursor.history_id)
        raw = json.dumps(
            {
                "createdAt": format_storage_timestamp(cursor.created_at),
                "historyId": history_id,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, StorageIntegrityError):
        raise ValueError("history cursor is invalid") from None
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def decode_history_cursor(value: object) -> HistoryCursor:
    """Reject all noncanonical encodings before they reach private storage."""

    if (
        type(value) is not str
        or not value
        or "=" in value
        or not all(
            character.isascii() and (character.isalnum() or character in "-_")
            for character in value
        )
    ):
        raise ValueError("history cursor is invalid")
    try:
        encoded = value.encode("ascii")
        raw = base64.b64decode(
            encoded + b"=" * (-len(encoded) % 4), altchars=b"-_", validate=True
        )
        payload = json.loads(raw.decode("utf-8"))
        if type(payload) is not dict or set(payload) != {"createdAt", "historyId"}:
            raise ValueError
        created_at = parse_storage_timestamp(payload["createdAt"])
        history_id = _validated_history_id(payload["historyId"])
        cursor = HistoryCursor(created_at=created_at, history_id=history_id)
    except (
        TypeError,
        ValueError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        StorageIntegrityError,
    ):
        raise ValueError("history cursor is invalid") from None
    if encode_history_cursor(cursor) != value:
        raise ValueError("history cursor is invalid")
    return cursor


def _validated_history_id(value: object) -> str:
    if type(value) is not str or _HISTORY_ID_PATTERN.fullmatch(value) is None:
        raise ValueError("history ID is invalid")
    return value


def _parse_history_list_request(request: Request) -> tuple[HistoryCursor | None, int]:
    if set(request.query_params) - {"cursor", "limit"}:
        raise ValueError("history query is invalid")
    cursor_values = request.query_params.getlist("cursor")
    limit_values = request.query_params.getlist("limit")
    if len(cursor_values) > 1 or len(limit_values) > 1:
        raise ValueError("history query is invalid")
    before = decode_history_cursor(cursor_values[0]) if cursor_values else None
    if not limit_values:
        return before, 50
    raw_limit = limit_values[0]
    if not re.fullmatch(r"(?:[1-9]|[1-9][0-9]|100)", raw_limit):
        raise ValueError("history query is invalid")
    return before, int(raw_limit)


def _history_validation_response() -> JSONResponse:
    return JSONResponse(
        {"error": {"code": "VALIDATION_ERROR"}},
        status_code=422,
        headers={"Cache-Control": "no-store"},
    )


def _history_not_found_response() -> JSONResponse:
    return JSONResponse(
        {"error": {"code": "HISTORY_NOT_FOUND"}},
        status_code=404,
        headers={"Cache-Control": "no-store"},
    )


def _assert_history_owner(user_id: int, value: HistoryListItem | HistoryDetail) -> None:
    if value.metadata.user_id != user_id:
        raise UserDataUnavailableError()


@router.get("/me/history", response_model=HistoryPageResponse)
async def get_history_page(
    request: Request, response: Response
) -> HistoryPageResponse | JSONResponse:
    captured = _captured_dependencies_and_services(request)
    if isinstance(captured, JSONResponse):
        return captured
    _dependencies, services = captured
    try:
        principal = await require_session_principal(request, services)
        before, limit = _parse_history_list_request(request)
        page = await services.history.list_page(
            user_id=principal.user_id, before=before, limit=limit
        )
        for item in page.items:
            _assert_history_owner(principal.user_id, item)
        next_cursor = (
            encode_history_cursor(page.next_cursor)
            if page.next_cursor is not None
            else None
        )
    except ValueError:
        return _history_validation_response()
    except HTTPException as exc:
        return _private_error_response(exc)
    except (StorageIntegrityError, UserDataUnavailableError, sqlite3.Error):
        return auth_unavailable_response(clear_attempt=False)
    response.headers["Cache-Control"] = "no-store"
    return HistoryPageResponse(
        items=tuple(HistoryListItemResponse.from_domain(item) for item in page.items),
        next_cursor=next_cursor,
    )


@router.get("/me/history/{history_id}", response_model=HistoryDetailResponse)
async def get_history_detail(
    request: Request, history_id: str, response: Response
) -> HistoryDetailResponse | JSONResponse:
    captured = _captured_dependencies_and_services(request)
    if isinstance(captured, JSONResponse):
        return captured
    dependencies, services = captured
    try:
        principal = await require_session_principal(request, services)
        checked_history_id = _validated_history_id(history_id)
        detail = await services.history.get(
            user_id=principal.user_id, history_id=checked_history_id
        )
        if detail is None:
            return _history_not_found_response()
        _assert_history_owner(principal.user_id, detail)
    except ValueError:
        return _history_validation_response()
    except HTTPException as exc:
        return _private_error_response(exc)
    except (StorageIntegrityError, UserDataUnavailableError, sqlite3.Error):
        return auth_unavailable_response(clear_attempt=False)
    resolved = dependencies.institutions.get_search_item(detail.draft.origin_site_id)
    response.headers["Cache-Control"] = "no-store"
    return HistoryDetailResponse.from_domain(
        detail,
        resolved_origin=(
            InstitutionSearchItemResponse.from_domain(resolved)
            if resolved is not None
            else None
        ),
        warnings=(("HISTORY_ORIGIN_UNAVAILABLE",) if resolved is None else ()),
    )


@router.delete("/me/history/{history_id}", status_code=204)
async def delete_history(request: Request, history_id: str) -> Response:
    captured = _captured_dependencies_and_services(request)
    if isinstance(captured, JSONResponse):
        return captured
    _dependencies, services = captured
    try:
        principal = await require_mutating_principal(request, services=services)
        checked_history_id = _validated_history_id(history_id)
        deleted = await services.history.delete(
            user_id=principal.user_id, history_id=checked_history_id
        )
    except ValueError:
        return _history_validation_response()
    except HTTPException as exc:
        return _private_error_response(exc)
    except (StorageIntegrityError, UserDataUnavailableError, sqlite3.Error):
        return auth_unavailable_response(clear_attempt=False)
    if not deleted:
        return _history_not_found_response()
    response = Response(status_code=204)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.delete("/me/history", status_code=204)
async def delete_all_history(request: Request) -> Response:
    captured = _captured_dependencies_and_services(request)
    if isinstance(captured, JSONResponse):
        return captured
    _dependencies, services = captured
    try:
        principal = await require_mutating_principal(request, services=services)
        await services.history.delete_all(user_id=principal.user_id)
    except HTTPException as exc:
        return _private_error_response(exc)
    except (StorageIntegrityError, UserDataUnavailableError, sqlite3.Error):
        return auth_unavailable_response(clear_attempt=False)
    response = Response(status_code=204)
    response.headers["Cache-Control"] = "no-store"
    return response


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
