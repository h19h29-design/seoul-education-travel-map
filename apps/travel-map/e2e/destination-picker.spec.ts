import { expect, test } from "@playwright/test";
import {
  fulfillJson,
  installMockApi,
  mapState,
  readFixture,
  triggerMapClick,
} from "./helpers";

type Place = {
  placeId: string;
  name: string;
  roadAddress: string | null;
  lotAddress: string | null;
  latitude: number;
  longitude: number;
};

type PlacesResponse = { items: Place[]; warnings: string[] };

const places = () => readFixture<PlacesResponse>("places.json");

async function selectOrigin(page: import("@playwright/test").Page): Promise<void> {
  await page.getByLabel("출발 기관").focus();
  await page
    .getByRole("option", { name: /샘물공립초등학교.*공립.*중구/ })
    .click();
}

async function selectCityHall(page: import("@playwright/test").Page): Promise<void> {
  await page.getByLabel("출장지").fill("서울시청");
  await page
    .getByRole("option", { name: /서울특별시청.*세종대로 110/ })
    .click();
}

function activeMarkerIds(state: Awaited<ReturnType<typeof mapState>>): number[] {
  const removed = new Set(state.events
    .filter(({ kind, map, type }) =>
      kind === "MarkerFake" && map === null && type === "setMap")
    .map(({ id }) => id));
  return state.created
    .filter(({ id, kind }) => kind === "MarkerFake" && !removed.has(id))
    .map(({ id }) => id);
}

test("road_or_lot_address_can_be_selected_as_destination", async ({ page }) => {
  const fixture = places();
  const unsafe: Place = {
    ...fixture.items[0],
    placeId: "fixture-inert-text",
    name: '<img src=x onerror="window.__task5Xss=\'executed\'">',
    roadAddress: "서울특별시 종로구 새문안로 1",
    lotAddress: null,
  };
  await installMockApi(page, {
    places: { items: [...fixture.items, unsafe], warnings: [] },
  });
  await page.goto("/");

  await page.getByLabel("출장지").fill("서울시청");
  await expect(page.locator("#destination-selection")).toContainText("총 3개");
  const roadAndLot = page.getByRole("option", { name: /서울특별시청/ });
  await expect(roadAndLot).toContainText("서울특별시 중구 세종대로 110");
  await expect(roadAndLot).toContainText("서울특별시 중구 태평로1가 31");
  await roadAndLot.click();
  await expect(page.locator("#destination-selection")).toContainText("세종대로 110");

  await page.getByLabel("출장지").fill("동자동");
  const lotOnly = page.getByRole("option", { name: /서울역 지번 안내소.*동자동 43-205/ });
  await expect(lotOnly).toBeVisible();
  await expect(lotOnly).not.toContainText("undefined");
  await lotOnly.click();
  await expect(page.locator("#destination-selection")).toContainText("동자동 43-205");
  await expect(page.locator("#destination-selection")).not.toContainText("undefined");

  await page.getByLabel("출장지").fill("새문안");
  await expect(page.locator("#destination-results img")).toHaveCount(0);
  await expect(page.getByRole("option", { name: /<img src=x/ })).toBeVisible();
  await expect.poll(() => page.evaluate(() =>
    (window as typeof window & { __task5Xss?: string }).__task5Xss,
  )).toBeUndefined();
});

