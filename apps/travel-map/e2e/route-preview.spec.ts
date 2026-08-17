import { expect, test } from "@playwright/test";
import {
  completePublicOfficialTrip,
  installMockApi,
  mapState,
  readFixture,
  triggerMapClick,
} from "./helpers";

// Mutation caught: collapsing the small-screen flow, hiding directional labels,
// or introducing an application-console error while expanding the map.
test("mobile_375_search_candidate_schedule_result_and_map_flow", async ({ page }) => {
  const applicationErrors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error" && !message.text().includes("favicon")) {
      applicationErrors.push(message.text());
    }
  });
  await page.setViewportSize({ width: 375, height: 812 });
  await installMockApi(page);
  await page.goto("/");
  await completePublicOfficialTrip(page);
  await page.getByRole("button", { name: "4시간" }).click();
  await page.getByRole("radio", { name: "일반 왕복" }).check();
  await page.getByRole("button", { name: "경로 계산" }).click();

  await expect(page.getByLabel("출발 기관")).toBeVisible();
  await expect(page.getByLabel("출장지")).toBeVisible();
  await expect(page.locator("[data-route-direction='OUTBOUND']")).toContainText("가는 길");
  await expect(page.locator("[data-route-direction='RETURN']")).toContainText("돌아오는 길");
  await page.getByRole("button", { name: "지도 펼치기" }).click();
  await expect(page.locator("#map")).toBeVisible();
  await page.getByRole("button", { name: "지도 접기" }).click();
  await expect(page.locator("#map")).toBeHidden();
  expect(await page.evaluate(() => {
    const elements = ["origin-search", "destination-search", "duration-hours", "results", "map-collapse"]
      .map((id) => document.getElementById(id));
    return elements.every((element, index) => (
      index === 0
      || Boolean(elements[index - 1]?.compareDocumentPosition(element) & Node.DOCUMENT_POSITION_FOLLOWING)
    ));
  })).toBeTruthy();
  expect(await page.locator("html").evaluate((node) => node.scrollWidth <= window.innerWidth)).toBeTruthy();
  expect(applicationErrors).toEqual([]);
});

test("keeps institution filters available without crowding the initial form", async ({
  page,
}) => {
  await installMockApi(page);
  await page.goto("/");

  await expect(
    page.getByRole("button", { name: "기관 검색 필터 열기" }),
  ).toBeVisible();
  await expect(page.getByLabel("기관유형")).toBeHidden();

  await page.getByRole("button", { name: "기관 검색 필터 열기" }).click();
  await expect(page.getByLabel("기관유형")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "기관 검색 필터 닫기" }),
  ).toBeVisible();
});

test("never renders a raw main site token when displayName is missing", async ({ page }) => {
  const institutions = readFixture<{ items: Array<Record<string, string>> }>(
    "institutions.json",
  );
  const malformed = { ...institutions.items[0], siteName: "main" };
  delete malformed.displayName;
  await installMockApi(page, { institutions: { items: [malformed] } });
  await page.goto("/");
  await page.getByLabel("출발 기관").fill("샘물");

  await expect(page.getByRole("option", { name: /main/ })).toHaveCount(0);
});

test("uses normalized institution filters to narrow origin results", async ({ page }) => {
  const filterQueries: URL[] = [];
  await installMockApi(page);
  page.on("request", (request) => {
    if (new URL(request.url()).pathname === "/api/v1/institutions") {
      filterQueries.push(new URL(request.url()));
    }
  });
  await page.goto("/");
  await page.getByRole("button", { name: "기관 검색 필터 열기" }).click();
  await page.getByLabel("기관유형").selectOption("ELEMENTARY_SCHOOL");
  await page.getByLabel("설립구분").selectOption("PUBLIC");
  await page.getByLabel("출발 기관").fill("샘물");

  await expect(
    page.getByRole("option", { name: /샘물공립초등학교.*공립.*중구/ }),
  ).toBeVisible();
  await expect(page.getByRole("option", { name: /샘물사립고등학교/ })).toHaveCount(0);
  expect(filterQueries.at(-1)?.searchParams.get("institution_type")).toBe("ELEMENTARY_SCHOOL");
  expect(filterQueries.at(-1)?.searchParams.get("foundation_type")).toBe("PUBLIC");
});

