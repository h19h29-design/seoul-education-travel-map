import { expect, test } from "@playwright/test";
import {
  completePublicOfficialTrip,
  installMockApi,
  readFixture,
} from "./helpers";

async function selectTripInputs(page: import("@playwright/test").Page): Promise<void> {
  await page.getByLabel("출발 기관").fill("샘물");
  await page
    .getByRole("option", { name: /샘물공립초등학교.*공립.*중구/ })
    .click();
  await page.getByLabel("출장지").fill("서울시청");
  await page
    .getByRole("option", { name: /서울특별시청.*세종대로 110/ })
    .click();
}

async function submitAndReadPayload(
  page: import("@playwright/test").Page,
): Promise<Record<string, unknown>> {
  const request = page.waitForRequest((candidate) => (
    candidate.method() === "POST"
    && new URL(candidate.url()).pathname === "/api/v1/trips/preview"
  ));
  await page.getByRole("button", { name: "경로 계산" }).click();
  return JSON.parse((await request).postData() || "{}") as Record<string, unknown>;
}

// Mutation caught: changing a quick choice without recalculating the visible end.
test("five_hour_duration_updates_end_time", async ({ page }) => {
  await installMockApi(page);
  await page.goto("/");
  await page.locator("#starts-date").fill("2026-08-10");
  await page.locator("#starts-time").fill("09:15");

  await page.getByRole("button", { name: "5시간" }).click();

  await expect(page.locator("#returns-date")).toHaveValue("2026-08-10");
  await expect(page.locator("#returns-time")).toHaveValue("14:15");
  await expect(page.locator("#duration-hours")).toHaveValue("5");
});

// Mutation caught: applying local duration arithmetic through UTC ISO slicing.
test("duration_rolls_end_into_next_date", async ({ page }) => {
  await installMockApi(page);
  await page.goto("/");
  await page.locator("#starts-date").fill("2026-08-10");
  await page.locator("#starts-time").fill("22:30");
  await page.locator("#duration-hours").fill("4");
  await page.locator("#duration-minutes").fill("0");

  await expect(page.locator("#returns-date")).toHaveValue("2026-08-11");
  await expect(page.locator("#returns-time")).toHaveValue("02:30");
});

// Mutation caught: treating manual end changes as cosmetic and retaining stale duration.
test("manual_end_change_updates_duration", async ({ page }) => {
  await installMockApi(page);
  await page.goto("/");
  await page.locator("#starts-date").fill("2026-08-10");
  await page.locator("#starts-time").fill("09:00");
  await page.locator("#returns-date").fill("2026-08-10");
  await page.locator("#returns-time").evaluate((element) => {
    element.value = "14:45";
    element.dispatchEvent(new Event("input", { bubbles: true }));
  });

  await expect(page.locator("#duration-hours")).toHaveValue("5");
  await expect(page.locator("#duration-minutes")).toHaveValue("45");
});

// Mutation caught: moving a start date/time while leaving the old end behind.
test("start_change_preserves_duration", async ({ page }) => {
  await installMockApi(page);
  await page.goto("/");
  await page.locator("#starts-date").fill("2026-08-10");
  await page.locator("#starts-time").fill("09:00");
  await page.getByRole("button", { name: "5시간" }).click();
  await page.locator("#starts-time").fill("23:00");

  await expect(page.locator("#returns-date")).toHaveValue("2026-08-11");
  await expect(page.locator("#returns-time")).toHaveValue("04:00");
  await expect(page.locator("#duration-hours")).toHaveValue("5");
});

// Mutation caught: accepting a zero/one-minute or more-than-24-hour calendar span.
test("one_minute_or_over_twenty_four_hours_blocks_submit", async ({ page }) => {
  await installMockApi(page);
  await page.goto("/");
  await selectTripInputs(page);
  await expect(page.getByRole("button", { name: "경로 계산" })).toBeEnabled();
  await page.locator("#duration-hours").click();
  await page.locator("#duration-hours").press("ControlOrMeta+A");
  await page.locator("#duration-hours").press("0");
  await page.locator("#duration-minutes").click();
  await page.locator("#duration-minutes").press("ControlOrMeta+A");
  await page.locator("#duration-minutes").press("1");
  await expect(page.getByRole("button", { name: "경로 계산" })).toBeDisabled();

  await page.locator("#duration-hours").fill("24");
  await page.locator("#duration-minutes").fill("1");
  await expect(page.getByRole("button", { name: "경로 계산" })).toBeDisabled();
});

