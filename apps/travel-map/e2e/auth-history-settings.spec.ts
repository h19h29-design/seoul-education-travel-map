import { expect, test } from "@playwright/test";
import {
  completePublicOfficialTrip,
  installAuthenticatedHistoryApi,
  installMockApi,
  readFixture,
} from "./helpers";

test("history draft requires a current destination selection", async ({ page }) => {
  await installAuthenticatedHistoryApi(page);
  let previewPosts = 0;
  page.on("request", (request) => {
    if (request.method() === "POST" && new URL(request.url()).pathname === "/api/v1/trips/preview") {
      previewPosts += 1;
    }
  });
  await page.goto("/");
  await page.getByRole("button", { name: "계산 이력" }).click();
  await page.getByRole("button", { name: "다시 계산" }).first().click();
  await expect(page.getByText("출장지를 다시 선택하세요.")).toBeVisible();
  await expect(page.getByRole("button", { name: "경로 계산" })).toBeDisabled();
  expect(previewPosts).toBe(0);
  await page.getByRole("option", { name: /서울특별시청.*세종대로 110/ }).click();
  await page.getByRole("button", { name: "경로 계산" }).click();
  await expect.poll(() => previewPosts).toBe(1);
});

test("history detail renders the stored calculation and rule summary", async ({ page }) => {
  await installAuthenticatedHistoryApi(page);
  await page.goto("/");
  await page.getByRole("button", { name: "계산 이력" }).click();
  await page.getByRole("button", { name: "상세 보기" }).first().click();
  const detail = page.getByRole("dialog", { name: "저장 당시 계산 상세" });
  await expect(detail.getByText("관내 출장")).toBeVisible();
  await expect(detail.getByText("20,000원")).toBeVisible();
  await expect(detail.getByText(/가는 길.*자동차.*35분.*12\.4km.*8,500원/)).toBeVisible();
  await expect(detail.getByText("2025-local-travel")).toBeVisible();
  await expect(detail.getByText("2025-01-01부터 적용")).toBeVisible();
});

test("history loads the page after the first hundred records", async ({ page }) => {
  const firstPage = readFixture<{ items: Array<Record<string, unknown>> }>("history.json");
  const firstHundred = Array.from({ length: 100 }, (_, index) => ({
    ...firstPage.items[0],
    destinationName: `첫 페이지 출장지 ${index + 1}`,
    id: `first-page-history-${String(index).padStart(4, "0")}`,
  }));
  const laterItem = {
    ...firstPage.items[0],
    destinationName: "다음 페이지 출장지",
    id: "next-page-history-0001",
  };
  await installAuthenticatedHistoryApi(page, {
    historyPages: {
      first: { items: firstHundred, nextCursor: "next-page" },
      afterCursor: { "next-page": { items: [laterItem], nextCursor: null } },
    },
  });
  await page.goto("/");
  await page.getByRole("button", { name: "계산 이력" }).click();
  await expect(page.getByRole("heading", { name: /첫 페이지 출장지 100/ })).toBeVisible();
  await page.getByRole("button", { name: "이력 더 보기" }).click();
  await expect(page.getByRole("heading", { name: /다음 페이지 출장지/ })).toBeVisible();
  await expect(page.getByRole("button", { name: "이력 더 보기" })).toBeHidden();
});

test("history deletion sends only the readable csrf token and refreshes", async ({ page }) => {
  await installAuthenticatedHistoryApi(page);
  let deleted = false;
  await page.route("**/api/v1/me/history**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === "DELETE" && path.startsWith("/api/v1/me/history/")) {
      deleted = true;
      return route.fulfill({ status: 204 });
    }
    if (request.method() === "GET" && path === "/api/v1/me/history") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(deleted ? { items: [], nextCursor: null } : readFixture("history.json")),
      });
    }
    return route.fallback();
  });
  let deleteCsrf: string | undefined;
  page.on("request", (request) => {
    if (request.method() === "DELETE" && new URL(request.url()).pathname.startsWith("/api/v1/me/history/")) {
      deleteCsrf = request.headers()["x-csrf-token"];
    }
  });
  await page.goto("/");
  await page.getByRole("button", { name: "계산 이력" }).click();
  await page.getByRole("button", { name: "삭제" }).first().click();
  await expect.poll(() => deleteCsrf).toBe("fixture-csrf-token");
  await expect(page.getByText("보관 중인 계산 이력이 없습니다.")).toBeVisible();
});

test("successful saved calculation refreshes authenticated history", async ({ page }) => {
  await installAuthenticatedHistoryApi(page);
  let historyRequests = 0;
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (request.method() === "GET" && url.pathname === "/api/v1/me/history") {
      historyRequests += 1;
    }
  });
  await page.goto("/");
  await expect(page.getByRole("button", { name: "로그아웃" })).toBeVisible();
  await completePublicOfficialTrip(page);
  await expect(page.getByRole("heading", { name: "추천 경로" })).toBeVisible();
  await expect.poll(() => historyRequests).toBe(1);
});