test("submits prior same-day allowance and displays only the remaining amount", async ({
  page,
}) => {
  const payloads: Record<string, unknown>[] = [];
  await installMockApi(page, {
    previewForPayload: (payload) => {
      payloads.push(payload);
      const preview = readFixture<Record<string, unknown>>("preview.json");
      const previous = Number(payload.previousAllowanceKrw);
      preview.allowance = { status: "ESTIMATED", amountKrw: 20_000 - previous, warnings: [] };
      return preview;
    },
  });
  await page.goto("/");
  await expect(page.getByLabel("기존 지급액(원)")).toBeDisabled();
  await completePublicOfficialTrip(page);
  await page.getByLabel("오늘 다른 관내출장이 있습니다").check();
  await expect(page.getByLabel("기존 지급액(원)")).toBeEnabled();
  await page.getByLabel("기존 지급액(원)").fill("10000");
  await page.getByRole("button", { name: "경로 계산" }).click();
  await expect(page.locator("#allowance-amount")).toHaveText("10,000원");
  await page.getByLabel("기존 지급액(원)").fill("20000");
  await page.getByRole("button", { name: "경로 계산" }).click();
  await expect(page.locator("#allowance-amount")).toHaveText("0원");
  expect(payloads.slice(1).map((payload) => payload.previousAllowanceKrw)).toEqual([10_000, 20_000]);
});

test("labels a supported-area twelve-kilometre result as expected non-local", async ({
  page,
}) => {
  const preview = readFixture<Record<string, unknown>>("preview.json");
  preview.coverage = { status: "BUFFER" };
  preview.classification = "NON_LOCAL_EXPECTED";
  preview.allowance = {
    status: "REVIEW_REQUIRED",
    amountKrw: null,
    warnings: ["NON_LOCAL_ALLOWANCE_OUT_OF_SCOPE"],
  };
  await installMockApi(page, { preview });
  await page.goto("/");
  await completePublicOfficialTrip(page);

  await expect(page.locator("#classification-result")).toHaveText("관외 예상");
  await expect(page.locator("#classification-warning")).toContainText("기관의 최종 관외 판단");
});

test("selects a private school origin and shows route rankings", async ({ page }) => {
  await installMockApi(page);
  await page.goto("/");
  await page.getByLabel("출발 기관").fill("샘물");
  await page
    .getByRole("option", { name: /샘물사립고등학교.*사립.*강남구/ })
    .click();
  await page.getByLabel("출장지").fill("서울시청");
  await page
    .getByRole("option", { name: /서울특별시청.*세종대로 110/ })
    .click();
  await page.getByRole("button", { name: "경로 계산" }).click();

  await expect(page.getByText("최단시간")).toHaveCount(2);
  await expect(page.getByText("최단거리")).toHaveCount(2);
  await expect(page.getByText("최저비용")).toHaveCount(2);
  await expect(page.locator("#allowance-amount")).toHaveText("20,000원");
  await expect(
    page.getByRole("heading", { name: "예상 이동비" }),
  ).toBeVisible();
});

// Production mutation caught: storing one global selected route discards the
// other direction and de-emphasizes its polyline when a return route is chosen.
test("keeps one selected and emphasized route for each round-trip direction", async ({
  page,
}) => {
  await installMockApi(page);
  await page.goto("/");
  await completePublicOfficialTrip(page);

  await expect(page.locator("[data-route-id='OUTBOUND:car-1']")).toHaveAttribute(
    "aria-current",
    "true",
  );
  await expect(page.locator("[data-route-id='RETURN:car-1']")).toHaveAttribute(
    "aria-current",
    "true",
  );
  const eventCountBeforeSelection = (await mapState(page)).events.length;
  await page.locator("[data-route-id='RETURN:walk-1']").click();

  await expect(page.locator("[data-route-id='OUTBOUND:car-1']")).toHaveAttribute(
    "aria-current",
    "true",
  );
  await expect(page.locator("[data-route-id='RETURN:walk-1']")).toHaveAttribute(
    "aria-current",
    "true",
  );
  await expect(page.locator("[data-route-id='RETURN:car-1']")).toHaveAttribute(
    "aria-current",
    "false",
  );
  await expect(page.locator("#map")).toHaveAttribute(
    "data-active-routes",
    "OUTBOUND:car-1 RETURN:walk-1",
  );
  const selectionEvents = (await mapState(page)).events
    .slice(eventCountBeforeSelection)
    .filter(({ kind, map, type }) => kind === "PolylineFake" && map === null && type === "setMap");
  expect(
    selectionEvents,
  ).toHaveLength(1);
});

