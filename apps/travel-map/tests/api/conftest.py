from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from app.cache import TtlLruCache
from app.dependencies import AppDependencies
from app.institutions.store import InstitutionStore
from app.main import create_app
from app.policy.coverage import CoverageService
from app.policy.engine import PolicyEngine
from app.policy.rules import RuleRepository
from app.providers.kakao_local import (
    BoundingBox,
    PlaceCandidate,
    PlaceSearchResult,
    ReversePlaceResult,
)
from app.rate_limit import FixedWindowRateLimiter
from app.routing.models import (
    Coordinate,
    CostStatus,
    ProviderResult,
    RouteCostBreakdown,
    RouteOption,
    RouteQuery,
    TravelMode,
)
from app.routing.orchestrator import RouteOrchestrator
from app.settings import Settings
from fastapi.testclient import TestClient
from pydantic import SecretStr

SEOUL = ZoneInfo("Asia/Seoul")
FIXTURES = Path("apps/travel-map/tests/fixtures")


class FakeRouteProvider:
    def __init__(self, name: str, mode: TravelMode) -> None:
        self.name = name
        self.supported_modes = frozenset({mode})
        self._mode = mode
        self.queries: list[RouteQuery] = []
        self._unknown_cost_calls: set[int] = set()

    @property
    def call_count(self) -> int:
        return len(self.queries)

    def return_unknown_cost_on_call(self, call_number: int) -> None:
        self._unknown_cost_calls.add(call_number)

    async def get_routes(self, query: RouteQuery) -> ProviderResult:
        self.queries.append(query)
        call_index = len(self.queries)
        if self._mode is TravelMode.TRANSIT:
            cost = 1_550 + 100 * (call_index - 1)
            status = CostStatus.KNOWN
            breakdown = RouteCostBreakdown(fare_krw=cost)
        elif self._mode is TravelMode.CAR:
            parking = (
                query.car_assumptions.parking_cost_krw
                if query.car_assumptions is not None
                else 0
            )
            fuel = 2_000 + 500 * (call_index - 1)
            cost = fuel + parking
            status = CostStatus.ESTIMATED
            breakdown = RouteCostBreakdown(fuel_krw=fuel, parking_krw=parking)
        else:
            cost = 0
            status = CostStatus.KNOWN
            breakdown = RouteCostBreakdown()
        if call_index in self._unknown_cost_calls:
            cost = None
            status = CostStatus.UNKNOWN
            breakdown = None
        route = RouteOption(
            id=f"{self.name.lower()}-{call_index}",
            mode=self._mode,
            duration_seconds={
                TravelMode.TRANSIT: 1_200,
                TravelMode.CAR: 900,
                TravelMode.WALK: 4_800,
            }[self._mode]
            + 100 * (call_index - 1),
            distance_meters={
                TravelMode.TRANSIT: 4_500,
                TravelMode.CAR: 4_000,
                TravelMode.WALK: 3_600,
            }[self._mode],
            mobility_cost_krw=cost,
            cost_status=status,
            cost_breakdown=breakdown,
            geometry=(query.origin, query.destination),
            source=self.name,
            source_as_of=datetime(2026, 8, 10, 9, 0, tzinfo=SEOUL),
        )
        return ProviderResult(provider=self.name, routes=(route,))


class FakeClassificationProvider:
    name = "FAKE_CLASSIFICATION"
    supported_modes = frozenset({TravelMode.CAR})

    def __init__(self, site_coordinate: Coordinate) -> None:
        self.site_coordinate = site_coordinate
        self.destination_coordinate: Coordinate | None = None
        self.queries: list[RouteQuery] = []
        self._distances = [1_500, 1_500]
        self._missing_calls: set[int] = set()

    def set_directional_distances(self, outbound: int, returning: int) -> None:
        self._distances = [outbound, returning]

    def return_no_route_on_call(self, call_number: int) -> None:
        self._missing_calls.add(call_number)

    async def get_routes(self, query: RouteQuery) -> ProviderResult:
        self.queries.append(query)
        if self.destination_coordinate is None:
            self.destination_coordinate = query.destination
        if len(self.queries) in self._missing_calls:
            return ProviderResult(provider=self.name, routes=())
        distance = self._distances.pop(0) if self._distances else 1_500
        route = RouteOption(
            id=f"classification-{len(self.queries)}",
            mode=TravelMode.CAR,
            duration_seconds=600,
            distance_meters=distance,
            mobility_cost_krw=None,
            cost_status=CostStatus.UNKNOWN,
            cost_breakdown=None,
            geometry=(query.origin, query.destination),
            source=self.name,
            source_as_of=datetime(2026, 8, 10, 9, 0, tzinfo=SEOUL),
        )
        return ProviderResult(provider=self.name, routes=(route,))


