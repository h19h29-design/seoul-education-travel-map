"""Directional trip-preview orchestration with independent policy evidence."""

import asyncio
from dataclasses import replace
from typing import Literal

from app.cache import CAR_AND_TRANSIT_TTL_SECONDS, WALK_TTL_SECONDS
from app.contracts import (
    AllowanceResponse,
    AmountResponse,
    BestResponse,
    ClassificationPathResponse,
    CoordinateResponse,
    CoverageResponse,
    OriginResponse,
    RouteCostResponse,
    RouteLegResponse,
    RouteResponse,
    TripPreviewRequest,
    TripPreviewResponse,
)
from app.dependencies import AppDependencies
from app.institutions.models import InstitutionSite
from app.policy.models import (
    Classification,
    CoverageState,
    DistanceEvidenceBasis,
    PolicyInput,
    PolicyProfile,
    PolicyResult,
)
from app.routing.models import (
    CarAssumptions,
    Coordinate,
    CostStatus,
    ProviderResult,
    RouteCollection,
    RouteOption,
    RouteQuery,
    TravelMode,
)
from app.trips.models import PlannedTripLeg, RouteDirection, TripPattern

PUBLIC_POLICY_PROFILE = PolicyProfile.SEOUL_EDU_PUBLIC_OFFICIAL_CONFIRMED


