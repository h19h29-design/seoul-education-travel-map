import { expect, test } from "@playwright/test";
import { fulfillJson, installMockApi, readFixture } from "./helpers";

type PolicyDisclosure = {
  profile: string;
  profileLabel: string;
  ruleSetId: string;
  effectiveFrom: string;
  localRoundTripExclusiveMeters: number;
  actualExpenseInclusiveMeters: number;
  fourHoursMinutes: number;
  underFourHoursKrw: number;
  fourHoursOrMoreKrw: number;
  officialVehicleDeductionKrw: number;
  sourceRefs: string[];
};

const policy = () => readFixture<PolicyDisclosure>("policy-current.json");

test("usage_help_explains_three_patterns_and_anonymous_use", async ({ page }) => {
  await installMockApi(page);
  await page.goto("/");

  await page.getByRole("button", { name: "사용 안내" }).click();
  const dialog = page.getByRole("dialog", { name: "사용 안내" });
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText("기관·출장지 후보");
  await expect(dialog).toContainText("일반 왕복");
  await expect(dialog).toContainText("일정 후 퇴근");
  await expect(dialog).toContainText("출장지로 바로 출근 후 근무지 복귀");
  await expect(dialog).toContainText("출장시간");
  await expect(dialog).toContainText("경로와 예상 여비");
  await expect(dialog).toContainText("로그인 없이");
  await expect(dialog).toContainText("선택 사항");
  await expect(dialog).toContainText("정확히 168시간");
});

test("policy_panel_renders_server_rule_without_html_sink", async ({ page }) => {
  const productionShaped = {
    ...policy(),
    ruleSetId: '<img src=x onerror="window.__policyXss=\'executed\'">서버 규칙',
  };
  const policyRequests: URL[] = [];
  await installMockApi(page, { policy: productionShaped });
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname === "/api/v1/policy/current") policyRequests.push(url);
  });
  await page.goto("/");

  await page.getByRole("button", { name: "관련 규정" }).click();
  const dialog = page.getByRole("dialog", { name: "관련 규정" });
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText(productionShaped.profileLabel);
  await expect(dialog).toContainText(productionShaped.ruleSetId);
  await expect(dialog.locator("img")).toHaveCount(0);
  await expect.poll(() => page.evaluate(() =>
    (window as typeof window & { __policyXss?: string }).__policyXss,
  )).toBeUndefined();
  expect(policyRequests).toHaveLength(1);
  expect(policyRequests[0].pathname).toBe("/api/v1/policy/current");
  await expect(dialog.getByRole("link", { name: "공식 출처 1" })).toHaveAttribute(
    "href",
    productionShaped.sourceRefs[0],
  );
  await expect(dialog.getByRole("link", { name: "공식 출처 1" })).toHaveAttribute(
    "target",
    "_blank",
  );
  await expect(dialog.getByRole("link", { name: "공식 출처 1" })).toHaveAttribute(
    "rel",
    "noopener noreferrer",
  );
});

