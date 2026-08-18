import { expect, test, type Page, type Route } from "@playwright/test";
import {
  fulfillJson,
  installMockApi,
  mapState,
  readFixture,
} from "./helpers";

type InstitutionItem = {
  institutionId: string;
  siteId: string;
  siteName: string;
  officialName: string;
  displayName: string;
  institutionType: string;
  foundationType: string;
  educationOffice: string;
  roadAddress: string;
  district: string;
  coordinate: { latitude: number; longitude: number };
  coordinateQuality: string;
  snapshotId: string;
  snapshotAsOf: string;
};

type InstitutionPage = {
  items: InstitutionItem[];
  total: number;
  nextOffset: number | null;
  snapshotId: string;
};

const institutions = () => readFixture<InstitutionPage>("institutions.json");

function pageOf(
  items: InstitutionItem[],
  {
    total = items.length,
    nextOffset = null,
    snapshotId = "fixture-001",
  }: Partial<Omit<InstitutionPage, "items">> = {},
): InstitutionPage {
  return { items, total, nextOffset, snapshotId };
}

async function selectFirstOrigin(page: Page): Promise<void> {
  await page.getByLabel("출발 기관").focus();
  await page
    .getByRole("option", { name: /샘물공립초등학교.*공립.*중구/ })
    .click();
}

async function selectFirstDestination(page: Page): Promise<void> {
  await page.getByLabel("출장지").fill("서울시청");
  await page
    .getByRole("option", { name: /서울특별시청.*세종대로 110/ })
    .click();
}

test("filter_only_blank_query_displays_institutions", async ({ page }) => {
  await installMockApi(page);
  await page.goto("/");

  const initialSearch = page.waitForRequest((request) => {
    const url = new URL(request.url());
    return url.pathname === "/api/v1/institutions"
      && url.searchParams.get("q") === ""
      && url.searchParams.get("offset") === "0";
  });
  await page.getByLabel("출발 기관").focus();
  const initialUrl = new URL((await initialSearch).url());

  await expect(
    page.getByRole("option", { name: /샘물공립초등학교/ }),
  ).toBeVisible();
  await expect(page.locator("#origin-selection")).toContainText("총 2개");
  expect(initialUrl.searchParams.get("limit")).toBe("20");

  await page.getByRole("button", { name: "기관 검색 필터 열기" }).click();
  const filteredSearch = page.waitForRequest((request) => {
    const url = new URL(request.url());
    return url.pathname === "/api/v1/institutions"
      && url.searchParams.get("q") === ""
      && url.searchParams.get("institution_type") === "HIGH_SCHOOL";
  });
  await page.getByLabel("기관유형").selectOption("HIGH_SCHOOL");
  await filteredSearch;
  await expect(
    page.getByRole("option", { name: /샘물사립고등학교/ }),
  ).toBeVisible();
  await expect(
    page.getByRole("option", { name: /샘물공립초등학교/ }),
  ).toHaveCount(0);
});

test("facets_populate_every_server_option_and_count", async ({ page }) => {
  const facets = readFixture<{
    institutionTypes: Array<{ value: string; label: string; count: number }>;
    foundationTypes: Array<{ value: string; label: string; count: number }>;
    educationOffices: Array<{ value: string; label: string; count: number }>;
    districts: Array<{ value: string; label: string; count: number }>;
  }>("institution-facets.json");
  await installMockApi(page);
  await page.goto("/");
  await page.getByRole("button", { name: "기관 검색 필터 열기" }).click();

  const controls = [
    ["기관유형", facets.institutionTypes],
    ["설립구분", facets.foundationTypes],
    ["교육지원청", facets.educationOffices],
    ["자치구", facets.districts],
  ] as const;
  for (const [label, options] of controls) {
    const select = page.getByLabel(label);
    await expect(select).toBeEnabled();
    await expect(select.locator("option")).toHaveCount(options.length + 1);
    expect(await select.locator("option").evaluateAll((nodes) =>
      nodes.map((node) => ({
        text: node.textContent,
        value: (node as HTMLOptionElement).value,
      })),
    )).toEqual([
      { text: "전체", value: "" },
      ...options.map((option) => ({
        text: `${option.label} (${option.count})`,
        value: option.value,
      })),
    ]);
  }
});