class TripPreviewService:
    def __init__(self, dependencies: AppDependencies) -> None:
        self._dependencies = dependencies

    async def preview(self, request: TripPreviewRequest) -> TripPreviewResponse:
        site = self._dependencies.institutions.require_site(request.origin_site_id)
        origin = _site_coordinate(site)
        destination = Coordinate(
            latitude=request.destination.latitude,
            longitude=request.destination.longitude,
        )
        coverage_state = self._dependencies.coverage.classify(destination)
        origin_response = _origin_response(
            site,
            origin,
            self._dependencies.institutions.display_name_for_site(site.site_id),
        )
        snapshot_id = _snapshot_id(self._dependencies, site.site_id)
        car_assumptions = CarAssumptions(
            fuel_type=request.car_assumptions.fuel_type,
            efficiency_km_per_liter=request.car_assumptions.efficiency_km_per_liter,
            parking_cost_krw=request.car_assumptions.parking_cost_krw,
        )
        planned_legs = _plan_trip_legs(
            request=request,
            origin=origin,
            destination=destination,
            car_assumptions=car_assumptions,
        )

        if (
            coverage_state is CoverageState.OUTSIDE
            and request.trip_pattern is TripPattern.ROUND_TRIP
        ):
            return _outside_response(
                request=request,
                origin=origin_response,
                snapshot_id=snapshot_id,
            )

        display_collections = await asyncio.gather(
            *(self._display_leg(leg) for leg in planned_legs)
        )
        classification_routes = await self._classification_routes(planned_legs)
        classification_path, classification_distance, classification_basis = (
            _classification_evidence(
                request.trip_pattern,
                classification_routes,
            )
        )
        policy = self._policy_result(
            request=request,
            coverage_state=coverage_state,
            classification_distance=classification_distance,
            classification_basis=classification_basis,
        )
        route_legs = tuple(
            _route_leg_response(planned, collection)
            for planned, collection in zip(
                planned_legs,
                display_collections,
                strict=True,
            )
        )
        provider_warnings = tuple(
            warning.code
            for collection in display_collections
            for warning in collection.warnings
        )
        warnings = _unique(provider_warnings + policy.allowance.warnings)
        return TripPreviewResponse(
            coverage=CoverageResponse(status=_coverage_status(coverage_state)),
            origin=origin_response,
            institution_snapshot_id=snapshot_id,
            trip_pattern=request.trip_pattern,
            route_legs=route_legs,
            policy_scope=PUBLIC_POLICY_PROFILE,
            classification=policy.classification,
            classification_distance_meters=classification_distance,
            classification_distance_basis=classification_basis,
            classification_path=classification_path,
            mobility_cost=_aggregate_mobility_cost(route_legs),
            allowance=AllowanceResponse(
                status=policy.allowance.status.value,
                amount_krw=policy.allowance.amount_krw,
                warnings=policy.allowance.warnings,
            ),
            rule_set_id=policy.rule_set_id,
            effective_from=policy.effective_from,
            source_refs=policy.source_refs,
            warnings=warnings,
        )

    async def _display_leg(self, planned: PlannedTripLeg) -> RouteCollection:
        collections = await asyncio.gather(
            *(
                self._display_mode_routes(
                    replace(
                        planned.query,
                        mode=mode,
                        car_assumptions=(
                            planned.query.car_assumptions
                            if mode is TravelMode.CAR
                            else None
                        ),
                    ),
                    mode,
                )
                for mode in TravelMode
            )
        )
        return self._dependencies.route_orchestrator.combine_collections(
            dict(zip(TravelMode, collections, strict=True))
        )

    async def _display_mode_routes(
        self,
        query: RouteQuery,
        mode: TravelMode,
    ) -> RouteCollection:
        key = self._dependencies.cache.route_key(
            provider="ROUTE_ORCHESTRATOR",
            mode=mode,
            origin=query.origin,
            destination=query.destination,
            depart_at=query.depart_at,
            options=(
                {
                    "fuelType": (
                        query.car_assumptions.fuel_type.value
                        if query.car_assumptions is not None
                        else None
                    ),
                    "efficiencyKmPerLiter": (
                        query.car_assumptions.efficiency_km_per_liter
                        if query.car_assumptions is not None
                        else None
                    ),
                    "parkingCostKrw": (
                        query.car_assumptions.parking_cost_krw
                        if query.car_assumptions is not None
                        else None
                    ),
                }
                if mode is TravelMode.CAR
                else {}
            ),
        )
        cached = self._dependencies.cache.get(key)
        if type(cached) is RouteCollection:
            return cached
        collected = await self._dependencies.route_orchestrator.collect(query, {mode})
        return self._dependencies.cache.set(
            key,
            collected,
            ttl_seconds=(
                WALK_TTL_SECONDS
                if mode is TravelMode.WALK
                else CAR_AND_TRANSIT_TTL_SECONDS
            ),
        )

    async def _classification_routes(
        self,
        planned_legs: tuple[PlannedTripLeg, ...],
    ) -> tuple[RouteOption | None, ...]:
        routes: list[RouteOption | None] = []
        for leg in planned_legs:
            routes.append(await self._classification_route(leg.query))
        return tuple(routes)

    async def _classification_route(self, query: RouteQuery) -> RouteOption | None:
        provider = self._dependencies.classification_provider
        key = self._dependencies.cache.route_key(
            provider=provider.name,
            mode=TravelMode.CAR,
            origin=query.origin,
            destination=query.destination,
            depart_at=query.depart_at,
            options={"priority": "DISTANCE", "alternatives": False},
        )
        cached = self._dependencies.cache.get(key)
        if type(cached) is RouteOption:
            return cached
        try:
            result = await provider.get_routes(query)
        except Exception:  # noqa: BLE001 - provider details must not become public
            return None
        if (
            type(result) is not ProviderResult
            or result.provider != provider.name
            or not result.routes
        ):
            return None
        route = result.routes[0]
        if route.mode is not TravelMode.CAR:
            return None
        return self._dependencies.cache.set(
            key,
            route,
            ttl_seconds=CAR_AND_TRANSIT_TTL_SECONDS,
        )

    def _policy_result(
        self,
        *,
        request: TripPreviewRequest,
        coverage_state: CoverageState,
        classification_distance: int | None,
        classification_basis: DistanceEvidenceBasis | None,
    ) -> PolicyResult:
        return self._dependencies.policy.calculate(
            PolicyInput(
                destination_in_seoul=coverage_state is CoverageState.SEOUL,
                measured_distance_m=classification_distance,
                distance_evidence_basis=classification_basis,
                starts_at=request.starts_at,
                ends_at=request.ends_at,
                policy_profile=PUBLIC_POLICY_PROFILE,
                vehicle_use=request.vehicle_use,
                has_other_local_trips_today=request.has_other_local_trips_today,
                previous_allowance_krw=request.previous_allowance_krw,
            )
        )