// Boundary coverage: the reviewed inclusive duration limits remain calculable.
test("two_and_1440_minute_durations_enable_exact_payloads", async ({ page }) => {
  await installMockApi(page);
  await page.goto("/");
  await selectTripInputs(page);
  await page.locator("#starts-date").fill("2026-08-10");
  await page.locator("#starts-time").fill("09:00");

  await page.locator("#duration-hours").fill("0");
  await page.locator("#duration-minutes").fill("2");
  await expect(page.getByRole("button", { name: "경로 계산" })).toBeEnabled();
  const twoMinutePayload = await submitAndReadPayload(page);
  expect(twoMinutePayload.startsAt).toBe("2026-08-10T09:00:00+09:00");
  expect(twoMinutePayload.endsAt).toBe("2026-08-10T09:02:00+09:00");

  await page.locator("#duration-hours").fill("24");
  await page.locator("#duration-minutes").fill("0");
  await expect(page.getByRole("button", { name: "경로 계산" })).toBeEnabled();
  const fullDayPayload = await submitAndReadPayload(page);
  expect(fullDayPayload.startsAt).toBe("2026-08-10T09:00:00+09:00");
  expect(fullDayPayload.endsAt).toBe("2026-08-11T09:00:00+09:00");

  await page.locator("#duration-hours").fill("0");
  await page.locator("#duration-minutes").fill("1");
  await expect(page.getByRole("button", { name: "경로 계산" })).toBeDisabled();
  await page.locator("#duration-hours").fill("24");
  await page.locator("#duration-minutes").fill("1");
  await expect(page.getByRole("button", { name: "경로 계산" })).toBeDisabled();
});

// Mutation caught: serializing a display label, legacy field, or an incorrect enum.
test("each_trip_pattern_sends_exact_enum_and_no_legacy_fields", async ({ page }) => {
  const payloads: Record<string, unknown>[] = [];
  await installMockApi(page, { previewForPayload: (payload) => {
    payloads.push(payload);
    return readFixture<Record<string, unknown>>("preview.json");
  } });
  await page.goto("/");
  await selectTripInputs(page);

  for (const [label, expected] of [
    ["일반 왕복", "ROUND_TRIP"],
    ["일정 후 퇴근", "OUTBOUND_ONLY_END_AFTER_SCHEDULE"],
    ["바로 출근 후 근무지 복귀", "RETURN_ONLY_DIRECT_TO_DESTINATION"],
  ]) {
    await page.getByRole("radio", { name: label }).check();
    const payload = await submitAndReadPayload(page);
    expect(payload.tripPattern).toBe(expected);
    expect(payload).not.toHaveProperty("returnsAt");
    expect(payload).not.toHaveProperty("policyProfile");
    expect(payload).not.toHaveProperty("tripType");
  }
  expect(payloads).toHaveLength(3);
});

// Mutation caught: keeping the old return-home terminology while serializing a one-way trip.
test("one_way_uses_approved_trip_end_labels_and_ends_at_payload", async ({ page }) => {
  await installMockApi(page);
  await page.goto("/");
  await selectTripInputs(page);
  await page.locator("#starts-date").fill("2026-08-10");
  await page.locator("#starts-time").fill("09:00");
  await page.getByRole("button", { name: "5시간" }).click();
  await page.getByRole("radio", { name: "일정 후 퇴근" }).check();

  await expect(page.getByText("출장 종료 일시", { exact: true })).toBeVisible();
  await expect(page.getByLabel("출장 종료 날짜")).toHaveValue("2026-08-10");
  await expect(page.getByLabel("출장 종료 시간")).toHaveValue("14:00");
  const payload = await submitAndReadPayload(page);
  expect(payload.endsAt).toBe("2026-08-10T14:00:00+09:00");
  expect(payload.tripPattern).toBe("OUTBOUND_ONLY_END_AFTER_SCHEDULE");
  expect(payload).not.toHaveProperty("returnsAt");
  expect(payload).not.toHaveProperty("tripType");
});

