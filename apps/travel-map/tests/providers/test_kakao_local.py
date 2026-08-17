import asyncio
import copy
import math

import httpx
import pytest
from app.institutions.sources.common import SourceDataError
from app.providers.kakao_local import (
    BoundingBox,
    KakaoLocalClient,
    PlaceCandidate,
    _parse_address_places,
)
from app.routing.models import Coordinate
from pydantic import SecretStr
from tests.providers.helpers import load_json


class _ChunkedBody(httpx.AsyncByteStream):
    async def __aiter__(self):  # type: ignore[no-untyped-def]
        yield b"x" * 101

    async def aclose(self) -> None:
        return None


def kakao_address_document(
    address: str,
    *,
    x: str = "126.968",
    y: str = "37.571",
) -> dict[str, object]:
    return {
        "x": x,
        "y": y,
        "road_address": {"address_name": address},
    }


def test_place_candidate_and_bounds_reject_wrong_types_nonfinite_and_order() -> None:
    with pytest.raises(TypeError):
        BoundingBox(west=True, south=37.4, east=127.3, north=37.75)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        BoundingBox(west=126.7, south=37.4, east=126.7, north=37.75)
    with pytest.raises(ValueError):
        BoundingBox(west=126.7, south=float("nan"), east=127.3, north=37.75)
    with pytest.raises(TypeError):
        PlaceCandidate("id", "name", "road", "lot", 37, 127.0)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "documents",
    (
        [],
        [kakao_address_document("서울 종로구 송월길 49")],
        [
            kakao_address_document("서울 종로구 송월길 48"),
            kakao_address_document("서울특별시 종로구 송월길 48"),
        ],
        [{"x": "126.968", "y": "37.571", "road_address": None}],
    ),
)
@pytest.mark.asyncio
async def test_kakao_geocoder_rejects_nonexact_or_ambiguous_alias_results(
    documents: list[object],
) -> None:
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json={"documents": documents})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = KakaoLocalClient(api_key="test-key", client=http)
        result = await client.geocode("서울특별시 종로구 송월길 48")

    assert result is None
    assert requests == 1


