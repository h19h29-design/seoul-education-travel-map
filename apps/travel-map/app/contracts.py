"""Pydantic contracts for the public no-login API."""

from datetime import datetime, timedelta
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from pydantic.alias_generators import to_camel

from app.institutions.facets import InstitutionFacetOption, InstitutionFacets
from app.institutions.models import InstitutionSearchItem
from app.policy.models import (
    Classification,
    DistanceEvidenceBasis,
    PolicyProfile,
    VehicleUse,
)
from app.routing.models import FuelType, TravelMode
from app.trips.models import RouteDirection, TripPattern


class ApiModel(BaseModel):
    """Strict external model using camelCase exactly at the HTTP boundary."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )


class ApiRequestModel(BaseModel):
    """Strict HTTP input model that accepts only its documented camelCase aliases."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        extra="forbid",
        validate_by_alias=True,
        validate_by_name=False,
    )


class DestinationInput(ApiRequestModel):
    name: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    address: Annotated[str, StringConstraints(min_length=1, max_length=240)]
    latitude: Annotated[float, Field(ge=33.0, le=39.5)]
    longitude: Annotated[float, Field(ge=124.0, le=132.0)]


class CarAssumptionsInput(ApiRequestModel):
    fuel_type: FuelType
    efficiency_km_per_liter: Annotated[float, Field(ge=3.0, le=30.0)]
    parking_cost_krw: Annotated[int, Field(ge=0, le=100_000)]


class TripPreviewRequest(ApiRequestModel):
    origin_site_id: Annotated[
        str,
        StringConstraints(pattern=r"^[a-z][a-z0-9-]*:[A-Za-z0-9:_-]+$"),
    ]
    destination: DestinationInput
    starts_at: datetime
    ends_at: datetime
    trip_pattern: TripPattern
    vehicle_use: VehicleUse
    car_assumptions: CarAssumptionsInput
    has_other_local_trips_today: bool
    previous_allowance_krw: Annotated[int, Field(ge=0, le=20_000)]

    @model_validator(mode="after")
    def interval_is_aware_and_bounded(self) -> Self:
        if any(
            value.tzinfo is None or value.utcoffset() is None
            for value in (self.starts_at, self.ends_at)
        ):
            raise ValueError("startsAt and endsAt must be timezone-aware")
        duration = self.ends_at - self.starts_at
        if not timedelta(minutes=2) <= duration <= timedelta(hours=24):
            raise ValueError("trip duration must be in [2 minutes, 24 hours]")
        return self


class CoordinateResponse(ApiModel):
    latitude: float
    longitude: float


class CoverageResponse(ApiModel):
    status: Literal["SEOUL", "BUFFER", "OUT_OF_COVERAGE"]


class OriginResponse(ApiModel):
    site_id: str
    name: str
    address: str
    coordinate: CoordinateResponse


class RouteCostResponse(ApiModel):
    fare_krw: int | None = None
    fuel_krw: int | None = None
    toll_krw: int | None = None
    parking_krw: int | None = None


class RouteResponse(ApiModel):
    id: str
    mode: TravelMode
    duration_seconds: int
    distance_meters: int
    mobility_cost_krw: int | None
    cost_status: str
    cost_breakdown: RouteCostResponse | None
    geometry: tuple[CoordinateResponse, ...]
    source: str
    source_as_of: datetime
    warnings: tuple[str, ...]


class BestResponse(ApiModel):
    fastest_route_id: str | None
    shortest_route_id: str | None
    cheapest_route_id: str | None


class AmountResponse(ApiModel):
    status: str
    amount_krw: int | None
    warnings: tuple[str, ...] = ()


class AllowanceResponse(AmountResponse):
    """Allowance is intentionally a separate public amount from mobility cost."""


class ClassificationPathResponse(ApiModel):
    id: str
    distance_meters: int
    geometry: tuple[CoordinateResponse, ...]
    source: str
    queried_at: datetime


class RouteLegResponse(ApiModel):
    direction: RouteDirection
    depart_at: datetime
    routes: tuple[RouteResponse, ...]
    best: BestResponse
    mobility_cost: AmountResponse