test("main_site_displays_and_selects_official_name", async ({ page }) => {
  const fixture = institutions();
  const productionShaped = {
    ...fixture.items[0],
    siteName: "main",
    officialName: "서버가 정식 기관명을 교체하지 못함",
    displayName: "승인 표시명",
  };
  await installMockApi(page, {
    institutions: pageOf([productionShaped]),
  });
  await page.goto("/");
  await page.getByLabel("출발 기관").focus();

  const option = page.getByRole("option", { name: /승인 표시명/ });
  await expect(option).toBeVisible();
  await expect(option).not.toContainText("main");
  await expect(option).not.toContainText("서버가 정식 기관명을 교체하지 못함");
  await option.click();
  await expect(page.getByLabel("출발 기관")).toHaveValue("승인 표시명");
});

test("institution_selection_pans_to_the_verified_origin_marker", async ({ page }) => {
  await installMockApi(page);
  await page.goto("/");
  await selectFirstOrigin(page);

  await expect.poll(async () => {
    const state = await mapState(page);
    return state.created.filter(({ kind, options }) =>
      kind === "MarkerFake" && options.title === "샘물공립초등학교").length;
  }).toBe(1);
  const state = await mapState(page);
  const pan = state.events.find(({ type }) => type === "panTo");
  expect(pan?.point).toEqual({ latitude: 37.56341, longitude: 126.98762 });
});

test("editing_selected_institution_clears_origin_marker_and_disables_submit", async ({
  page,
}) => {
  await installMockApi(page);
  await page.goto("/");
  await selectFirstOrigin(page);
  await selectFirstDestination(page);
  await expect(page.getByRole("button", { name: "경로 계산" })).toBeEnabled();

  const selectedState = await mapState(page);
  const originMarker = selectedState.created.find(({ kind, options }) =>
    kind === "MarkerFake" && options.title === "샘물공립초등학교");
  expect(originMarker).toBeDefined();
  await page.getByLabel("출발 기관").fill("수정");
  await expect(page.getByRole("button", { name: "경로 계산" })).toBeDisabled();
  await expect.poll(async () =>
    (await mapState(page)).events.some(({ id, map, type }) =>
      id === originMarker?.id && map === null && type === "setMap"),
  ).toBe(true);

  await page.getByRole("option", { name: /샘물공립초등학교/ }).click();
  await expect(page.getByRole("button", { name: "경로 계산" })).toBeEnabled();
  await page.getByRole("button", { name: "기관 검색 필터 열기" }).click();
  await page.getByLabel("기관유형").selectOption("HIGH_SCHOOL");
  await expect(page.getByRole("button", { name: "경로 계산" })).toBeDisabled();
});

