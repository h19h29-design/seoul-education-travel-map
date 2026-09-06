import { expect, test } from "@playwright/test";
import { fulfillJson, installMockApi, readFixture } from "./helpers";

test("stateless beta hides private controls and does not probe private APIs", async ({ page }) => {
  const privateRequests: string[] = [];
  await installMockApi(page);
  await page.route("**/api/v1/bootstrap", async (route) => {
    await fulfillJson(route, {
      ...readFixture("bootstrap.json"),
      privateFeaturesEnabled: false,
    });
  });
  page.on("request", (request) => {
    const path = new URL(request.url()).pathname;
    if (path === "/api/v1/me" || path.startsWith("/auth/")) {
      privateRequests.push(path);
    }
  });

  await page.goto("/");

  await expect(page.getByRole("button", { name: "계산 이력" })).toBeHidden();
  await expect(page.getByRole("button", { name: "설정" })).toBeHidden();
  await expect(page.getByRole("button", { name: "Kakao 로그인" })).toBeHidden();
  await expect(page.locator("#notice")).toContainText("1회성 베타");
  await expect(page.locator("#private-auth-dialog")).toBeHidden();
  expect(privateRequests).toEqual([]);
});