test("dialogs_close_on_escape_and_restore_trigger_focus", async ({ page }) => {
  await installMockApi(page);
  await page.goto("/");

  const helpButton = page.getByRole("button", { name: "사용 안내" });
  await helpButton.focus();
  await helpButton.press("Enter");
  const helpDialog = page.getByRole("dialog", { name: "사용 안내" });
  await expect(helpDialog).toBeVisible();
  await expect(helpDialog.getByRole("heading", { name: "사용 안내" })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(helpDialog).toBeHidden();
  await expect(helpButton).toBeFocused();

  const policyButton = page.getByRole("button", { name: "관련 규정" });
  await policyButton.click();
  const policyDialog = page.getByRole("dialog", { name: "관련 규정" });
  await expect(policyDialog).toBeVisible();
  await expect(policyDialog.getByRole("heading", { name: "관련 규정" })).toBeFocused();
  await policyDialog.getByRole("button", { name: "닫기" }).click();
  await expect(policyDialog).toBeHidden();
  await expect(policyButton).toBeFocused();
});

test("policy_panel_shows_estimate_and_employment_disclaimers", async ({ page }) => {
  await installMockApi(page);
  await page.goto("/");

  await page.getByRole("button", { name: "관련 규정" }).click();
  const dialog = page.getByRole("dialog", { name: "관련 규정" });
  await expect(dialog).toContainText("예상값");
  await expect(dialog).toContainText("지급 확정");
  await expect(dialog).toContainText("Kakao 로그인");
  await expect(dialog).toContainText("재직 확인이 아닙니다");
});

test("policy_panel_ignores_late_closed_request_after_reopen", async ({ page }) => {
  await page.addInitScript(() => {
    AbortController.prototype.abort = function deferAbortForRaceCoverage() {};
  });
  const stalePolicy = { ...policy(), ruleSetId: "stale-rule" };
  const currentPolicy = { ...policy(), ruleSetId: "current-rule" };
  const releases: Array<() => void> = [];
  await installMockApi(page);
  await page.route("**/api/v1/policy/current", async (route) => {
    const requestIndex = releases.length;
    await new Promise<void>((resolve) => releases.push(resolve));
    await fulfillJson(route, requestIndex === 0 ? stalePolicy : currentPolicy);
  });
  await page.goto("/");

  const policyButton = page.getByRole("button", { name: "관련 규정" });
  const dialog = page.getByRole("dialog", { name: "관련 규정" });
  await policyButton.click();
  await expect.poll(() => releases).toHaveLength(1);
  await dialog.getByRole("button", { name: "닫기" }).click();
  await expect(dialog).toBeHidden();

  await policyButton.click();
  await expect.poll(() => releases).toHaveLength(2);
  releases[1]();
  await expect(dialog).toContainText("current-rule");
  releases[0]();
  await expect(dialog).toContainText("current-rule");
  await expect(dialog).not.toContainText("stale-rule");
});

test("closing_policy_load_aborts_without_showing_a_generic_error", async ({ page }) => {
  await page.addInitScript(() => {
    const nativeAbort = AbortController.prototype.abort;
    Object.assign(window, { __policyAbortCount: 0 });
    AbortController.prototype.abort = function recordPolicyAbort(...args) {
      const testWindow = window as typeof window & { __policyAbortCount: number };
      testWindow.__policyAbortCount += 1;
      return nativeAbort.apply(this, args);
    };
  });
  const applicationErrors: string[] = [];
  let releaseRequest: (() => void) | undefined;
  await installMockApi(page);
  page.on("console", (message) => {
    if (message.type() === "error" && !message.text().includes("favicon")) {
      applicationErrors.push(message.text());
    }
  });
  await page.route("**/api/v1/policy/current", async (route) => {
    await new Promise<void>((resolve) => { releaseRequest = resolve; });
    try {
      await route.abort();
    } catch {
      // The browser may have already cancelled this intercepted request.
    }
  });
  await page.goto("/");
  await page.evaluate(() => {
    const testWindow = window as typeof window & { __policyAbortCount: number };
    testWindow.__policyAbortCount = 0;
  });

  const policyButton = page.getByRole("button", { name: "관련 규정" });
  const dialog = page.getByRole("dialog", { name: "관련 규정" });
  await policyButton.click();
  await expect.poll(() => Boolean(releaseRequest)).toBe(true);
  await dialog.getByRole("button", { name: "닫기" }).click();
  await expect(dialog).toBeHidden();
  await expect.poll(() => page.evaluate(() =>
    (window as typeof window & { __policyAbortCount: number }).__policyAbortCount,
  )).toBeGreaterThan(0);
  releaseRequest?.();
  await expect(page.locator("#policy-dialog [data-policy-status]")).not.toContainText(
    "불러오지 못했습니다",
  );
  expect(applicationErrors).toEqual([]);
});

test("mobile_menu_opens_each_info_panel_without_overflow", async ({ page }) => {
  const applicationErrors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error" && !message.text().includes("favicon")) {
      applicationErrors.push(message.text());
    }
  });
  await installMockApi(page);

  for (const width of [375, 390]) {
    await page.setViewportSize({ width, height: 812 });
    await page.goto("/");
    const menuButton = page.getByRole("button", { name: "메뉴 열기" });
    const menu = page.getByRole("navigation", { name: "도움말 및 설정" });
    await expect(menuButton).toBeVisible();
    await expect(menu).toBeHidden();
    await menuButton.click();
    await expect(menuButton).toHaveAttribute("aria-expanded", "true");
    await expect(menu).toBeVisible();

    for (const [triggerName, dialogName] of [
      ["사용 안내", "사용 안내"],
      ["관련 규정", "관련 규정"],
    ]) {
      const trigger = menu.getByRole("button", { name: triggerName });
      await trigger.click();
      const dialog = page.getByRole("dialog", { name: dialogName });
      await expect(dialog).toBeVisible();
      await dialog.getByRole("button", { name: "닫기" }).click();
      await expect(dialog).toBeHidden();
      await expect(trigger).toBeFocused();
    }
    await menuButton.click();
    await expect(menu).toBeHidden();
    expect(await page.locator("html").evaluate((node) =>
      node.scrollWidth <= window.innerWidth,
    )).toBeTruthy();
  }
  expect(applicationErrors).toEqual([]);
});
