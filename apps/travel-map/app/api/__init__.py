"""Versioned public API routers."""

from fastapi import APIRouter

from app.api.auth import session_router
from app.api.bootstrap import router as bootstrap_router
from app.api.geodata import router as geodata_router
from app.api.institutions import router as institutions_router
from app.api.me import router as me_router
from app.api.places import router as places_router
from app.api.policy import router as policy_router
from app.api.trips import router as trips_router


def create_router(*, include_private: bool = True) -> APIRouter:
    """Build the versioned API with an explicit private-feature boundary."""

    router = APIRouter(prefix="/api/v1")
    router.include_router(bootstrap_router)
    router.include_router(institutions_router)
    router.include_router(geodata_router)
    router.include_router(places_router)
    router.include_router(policy_router)
    router.include_router(trips_router)
    if include_private:
        router.include_router(session_router)
        router.include_router(me_router)
    return router


router = create_router()