class FakePlaceClient:
    def __init__(self) -> None:
        self.search_calls = 0
        self.reverse_calls = 0

    async def search(
        self,
        query: str,
        *,
        bounds: BoundingBox,
    ) -> PlaceSearchResult:
        self.search_calls += 1
        return PlaceSearchResult(
            candidates=(
                PlaceCandidate(
                    place_id="fixture-city-hall",
                    name="서울특별시청",
                    road_address="서울특별시 중구 세종대로 110",
                    lot_address="서울특별시 중구 태평로1가 31",
                    latitude=37.5662952,
                    longitude=126.9779451,
                ),
            ),
            warnings=(),
        )

    async def reverse_geocode(self, coordinate: Coordinate) -> ReversePlaceResult:
        self.reverse_calls += 1
        return ReversePlaceResult(
            candidate=PlaceCandidate(
                place_id="fixture-reverse",
                name="서울특별시청",
                road_address="서울특별시 중구 세종대로 110",
                lot_address="서울특별시 중구 태평로1가 31",
                latitude=coordinate.latitude,
                longitude=coordinate.longitude,
            ),
            warnings=(),
        )

    async def aclose(self) -> None:
        return None


@pytest.fixture
def fake_provider() -> FakeRouteProvider:
    return FakeRouteProvider("FAKE_TRANSIT", TravelMode.TRANSIT)


@pytest.fixture
def fake_classification_provider() -> FakeClassificationProvider:
    return FakeClassificationProvider(Coordinate(latitude=37.5501, longitude=126.9801))


@pytest.fixture
def fake_place_client() -> FakePlaceClient:
    return FakePlaceClient()


@pytest.fixture
def fake_route_providers(
    fake_provider: FakeRouteProvider,
) -> dict[TravelMode, FakeRouteProvider]:
    return {
        TravelMode.TRANSIT: fake_provider,
        TravelMode.CAR: FakeRouteProvider("FAKE_CAR", TravelMode.CAR),
        TravelMode.WALK: FakeRouteProvider("FAKE_WALK", TravelMode.WALK),
    }


@pytest.fixture
def cache_clock() -> list[float]:
    return [0.0]


@pytest.fixture
def settings() -> Settings:
    return Settings(
        environment="test",
        kakao_javascript_key=SecretStr("public-js-key"),
        kakao_rest_api_key=SecretStr("rest-secret"),
        seoul_transit_service_key=SecretStr("seoul-secret"),
        opinet_cert_key=SecretStr("opinet-secret"),
        allowed_hosts=("testserver",),
        allowed_origins=("https://travel.example.test",),
    )


@pytest.fixture
def client(
    settings: Settings,
    fake_route_providers: dict[TravelMode, FakeRouteProvider],
    fake_classification_provider: FakeClassificationProvider,
    fake_place_client: FakePlaceClient,
    cache_clock: list[float],
) -> Iterator[TestClient]:
    store = InstitutionStore.load(FIXTURES / "institutions/snapshot")
    coverage = CoverageService.from_geojson(
        seoul_path=FIXTURES / "geodata/seoul-square.geojson",
        buffer_distance_m=12_000,
    )
    dependencies = AppDependencies(
        settings=settings,
        institutions=store,
        coverage=coverage,
        policy=PolicyEngine(
            RuleRepository.from_directory("apps/travel-map/resources/rules")
        ),
        route_orchestrator=RouteOrchestrator(
            {mode: (provider,) for mode, provider in fake_route_providers.items()},
            max_concurrency=4,
        ),
        classification_provider=fake_classification_provider,
        place_client=fake_place_client,
        cache=TtlLruCache(max_entries=100, now=lambda: cache_clock[0]),
        rate_limiter=FixedWindowRateLimiter(
            limits={"places": (10, 60.0), "preview": (20, 60.0)}
        ),
        seoul_geojson=(FIXTURES / "geodata/seoul-square.geojson").read_bytes(),
        support_geojson=(FIXTURES / "geodata/seoul-square.geojson").read_bytes(),
    )
    with TestClient(create_app(settings, dependencies)) as test_client:
        yield test_client


def trip_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "originSiteId": "test-neis:B10:SEMWATER-ES:main",
        "destination": {
            "name": "서울특별시청",
            "address": "서울특별시 중구 세종대로 110",
            "latitude": 37.5662952,
            "longitude": 126.9779451,
        },
        "startsAt": "2026-08-10T09:00:00+09:00",
        "endsAt": "2026-08-10T13:00:00+09:00",
        "tripPattern": "ROUND_TRIP",
        "vehicleUse": "NONE",
        "carAssumptions": {
            "fuelType": "GASOLINE",
            "efficiencyKmPerLiter": 10.0,
            "parkingCostKrw": 0,
        },
        "hasOtherLocalTripsToday": False,
        "previousAllowanceKrw": 0,
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
