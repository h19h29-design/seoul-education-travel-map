import { expect, test } from "@playwright/test";
import {
  completePublicOfficialTrip,
  installMockApi,
  mapState,
  readFixture,
  triggerMapClick,
} from "./helpers";

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
  await expect(page.locator("#classification-distance")).toContainText("기관의 최종 관외 판단");
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
    .filter(({ kind, type }) => kind === "PolylineFake" && type === "setOptions");
  expect(selectionEvents).toHaveLength(6);
  expect(
    selectionEvents.filter(({ options }) => options?.strokeWeight === 8),
  ).toHaveLength(2);
});

// Production mutation caught: retaining the caller-editable legacy policy
// selector or requiring its value before enabling the fixed-policy preview.
test("discloses the fixed public policy without an editable selector", async ({ page }) => {
  await installMockApi(page);
  await page.goto("/");

  await expect(page.locator("#policy-profile")).toHaveCount(0);
  await expect(
    page.getByRole("note", { name: "고정 적용 규정" }),
  ).toContainText("서울시교육청 공무원 여비 규정");
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

  await expect(page.getByText("주차비는 예상값입니다")).toBeVisible();
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
  await page.waitForTimeout(100);
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
  ).toHaveLength(7);
  expect(
    initialMapState.events.some(
      ({ options, type }) =>
        type === "setOptions" && options?.strokeWeight === 8,
    ),
  ).toBe(true);

  await page.locator("[data-route-id='RETURN:walk-1']").click();
  await expect
    .poll(async () =>
      (await mapState(page)).events.some(
        ({ options, type }) =>
          type === "setOptions" && options?.strokeWeight === 8,
      ),
    )
    .toBe(true);

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
    .toBeGreaterThanOrEqual(6);
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
