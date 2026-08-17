import sqlite3
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request

from app.api.common import client_ip, dependencies_for
from app.auth.models import UserServices
from app.contracts import TripPreviewRequest, TripPreviewResponse
from app.dependencies import AppDependencies
from app.institutions.store import UnknownSiteError
from app.services.trip_preview import TripPreviewService
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
    try:
        preview = await TripPreviewService(dependencies).preview(payload)
    except UnknownSiteError:
        raise HTTPException(status_code=404, detail="UNKNOWN_ORIGIN_SITE") from None
    return await _preview_with_user_storage_boundary(request, dependencies, preview)


async def _preview_with_user_storage_boundary(
    request: Request,
    dependencies: AppDependencies,
    preview: TripPreviewResponse,
) -> TripPreviewResponse:
    raw_token = request.cookies.get(_SESSION_COOKIE)
    if raw_token is None:
        return preview
    user_services = dependencies.user_services
    if type(user_services) is not UserServices:
        return _with_history_not_saved(preview)
    try:
        await user_services.sessions.resolve(raw_token=raw_token, now=datetime.now(UTC))
    except (StorageIntegrityError, sqlite3.Error):
        return _with_history_not_saved(preview)
    return preview


def _with_history_not_saved(preview: TripPreviewResponse) -> TripPreviewResponse:
    if _HISTORY_NOT_SAVED in preview.warnings:
        return preview
    return preview.model_copy(
        update={"warnings": (*preview.warnings, _HISTORY_NOT_SAVED)}
    )
