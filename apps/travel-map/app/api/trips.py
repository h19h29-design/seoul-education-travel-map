import sqlite3

from fastapi import APIRouter, HTTPException, Request

from app.api.auth import require_mutating_principal
from app.api.common import client_ip, dependencies_for
from app.auth.models import SessionPrincipal, UserServices
from app.contracts import TripPreviewRequest, TripPreviewResponse
from app.dependencies import AppDependencies
from app.institutions.store import UnknownSiteError
from app.services.trip_preview import TripPreviewService, project_history_records
from app.storage.crypto import UserDataUnavailableError
from app.storage.models import StorageIntegrityError

router = APIRouter(tags=["trips"])
_SESSION_COOKIE = "__Host-travel_session"
_HISTORY_NOT_SAVED = "HISTORY_NOT_SAVED"


@router.post("/trips/preview", response_model=TripPreviewResponse)
async def trip_preview(
    request: Request,
    payload: TripPreviewRequest,
) -> TripPreviewResponse:
    dependencies = dependencies_for(request)
    decision = dependencies.rate_limiter.check(
        "preview",
        client_ip(request, dependencies.settings.trusted_proxy_cidrs or ()),
    )
    if not decision.allowed:
        raise HTTPException(
            status_code=429,
            detail="RATE_LIMITED",
            headers={"Retry-After": str(decision.retry_after_seconds)},
        )
    principal, user_services, history_unavailable = await _preview_user_context(
        request, dependencies
    )
    try:
        preview = await TripPreviewService(dependencies).preview(payload)
    except UnknownSiteError:
        raise HTTPException(status_code=404, detail="UNKNOWN_ORIGIN_SITE") from None
    if history_unavailable:
        return _with_history_not_saved(preview)
    if principal is None or user_services is None:
        return preview
    try:
        draft, summary = project_history_records(payload, preview)
        await user_services.history.create(
            user_id=principal.user_id,
            draft=draft,
            summary=summary,
        )
    except (StorageIntegrityError, UserDataUnavailableError, sqlite3.Error):
        return _with_history_not_saved(preview)
    return preview


async def _preview_user_context(
    request: Request,
    dependencies: AppDependencies,
) -> tuple[SessionPrincipal | None, UserServices | None, bool]:
    raw_token = request.cookies.get(_SESSION_COOKIE)
    if raw_token is None:
        return None, None, False
    user_services = dependencies.user_services
    if type(user_services) is not UserServices:
        return None, None, True
    try:
        principal = await require_mutating_principal(request, services=user_services)
    except HTTPException as exc:
        if exc.status_code == 401:
            return None, None, False
        raise
    except (StorageIntegrityError, UserDataUnavailableError, sqlite3.Error):
        return None, None, True
    return principal, user_services, False


def _with_history_not_saved(preview: TripPreviewResponse) -> TripPreviewResponse:
    if _HISTORY_NOT_SAVED in preview.warnings:
        return preview
    return preview.model_copy(
        update={"warnings": (*preview.warnings, _HISTORY_NOT_SAVED)}
    )