// Production mutation caught: retaining the caller-editable legacy policy
// selector or requiring its value before enabling the fixed-policy preview.
test("discloses the fixed public policy without an editable selector", async ({ page }) => {
  await installMockApi(page);
  await page.goto("/");

  await expect(page.locator("#policy-profile")).toHaveCount(0);
  await expect(
    page.getByRole("note", { name: "고정 적용 규정" }),
  ).toContainText("서울특별시교육청 공무원 여비 기준");
  await page.getByLabel("출발 기관").fill("샘물");
  await page
    .getByRole("option", { name: /샘물공립초등학교.*공립.*중구/ })
    .click();
  await page.getByLabel("출장지").fill("서울시청");
  await page
    .getByRole("option", { name: /서울특별시청.*세종대로 110/ })
    .click();

  await expect(page.getByRole("button", { name: "경로 계산" })).toBeEnabled();
  await page.getByRole("button", { name: "경로 계산" }).click();
  await expect(page.getByRole("heading", { name: "추천 경로" })).toBeVisible();
});

// Production mutation caught: reading removed top-level routes/best, sending legacy
// request fields, or keying same-id directional route cards without their direction.
test("browser renders route legs without legacy top-level routes", async ({ page }) => {
  let submitted: Record<string, unknown> = {};
  await installMockApi(page, {
    previewForPayload: (payload) => {
      submitted = payload;
      return readFixture<Record<string, unknown>>("preview.json");
    },
  });
  await page.goto("/");
  await completePublicOfficialTrip(page);

  await expect(page.locator("#route-count")).toHaveText("6개");
  await expect(page.locator("[data-route-id='OUTBOUND:car-1']")).toBeVisible();
  await expect(page.locator("[data-route-id='RETURN:car-1']")).toBeVisible();
  await page.locator("[data-route-id='RETURN:car-1']").click();
  await expect(page.locator("#map")).toHaveAttribute(
    "data-active-routes",
    "OUTBOUND:car-1 RETURN:car-1",
  );
  expect(submitted.tripPattern).toBe("ROUND_TRIP");
  expect(submitted.endsAt).toBeTruthy();
  expect(submitted).not.toHaveProperty("returnsAt");
  expect(submitted).not.toHaveProperty("policyProfile");
});

test("keeps localized time controls readable on mobile", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await installMockApi(page);
  await page.goto("/");

  expect(
    await page
      .locator("#returns-time")
      .evaluate((node) => node.getBoundingClientRect().width >= 116),
  ).toBeTruthy();
});

test("keeps localized date controls readable in the input rail", async ({ page }) => {
  await installMockApi(page);
  await page.goto("/");

  expect(
    await page
      .locator("#returns-date")
      .evaluate((node) => node.getBoundingClientRect().width >= 200),
  ).toBeTruthy();
});

test("shows the input rail, rankings, and collapsible map without mobile overflow", async ({
  page,
}) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await installMockApi(page);
  await page.goto("/");
  await expect(page.getByLabel("출발 기관")).toBeVisible();
  await expect(page.getByLabel("출장지")).toBeVisible();
  await expect(
    page.getByRole("note", { name: "고정 적용 규정" }),
  ).toBeVisible();

  await completePublicOfficialTrip(page);
  await expect(page.getByRole("heading", { name: "추천 경로" })).toBeVisible();
  await page.getByRole("button", { name: "지도 펼치기" }).click();
  await expect(page.locator("#map")).toBeVisible();
  expect(
    await page
      .locator("html")
      .evaluate((node) => node.scrollWidth <= window.innerWidth),
  ).toBeTruthy();
});