test("saved calculation refreshes history after a delayed session resolves", async ({ page }) => {
  await installAuthenticatedHistoryApi(page);
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
      body: JSON.stringify(readFixture("me-authenticated.json")),
    });
  });
  let historyRequests = 0;
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (request.method() === "GET" && url.pathname === "/api/v1/me/history") {
      historyRequests += 1;
    }
  });
  await page.goto("/");
  await meSeen;
  try {
    await completePublicOfficialTrip(page);
    await expect(page.getByRole("heading", { name: "추천 경로" })).toBeVisible();
    expect(historyRequests).toBe(0);
  } finally {
    releaseMe();
  }
  await expect(page.getByRole("button", { name: "로그아웃" })).toBeVisible();
  await expect.poll(() => historyRequests).toBe(1);
});

test("history detail returns keyboard focus to the visible history row", async ({ page }) => {
  await installAuthenticatedHistoryApi(page);
  await page.goto("/");
  await page.getByRole("button", { name: "계산 이력" }).click();
  const detailButton = page.getByRole("button", { name: "상세 보기" }).first();
  await detailButton.click();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog", { name: "계산 이력" })).toBeVisible();
  await expect(detailButton).toBeFocused();
});

test("logout discards an in-flight history page before it can render", async ({ page }) => {
  await installAuthenticatedHistoryApi(page);
  let releaseHistory!: () => void;
  let markHistorySeen!: () => void;
  const heldHistory = new Promise<void>((resolve) => { releaseHistory = resolve; });
  const historySeen = new Promise<void>((resolve) => { markHistorySeen = resolve; });
  await page.route("**/api/v1/me/history**", async (route) => {
    const request = route.request();
    if (request.method() === "GET" && new URL(request.url()).pathname === "/api/v1/me/history") {
      markHistorySeen();
      await heldHistory;
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(readFixture("history.json")),
      });
    }
    return route.fallback();
  });
  await page.route("**/api/v1/auth/logout", (route) => route.fulfill({ status: 204 }));
  await page.goto("/");
  await expect(page.getByRole("button", { name: "로그아웃" })).toBeVisible();
  await page.getByRole("button", { name: "계산 이력" }).click();
  await historySeen;
  await page.getByRole("dialog", { name: "계산 이력" }).getByRole("button", { name: "닫기" }).click();
  await page.getByRole("button", { name: "로그아웃" }).click();
  releaseHistory();
  await expect(page.getByRole("button", { name: "Kakao 로그인" })).toBeVisible();
  await expect(page.locator("#history-rows")).toBeEmpty();
});

test("logout clears rendered history detail from the document", async ({ page }) => {
  await installAuthenticatedHistoryApi(page);
  await page.route("**/api/v1/auth/logout", (route) => route.fulfill({ status: 204 }));
  await page.goto("/");
  await expect(page.getByRole("button", { name: "로그아웃" })).toBeVisible();
  await page.getByRole("button", { name: "계산 이력" }).click();
  await page.getByRole("button", { name: "상세 보기" }).first().click();
  await expect(page.getByRole("dialog", { name: "저장 당시 계산 상세" })).toBeVisible();
  await page.keyboard.press("Escape");
  await page.getByRole("dialog", { name: "계산 이력" }).getByRole("button", { name: "닫기" }).click();
  await page.getByRole("button", { name: "로그아웃" }).click();
  await expect(page.getByRole("button", { name: "Kakao 로그인" })).toBeVisible();
  await expect(page.locator("#history-detail-content")).toBeEmpty();
});

test("history deletion refresh discards an older loading page", async ({ page }) => {
  const currentItem = {
    ...readFixture<{ items: Array<Record<string, unknown>> }>("history.json").items[0],
    destinationName: "현재 이력",
  };
  const staleItem = { ...currentItem, destinationName: "삭제 뒤 늦은 이력" };
  await installAuthenticatedHistoryApi(page);
  let deleted = false;
  let releaseNextPage!: () => void;
  let markNextPageSeen!: () => void;
  const heldNextPage = new Promise<void>((resolve) => { releaseNextPage = resolve; });
  const nextPageSeen = new Promise<void>((resolve) => { markNextPageSeen = resolve; });
  await page.route("**/api/v1/me/history**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (request.method() === "DELETE") {
      deleted = true;
      return route.fulfill({ status: 204 });
    }
    if (request.method() === "GET" && url.pathname === "/api/v1/me/history") {
      if (url.searchParams.get("cursor") === "next-page") {
        markNextPageSeen();
        await heldNextPage;
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ items: [staleItem], nextCursor: null }),
        });
      }
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(deleted
          ? { items: [], nextCursor: null }
          : { items: [currentItem], nextCursor: "next-page" }),
      });
    }
    return route.fallback();
  });
  await page.goto("/");
  await page.getByRole("button", { name: "계산 이력" }).click();
  await page.getByRole("button", { name: "이력 더 보기" }).click();
  await nextPageSeen;
  await page.getByRole("button", { name: "삭제" }).first().click();
  releaseNextPage();
  await expect(page.getByText("보관 중인 계산 이력이 없습니다.")).toBeVisible();
  await expect(page.getByText("삭제 뒤 늦은 이력")).toHaveCount(0);
});

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
