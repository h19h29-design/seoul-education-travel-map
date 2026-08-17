from typing import Annotated

from fastapi import APIRouter, Query, Request

from app.api.common import dependencies_for
from app.contracts import (
    InstitutionFacetsResponse,
    InstitutionSearchItemResponse,
    InstitutionSearchResponse,
)

router = APIRouter(tags=["institutions"])


@router.get("/institutions", response_model=InstitutionSearchResponse)
async def institutions(
    request: Request,
    q: Annotated[str, Query(max_length=120)] = "",
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
    institution_type: Annotated[str | None, Query(max_length=80)] = None,
    foundation_type: Annotated[str | None, Query(max_length=80)] = None,
    education_office: Annotated[str | None, Query(max_length=120)] = None,
    district: Annotated[str | None, Query(max_length=80)] = None,
) -> InstitutionSearchResponse:
    dependencies = dependencies_for(request)
    page = dependencies.institutions.search_page(
        query=q,
        institution_type=institution_type,
        foundation_type=foundation_type,
        education_office=education_office,
        district=district,
        limit=limit,
        offset=offset,
    )
    return InstitutionSearchResponse(
        items=tuple(
            InstitutionSearchItemResponse.from_domain(item) for item in page.items
        ),
        total=page.total,
        next_offset=page.next_offset,
        snapshot_id=page.snapshot_id,
    )


@router.get("/institutions/facets", response_model=InstitutionFacetsResponse)
async def institution_facets(request: Request) -> InstitutionFacetsResponse:
    return InstitutionFacetsResponse.from_domain(
        dependencies_for(request).institutions.facets()
    )
