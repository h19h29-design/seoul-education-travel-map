import { expect, test } from "@playwright/test";
import {
  completePublicOfficialTrip,
  installMockApi,
  readFixture,
} from "./helpers";

test("anonymous form works while me remains unresolved", async ({ page }) => {
  await installMockApi(page);
  let releaseMe!: () => void;
  let markMeSeen!: () => void;
  const heldMe = new Promise<void>((resolve) => { releaseMe = resolve; });
  const meSeen = new Promise<void>((resolve) => { markMeSeen = resolve; });
  await page.route("**/api/v1/me", async (route) => {
    markMeSeen();
    await heldMe;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(readFixture("me-anonymous.json")),
    });
  });
  await page.goto("/");
  await meSeen;
  try {
    await expect(page.getByLabel("출발 기관")).toBeEnabled();
    await expect(page.getByLabel("출장지")).toBeEnabled();
    await completePublicOfficialTrip(page);
    await expect(page.getByRole("heading", { name: "추천 경로" })).toBeVisible();
  } finally {
    releaseMe();
  }
});

test("returning session can calculate before me resolves with csrf", async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(document, "cookie", {
      configurable: true,
      get: () => "__Host-travel_session=opaque; __Host-travel_csrf=csrf-token",
    });
  });
  await installMockApi(page);
  let releaseMe!: () => void;
  let markMeSeen!: () => void;
  let previewCsrf: string | undefined;
  const heldMe = new Promise<void>((resolve) => { releaseMe = resolve; });
  const meSeen = new Promise<void>((resolve) => { markMeSeen = resolve; });
  await page.route("**/api/v1/me", async (route) => {
    markMeSeen();
    await heldMe;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(readFixture("me-authenticated.json")),
    });
  });
  page.on("request", (request) => {
    if (new URL(request.url()).pathname === "/api/v1/trips/preview") {
      previewCsrf = request.headers()["x-csrf-token"];
    }
  });
  await page.goto("/");
  await meSeen;
  try {
    await completePublicOfficialTrip(page);
    await expect(page.getByRole("heading", { name: "추천 경로" })).toBeVisible();
    expect(previewCsrf).toBe("csrf-token");
  } finally {
    releaseMe();
  }
});

test("logged out private controls offer optional Kakao login", async ({ page }) => {
  await installMockApi(page);
  await page.route("**/api/v1/me", (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(readFixture("me-anonymous.json")),
  }));
  await page.goto("/");

  await page.getByRole("button", { name: "계산 이력" }).click();
  const dialog = page.getByRole("dialog", { name: "로그인이 필요한 기능" });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Kakao 로그인" })).toBeVisible();
  await expect(page.getByLabel("출발 기관")).toBeEnabled();
  await expect(page.getByLabel("출장지")).toBeEnabled();
});

test("login control uses the fixed Kakao authorization start path", async ({ page }) => {
  await installMockApi(page);
  await page.route("**/api/v1/me", (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(readFixture("me-anonymous.json")),
  }));
  await page.route("**/auth/kakao/start", (route) => route.fulfill({
    status: 200,
    contentType: "text/html",
    body: "<title>login</title>",
  }));
  await page.goto("/");
  const request = page.waitForRequest("**/auth/kakao/start");
  await page.getByRole("button", { name: "Kakao 로그인" }).click();
  expect(new URL((await request).url()).pathname).toBe("/auth/kakao/start");
});

test("login state restores only the server-resolved active default workplace", async ({ page }) => {
  await installMockApi(page);
  await page.route("**/api/v1/me", (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(readFixture("me-authenticated.json")),
  }));
  await page.route("**/api/v1/me/settings", (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({
      source: "SAVED",
      settings: {
        defaultOriginSiteId: "test-neis:B10:SEMWATER-ES:main",
        defaultTripPattern: "OUTBOUND_ONLY_END_AFTER_SCHEDULE",
        defaultDurationMinutes: 300,
        vehicleUse: "PRIVATE",
        fuelType: "DIESEL",
        efficiencyKmPerLiter: 14.5,
        parkingCostKrw: 3000,
        routeSort: "distance",
      },
      resolvedDefaultOrigin: {
        institutionId: "test-neis:B10:SEMWATER-ES",
        siteId: "test-neis:B10:SEMWATER-ES:main",
        officialName: "샘물공립초등학교",
        displayName: "샘물공립초등학교",
        institutionType: "ELEMENTARY_SCHOOL",
        foundationType: "PUBLIC",
        educationOffice: "SEOUL_EDU_SUPPORT_JUNGBU",
        roadAddress: "서울특별시 중구 샘물로 12",
        district: "중구",
        coordinate: { latitude: 37.56341, longitude: 126.98762 },
        coordinateQuality: "ROOFTOP",
        snapshotId: "fixture-001",
        snapshotAsOf: "2026-08-10",
      },
      warnings: [],
    }),
  }));
  await page.goto("/");

  await expect(page.getByRole("button", { name: "로그아웃" })).toBeVisible();
  await expect(page.getByLabel("출발 기관")).toHaveValue("샘물공립초등학교");
  await expect(page.getByRole("radio", { name: "일정 후 퇴근" })).toBeChecked();
  await expect(page.getByLabel("출장 시간 시간")).toHaveValue("5");
});

test("inactive default workplace is not auto selected", async ({ page }) => {
  await installMockApi(page);
  await page.route("**/api/v1/me", (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(readFixture("me-authenticated.json")),
  }));
  await page.route("**/api/v1/me/settings", (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({
      source: "SAVED",
      settings: {
        defaultOriginSiteId: "test-neis:B10:RETIRED:main",
        defaultTripPattern: "ROUND_TRIP",
        defaultDurationMinutes: 240,
        vehicleUse: "NONE",
        fuelType: "GASOLINE",
        efficiencyKmPerLiter: 10,
        parkingCostKrw: 0,
        routeSort: "time",
      },
      resolvedDefaultOrigin: null,
      warnings: ["DEFAULT_ORIGIN_UNAVAILABLE"],
    }),
  }));
  await page.goto("/");

  await expect(page.getByRole("button", { name: "로그아웃" })).toBeVisible();
  await expect(page.getByLabel("출발 기관")).toHaveValue("");
  await expect(page.getByText("기본 근무지를 다시 선택하세요.")).toBeVisible();
});

test("logout keeps anonymous calculation available", async ({ page }) => {
  await installMockApi(page);
  await page.route("**/api/v1/me", (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(readFixture("me-authenticated.json")),
  }));
  await page.route("**/api/v1/auth/logout", (route) => route.fulfill({
    status: 204,
  }));
  await page.goto("/");
  await page.getByRole("button", { name: "로그아웃" }).click();

  await expect(page.getByRole("button", { name: "Kakao 로그인" })).toBeVisible();
  await completePublicOfficialTrip(page);
  await expect(page.getByRole("heading", { name: "추천 경로" })).toBeVisible();
});
