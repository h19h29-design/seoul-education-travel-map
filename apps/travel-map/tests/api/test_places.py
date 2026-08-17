from app.cache import PLACES_TTL_SECONDS, TtlLruCache
from app.providers.kakao_local import (
    PlaceCandidate,
    PlaceSearchResult,
    ReversePlaceResult,
)
from app.routing.models import Coordinate, TravelMode


# Break caught: repeated equivalent place searches calling the provider instead of cache.
def test_places_search_is_cached(client, fake_place_client) -> None:
    async def search(_query: str, *, bounds: object) -> PlaceSearchResult:
        fake_place_client.search_calls += 1
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

    fake_place_client.search = search
    first = client.get("/api/v1/places", params={"q": "서울시청"})
    second = client.get("/api/v1/places", params={"q": "서울시청"})

    assert first.status_code == second.status_code == 200
    assert first.json()["items"][0]["placeId"] == "fixture-city-hall"
    assert fake_place_client.search_calls == 1


def test_reverse_places_returns_a_public_place_candidate(
    client, fake_place_client
) -> None:
    async def reverse_geocode(coordinate: Coordinate) -> ReversePlaceResult:
        fake_place_client.reverse_calls += 1
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

    fake_place_client.reverse_geocode = reverse_geocode
    response = client.get(
        "/api/v1/places/reverse",
        params={"latitude": 37.5662952, "longitude": 126.9779451},
    )

    assert response.status_code == 200
    assert response.json()["item"]["placeId"] == "fixture-reverse"
    assert fake_place_client.reverse_calls == 1


# Break caught: reverse geocoding repeatedly calls the provider, or merges distinct
# coordinates, instead of using an exact 24-hour cache key.
def test_reverse_places_uses_an_exact_coordinate_cache(
    client, fake_place_client, cache_clock
) -> None:
    async def reverse_geocode(coordinate: Coordinate) -> ReversePlaceResult:
        fake_place_client.reverse_calls += 1
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

    fake_place_client.reverse_geocode = reverse_geocode
    first = client.get(
        "/api/v1/places/reverse",
        params={"latitude": 37.5662952, "longitude": 126.9779451},
    )
    repeated = client.get(
        "/api/v1/places/reverse",
        params={"latitude": 37.5662952, "longitude": 126.9779451},
    )
    nearby = client.get(
        "/api/v1/places/reverse",
        params={"latitude": 37.5662953, "longitude": 126.9779451},
    )

    assert first.json() == repeated.json()
    assert fake_place_client.reverse_calls == 2
    assert nearby.status_code == 200

    cache_clock[0] = PLACES_TTL_SECONDS
    expired = client.get(
        "/api/v1/places/reverse",
        params={"latitude": 37.5662952, "longitude": 126.9779451},
    )
    assert expired.status_code == 200
    assert fake_place_client.reverse_calls == 3


# Break caught: the places cache retained candidates only, so a cached response lost
# the partial-provider warning attached to its original immutable result.
def test_places_search_caches_the_merged_result_and_its_warnings(
    client, fake_place_client
) -> None:
    async def search(_query: str, *, bounds: object) -> PlaceSearchResult:
        fake_place_client.search_calls += 1
        return PlaceSearchResult(
            candidates=(
                PlaceCandidate(
                    place_id="address:fixture",
                    name="서울 중구 태평로1가 31",
                    road_address="",
                    lot_address="서울 중구 태평로1가 31",
                    latitude=37.5663,
                    longitude=126.9779,
                ),
            ),
            warnings=("KEYWORD_SEARCH_UNAVAILABLE",),
        )

    fake_place_client.search = search

    first = client.get("/api/v1/places", params={"q": "태평로1가 31"})
    second = client.get("/api/v1/places", params={"q": "태평로1가 31"})

    assert first.status_code == second.status_code == 200
    assert (
        first.json()["warnings"]
        == second.json()["warnings"]
        == ["KEYWORD_SEARCH_UNAVAILABLE"]
    )
    assert fake_place_client.search_calls == 1


# Break caught: a 503 result was cached, so a recovered provider remained
# unavailable until the places TTL expired.
def test_places_search_does_not_cache_total_provider_outage(
    client, fake_place_client
) -> None:
    async def search(_query: str, *, bounds: object) -> PlaceSearchResult:
        fake_place_client.search_calls += 1
        if fake_place_client.search_calls == 1:
            return PlaceSearchResult(
                candidates=(), warnings=("PLACE_PROVIDER_UNAVAILABLE",)
            )
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

    fake_place_client.search = search

    unavailable = client.get("/api/v1/places", params={"q": "서울시청"})
    recovered = client.get("/api/v1/places", params={"q": "서울시청"})

    assert unavailable.status_code == 503
    assert recovered.status_code == 200
    assert recovered.json()["items"][0]["placeId"] == "fixture-city-hall"
    assert fake_place_client.search_calls == 2


# Break caught: cache keys using raw coordinates and treating requests within 1e-5 apart as distinct.
def test_route_cache_key_quantizes_coordinates_to_five_decimals() -> None:
    cache = TtlLruCache(max_entries=2)
    first = cache.route_key(
        provider="FAKE",
        mode=TravelMode.CAR,
        origin=Coordinate(37.5000001, 126.9000001),
        destination=Coordinate(37.6000001, 127.0000001),
        depart_at="2026-08-10T09:00:00+09:00",
        options={"priority": "DISTANCE"},
    )
    second = cache.route_key(
        provider="FAKE",
        mode=TravelMode.CAR,
        origin=Coordinate(37.5000002, 126.9000002),
        destination=Coordinate(37.6000002, 127.0000002),
        depart_at="2026-08-10T09:00:00+09:00",
        options={"priority": "DISTANCE"},
    )

    assert first == second