class TripPreviewResponse(ApiModel):
    coverage: CoverageResponse
    origin: OriginResponse
    institution_snapshot_id: str | None
    trip_pattern: TripPattern
    route_legs: tuple[RouteLegResponse, ...]
    policy_scope: PolicyProfile
    classification: Classification
    classification_distance_meters: int | None
    classification_distance_basis: DistanceEvidenceBasis | None
    classification_path: ClassificationPathResponse | None
    mobility_cost: AmountResponse
    allowance: AllowanceResponse
    rule_set_id: str | None
    effective_from: str | None
    source_refs: tuple[str, ...]
    warnings: tuple[str, ...]


class PolicyDisclosureResponse(ApiModel):
    profile: Literal["SEOUL_EDU_PUBLIC_OFFICIAL_CONFIRMED"]
    profile_label: Literal["서울특별시교육청 공무원 여비 기준"]
    rule_set_id: str
    effective_from: str
    local_round_trip_exclusive_meters: int
    actual_expense_inclusive_meters: int
    four_hours_minutes: int
    under_four_hours_krw: int
    four_hours_or_more_krw: int
    official_vehicle_deduction_krw: int
    source_refs: tuple[str, ...]


class InstitutionSearchItemResponse(ApiModel):
    institution_id: str
    site_id: str
    site_name: str
    official_name: str
    display_name: str
    institution_type: str
    foundation_type: str
    education_office: str | None
    road_address: str
    district: str
    coordinate: CoordinateResponse
    coordinate_quality: str
    snapshot_id: str
    snapshot_as_of: str

    @classmethod
    def from_domain(cls, value: InstitutionSearchItem) -> Self:
        return cls(
            institution_id=value.institution_id,
            site_id=value.site_id,
            site_name=value.site_name,
            official_name=value.official_name,
            display_name=value.display_name,
            institution_type=value.institution_type,
            foundation_type=value.foundation_type,
            education_office=value.education_office,
            road_address=value.road_address,
            district=value.district,
            coordinate=CoordinateResponse(
                latitude=value.coordinate.latitude,
                longitude=value.coordinate.longitude,
            ),
            coordinate_quality=value.coordinate_quality,
            snapshot_id=value.snapshot_id,
            snapshot_as_of=value.snapshot_as_of,
        )


class InstitutionSearchResponse(ApiModel):
    items: tuple[InstitutionSearchItemResponse, ...]
    total: int
    next_offset: int | None
    snapshot_id: str


class InstitutionFacetOptionResponse(ApiModel):
    value: str
    label: str
    count: int

    @classmethod
    def from_domain(cls, value: InstitutionFacetOption) -> Self:
        return cls(value=value.value, label=value.label, count=value.count)


class InstitutionFacetsResponse(ApiModel):
    snapshot_id: str
    institution_types: tuple[InstitutionFacetOptionResponse, ...]
    foundation_types: tuple[InstitutionFacetOptionResponse, ...]
    education_offices: tuple[InstitutionFacetOptionResponse, ...]
    districts: tuple[InstitutionFacetOptionResponse, ...]

    @classmethod
    def from_domain(cls, value: InstitutionFacets) -> Self:
        return cls(
            snapshot_id=value.snapshot_id,
            institution_types=tuple(
                InstitutionFacetOptionResponse.from_domain(item)
                for item in value.institution_types
            ),
            foundation_types=tuple(
                InstitutionFacetOptionResponse.from_domain(item)
                for item in value.foundation_types
            ),
            education_offices=tuple(
                InstitutionFacetOptionResponse.from_domain(item)
                for item in value.education_offices
            ),
            districts=tuple(
                InstitutionFacetOptionResponse.from_domain(item)
                for item in value.districts
            ),
        )


class PlacesResponse(ApiModel):
    items: tuple[dict[str, object], ...]
    warnings: tuple[str, ...] = ()


class ReversePlaceResponse(ApiModel):
    item: dict[str, object] | None
    warnings: tuple[str, ...] = ()


class MeResponse(ApiModel):
    authenticated: bool
    session_expires_at: datetime | None = None
