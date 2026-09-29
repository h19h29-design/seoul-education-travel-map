import { expect, test } from "@playwright/test";
import {
  completePublicOfficialTrip,
  installMockApi,
  mapState,
  openEndScheduleDetail,
  openFormView,
} from "./helpers";

// Mutation caught: booting the presentation layer without the two-view tablist,
// without marking the form view active, or with an enabled empty results tab.
test("초기에는 조건 입력 화면이고 결과 탭은 비활성화된다", async ({ page }) => {
  await installMockApi(page);
  await page.goto("/");

  await expect(page.locator("body")).toHaveAttribute("data-travel-ui", "2");
  await expect(page.locator("body")).toHaveClass(/ui-form-view/);

  const formTab = page.locator("#ui-form-tab");
  const resultTab = page.locator("#ui-results-tab");
  await expect(formTab).toHaveAttribute("role", "tab");
  await expect(resultTab).toHaveAttribute("role", "tab");
  await expect(formTab).toHaveAttribute("aria-selected", "true");
  await expect(resultTab).toHaveAttribute("aria-selected", "false");
  await expect(resultTab).toBeDisabled();

  await expect(page.locator("#ui-input-panel")).toBeVisible();
  await expect(page.locator("#results")).toBeHidden();

  const calculate = page.locator("#calculate-button");
  await expect(calculate).toHaveText("경로·예상 여비 확인");
  await expect(calculate).toBeDisabled();
});

// Mutation caught: re-rendering the form instead of hiding it (losing user
// input) or dropping the Kakao polylines when the results view becomes active.
test("계산하면 결과 화면으로 전환되고 지도 경로와 입력값이 유지된다", async ({ page }) => {
  await installMockApi(page);
  await page.goto("/");
  await completePublicOfficialTrip(page);

  const resultTab = page.locator("#ui-results-tab");
  await expect(page.locator("#results")).toBeVisible();
  await expect(resultTab).toBeEnabled();
  await expect(resultTab).toHaveAttribute("aria-selected", "true");
  await expect(page.locator("body")).toHaveClass(/ui-results-view/);
  await expect(page.locator("#ui-input-panel")).toBeHidden();

  await expect
    .poll(async () =>
      (await mapState(page)).created.filter(({ kind }) => kind === "PolylineFake")
        .length,
    )
    .toBeGreaterThanOrEqual(3);

  await page.locator("#ui-form-tab").click();
  await expect(page.locator("body")).toHaveClass(/ui-form-view/);
  await expect(page.locator("#ui-input-panel")).toBeVisible();
  await expect(page.getByLabel("출발 기관")).toBeVisible();
  await expect(page.getByLabel("출장지")).toBeVisible();
});

// Mutation caught: leaving a stale results view selectable after the inputs
// change, or hiding the form view instead of returning to it.
test("결과 뒤 입력을 바꾸면 결과가 무효화되고 조건 화면으로 돌아간다", async ({ page }) => {
  await installMockApi(page);
  await page.goto("/");
  await completePublicOfficialTrip(page);
  await expect(page.locator("#results")).toBeVisible();

  await page.locator("#ui-form-tab").click();
  await page.getByLabel("출장지").fill("서울");

  await expect(page.locator("#results")).toBeHidden();
  await expect(page.locator("body")).toHaveClass(/ui-form-view/);
  await expect(page.locator("#ui-results-tab")).toBeDisabled();
});

// Mutation caught: relabelling the radios without preserving their enum values
// or showing the direction sentence that matches the selected pattern.
test("출장 형태 라벨과 안내 문구가 방향에 맞게 연결된다", async ({ page }) => {
  await installMockApi(page);
  await page.goto("/");

  const note = page.locator(".ui-pattern-note");
  await expect(page.getByRole("radio", { name: "일반 왕복" })).toHaveAttribute(
    "value",
    "ROUND_TRIP",
  );
  await expect(page.getByRole("radio", { name: "일정 후 퇴근" })).toHaveAttribute(
    "value",
    "OUTBOUND_ONLY_END_AFTER_SCHEDULE",
  );
  await expect(
    page.getByRole("radio", { name: "바로 출근 후 근무지 복귀" }),
  ).toHaveAttribute("value", "RETURN_ONLY_DIRECT_TO_DESTINATION");

  await expect(note).toHaveText("근무지에서 출발해, 출장 후 근무지로 돌아옵니다.");

  await page.getByRole("radio", { name: "일정 후 퇴근" }).check();
  await expect(note).toHaveText("근무지 → 출장지만 계산합니다. 퇴근길은 제외합니다.");

  await page.getByRole("radio", { name: "바로 출근 후 근무지 복귀" }).check();
  await expect(note).toHaveText(
    "출장지 → 근무지만 계산합니다. 집에서 출발하는 길은 제외합니다.",
  );

  await page.getByRole("radio", { name: "일반 왕복" }).check();
  await expect(note).toHaveText("근무지에서 출발해, 출장 후 근무지로 돌아옵니다.");
});