test("shows a route-level warning for partial route data", async ({ page }) => {
  await installMockApi(page);
  await page.goto("/");
  await completePublicOfficialTrip(page);

  await expect(page.locator(".route-warning")).toHaveText("⚠ 주차비는 예상값입니다");
});

// Mutation caught: dropping server-owned aggregate mobility warnings when one fastest leg is unknown.
test("shows_server_owned_partial_mobility_warning_without_changing_standard_amount", async ({ page }) => {
  const preview = readFixture<Record<string, unknown>>("preview.json");
  const partialMobility = readFixture<{
    mobilityCost: Record<string, unknown>;
    fastestLeg: { direction: string; routeId: string; updates: Record<string, unknown> };
  }>("preview-partial-mobility.json");
  preview.mobilityCost = partialMobility.mobilityCost;
  const fastestLeg = (preview.routeLegs as Array<{
    direction: string;
    routes: Array<Record<string, unknown>>;
  }>).find((leg) => leg.direction === partialMobility.fastestLeg.direction)!;
  Object.assign(
    fastestLeg.routes.find((route) => route.id === partialMobility.fastestLeg.routeId)!,
    partialMobility.fastestLeg.updates,
  );
  await installMockApi(page, { preview });
  await page.goto("/");
  await completePublicOfficialTrip(page);

  await expect(page.locator("#mobility-cost")).toHaveText("비용 정보 없음");
  await expect(page.locator("#mobility-status")).toHaveText("확인 필요");
  await expect(page.locator("#mobility-warning")).toBeVisible();
  await expect(page.locator("#mobility-warning")).toHaveText("이동비 일부 정보 확인 필요");
  await expect(page.locator("#allowance-amount")).toHaveText("20,000원");
});

// Mutation caught: rendering a delayed preview after its destination authority is removed.
test("delayed_preview_is_discarded_when_destination_is_removed", async ({ page }) => {
  const applicationErrors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error" && !message.text().includes("favicon")) {
      applicationErrors.push(message.text());
    }
  });
  let releasePreview: (preview: object) => void = () => {};
  const delayedPreview = new Promise<object>((resolve) => { releasePreview = resolve; });
  await installMockApi(page, { previewForPayload: () => delayedPreview });
  await page.goto("/");
  await page.getByLabel("출발 기관").fill("샘물");
  await page.getByRole("option", { name: /샘물공립초등학교.*공립.*중구/ }).click();
  await page.getByLabel("출장지").fill("서울시청");
  await page.getByRole("option", { name: /서울특별시청.*세종대로 110/ }).click();
  const staleResponse = page.waitForResponse((response) => (
    new URL(response.url()).pathname === "/api/v1/trips/preview"
  ));

  await page.getByRole("button", { name: "경로 계산" }).click();
  await page.getByLabel("출장지").fill("서울");
  await expect(page.locator("#calculate-button")).toBeDisabled();
  releasePreview(readFixture<Record<string, unknown>>("preview.json"));
  await staleResponse;

  await expect(page.locator("#results")).toBeHidden();
  await expect(page.locator("#form-error")).toBeHidden();
  await expect.poll(async () => (
    (await mapState(page)).created.filter(({ kind }) => kind === "PolylineFake").length
  )).toBe(0);
  expect(applicationErrors).toEqual([]);
});