def _plan_trip_legs(
    *,
    request: TripPreviewRequest,
    origin: Coordinate,
    destination: Coordinate,
    car_assumptions: CarAssumptions,
) -> tuple[PlannedTripLeg, ...]:
    outbound = PlannedTripLeg(
        direction=RouteDirection.OUTBOUND,
        query=RouteQuery(
            origin=origin,
            destination=destination,
            depart_at=request.starts_at,
            mode=TravelMode.CAR,
            car_assumptions=car_assumptions,
        ),
    )
    returning_assumptions = (
        replace(car_assumptions, parking_cost_krw=0)
        if request.trip_pattern is TripPattern.ROUND_TRIP
        else car_assumptions
    )
    returning = PlannedTripLeg(
        direction=RouteDirection.RETURN,
        query=RouteQuery(
            origin=destination,
            destination=origin,
            depart_at=request.ends_at,
            mode=TravelMode.CAR,
            car_assumptions=returning_assumptions,
        ),
    )
    if request.trip_pattern is TripPattern.ROUND_TRIP:
        return (outbound, returning)
    if request.trip_pattern is TripPattern.OUTBOUND_ONLY_END_AFTER_SCHEDULE:
        return (outbound,)
    if request.trip_pattern is TripPattern.RETURN_ONLY_DIRECT_TO_DESTINATION:
        return (returning,)
    raise ValueError("unsupported trip pattern")


def _site_coordinate(site: InstitutionSite) -> Coordinate:
    if site.routing_anchor_latitude is None or site.routing_anchor_longitude is None:
        raise ValueError("active institution site has no verified routing anchor")
    return Coordinate(
        latitude=site.routing_anchor_latitude,
        longitude=site.routing_anchor_longitude,
    )


def _origin_response(
    site: InstitutionSite,
    coordinate: Coordinate,
    display_name: str,
) -> OriginResponse:
    return OriginResponse(
        site_id=site.site_id,
        name=display_name,
        address=site.road_address,
        coordinate=CoordinateResponse(
            latitude=coordinate.latitude,
            longitude=coordinate.longitude,
        ),
    )


def _snapshot_id(dependencies: AppDependencies, site_id: str) -> str | None:
    for item in dependencies.institutions.search(query=site_id, limit=50):
        if item.site_id == site_id:
            return item.snapshot_id
    return None


def _coverage_status(
    state: CoverageState,
) -> Literal["SEOUL", "BUFFER", "OUT_OF_COVERAGE"]:
    if state is CoverageState.SEOUL:
        return "SEOUL"
    if state is CoverageState.BUFFER:
        return "BUFFER"
    if state is CoverageState.OUTSIDE:
        return "OUT_OF_COVERAGE"
    raise ValueError("unsupported coverage state")


def _outside_response(
    *,
    request: TripPreviewRequest,
    origin: OriginResponse,
    snapshot_id: str | None,
) -> TripPreviewResponse:
    unavailable = AmountResponse(
        status="UNAVAILABLE",
        amount_krw=None,
        warnings=("OUT_OF_COVERAGE",),
    )
    return TripPreviewResponse(
        coverage=CoverageResponse(status="OUT_OF_COVERAGE"),
        origin=origin,
        institution_snapshot_id=snapshot_id,
        trip_pattern=request.trip_pattern,
        route_legs=(),
        policy_scope=PUBLIC_POLICY_PROFILE,
        classification=Classification.REVIEW_REQUIRED,
        classification_distance_meters=None,
        classification_distance_basis=None,
        classification_path=None,
        mobility_cost=unavailable,
        allowance=AllowanceResponse(
            status="REVIEW_REQUIRED",
            amount_krw=None,
            warnings=("OUT_OF_COVERAGE",),
        ),
        rule_set_id=None,
        effective_from=None,
        source_refs=(),
        warnings=("OUT_OF_COVERAGE",),
    )