// Mutation caught: collapsing directional routes into one flat, unlabeled section.
test("round_trip_renders_two_route_leg_sections_and_two_selected_polylines", async ({ page }) => {
  await installMockApi(page);
  await page.goto("/");
  await completePublicOfficialTrip(page);

  await expect(page.locator("[data-route-direction='OUTBOUND']")).toContainText("가는 길");
  await expect(page.locator("[data-route-direction='RETURN']")).toContainText("돌아오는 길");
  await expect(page.locator("[data-route-id='OUTBOUND:car-1']")).toHaveAttribute("aria-current", "true");
  await expect(page.locator("[data-route-id='RETURN:car-1']")).toHaveAttribute("aria-current", "true");
  await expect(page.locator("#map")).toHaveAttribute("data-active-routes", "OUTBOUND:car-1 RETURN:car-1");
});

// Mutation caught: using the provider route ID alone as a map-selection key.
test("same_route_id_in_outbound_and_return_remains_independently_selectable", async ({ page }) => {
  await installMockApi(page);
  await page.goto("/");
  await completePublicOfficialTrip(page);

  await page.locator("[data-route-id='RETURN:walk-1']").click();
  await expect(page.locator("[data-route-id='OUTBOUND:car-1']")).toHaveAttribute("aria-current", "true");
  await expect(page.locator("[data-route-id='RETURN:walk-1']")).toHaveAttribute("aria-current", "true");
  await expect(page.locator("#map")).toHaveAttribute("data-active-routes", "OUTBOUND:car-1 RETURN:walk-1");
});

// Mutation caught: inventing a return display section for one-way-outbound trips.
test("outbound_only_renders_no_return_leg", async ({ page }) => {
  const preview = readFixture<Record<string, unknown>>("preview.json");
  preview.tripPattern = "OUTBOUND_ONLY_END_AFTER_SCHEDULE";
  preview.routeLegs = (preview.routeLegs as unknown[]).slice(0, 1);
  await installMockApi(page, { preview });
  await page.goto("/");
  await selectTripInputs(page);
  await page.getByRole("radio", { name: "일정 후 퇴근" }).check();
  await submitAndReadPayload(page);

  await expect(page.locator("[data-route-direction='OUTBOUND']")).toBeVisible();
  await expect(page.locator("[data-route-direction='RETURN']")).toHaveCount(0);
});

// Mutation caught: inventing an outbound display section for one-way-return trips.
test("return_only_renders_no_outbound_leg", async ({ page }) => {
  const preview = readFixture<Record<string, unknown>>("preview.json");
  preview.tripPattern = "RETURN_ONLY_DIRECT_TO_DESTINATION";
  preview.routeLegs = (preview.routeLegs as unknown[]).slice(1);
  await installMockApi(page, { preview });
  await page.goto("/");
  await selectTripInputs(page);
  await page.getByRole("radio", { name: "바로 출근 후 근무지 복귀" }).check();
  await submitAndReadPayload(page);

  await expect(page.locator("[data-route-direction='RETURN']")).toBeVisible();
  await expect(page.locator("[data-route-direction='OUTBOUND']")).toHaveCount(0);
});

// Mutation caught: allowing callers to choose the server-owned policy.
test("fixed_policy_badge_replaces_policy_selector", async ({ page }) => {
  await installMockApi(page);
  await page.goto("/");

  await expect(page.locator("#policy-profile")).toHaveCount(0);
  await expect(page.locator("#policy-disclosure")).toContainText("서울특별시교육청 공무원 여비 기준");
  await expect(page.locator("#policy-disclosure")).toContainText("Kakao 로그인은 재직 확인이 아닙니다");
  await expect(page.locator("#policy-disclosure select")).toHaveCount(0);
});