test("destination_selection_shows_one_replaceable_marker_and_pans_map", async ({
  page,
}) => {
  await installMockApi(page);
  await page.goto("/");
  await selectOrigin(page);
  await selectCityHall(page);

  let state = await mapState(page);
  const cityMarker = state.created.find(({ kind, options }) =>
    kind === "MarkerFake" && options.title === "서울특별시청");
  expect(cityMarker).toBeDefined();
  expect(state.events.findLast(({ type }) => type === "panTo")?.point).toEqual({
    latitude: 37.5662952,
    longitude: 126.9779451,
  });

  await page.getByLabel("출장지").fill("동자동");
  await page
    .getByRole("option", { name: /서울역 지번 안내소.*동자동 43-205/ })
    .click();
  state = await mapState(page);
  expect(state.events.some(({ id, map, type }) =>
    id === cityMarker?.id && map === null && type === "setMap")).toBe(true);
  expect(activeMarkerIds(state)).toHaveLength(2);
  expect(state.events.findLast(({ type }) => type === "panTo")?.point).toEqual({
    latitude: 37.554648,
    longitude: 126.970708,
  });

  const markerCountBeforePreview = state.created.filter(({ kind }) => kind === "MarkerFake").length;
  const activeCandidatesBeforePreview = activeMarkerIds(state);
  await page.getByRole("button", { name: "경로 계산" }).click();
  await expect(page.getByRole("heading", { name: "추천 경로" })).toBeVisible();
  state = await mapState(page);
  expect(state.created.filter(({ kind }) => kind === "MarkerFake")).toHaveLength(
    markerCountBeforePreview,
  );
  expect(activeMarkerIds(state)).toEqual(activeCandidatesBeforePreview);
  await expect(page.locator("#map")).toHaveAttribute(
    "data-active-routes",
    "OUTBOUND:car-1 RETURN:car-1",
  );
});

test("editing_selected_destination_invalidates_selection_and_marker", async ({
  page,
}) => {
  await page.addInitScript(() => {
    Object.assign(window, { __task5AbortCalls: 0 });
    AbortController.prototype.abort = function abortWithoutCancelling() {
      const testWindow = window as typeof window & { __task5AbortCalls: number };
      testWindow.__task5AbortCalls += 1;
    };
  });
  await installMockApi(page);
  const fixture = places();
  const slow: Place = { ...fixture.items[0], placeId: "slow", name: "느린 주소" };
  const fast: Place = { ...fixture.items[0], placeId: "fast", name: "최신 주소" };
  let releaseSlow: (() => void) | undefined;
  let errorAttempts = 0;
  await page.route("**/api/v1/places**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname !== "/api/v1/places") return route.fallback();
    const query = url.searchParams.get("q");
    if (query === "느린주소") {
      await new Promise<void>((resolve) => { releaseSlow = resolve; });
      return fulfillJson(route, { items: [slow], warnings: [] });
    }
    if (query === "빠른주소") {
      return fulfillJson(route, { items: [fast], warnings: [] });
    }
    if (query === "없음") return fulfillJson(route, { items: [], warnings: [] });
    if (query === "오류") {
      errorAttempts += 1;
      return errorAttempts === 1
        ? fulfillJson(route, { error: { code: "PLACE_PROVIDER_UNAVAILABLE" } }, 503)
        : fulfillJson(route, { items: [fast], warnings: [] });
    }
    return route.fallback();
  });
  await page.goto("/");
  await selectOrigin(page);
  await selectCityHall(page);
  const selectedState = await mapState(page);
  const destinationMarker = selectedState.created.find(({ kind, options }) =>
    kind === "MarkerFake" && options.title === "서울특별시청");

  await page.getByLabel("출장지").fill("서");
  await expect(page.getByRole("button", { name: "경로 계산" })).toBeDisabled();
  await expect(page.locator("#destination-selection")).toContainText("2자 이상");
  await expect.poll(async () =>
    (await mapState(page)).events.some(({ id, map, type }) =>
      id === destinationMarker?.id && map === null && type === "setMap"),
  ).toBe(true);

  const slowRequest = page.waitForRequest((request) =>
    new URL(request.url()).searchParams.get("q") === "느린주소");
  await page.getByLabel("출장지").fill("느린주소");
  await slowRequest;
  await expect(page.locator("#destination-selection")).toContainText("검색하고 있습니다");
  await page.getByLabel("출장지").fill("빠른주소");
  await expect(page.getByRole("option", { name: /최신 주소/ })).toBeVisible();
  releaseSlow?.();
  await expect(page.getByRole("option", { name: /느린 주소/ })).toHaveCount(0);
  await expect(page.locator("#destination-selection")).toContainText("총 1개");

  await page.getByLabel("출장지").fill("없음");
  await expect(page.locator("#destination-selection")).toContainText("0개");
  await page.getByLabel("출장지").fill("오류");
  await expect(page.locator("#destination-selection")).toContainText("불러오지 못했습니다");
  const retry = page.getByRole("button", { name: "출장지 검색 다시 시도" });
  await expect(retry).toBeVisible();
  expect(await retry.evaluate((node) => node.closest('[role="listbox"]'))).toBeNull();
  await retry.click();
  await expect(page.getByRole("option", { name: /최신 주소/ })).toBeVisible();
  expect(await page.evaluate(() =>
    (window as typeof window & { __task5AbortCalls: number }).__task5AbortCalls,
  )).toBeGreaterThan(0);
});