@pytest.mark.parametrize(
    ("x", "y"),
    (
        ("nan", "37.571"),
        ("126.968", "inf"),
        ("181", "37.571"),
        ("126.968", "91"),
    ),
)
@pytest.mark.asyncio
async def test_kakao_geocoder_rejects_invalid_coordinate(
    x: str,
    y: str,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "documents": [
                    kakao_address_document(
                        "서울 종로구 송월길 48",
                        x=x,
                        y=y,
                    )
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = KakaoLocalClient(api_key="test-key", client=http)
        with pytest.raises(
            SourceDataError,
            match="Kakao Local coordinates are invalid",
        ):
            await client.geocode("서울특별시 종로구 송월길 48")


@pytest.mark.asyncio
async def test_place_search_uses_rect_encoding_limits_results_and_keeps_key_off_url() -> (
    None
):
    secret = "local-header-secret"
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers["Authorization"] == f"KakaoAK {secret}"
        assert request.url.params["query"] == "서울 시청/도서관"
        assert request.url.params["rect"] == "126.7,37.4,127.3,37.75"
        assert request.url.params["page"] == "1"
        assert request.url.params["size"] == "15"
        assert secret not in str(request.url)
        payload = (
            load_json("kakao-keyword.json")
            if request.url.path.endswith("keyword.json")
            else {"meta": {"total_count": 0}, "documents": []}
        )
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json;charset=UTF-8"},
            json=payload,
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = KakaoLocalClient(http=http, rest_key=SecretStr(secret))
    bounds = BoundingBox(west=126.7, south=37.4, east=127.3, north=37.75)

    result = await client.search("서울 시청/도서관", bounds=bounds)
    await client.aclose()

    assert [place.place_id for place in result.candidates] == [
        "kakao-place-2",
        "kakao-place-1",
    ]
    assert result.candidates[1].latitude == 37.56661
    assert len(result.candidates) <= 15
    assert len(seen) == 2
    assert not http.is_closed
    await http.aclose()


@pytest.mark.asyncio
async def test_place_search_rejects_blank_short_and_long_without_request() -> None:
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = KakaoLocalClient(http=http, rest_key=SecretStr("test-key"))
        bounds = BoundingBox(west=126.7, south=37.4, east=127.3, north=37.75)
        assert (await client.search("   ", bounds=bounds)).candidates == ()
        assert (await client.search("가", bounds=bounds)).candidates == ()
        assert (await client.search("가" * 81, bounds=bounds)).candidates == ()

    assert requests == 0


@pytest.mark.asyncio
async def test_place_search_excludes_out_of_rect_and_duplicate_ids() -> None:
    payload = load_json("kakao-keyword.json")
    duplicate = copy.deepcopy(payload["documents"][1])  # type: ignore[index]
    duplicate["id"] = "kakao-place-1"
    outside = copy.deepcopy(payload["documents"][0])  # type: ignore[index]
    outside.update({"id": "busan-place", "x": "129.0756", "y": "35.1796"})
    payload["documents"] = [payload["documents"][0], duplicate, outside]  # type: ignore[index]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("address.json"):
            return httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                json={"meta": {"total_count": 0}, "documents": []},
            )
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            json=payload,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = KakaoLocalClient(http=http, rest_key=SecretStr("key"))
        result = await client.search(
            "서울시청",
            bounds=BoundingBox(126.7, 37.4, 127.3, 37.75),
        )

    assert [place.place_id for place in result.candidates] == ["kakao-place-1"]
    assert result.warnings == ("DUPLICATE_PLACE_ID", "OUT_OF_BOUNDS_RESULT")


@pytest.mark.asyncio
async def test_reverse_geocode_prefers_road_address() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["x"] == "126.9779"
        assert request.url.params["y"] == "37.5663"
        assert request.url.params["input_coord"] == "WGS84"
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            json=load_json("kakao-coord2address.json"),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = KakaoLocalClient(http=http, rest_key=SecretStr("test-key"))
        result = await client.reverse_geocode(Coordinate(37.5663, 126.9779))

    assert result.candidate is not None
    assert result.candidate.name == "서울특별시청"
    assert result.candidate.road_address == "서울 중구 세종대로 110"
    assert result.candidate.lot_address == "서울 중구 태평로1가 31"


@pytest.mark.asyncio
async def test_local_http_errors_are_bounded_deterministic_and_secret_free() -> None:
    secret = "never-leak-local"
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            429,
            headers={"Content-Type": "application/json"},
            json={"secretEcho": secret},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = KakaoLocalClient(http=http, rest_key=SecretStr(secret))
        result = await client.search(
            "서울시청",
            bounds=BoundingBox(126.7, 37.4, 127.3, 37.75),
        )

    assert result.candidates == ()
    assert result.warnings == ("PLACE_PROVIDER_UNAVAILABLE",)
    assert requests == 4
    assert secret not in repr(client)
    assert secret not in repr(result.warnings)


@pytest.mark.asyncio
async def test_local_rejects_wrong_content_type_and_oversized_body() -> None:
    responses = iter(
        (
            httpx.Response(200, headers={"Content-Type": "text/html"}, text="{}"),
            httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                content=b"x" * 101,
            ),
        )
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return next(responses)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = KakaoLocalClient(
            http=http,
            rest_key=SecretStr("test-key"),
            max_response_bytes=100,
        )
        bounds = BoundingBox(126.7, 37.4, 127.3, 37.75)
        assert (await client.search("서울시청", bounds=bounds)).candidates == ()
        assert (await client.search("서울도서관", bounds=bounds)).candidates == ()


@pytest.mark.asyncio
async def test_streaming_size_failure_is_not_retried_or_reclassified() -> None:
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            stream=_ChunkedBody(),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = KakaoLocalClient(
            http=http,
            rest_key=SecretStr("test-key"),
            max_response_bytes=100,
        )
        result = await client.search(
            "서울시청",
            bounds=BoundingBox(126.7, 37.4, 127.3, 37.75),
        )

    assert result.candidates == ()
    assert requests == 2


# Break caught: address destination candidates were silently discarded because only
# Kakao's keyword endpoint was queried.
@pytest.mark.asyncio
async def test_place_search_merges_keyword_and_road_address_candidates() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = (
            load_json("kakao-keyword.json")
            if request.url.path.endswith("keyword.json")
            else load_json("kakao-address-search.json")
        )
        return httpx.Response(
            200, headers={"Content-Type": "application/json"}, json=payload
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = KakaoLocalClient(http=http, rest_key=SecretStr("test-key"))
        result = await client.search(
            "서울시청", bounds=BoundingBox(126.7, 37.4, 127.3, 37.75)
        )

    assert [candidate.place_id for candidate in result.candidates] == [
        "kakao-place-1",
        "kakao-place-2",
    ]
    assert result.warnings == ()


# Break caught: a ROAD_ADDR top-level address was mislabelled as a lot address
# instead of preserving its nested lot-address object.
@pytest.mark.asyncio
async def test_place_search_parses_road_and_nested_lot_addresses() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = {"meta": {"total_count": 0}, "documents": []}
        if request.url.path.endswith("/search/address.json"):
            payload = load_json("kakao-address-search.json")
        return httpx.Response(
            200, headers={"Content-Type": "application/json"}, json=payload
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = KakaoLocalClient(http=http, rest_key=SecretStr("test-key"))
        result = await client.search(
            "세종대로 110", bounds=BoundingBox(126.7, 37.4, 127.3, 37.75)
        )

    candidate = result.candidates[0]
    assert candidate.road_address == "서울 중구 세종대로 110"
    assert candidate.lot_address == "서울 중구 태평로1가 31"


# Break caught: address-only lot results could not be selected as anonymous destinations.
@pytest.mark.asyncio
async def test_place_search_returns_lot_address_only_candidate() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = {"meta": {"total_count": 1}, "documents": []}
        if request.url.path.endswith("/search/address.json"):
            payload = {
                "meta": {"total_count": 1},
                "documents": [load_json("kakao-address-search.json")["documents"][1]],
            }
        return httpx.Response(
            200, headers={"Content-Type": "application/json"}, json=payload
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = KakaoLocalClient(http=http, rest_key=SecretStr("test-key"))
        result = await client.search(
            "태평로1가 31", bounds=BoundingBox(126.7, 37.4, 127.3, 37.75)
        )

    candidate = result.candidates[0]
    assert candidate.name == "서울 중구 태평로1가 31"
    assert candidate.road_address == ""
    assert candidate.lot_address == "서울 중구 태평로1가 31"
    assert candidate.place_id.startswith("address:")


# Break caught: decimal rendering collapsed adjacent representable coordinates into
# the same anonymous-address identifier.
def test_address_candidate_ids_distinguish_adjacent_float_coordinates() -> None:
    first_longitude = 126.9779
    adjacent_longitude = math.nextafter(first_longitude, math.inf)
    payload = {
        "meta": {"total_count": 1},
        "documents": [
            {
                "address_name": "서울 중구 태평로1가 31",
                "address_type": "REGION_ADDR",
                "road_address": None,
                "x": repr(first_longitude),
                "y": "37.5663",
            }
        ],
    }
    adjacent_payload = copy.deepcopy(payload)
    adjacent_payload["documents"][0]["x"] = repr(adjacent_longitude)  # type: ignore[index]
    bounds = BoundingBox(126.7, 37.4, 127.3, 37.75)

    first = _parse_address_places(payload, bounds)[0][0]
    adjacent = _parse_address_places(adjacent_payload, bounds)[0][0]

    assert first.place_id != adjacent.place_id


# Break caught: coordinate/address duplicates could retain a less useful address-only candidate.
@pytest.mark.asyncio
async def test_place_search_deduplicates_address_candidate_in_favor_of_named_place() -> (
    None
):
    def handler(request: httpx.Request) -> httpx.Response:
        payload = (
            load_json("kakao-keyword.json")
            if request.url.path.endswith("keyword.json")
            else {
                "meta": {"total_count": 1},
                "documents": [load_json("kakao-address-search.json")["documents"][0]],
            }
        )
        return httpx.Response(
            200, headers={"Content-Type": "application/json"}, json=payload
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = KakaoLocalClient(http=http, rest_key=SecretStr("test-key"))
        result = await client.search(
            "서울시청", bounds=BoundingBox(126.7, 37.4, 127.3, 37.75)
        )

    assert result.candidates[0].place_id == "kakao-place-1"
    assert len(result.candidates) == 2


# Break caught: one endpoint failure discarded successful candidates from the other endpoint.
@pytest.mark.asyncio
async def test_place_search_keeps_address_results_when_keyword_endpoint_fails() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("keyword.json"):
            return httpx.Response(500)
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            json=load_json("kakao-address-search.json"),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = KakaoLocalClient(http=http, rest_key=SecretStr("test-key"))
        result = await client.search(
            "서울시청", bounds=BoundingBox(126.7, 37.4, 127.3, 37.75)
        )

    assert [candidate.lot_address for candidate in result.candidates]
    assert result.warnings == ("KEYWORD_SEARCH_UNAVAILABLE",)


# Break caught: a partial failure was incorrectly surfaced as a total provider outage.
@pytest.mark.asyncio
async def test_place_search_reports_unavailable_only_when_both_endpoints_fail() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = KakaoLocalClient(http=http, rest_key=SecretStr("test-key"))
        result = await client.search(
            "서울시청", bounds=BoundingBox(126.7, 37.4, 127.3, 37.75)
        )

    assert result.candidates == ()
    assert result.warnings == ("PLACE_PROVIDER_UNAVAILABLE",)


# Break caught: the mutable warning field let an interleaved request replace another result's status.
@pytest.mark.asyncio
async def test_interleaved_search_and_reverse_keep_their_own_warnings() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("keyword.json"):
            entered.set()
            await release.wait()
            return httpx.Response(500)
        if request.url.path.endswith("/search/address.json"):
            return httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                json={"meta": {}, "documents": []},
            )
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = KakaoLocalClient(http=http, rest_key=SecretStr("test-key"))
        search_task = asyncio.create_task(
            client.search("서울시청", bounds=BoundingBox(126.7, 37.4, 127.3, 37.75))
        )
        await entered.wait()
        reverse = await client.reverse_geocode(Coordinate(37.5663, 126.9779))
        release.set()
        search = await search_task

    assert reverse.candidate is None
    assert reverse.warnings == ("UPSTREAM_UNAVAILABLE",)
    assert search.warnings == ("KEYWORD_SEARCH_UNAVAILABLE",)


# Break caught: a task implementation that serializes endpoint requests delays the
# second endpoint until the first response releases.
@pytest.mark.asyncio
async def test_place_search_starts_keyword_and_address_before_release() -> None:
    keyword_entered = asyncio.Event()
    address_entered = asyncio.Event()
    release = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("keyword.json"):
            keyword_entered.set()
            await release.wait()
            payload = load_json("kakao-keyword.json")
        else:
            address_entered.set()
            await release.wait()
            payload = load_json("kakao-address-search.json")
        return httpx.Response(
            200, headers={"Content-Type": "application/json"}, json=payload
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = KakaoLocalClient(http=http, rest_key=SecretStr("test-key"))
        task = asyncio.create_task(
            client.search("서울시청", bounds=BoundingBox(126.7, 37.4, 127.3, 37.75))
        )
        await asyncio.wait_for(keyword_entered.wait(), timeout=0.2)
        await asyncio.wait_for(address_entered.wait(), timeout=0.2)
        release.set()
        result = await task

    assert result.candidates


# Break caught: an unexpected endpoint exception let the sibling authenticated
# request outlive `search()` instead of cancelling and consuming it.
@pytest.mark.asyncio
async def test_place_search_cancels_and_consumes_sibling_on_unexpected_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    address_entered = asyncio.Event()
    address_cancelled = asyncio.Event()
    address_finished = asyncio.Event()
    release = asyncio.Event()
    trace: list[str] = []

    async def unexpected_keyword(_query: str, _bounds: BoundingBox) -> object:
        await address_entered.wait()
        raise RuntimeError("unexpected provider defect")

    async def blocking_address(_query: str, _bounds: BoundingBox) -> object:
        address_entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            address_cancelled.set()
            trace.append("address-cancelled")
            raise
        finally:
            await asyncio.sleep(0)
            address_finished.set()
            trace.append("address-finished")
        return object()

    client = KakaoLocalClient(rest_key=SecretStr("test-key"))
    monkeypatch.setattr(client, "_search_keyword", unexpected_keyword)
    monkeypatch.setattr(client, "_search_address", blocking_address)
    try:
        with pytest.raises(RuntimeError, match="unexpected provider defect"):
            await client.search(
                "서울시청", bounds=BoundingBox(126.7, 37.4, 127.3, 37.75)
            )
        trace.append("caller-caught")
        assert trace == [
            "address-cancelled",
            "address-finished",
            "caller-caught",
        ]
        assert address_cancelled.is_set()
        assert address_finished.is_set()
    finally:
        release.set()
        await asyncio.wait_for(address_finished.wait(), timeout=0.2)
        await client.aclose()


# Break caught: failure creating the second task leaked its unstarted coroutine
# and let the first endpoint task outlive the task-creation exception.
@pytest.mark.asyncio
async def test_place_search_cleans_up_when_second_task_creation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trace: list[str] = []
    original_create_task = asyncio.create_task

    class LifecycleTask(asyncio.Future[object]):
        def __init__(self) -> None:
            super().__init__()
            self._cleanup: asyncio.Task[None] | None = None

        def cancel(self, msg: object | None = None) -> bool:
            del msg
            if self._cleanup is None:
                self._cleanup = original_create_task(self._finish())
            return True

        async def _finish(self) -> None:
            try:
                await asyncio.sleep(0)
            finally:
                trace.append("keyword-finally")
                self.set_result(object())

    first_task = LifecycleTask()
    create_calls = 0

    async def address_body() -> object:
        await asyncio.sleep(0)
        return object()

    address_coroutine = address_body()

    def fail_second_task_creation(_coroutine: object) -> LifecycleTask:
        nonlocal create_calls
        create_calls += 1
        if create_calls == 1:
            return first_task
        raise RuntimeError("task scheduler unavailable")

    def keyword(_query: str, _bounds: BoundingBox) -> object:
        return object()

    def address(_query: str, _bounds: BoundingBox) -> object:
        return address_coroutine

    client = KakaoLocalClient(rest_key=SecretStr("test-key"))
    monkeypatch.setattr(client, "_search_keyword", keyword)
    monkeypatch.setattr(client, "_search_address", address)
    monkeypatch.setattr(asyncio, "create_task", fail_second_task_creation)
    try:
        with pytest.raises(RuntimeError, match="task scheduler unavailable"):
            await client.search(
                "서울시청", bounds=BoundingBox(126.7, 37.4, 127.3, 37.75)
            )
        trace.append("caller-caught")
        assert trace == ["keyword-finally", "caller-caught"]
        assert address_coroutine.cr_frame is None
    finally:
        first_task.cancel()
        await first_task
        address_coroutine.close()
        await client.aclose()


@pytest.mark.asyncio
async def test_owned_local_client_aclose_is_idempotent() -> None:
    client = KakaoLocalClient(rest_key=SecretStr("test-key"))
    owned_http = client._http

    await client.aclose()
    await client.aclose()

    assert owned_http.is_closed