test("new_institution_query_cannot_select_stale_options", async ({ page }) => {
  await installMockApi(page);
  const fixture = institutions();
  const previous: InstitutionItem = {
    ...fixture.items[0],
    institutionId: "test-neis:B10:PREVIOUS",
    siteId: "test-neis:B10:PREVIOUS:main",
    officialName: "이전 출발기관",
    displayName: "이전 출발기관",
  };
  const current: InstitutionItem = {
    ...fixture.items[0],
    institutionId: "test-neis:B10:CURRENT",
    siteId: "test-neis:B10:CURRENT:main",
    officialName: "새 출발기관",
    displayName: "새 출발기관",
  };
  let releaseCurrent: (() => void) | undefined;
  await page.route("**/api/v1/institutions**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname !== "/api/v1/institutions") return route.fallback();
    if (url.searchParams.get("q") === "이전기관검색") {
      return fulfillJson(route, pageOf([previous]));
    }
    if (url.searchParams.get("q") === "새기관검색") {
      await new Promise<void>((resolve) => { releaseCurrent = resolve; });
      return fulfillJson(route, pageOf([current]));
    }
    return route.fallback();
  });
  await page.goto("/");
  await selectFirstDestination(page);
  const input = page.getByLabel("출발 기관");
  await input.fill("이전기관검색");
  await expect(page.getByRole("option", { name: /이전 출발기관/ })).toBeVisible();

  const currentRequest = page.waitForRequest((request) => {
    const url = new URL(request.url());
    return url.pathname === "/api/v1/institutions"
      && url.searchParams.get("q") === "새기관검색";
  });
  await input.fill("새기관검색");
  await currentRequest;
  await expect.poll(() => Boolean(releaseCurrent)).toBe(true);
  await input.press("ArrowDown");
  await input.press("Enter");

  await expect(input).toHaveValue("새기관검색");
  await expect(input).not.toHaveAttribute("aria-activedescendant", /.+/);
  await expect(page.getByRole("button", { name: "경로 계산" })).toBeDisabled();
  expect((await mapState(page)).created.some(({ kind, options }) =>
    kind === "MarkerFake" && options.title === "이전 출발기관")).toBe(false);

  releaseCurrent?.();
  await expect(page.getByRole("option", { name: /새 출발기관/ })).toBeVisible();
  await expect(page.getByRole("button", { name: "경로 계산" })).toBeDisabled();
  await input.press("ArrowDown");
  await input.press("Enter");
  await expect(input).toHaveValue("새 출발기관");
  await expect(page.getByRole("button", { name: "경로 계산" })).toBeEnabled();
});

test("institution_picker_announces_loading_zero_and_error_states", async ({
  page,
}) => {
  await page.addInitScript(() => {
    const originalAbort = AbortController.prototype.abort;
    Object.assign(window, { __task5AbortCalls: 0, __task5OriginalAbort: originalAbort });
    AbortController.prototype.abort = function abortWithoutCancelling() {
      const testWindow = window as typeof window & { __task5AbortCalls: number };
      testWindow.__task5AbortCalls += 1;
    };
  });
  await installMockApi(page);
  const fixture = institutions();
  let releaseStaleError: (() => void) | undefined;
  let errorAttempts = 0;
  await page.route("**/api/v1/institutions**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname !== "/api/v1/institutions") return route.fallback();
    const query = url.searchParams.get("q");
    if (query === "지연 오류") {
      await new Promise<void>((resolve) => { releaseStaleError = resolve; });
      return fulfillJson(route, { error: { code: "INSTITUTIONS_UNAVAILABLE" } }, 503);
    }
    if (query === "최신") return fulfillJson(route, pageOf([fixture.items[0]]));
    if (query === "없음") return fulfillJson(route, pageOf([]));
    if (query === "오류") {
      errorAttempts += 1;
      return errorAttempts === 1
        ? fulfillJson(route, { error: { code: "INSTITUTIONS_UNAVAILABLE" } }, 503)
        : fulfillJson(route, pageOf([fixture.items[0]]));
    }
    return route.fallback();
  });
  await page.goto("/");

  const staleRequest = page.waitForRequest((request) =>
    new URL(request.url()).searchParams.get("q") === "지연 오류");
  await page.getByLabel("출발 기관").fill("지연 오류");
  await staleRequest;
  await expect(page.locator("#origin-selection")).toContainText("검색하고 있습니다");

  await page.getByLabel("출발 기관").fill("최신");
  await expect(page.getByRole("option", { name: /샘물공립초등학교/ })).toBeVisible();
  releaseStaleError?.();
  await expect(page.locator("#origin-selection")).toContainText("총 1개");
  await expect(page.locator("#origin-selection")).not.toContainText("오류");

  await page.getByLabel("출발 기관").fill("없음");
  await expect(page.locator("#origin-selection")).toContainText("0개");
  await expect(page.locator("#origin-results")).toBeHidden();

  await page.getByLabel("출발 기관").fill("오류");
  await expect(page.locator("#origin-selection")).toContainText("불러오지 못했습니다");
  const retry = page.getByRole("button", { name: "기관 검색 다시 시도" });
  await expect(retry).toBeVisible();
  expect(await retry.evaluate((node) => node.closest('[role="listbox"]'))).toBeNull();
  await retry.click();
  await expect(page.getByRole("option", { name: /샘물공립초등학교/ })).toBeVisible();
  expect(await page.evaluate(() =>
    (window as typeof window & { __task5AbortCalls: number }).__task5AbortCalls,
  )).toBeGreaterThan(0);
});