test("new_destination_query_cannot_select_stale_options", async ({ page }) => {
  await installMockApi(page);
  const fixture = places();
  const previous: Place = {
    ...fixture.items[0],
    placeId: "previous-place",
    name: "이전 출장지",
  };
  const current: Place = {
    ...fixture.items[0],
    placeId: "current-place",
    name: "새 출장지",
    latitude: 37.57,
    longitude: 126.98,
  };
  let releaseCurrent: (() => void) | undefined;
  await page.route("**/api/v1/places**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname !== "/api/v1/places") return route.fallback();
    if (url.searchParams.get("q") === "이전주소검색") {
      return fulfillJson(route, { items: [previous], warnings: [] });
    }
    if (url.searchParams.get("q") === "새주소검색") {
      await new Promise<void>((resolve) => { releaseCurrent = resolve; });
      return fulfillJson(route, { items: [current], warnings: [] });
    }
    return route.fallback();
  });
  await page.goto("/");
  await selectOrigin(page);
  const input = page.getByLabel("출장지");
  await input.fill("이전주소검색");
  await expect(page.getByRole("option", { name: /이전 출장지/ })).toBeVisible();

  const currentRequest = page.waitForRequest((request) => {
    const url = new URL(request.url());
    return url.pathname === "/api/v1/places"
      && url.searchParams.get("q") === "새주소검색";
  });
  await input.fill("새주소검색");
  await currentRequest;
  await expect.poll(() => Boolean(releaseCurrent)).toBe(true);
  await input.press("ArrowDown");
  await input.press("Enter");

  await expect(input).toHaveValue("새주소검색");
  await expect(input).not.toHaveAttribute("aria-activedescendant", /.+/);
  await expect(page.getByRole("button", { name: "경로 계산" })).toBeDisabled();
  expect((await mapState(page)).created.some(({ kind, options }) =>
    kind === "MarkerFake" && options.title === "이전 출장지")).toBe(false);

  releaseCurrent?.();
  await expect(page.getByRole("option", { name: /새 출장지/ })).toBeVisible();
  await expect(page.getByRole("button", { name: "경로 계산" })).toBeDisabled();
  await input.press("ArrowDown");
  await input.press("Enter");
  await expect(input).toHaveValue("새 출장지");
  await expect(page.getByRole("button", { name: "경로 계산" })).toBeEnabled();
});