// Mutation caught: calling one-way/missing evidence a round-trip distance.
test("distance_basis_copy_never_labels_one_way_or_missing_evidence_as_round_trip", async ({ page }) => {
  const oneWay = readFixture<Record<string, unknown>>("preview.json");
  oneWay.classificationDistanceBasis = "ONE_WAY_LOWER_BOUND";
  oneWay.classificationDistanceMeters = 6_400;
  const missing = readFixture<Record<string, unknown>>("preview.json");
  missing.classificationDistanceBasis = null;
  missing.classificationDistanceMeters = null;
  const previews = [oneWay, missing];
  await installMockApi(page, { previewForPayload: () => previews.shift()! });
  await page.goto("/");
  await selectTripInputs(page);

  await submitAndReadPayload(page);
  await expect(page.locator("#classification-distance")).toHaveText("편도 확인 거리(하한) 6.4km");
  await expect(page.locator("#classification-distance")).not.toContainText("왕복");
  await submitAndReadPayload(page);
  await expect(page.locator("#classification-distance")).toHaveText("거리 근거 없음 · 지급액 검토 필요");
  await expect(page.locator("#classification-distance")).not.toContainText("왕복");
});

// Mutation caught: reading legacy/nested settings instead of the reviewed settings response.
test("settings_defaults_populate_every_trip_input_from_server_field_names", async ({ page }) => {
  await installMockApi(page);
  await page.goto("/");

  const settings = await page.evaluate(async () => {
    const { createTripForm } = await import("/static/trip-form.js");
    const calls: Array<Record<string, unknown>> = [];
    let selectedOrigin: Record<string, unknown> | null = null;
    const form = createTripForm({
      originPicker: {
        selected: () => selectedOrigin,
        clear: () => { selectedOrigin = null; },
        selectResolved: (candidate: Record<string, unknown>) => {
          selectedOrigin = candidate;
          return true;
        },
      },
      destinationPicker: { clear: () => {}, selected: () => null },
      schedule: {
        applyDefaults: (value: Record<string, unknown>) => calls.push(value),
        valid: () => true,
      },
      elements: {
        form: document.querySelector<HTMLFormElement>("#trip-form")!,
        vehicleUse: document.querySelector<HTMLSelectElement>("#vehicle-use")!,
        fuelType: document.querySelector<HTMLSelectElement>("#fuel-type")!,
        efficiency: document.querySelector<HTMLInputElement>("#efficiency")!,
        parkingCost: document.querySelector<HTMLInputElement>("#parking-cost")!,
        otherTrips: document.querySelector<HTMLInputElement>("#other-trips")!,
        previousAllowance: document.querySelector<HTMLInputElement>("#previous-allowance")!,
      },
    });
    form.applySettings({
      defaultDurationMinutes: 5 * 60,
      defaultTripPattern: "OUTBOUND_ONLY_END_AFTER_SCHEDULE",
      efficiencyKmPerLiter: 14.5,
      fuelType: "DIESEL",
      parkingCostKrw: 3_000,
      vehicleUse: "PRIVATE",
    }, { siteId: "test-neis:current" });
    return {
      calls,
      efficiency: document.querySelector<HTMLInputElement>("#efficiency")!.value,
      fuelType: document.querySelector<HTMLSelectElement>("#fuel-type")!.value,
      origin: selectedOrigin,
      parkingCost: document.querySelector<HTMLInputElement>("#parking-cost")!.value,
      vehicleUse: document.querySelector<HTMLSelectElement>("#vehicle-use")!.value,
    };
  });

  expect(settings).toEqual({
    calls: [{ durationMinutes: 300, tripPattern: "OUTBOUND_ONLY_END_AFTER_SCHEDULE" }],
    efficiency: "14.5",
    fuelType: "DIESEL",
    origin: { siteId: "test-neis:current" },
    parkingCost: "3000",
    vehicleUse: "PRIVATE",
  });
});