test("facet_failure_leaves_text_search_and_anonymous_calculation_available", async ({
  browser,
  page,
}) => {
  const requestedPaths: string[] = [];
  page.on("request", (request) => requestedPaths.push(new URL(request.url()).pathname));
  await installMockApi(page, { bootstrapStatus: 503, facetsStatus: 503 });
  await page.goto("/");

  await expect(page.locator("#institution-facets-status")).toContainText("필터를 불러오지 못했습니다");
  await expect(page.getByRole("button", { name: "기관 필터 다시 시도" })).toBeVisible();
  await expect(page.getByLabel("기관유형")).toBeDisabled();
  await selectFirstOrigin(page);
  await selectFirstDestination(page);
  await page.getByRole("button", { name: "경로 계산" }).click();
  await expect(page.getByRole("heading", { name: "추천 경로" })).toBeVisible();
  await expect(page.locator("#allowance-amount")).toHaveText("20,000원");
  expect(requestedPaths).toContain("/api/v1/me");

  const hangingContext = await browser.newContext();
  const hangingPage = await hangingContext.newPage();
  try {
    await installMockApi(hangingPage, { bootstrapStatus: 503, facetsHang: true });
    await hangingPage.goto("/");
    await selectFirstOrigin(hangingPage);
    await selectFirstDestination(hangingPage);
    await hangingPage.getByRole("button", { name: "경로 계산" }).click();
    await expect(hangingPage.getByRole("heading", { name: "추천 경로" })).toBeVisible();
  } finally {
    await hangingContext.close();
  }
});

test("institution_pagination_appends_without_duplicates", async ({ page }) => {
  await page.addInitScript(() => {
    Object.assign(window, { __task5AbortCalls: 0 });
    AbortController.prototype.abort = function abortWithoutCancelling() {
      const testWindow = window as typeof window & { __task5AbortCalls: number };
      testWindow.__task5AbortCalls += 1;
    };
  });
  await installMockApi(page);
  const fixture = institutions();
  const first = fixture.items[0];
  const second = fixture.items[1];
  const third: InstitutionItem = {
    ...first,
    institutionId: "test-neis:B10:THIRD",
    siteId: "test-neis:B10:THIRD:main",
    officialName: "세 번째 기관",
    displayName: "세 번째 기관",
  };
  const stale: InstitutionItem = {
    ...first,
    institutionId: "test-neis:B10:STALE",
    siteId: "test-neis:B10:STALE:main",
    officialName: "이전 페이지 누출",
    displayName: "이전 페이지 누출",
  };
  let releasePageTwo: (() => void) | undefined;
  let releaseStalePage: (() => void) | undefined;
  let pageTwoRequests = 0;
  await page.route("**/api/v1/institutions**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname !== "/api/v1/institutions") return route.fallback();
    const offset = url.searchParams.get("offset");
    const type = url.searchParams.get("institution_type");
    if (offset === "0" && type === "HIGH_SCHOOL") {
      return fulfillJson(route, pageOf([second]));
    }
    if (offset === "0") {
      return fulfillJson(route, pageOf([first, second], { total: 4, nextOffset: 20 }));
    }
    if (offset === "20") {
      pageTwoRequests += 1;
      await new Promise<void>((resolve) => { releasePageTwo = resolve; });
      return fulfillJson(route, pageOf([second, third, { ...third }], { total: 4, nextOffset: 40 }));
    }
    if (offset === "40") {
      await new Promise<void>((resolve) => { releaseStalePage = resolve; });
      return fulfillJson(route, pageOf([stale], { total: 4 }));
    }
    return route.fallback();
  });
  await page.goto("/");
  await page.getByLabel("출발 기관").focus();
  const originOptions = page.locator('#origin-results [role="option"]');
  await expect(originOptions).toHaveCount(2);

  const loadMore = page.getByRole("button", { name: "기관 더 보기" });
  await expect(loadMore).toBeVisible();
  expect(await loadMore.evaluate((node) => node.closest('[role="listbox"]'))).toBeNull();
  const pageTwoRequest = page.waitForRequest((request) =>
    new URL(request.url()).searchParams.get("offset") === "20");
  await loadMore.evaluate((button: HTMLButtonElement) => {
    button.click();
    button.click();
  });
  await pageTwoRequest;
  await expect(loadMore).toBeDisabled();
  expect(pageTwoRequests).toBe(1);
  releasePageTwo?.();
  await expect(originOptions).toHaveCount(3);
  await expect(page.getByRole("option", { name: /세 번째 기관/ })).toBeVisible();
  await expect(page.locator("#origin-selection")).toContainText("총 4개");

  const staleRequest = page.waitForRequest((request) =>
    new URL(request.url()).searchParams.get("offset") === "40");
  await loadMore.click();
  await staleRequest;
  await page.getByRole("button", { name: "기관 검색 필터 열기" }).click();
  await page.getByLabel("기관유형").selectOption("HIGH_SCHOOL");
  await expect(page.getByRole("option", { name: /샘물사립고등학교/ })).toBeVisible();
  releaseStalePage?.();
  await expect(page.getByRole("option", { name: /이전 페이지 누출/ })).toHaveCount(0);
  expect(await page.evaluate(() =>
    (window as typeof window & { __task5AbortCalls: number }).__task5AbortCalls,
  )).toBeGreaterThan(0);
});