test("map_click_requires_reverse_candidate_confirmation", async ({ page }) => {
  await page.addInitScript(() => {
    Object.assign(window, { __task5AbortCalls: 0 });
    AbortController.prototype.abort = function abortWithoutCancelling() {
      const testWindow = window as typeof window & { __task5AbortCalls: number };
      testWindow.__task5AbortCalls += 1;
    };
  });
  await installMockApi(page);
  const fixture = places();
  const first: Place = {
    ...fixture.items[0],
    placeId: "reverse-first",
    name: "첫 번째 지도 후보",
    latitude: 37.51,
    longitude: 126.91,
  };
  const second: Place = {
    ...fixture.items[0],
    placeId: "reverse-second",
    name: "두 번째 지도 후보",
    latitude: 37.52,
    longitude: 126.92,
  };
  let releaseFirst: (() => void) | undefined;
  let releaseSecond: (() => void) | undefined;
  await page.route("**/api/v1/places/reverse**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname !== "/api/v1/places/reverse") return route.fallback();
    if (url.searchParams.get("latitude") === "37.51") {
      await new Promise<void>((resolve) => { releaseFirst = resolve; });
      return fulfillJson(route, { item: first, warnings: [] });
    }
    if (url.searchParams.get("latitude") === "37.52") {
      await new Promise<void>((resolve) => { releaseSecond = resolve; });
      return fulfillJson(route, { item: second, warnings: [] });
    }
    return route.fallback();
  });
  await page.goto("/");
  await selectOrigin(page);
  await selectCityHall(page);

  const firstRequest = page.waitForRequest((request) => {
    const url = new URL(request.url());
    return url.pathname === "/api/v1/places/reverse"
      && url.searchParams.get("latitude") === "37.51";
  });
  const firstClick = triggerMapClick(page, 37.51, 126.91);
  await firstRequest;
  await expect.poll(() => Boolean(releaseFirst)).toBe(true);
  await expect(page.getByRole("button", { name: "경로 계산" })).toBeDisabled();
  await expect(page.locator("#destination-selection")).toContainText("주소를 확인하고 있습니다");

  const secondRequest = page.waitForRequest((request) => {
    const url = new URL(request.url());
    return url.pathname === "/api/v1/places/reverse"
      && url.searchParams.get("latitude") === "37.52";
  });
  const secondClick = triggerMapClick(page, 37.52, 126.92);
  await secondRequest;
  await expect.poll(() => Boolean(releaseSecond)).toBe(true);
  releaseSecond?.();
  await secondClick;
  await expect(page.getByRole("option", { name: /두 번째 지도 후보/ })).toBeVisible();
  await expect(page.getByRole("button", { name: "경로 계산" })).toBeDisabled();
  releaseFirst?.();
  await firstClick;
  await expect(page.getByRole("option", { name: /두 번째 지도 후보/ })).toBeVisible();
  await expect(page.getByRole("option", { name: /첫 번째 지도 후보/ })).toHaveCount(0);

  await page.getByRole("option", { name: /두 번째 지도 후보/ }).click();
  await expect(page.getByRole("button", { name: "경로 계산" })).toBeEnabled();
  expect(await page.evaluate(() =>
    (window as typeof window & { __task5AbortCalls: number }).__task5AbortCalls,
  )).toBeGreaterThan(0);
});

test("keyboard_arrows_enter_and_escape_control_both_listboxes", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  let previewRequests = 0;
  page.on("request", (request) => {
    if (request.method() === "POST"
      && new URL(request.url()).pathname === "/api/v1/trips/preview") {
      previewRequests += 1;
    }
  });
  await installMockApi(page);
  await page.goto("/");

  const origin = page.getByLabel("출발 기관");
  await origin.focus();
  await expect(origin).toHaveAttribute("aria-controls", "origin-results");
  await expect(origin).toHaveAttribute("aria-expanded", "true");
  await origin.press("ArrowDown");
  const originActiveId = await origin.getAttribute("aria-activedescendant");
  expect(originActiveId).toBeTruthy();
  await expect(page.locator(`#${originActiveId}`)).toHaveAttribute("aria-selected", "true");
  await origin.press("Enter");
  await expect(origin).toHaveValue("샘물공립초등학교");
  await expect(page.locator("#origin-selection")).toContainText("선택했습니다");

  const destination = page.getByLabel("출장지");
  await destination.fill("서울시청");
  await expect(destination).toHaveAttribute("aria-controls", "destination-results");
  await expect(destination).toHaveAttribute("aria-expanded", "true");
  await destination.press("ArrowDown");
  const destinationActiveId = await destination.getAttribute("aria-activedescendant");
  expect(destinationActiveId).toBeTruthy();
  await expect(page.locator(`#${destinationActiveId}`)).toHaveAttribute("aria-selected", "true");
  await destination.press("Escape");
  await expect(destination).toHaveAttribute("aria-expanded", "false");
  expect(previewRequests).toBe(0);

  await destination.fill("서울시청");
  await destination.press("ArrowDown");
  await destination.press("Enter");
  await expect(destination).toHaveValue("서울특별시청");
  await expect(page.locator("#destination-selection")).toContainText("선택했습니다");
  await expect(page.getByRole("button", { name: "경로 계산" })).toBeEnabled();
  expect(previewRequests).toBe(0);
  expect(await page.locator("html").evaluate((node) =>
    node.scrollWidth <= window.innerWidth,
  )).toBe(true);
});