// Mutation caught: treating an encrypted history draft like a mutable preview payload.
test("recalculation_draft_starts_a_fresh_destination_search_from_stored_labels", async ({ page }) => {
  await installMockApi(page);
  await page.goto("/");

  const applied = await page.evaluate(async () => {
    const { createTripForm } = await import("/static/trip-form.js");
    const scheduleCalls: Array<Record<string, unknown>> = [];
    const destinationQueries: string[] = [];
    let selectedOrigin: Record<string, unknown> | null = null;
    const form = createTripForm({
      originPicker: {
        selected: () => selectedOrigin,
        clear: () => { selectedOrigin = null; },
        selectResolved: (candidate: Record<string, unknown>) => {
          selectedOrigin = candidate;
          return true;
        },
      },
      destinationPicker: {
        clear: () => {},
        selected: () => null,
        setQueryAndSearch: (query: string) => destinationQueries.push(query),
      },
      schedule: {
        applyDraft: (value: Record<string, unknown>) => scheduleCalls.push(value),
        valid: () => true,
      },
      elements: {
        form: document.querySelector<HTMLFormElement>("#trip-form")!,
        vehicleUse: document.querySelector<HTMLSelectElement>("#vehicle-use")!,
        fuelType: document.querySelector<HTMLSelectElement>("#fuel-type")!,
        efficiency: document.querySelector<HTMLInputElement>("#efficiency")!,
        parkingCost: document.querySelector<HTMLInputElement>("#parking-cost")!,
        otherTrips: document.querySelector<HTMLInputElement>("#other-trips")!,
        previousAllowance: document.querySelector<HTMLInputElement>("#previous-allowance")!,
      },
    });
    const outcome = form.applyRecalculationDraft({
      destinationAddress: "서울특별시 중구 세종대로 110",
      destinationName: "서울특별시청",
      endsAt: "2026-08-10T14:00:00+09:00",
      startsAt: "2026-08-10T09:00:00+09:00",
      tripPattern: "ROUND_TRIP",
    }, { siteId: "test-neis:current" });
    return { destinationQueries, outcome, scheduleCalls, selectedOrigin };
  });

  expect(applied).toEqual({
    destinationQueries: ["서울특별시청"],
    outcome: { destinationSearchStarted: true, originResolved: true },
    scheduleCalls: [{
      endsAt: "2026-08-10T14:00:00+09:00",
      startsAt: "2026-08-10T09:00:00+09:00",
      tripPattern: "ROUND_TRIP",
    }],
    selectedOrigin: { siteId: "test-neis:current" },
  });
});

// Mutation caught: retaining a previous history origin when the current origin no longer resolves.
test("recalculation_draft_clears_stale_origin_before_new_destination_authority", async ({ page }) => {
  await installMockApi(page);
  await page.goto("/");

  const applied = await page.evaluate(async () => {
    const { createTripForm } = await import("/static/trip-form.js");
    let selectedOrigin: Record<string, unknown> | null = { siteId: "stale:origin" };
    let selectedDestination: Record<string, unknown> | null = { name: "stale destination" };
    const clearCalls: string[] = [];
    const form = createTripForm({
      originPicker: {
        clear: () => {
          clearCalls.push("origin");
          selectedOrigin = null;
        },
        selected: () => selectedOrigin,
        selectResolved: (candidate: Record<string, unknown>) => {
          selectedOrigin = candidate;
          return true;
        },
      },
      destinationPicker: {
        clear: () => {
          clearCalls.push("destination");
          selectedDestination = null;
        },
        selected: () => selectedDestination,
        setQueryAndSearch: () => Promise.resolve(),
      },
      schedule: {
        applyDraft: () => {},
        valid: () => true,
      },
      elements: {
        form: document.querySelector<HTMLFormElement>("#trip-form")!,
        vehicleUse: document.querySelector<HTMLSelectElement>("#vehicle-use")!,
        fuelType: document.querySelector<HTMLSelectElement>("#fuel-type")!,
        efficiency: document.querySelector<HTMLInputElement>("#efficiency")!,
        parkingCost: document.querySelector<HTMLInputElement>("#parking-cost")!,
        otherTrips: document.querySelector<HTMLInputElement>("#other-trips")!,
        previousAllowance: document.querySelector<HTMLInputElement>("#previous-allowance")!,
      },
    });
    const outcome = form.applyRecalculationDraft({ destinationName: "새 출장지" }, null);
    selectedDestination = { name: "새 출장지" };
    return { clearCalls, outcome, previewEligible: form.valid(), selectedOrigin };
  });

  expect(applied).toEqual({
    clearCalls: ["origin", "destination"],
    outcome: { destinationSearchStarted: true, originResolved: false },
    previewEligible: false,
    selectedOrigin: null,
  });
});