// Mutation caught: leaving the quick-duration pressed state or the collapsed
// end summary stale after the schedule changes.
test("빠른 시간 선택과 종료 일시 요약이 동기화된다", async ({ page }) => {
  await installMockApi(page);
  await page.goto("/");

  const endSummary = page.locator("#ui-end-summary");
  const fourHours = page.locator('[data-duration-hours="4"]');
  const fiveHours = page.locator('[data-duration-hours="5"]');

  // The app applies its own schedule defaults before the user touches anything.
  await expect(endSummary).toHaveText(/^(당일|\d{2}\/\d{2}) \d{2}:\d{2}$/);
  await expect(fourHours).toHaveAttribute("aria-pressed", "true");
  await expect(fiveHours).toHaveAttribute("aria-pressed", "false");

  await openEndScheduleDetail(page);
  const defaultEndDate = await page.locator("#returns-date").inputValue();
  await page.locator("#returns-date").fill("");
  await expect(endSummary).toHaveText("일정을 입력하세요");
  await page.locator("#returns-date").fill(defaultEndDate);
  await expect(endSummary).toHaveText(/^(당일|\d{2}\/\d{2}) \d{2}:\d{2}$/);

  await page.locator("#starts-date").fill("2026-08-10");
  await page.locator("#starts-time").fill("09:00");
  await page.getByRole("button", { name: "5시간" }).click();

  await expect(fiveHours).toHaveAttribute("aria-pressed", "true");
  await expect(fourHours).toHaveAttribute("aria-pressed", "false");
  await expect(endSummary).toHaveText("당일 14:00");

  await page.locator("#starts-time").fill("22:30");
  await page.locator("#duration-hours").fill("4");
  await page.locator("#duration-minutes").fill("0");

  await expect(endSummary).toHaveText("08/11 02:30");
});

// Mutation caught: never flagging the collapsed options block when a vehicle
// assumption is chosen.
test("차량 가정을 선택하면 추가 옵션 블록이 활성 표시된다", async ({ page }) => {
  await installMockApi(page);
  await page.goto("/");

  const more = page.locator(".ui-more-options");
  await expect(more).not.toHaveClass(/has-options/);

  await more.locator("summary").click();
  await page.locator("#vehicle-use").selectOption("PRIVATE");

  await expect(more).toHaveClass(/has-options/);
});

// Mutation caught: the presentation layer writing to storage, assigning cookies,
// or issuing a private request that the calculation flow does not own.
test("새 화면은 저장소에 쓰지 않고 예상 밖 비공개 요청을 만들지 않는다", async ({ page }) => {
  const mockPaths = new Set([
    "/api/v1/bootstrap",
    "/api/v1/me",
    "/api/v1/institutions/facets",
    "/api/v1/institutions",
    "/api/v1/places/reverse",
    "/api/v1/places",
    "/api/v1/policy/current",
    "/api/v1/geodata/seoul",
    "/api/v1/geodata/support",
    "/api/v1/trips/preview",
  ]);
  const unexpectedRequests: string[] = [];
  page.on("request", (request) => {
    const path = new URL(request.url()).pathname;
    if (
      (path.startsWith("/api/v1/") || path.startsWith("/auth/"))
      && !mockPaths.has(path)
    ) {
      unexpectedRequests.push(path);
    }
  });
  await installMockApi(page);
  await page.addInitScript(() => {
    type Writes = { cookie: string[]; local: string[]; session: string[] };
    const writes: Writes = { cookie: [], local: [], session: [] };
    (window as unknown as { __storageWrites: Writes }).__storageWrites = writes;

    const nativeSetItem = Storage.prototype.setItem;
    Storage.prototype.setItem = function (this: Storage, key: string, value: string) {
      const bucket = this === window.localStorage
        ? writes.local
        : this === window.sessionStorage
          ? writes.session
          : writes.cookie;
      bucket.push(`${key}=${value}`);
      return nativeSetItem.call(this, key, String(value));
    };

    const cookieDescriptor = Object.getOwnPropertyDescriptor(
      Document.prototype,
      "cookie",
    );
    const cookieGetter = cookieDescriptor?.get;
    const cookieSetter = cookieDescriptor?.set;
    if (cookieGetter && cookieSetter) {
      Object.defineProperty(document, "cookie", {
        configurable: true,
        get() {
          return cookieGetter.call(document);
        },
        set(value: string) {
          writes.cookie.push(String(value));
          cookieSetter.call(document, value);
        },
      });
    }
  });
  await page.goto("/");
  await completePublicOfficialTrip(page);
  await expect(page.locator("#results")).toBeVisible();

  const writes = await page.evaluate(() => (
    window as unknown as {
      __storageWrites: { cookie: string[]; local: string[]; session: string[] };
    }
  ).__storageWrites);
  expect(writes.local).toEqual([]);
  expect(writes.session).toEqual([]);
  expect(writes.cookie).toEqual([]);
  expect(unexpectedRequests).toEqual([]);
});