// Mutation caught: applying a delayed route response to a replacement destination.
test("only_a_fresh_preview_renders_after_destination_is_replaced", async ({ page }) => {
  const applicationErrors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error" && !message.text().includes("favicon")) {
      applicationErrors.push(message.text());
    }
  });
  let releaseFirstPreview: (preview: object) => void = () => {};
  const firstPreview = new Promise<object>((resolve) => { releaseFirstPreview = resolve; });
  let previewCount = 0;
  const freshPreview = readFixture<Record<string, unknown>>("preview.json");
  (freshPreview.routeLegs as Array<{ routes: Array<Record<string, unknown>> }>)[0]
    .routes[0].source = "FRESH_DESTINATION";
  await installMockApi(page, {
    previewForPayload: () => {
      previewCount += 1;
      return previewCount === 1 ? firstPreview : freshPreview;
    },
  });
  await page.goto("/");
  await page.getByLabel("출발 기관").fill("샘물");
  await page.getByRole("option", { name: /샘물공립초등학교.*공립.*중구/ }).click();
  await page.getByLabel("출장지").fill("서울시청");
  await page.getByRole("option", { name: /서울특별시청.*세종대로 110/ }).click();
  const staleResponse = page.waitForResponse((response) => (
    new URL(response.url()).pathname === "/api/v1/trips/preview"
  ));

  await page.getByRole("button", { name: "경로 계산" }).click();
  await page.getByLabel("출장지").fill("서울역");
  await page.getByRole("option", { name: /서울역 지번 안내소.*동자동 43-205/ }).click();
  await expect(page.locator("#calculate-button")).toBeEnabled();
  releaseFirstPreview(readFixture<Record<string, unknown>>("preview.json"));
  await staleResponse;
  await expect(page.locator("#results")).toBeHidden();
  await expect.poll(async () => (
    (await mapState(page)).created.filter(({ kind }) => kind === "PolylineFake").length
  )).toBe(0);

  await page.getByRole("button", { name: "경로 계산" }).click();
  await expect(page.getByText("FRESH_DESTINATION 기준")).toBeVisible();
  await expect(page.locator("#form-error")).toBeHidden();
  expect(previewCount).toBe(2);
  expect(applicationErrors).toEqual([]);
});

test("renders route source data as inert text", async ({ page }) => {
  const preview = readFixture<{
    routeLegs: Array<{ routes: Array<{ source: string }> }>;
  }>("preview.json");
  preview.routeLegs[0].routes[0].source =
    '<img src=x onerror="window.__task8Xss=\'executed\'">';
  await installMockApi(page, { preview });
  await page.goto("/");
  await completePublicOfficialTrip(page);

  await expect(page.locator("#route-list img")).toHaveCount(0);
  expect(await page.locator(".route-source").allTextContents()).toContain(
    '<img src=x onerror="window.__task8Xss=\'executed\'"> 기준',
  );
  await expect
    .poll(() => page.evaluate(() => window.__task8Xss))
    .toBeUndefined();
});

test("map click invalidates the selected destination until its result is confirmed", async ({
  page,
}) => {
  let previewPosts = 0;
  page.on("request", (request) => {
    if (
      request.method() === "POST" &&
      new URL(request.url()).pathname === "/api/v1/trips/preview"
    ) {
      previewPosts += 1;
    }
  });
  await installMockApi(page);
  await page.goto("/");
  await expect(page.locator("#map-status")).toHaveText(
    "지도에서 위치를 선택해 출장지 주소를 확인할 수 있습니다.",
  );
  await page.getByLabel("출발 기관").fill("샘물");
  await page
    .getByRole("option", { name: /샘물공립초등학교.*공립.*중구/ })
    .click();
  await page.getByLabel("출장지").fill("서울시청");
  await page
    .getByRole("option", { name: /서울특별시청.*세종대로 110/ })
    .click();
  await triggerMapClick(page);

  await expect(
    page.getByRole("option", { name: /서울특별시청 지도 선택.*세종대로 110/ }),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "경로 계산" })).toBeDisabled();
  await page.locator("#trip-form").evaluate((form) => form.requestSubmit());
  await expect(page.locator("#form-error")).toBeVisible();
  expect(previewPosts).toBe(0);

  await page
    .getByRole("option", { name: /서울특별시청 지도 선택.*세종대로 110/ })
    .click();
  const previewRequest = page.waitForRequest(
    (request) =>
      request.method() === "POST" &&
      new URL(request.url()).pathname === "/api/v1/trips/preview",
  );
  await page.getByRole("button", { name: "경로 계산" }).click();
  const payload = JSON.parse((await previewRequest).postData() || "{}");
  expect(payload.destination.name).toBe("서울특별시청 지도 선택");
});