def _classification_evidence(
    trip_pattern: TripPattern,
    routes: tuple[RouteOption | None, ...],
) -> tuple[
    ClassificationPathResponse | None,
    int | None,
    DistanceEvidenceBasis | None,
]:
    if not routes or any(route is None for route in routes):
        return None, None, None
    available = tuple(route for route in routes if route is not None)
    basis = (
        DistanceEvidenceBasis.ROUND_TRIP_EXACT
        if trip_pattern is TripPattern.ROUND_TRIP
        else DistanceEvidenceBasis.ONE_WAY_LOWER_BOUND
    )
    distance = sum(route.distance_meters for route in available)
    return (
        ClassificationPathResponse(
            id="+".join(route.id for route in available),
            distance_meters=distance,
            geometry=tuple(
                _coordinate_response(coordinate)
                for route in available
                for coordinate in route.geometry
            ),
            source="+".join(route.source for route in available),
            queried_at=max(route.source_as_of for route in available),
        ),
        distance,
        basis,
    )


def _route_leg_response(
    planned: PlannedTripLeg,
    collection: RouteCollection,
) -> RouteLegResponse:
    best = BestResponse(
        fastest_route_id=collection.best.fastest_route_id,
        shortest_route_id=collection.best.shortest_route_id,
        cheapest_route_id=collection.best.cheapest_route_id,
    )
    return RouteLegResponse(
        direction=planned.direction,
        depart_at=planned.query.depart_at,
        routes=tuple(_route_response(route) for route in collection.routes),
        best=best,
        mobility_cost=_mobility_cost(collection.routes, best.fastest_route_id),
    )


def _route_response(route: RouteOption) -> RouteResponse:
    breakdown = route.cost_breakdown
    return RouteResponse(
        id=route.id,
        mode=route.mode,
        duration_seconds=route.duration_seconds,
        distance_meters=route.distance_meters,
        mobility_cost_krw=route.mobility_cost_krw,
        cost_status=route.cost_status.value,
        cost_breakdown=(
            RouteCostResponse(
                fare_krw=breakdown.fare_krw,
                fuel_krw=breakdown.fuel_krw,
                toll_krw=breakdown.toll_krw,
                parking_krw=breakdown.parking_krw,
            )
            if breakdown is not None
            else None
        ),
        geometry=tuple(_coordinate_response(item) for item in route.geometry),
        source=route.source,
        source_as_of=route.source_as_of,
        warnings=route.warnings,
    )


def _coordinate_response(value: Coordinate) -> CoordinateResponse:
    return CoordinateResponse(latitude=value.latitude, longitude=value.longitude)


def _mobility_cost(
    routes: tuple[RouteOption, ...],
    fastest_route_id: str | None,
) -> AmountResponse:
    route = next((item for item in routes if item.id == fastest_route_id), None)
    if route is None or route.cost_status is CostStatus.UNKNOWN:
        return AmountResponse(
            status="UNKNOWN",
            amount_krw=None,
            warnings=route.warnings if route is not None else (),
        )
    return AmountResponse(
        status=route.cost_status.value,
        amount_krw=route.mobility_cost_krw,
        warnings=route.warnings,
    )


def _aggregate_mobility_cost(
    route_legs: tuple[RouteLegResponse, ...],
) -> AmountResponse:
    if not route_legs or any(
        leg.mobility_cost.status not in {"KNOWN", "ESTIMATED"}
        or leg.mobility_cost.amount_krw is None
        for leg in route_legs
    ):
        return AmountResponse(
            status="UNKNOWN",
            amount_krw=None,
            warnings=("PARTIAL_MOBILITY_COST",),
        )
    status = (
        "ESTIMATED"
        if any(leg.mobility_cost.status == "ESTIMATED" for leg in route_legs)
        else "KNOWN"
    )
    return AmountResponse(
        status=status,
        amount_krw=sum(
            leg.mobility_cost.amount_krw
            for leg in route_legs
            if leg.mobility_cost.amount_krw is not None
        ),
        warnings=_unique(
            tuple(
                warning for leg in route_legs for warning in leg.mobility_cost.warnings
            )
        ),
    )


def _unique(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))