test("failed_or_selected_base_search_resets_institution_pagination", async ({
  page,
}) => {
  await installMockApi(page);
  const fixture = institutions();
  let oldOffsetRequests = 0;
  await page.route("**/api/v1/institutions**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname !== "/api/v1/institutions") return route.fallback();
    const query = url.searchParams.get("q");
    const offset = url.searchParams.get("offset");
    if (offset === "20") {
      oldOffsetRequests += 1;
      return fulfillJson(route, pageOf([], { total: 2 }));
    }
    if (query === "페이지검색" && offset === "0") {
      return fulfillJson(route, pageOf([fixture.items[0]], {
        total: 2,
        nextOffset: 20,
      }));
    }
    if (query === "오류검색" && offset === "0") {
      return fulfillJson(route, { error: { code: "INSTITUTIONS_UNAVAILABLE" } }, 503);
    }
    return route.fallback();
  });
  await page.goto("/");
  const input = page.getByLabel("출발 기관");
  const loadMore = page.locator("#origin-load-more");

  await input.fill("페이지검색");
  await expect(page.getByRole("option", { name: /샘물공립초등학교/ })).toBeVisible();
  await expect(loadMore).toBeVisible();
  await input.fill("오류검색");
  await expect(page.locator("#origin-selection")).toContainText("불러오지 못했습니다");
  await expect(loadMore).toBeHidden();
  await loadMore.evaluate((button: HTMLButtonElement) => button.click());

  const refreshedPage = page.waitForRequest((request) => {
    const url = new URL(request.url());
    return url.pathname === "/api/v1/institutions"
      && url.searchParams.get("q") === "페이지검색"
      && url.searchParams.get("offset") === "0";
  });
  await input.fill("페이지검색");
  await refreshedPage;
  await expect(page.getByRole("option", { name: /샘물공립초등학교/ })).toBeVisible();
  expect(oldOffsetRequests).toBe(0);

  await page.getByRole("option", { name: /샘물공립초등학교/ }).click();
  await expect(loadMore).toBeHidden();
  await loadMore.evaluate((button: HTMLButtonElement) => button.click());
  const barrier = page.waitForRequest((request) => {
    const url = new URL(request.url());
    return url.pathname === "/api/v1/institutions"
      && url.searchParams.get("q") === "장벽검색";
  });
  await input.fill("장벽검색");
  await barrier;
  expect(oldOffsetRequests).toBe(0);
});