test("mobile map exposes a collapse control above the expanded canvas", async ({
  page,
}) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await installMockApi(page);
  await page.goto("/");

  await page.getByRole("button", { name: "지도 펼치기" }).click();
  await expect(page.getByRole("button", { name: "지도 접기" })).toBeVisible();
  await page.getByRole("button", { name: "지도 접기" }).click();
  await expect(page.getByRole("button", { name: "지도 펼치기" })).toBeVisible();
  await expect(page.locator("#map")).toBeHidden();
});

test("map polyline, cleanup, and boundary effects reach the Kakao adapter", async ({
  page,
}) => {
  await installMockApi(page);
  await page.goto("/");
  await completePublicOfficialTrip(page);
  await expect(page.locator("[data-route-id='OUTBOUND:car-1']")).toBeVisible();

  const initialMapState = await mapState(page);
  expect(
    initialMapState.created.filter(({ kind }) => kind === "PolylineFake"),
  ).toHaveLength(3);
  expect(
    initialMapState.created.filter(
      ({ kind, options }) => kind === "PolylineFake" && options.strokeWeight === 8,
    ),
  ).toHaveLength(2);

  await page.locator("[data-route-id='RETURN:walk-1']").click();
  await expect.poll(async () =>
    (await mapState(page)).created.filter(({ kind }) => kind === "PolylineFake").length,
  ).toBe(4);
  expect(
    (await mapState(page)).events.filter(
      ({ kind, map, type }) => kind === "PolylineFake" && map === null && type === "setMap",
    ),
  ).toHaveLength(1);

  await page.getByLabel("서울 경계").check();
  await expect
    .poll(async () =>
      (await mapState(page)).created.filter(({ kind }) => kind === "PolygonFake")
        .length,
    )
    .toBe(1);
  await page.getByLabel("서울 경계").uncheck();
  await expect
    .poll(async () =>
      (await mapState(page)).events.filter(
        ({ kind, map, type }) =>
          kind === "PolygonFake" && map === null && type === "setMap",
      ).length,
    )
    .toBe(1);

  await page.getByRole("button", { name: "경로 계산" }).click();
  await expect
    .poll(async () =>
      (await mapState(page)).events.filter(
        ({ map, type }) => map === null && type === "setMap",
      ).length,
    )
    .toBeGreaterThanOrEqual(4);
});

test("keeps checked boundary layers after a route preview is rendered again", async ({
  page,
}) => {
  await installMockApi(page);
  await page.goto("/");
  await completePublicOfficialTrip(page);
  await page.getByLabel("서울 경계").check();
  await page.getByLabel("12km 지원영역").check();
  await expect
    .poll(async () =>
      (await mapState(page)).created.filter(({ kind }) => kind === "PolygonFake").length,
    )
    .toBe(2);

  await page.getByRole("button", { name: "경로 계산" }).click();

  await expect(page.getByLabel("서울 경계")).toBeChecked();
  await expect(page.getByLabel("12km 지원영역")).toBeChecked();
  await expect
    .poll(async () =>
      (await mapState(page)).created.filter(({ kind }) => kind === "PolygonFake").length,
    )
    .toBe(2);
});

test("keyboard option selection authorizes calculation while free text does not", async ({
  page,
}) => {
  await installMockApi(page);
  await page.goto("/");

  await page.getByLabel("출발 기관").fill("샘물");
  await page.getByLabel("출발 기관").press("ArrowDown");
  await page.getByLabel("출발 기관").press("Enter");
  await expect(page.getByLabel("출발 기관")).toHaveValue("샘물공립초등학교");
  await page.getByLabel("출장지").fill("서울시청");
  await expect(page.getByRole("button", { name: "경로 계산" })).toBeDisabled();

  await page.getByLabel("출장지").press("ArrowDown");
  await page.getByLabel("출장지").press("Enter");
  await expect(page.getByRole("button", { name: "경로 계산" })).toBeEnabled();
});
