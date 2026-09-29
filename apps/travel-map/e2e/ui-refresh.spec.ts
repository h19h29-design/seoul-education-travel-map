import { expect, test } from "@playwright/test";
import { completePublicOfficialTrip, installMockApi } from "./helpers";

for (const width of [320, 375, 390, 768, 1024, 1440]) {
  test(`refreshed ${width}px layout keeps the map and real controls usable`, async ({ page }) => {
    const pageErrors: string[] = [];
    page.on("pageerror", (error) => pageErrors.push(error.message));
    await page.setViewportSize({ width, height: 900 });
    await installMockApi(page);
    await page.goto("/");

    await expect(page.getByRole("heading", { name: "출장 준비는 간단하게." })).toBeVisible();
    expect(await page.locator(".ui-brand-mark svg").evaluate((element) => element.namespaceURI)).toBe("http://www.w3.org/2000/svg");
    await expect(page.getByRole("tab", { name: "계산 결과" })).toBeDisabled();
    await expect(page.getByRole("note", { name: "고정 적용 규정" })).toBeVisible();
    await expect(page.locator("#map")).toBeVisible();
    await expect(page.locator(".map-toolbar")).toBeVisible();
    expect(await page.locator("#map").evaluate((element) => element.getBoundingClientRect().height >= 180)).toBe(true);
    expect(await page.locator("html").evaluate((element) => element.scrollWidth <= window.innerWidth)).toBe(true);
    if (process.env.TRAVEL_UI_CAPTURE === "1" && [320, 375, 1440].includes(width)) {
      await page.screenshot({ path: `test-results/ui-refresh-${width}-input.png`, fullPage: true });
    }

    await completePublicOfficialTrip(page);
    await expect(page.getByRole("tab", { name: "계산 결과" })).toHaveAttribute("aria-selected", "true");
    await expect(page.locator("[data-route-direction='OUTBOUND'] .route-card").first()).toBeVisible();
    await expect(page.locator("[data-route-direction='RETURN'] .route-card").first()).toBeVisible();
    await expect(page.locator("#map")).toBeVisible();
    await page.getByRole("tab", { name: "계산 결과" }).focus();
    await page.keyboard.press("ArrowLeft");
    await expect(page.getByRole("tab", { name: "출장 조건" })).toHaveAttribute("aria-selected", "true");
    await page.keyboard.press("ArrowRight");
    await expect(page.getByRole("tab", { name: "계산 결과" })).toHaveAttribute("aria-selected", "true");
    expect(await page.locator("html").evaluate((element) => element.scrollWidth <= window.innerWidth)).toBe(true);
    if (process.env.TRAVEL_UI_CAPTURE === "1" && [320, 375, 1440].includes(width)) {
      await page.screenshot({ path: `test-results/ui-refresh-${width}-results.png`, fullPage: true });
    }
    expect(pageErrors).toEqual([]);
  });
}
