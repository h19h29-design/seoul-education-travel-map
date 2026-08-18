"""Pydantic contracts for the public no-login API."""

from datetime import datetime, timedelta
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    StringConstraints,
    model_validator,
)
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
from app.storage.models import (
    HistoryDetail,
    HistoryListItem,
    HistoryRecalculationDraft,
    HistoryRouteLegSummary,
    StoredUserSettings,
)
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


class UserSettingsInput(ApiRequestModel):
    default_origin_site_id: str | None
    default_trip_pattern: TripPattern
    default_duration_minutes: Annotated[StrictInt, Field(ge=2, le=1_440)]
    vehicle_use: VehicleUse
    fuel_type: FuelType
    efficiency_km_per_liter: Annotated[StrictFloat, Field(ge=3.0, le=30.0)]
    parking_cost_krw: Annotated[StrictInt, Field(ge=0, le=100_000)]
    route_sort: Literal["time", "distance", "cost"]

    @classmethod
    def from_stored(cls, value: StoredUserSettings) -> Self:
        return cls.model_validate(
            {
                "defaultOriginSiteId": value.default_origin_site_id,
                "defaultTripPattern": value.default_trip_pattern,
                "defaultDurationMinutes": value.default_duration_minutes,
                "vehicleUse": value.vehicle_use,
                "fuelType": value.fuel_type,
                "efficiencyKmPerLiter": value.efficiency_km_per_liter,
                "parkingCostKrw": value.parking_cost_krw,
                "routeSort": value.route_sort,
            }
        )

    def to_stored(self) -> StoredUserSettings:
        return StoredUserSettings(
            default_origin_site_id=self.default_origin_site_id,
            default_trip_pattern=self.default_trip_pattern,
            default_duration_minutes=self.default_duration_minutes,
            vehicle_use=self.vehicle_use,
            fuel_type=self.fuel_type,
            efficiency_km_per_liter=self.efficiency_km_per_liter,
            parking_cost_krw=self.parking_cost_krw,
            route_sort=self.route_sort,
        )


class UserSettingsResponse(ApiModel):
    settings: UserSettingsInput
    source: Literal["DEFAULT", "SAVED"]
    resolved_default_origin: InstitutionSearchItemResponse | None
    warnings: tuple[str, ...]


class HistoryListItemResponse(ApiModel):
    id: str
    calculated_at: datetime
    expires_at: datetime
    origin_name: str
    destination_name: str
    trip_pattern: TripPattern
    classification: str
    allowance_status: str
    allowance_krw: int | None

    @classmethod
    def from_domain(cls, value: HistoryListItem) -> Self:
        return cls(
            id=value.metadata.id,
            calculated_at=value.metadata.created_at,
            expires_at=value.metadata.expires_at,
            origin_name=value.origin_name,
            destination_name=value.destination_name,
            trip_pattern=value.trip_pattern,
            classification=value.classification,
            allowance_status=value.allowance_status,
            allowance_krw=value.allowance_krw,
        )


class HistoryRecalculationDraftResponse(ApiModel):
    origin_site_id: str
    origin_name: str
    destination_name: str
    destination_address: str
    trip_pattern: TripPattern
    starts_at: datetime
    ends_at: datetime

    @classmethod
    def from_domain(cls, value: HistoryRecalculationDraft) -> Self:
        return cls(
            origin_site_id=value.origin_site_id,
            origin_name=value.origin_name,
            destination_name=value.destination_name,
            destination_address=value.destination_address,
            trip_pattern=value.trip_pattern,
            starts_at=value.starts_at,
            ends_at=value.ends_at,
        )


class HistoryRouteLegSummaryResponse(ApiModel):
    direction: RouteDirection
    mode: TravelMode
    duration_seconds: int
    distance_meters: int
    mobility_cost_krw: int | None

    @classmethod
    def from_domain(cls, value: HistoryRouteLegSummary) -> Self:
        return cls(
            direction=value.direction,
            mode=value.mode,
            duration_seconds=value.duration_seconds,
            distance_meters=value.distance_meters,
            mobility_cost_krw=value.mobility_cost_krw,
        )


class HistoryDetailResponse(ApiModel):
    item: HistoryListItemResponse
    recalculation_draft: HistoryRecalculationDraftResponse
    resolved_origin: InstitutionSearchItemResponse | None
    route_summary: tuple[HistoryRouteLegSummaryResponse, ...]
    rule_set_id: str | None
    effective_from: str | None
    warnings: tuple[str, ...]

    @classmethod
    def from_domain(
        cls,
        value: HistoryDetail,
        *,
        resolved_origin: InstitutionSearchItemResponse | None,
        warnings: tuple[str, ...],
    ) -> Self:
        return cls(
            item=HistoryListItemResponse(
                id=value.metadata.id,
                calculated_at=value.metadata.created_at,
                expires_at=value.metadata.expires_at,
                origin_name=value.draft.origin_name,
                destination_name=value.draft.destination_name,
                trip_pattern=value.draft.trip_pattern,
                classification=value.summary.classification,
                allowance_status=value.summary.allowance_status,
                allowance_krw=value.summary.allowance_krw,
            ),
            recalculation_draft=HistoryRecalculationDraftResponse.from_domain(
                value.draft
            ),
            resolved_origin=resolved_origin,
            route_summary=tuple(
                HistoryRouteLegSummaryResponse.from_domain(item)
                for item in value.summary.route_legs
            ),
            rule_set_id=value.summary.rule_set_id,
            effective_from=value.summary.effective_from,
            warnings=warnings,
        )


class HistoryPageResponse(ApiModel):
    items: tuple[HistoryListItemResponse, ...]
    next_cursor: str | None