// Mutation caught: keeping the map behind a collapse step, shrinking the canvas,
// or reflowing the page past the mobile viewport width.
test("모바일에서 지도가 접힘 없이 고정 높이로 보인다", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await installMockApi(page);
  await page.goto("/");

  await expect(page.locator("#map")).toBeVisible();
  await expect(page.locator("#map-collapse")).toBeHidden();

  const stageHeight = await page
    .locator(".map-stage")
    .evaluate((node) => node.getBoundingClientRect().height);
  expect(stageHeight).toBeGreaterThanOrEqual(340);
  expect(stageHeight).toBeLessThanOrEqual(380);

  const mapHeight = await page
    .locator("#map")
    .evaluate((node) => node.getBoundingClientRect().height);
  expect(mapHeight).toBeGreaterThan(200);
  expect(mapHeight).toBeLessThanOrEqual(400);

  expect(
    await page
      .locator("html")
      .evaluate((node) => node.scrollWidth <= window.innerWidth),
  ).toBeTruthy();
});

// Mutation caught: only allowing pointer clicks on the tablist and ignoring
// arrow-key navigation between the two views.
test("결과가 있으면 방향키로 두 탭을 오갈 수 있다", async ({ page }) => {
  await installMockApi(page);
  await page.goto("/");
  await completePublicOfficialTrip(page);

  const formTab = page.locator("#ui-form-tab");
  const resultTab = page.locator("#ui-results-tab");
  await expect(page.locator("#results")).toBeVisible();
  await expect(resultTab).toBeEnabled();

  await formTab.focus();
  await formTab.press("ArrowRight");
  await expect(resultTab).toBeFocused();
  await expect(resultTab).toHaveAttribute("aria-selected", "true");

  await resultTab.press("ArrowLeft");
  await expect(formTab).toBeFocused();
  await expect(formTab).toHaveAttribute("aria-selected", "true");
});

// Mutation caught: floating the action above the form on small screens, where it
// covers the date and time inputs instead of ending the flow.
test("모바일에서 계산 버튼이 일정 입력을 덮지 않는다", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await installMockApi(page);
  await page.goto("/");

  const geometry = await page.evaluate(() => {
    const box = (selector: string) => {
      const node = document.querySelector(selector);
      if (!node) return null;
      const rect = node.getBoundingClientRect();
      return { top: Math.round(rect.top), bottom: Math.round(rect.bottom) };
    };
    return {
      dock: box(".ui-submit-dock"),
      startsDate: box("#starts-date"),
      startsTime: box("#starts-time"),
    };
  });

  expect(geometry.dock).not.toBeNull();
  expect(geometry.dock!.top).toBeGreaterThanOrEqual(geometry.startsTime!.bottom - 1);
  expect(geometry.dock!.top).toBeGreaterThanOrEqual(geometry.startsDate!.bottom - 1);
});

// Mutation caught: a recalculation that leaves #results already un-hidden never
// switches the visible view, so the fresh result stays behind the form panel.
test("조건 탭에서 다시 계산하면 새 결과 화면으로 전환된다", async ({ page }) => {
  await installMockApi(page);
  await page.goto("/");
  await completePublicOfficialTrip(page);
  await expect(page.locator("#results")).toBeVisible();

  await openFormView(page);
  await page.getByRole("button", { name: "5시간" }).click();
  await page.getByRole("button", { name: "경로·예상 여비 확인" }).click();

  await expect(page.locator("#results")).toBeVisible();
  await expect(page.locator("body")).toHaveClass(/ui-results-view/);
  await expect(page.locator("#ui-results-tab")).toHaveAttribute(
    "aria-selected",
    "true",
  );
});

// Mutation caught: a refreshed block that overflows the page on one of the
// reviewed widths, or that hides the primary action behind it.
test("검토한 화면 너비마다 가로 넘침 없이 계산 버튼이 보인다", async ({ page }) => {
  await installMockApi(page);
  await page.goto("/");

  for (const width of [320, 375, 390, 768, 1024, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    expect(
      await page
        .locator("html")
        .evaluate((node) => node.scrollWidth <= window.innerWidth + 1),
      `horizontal overflow at ${width}px`,
    ).toBeTruthy();
    await expect(
      page.getByRole("button", { name: "경로·예상 여비 확인" }),
      `primary action hidden at ${width}px`,
    ).toBeVisible();
  }
});
