import { api, ApiError } from "./api.js";
import { createDestinationPicker } from "./destination-picker.js";
import { createInstitutionPicker } from "./institution-picker.js";
import { KakaoMapController } from "./kakao-map.js";

const $ = (selector) => document.querySelector(selector);
const controls = {
  form: $("#trip-form"),
  origin: $("#origin-search"),
  destination: $("#destination-search"),
  originResults: $("#origin-results"),
  destinationResults: $("#destination-results"),
  originNote: $("#origin-selection"),
  destinationNote: $("#destination-selection"),
  originSearchRetry: $("#origin-search-retry"),
  destinationSearchRetry: $("#destination-search-retry"),
  originLoadMore: $("#origin-load-more"),
  facetsStatus: $("#institution-facets-status"),
  facetsRetry: $("#institution-facets-retry"),
  formError: $("#form-error"),
  results: $("#results"),
  routeList: $("#route-list"),
  routeCount: $("#route-count"),
  calculateButton: $("#calculate-button"),
  mapCollapse: $("#map-collapse"),
  filtersToggle: $("#institution-filters-toggle"),
  otherTrips: $("#other-trips"),
  previousAllowanceField: $("#previous-allowance-field"),
  previousAllowance: $("#previous-allowance"),
};

const state = {
  origin: null,
  destination: null,
  preview: null,
  activeRouteIds: {},
  sort: "time",
};

const map = new KakaoMapController($("#map"), $("#map-status"));
const modeName = { TRANSIT: "대중교통", CAR: "자동차", WALK: "도보" };
const directionName = { OUTBOUND: "가는 길", RETURN: "돌아오는 길" };
const bestName = {
  fastestRouteId: "최단시간",
  shortestRouteId: "최단거리",
  cheapestRouteId: "최저비용",
};

const institutionPicker = createInstitutionPicker({
  api,
  map,
  elements: {
    input: controls.origin,
    listbox: controls.originResults,
    status: controls.originNote,
    retryButton: controls.originSearchRetry,
    loadMoreButton: controls.originLoadMore,
    facetsStatus: controls.facetsStatus,
    facetsRetryButton: controls.facetsRetry,
    filtersToggle: controls.filtersToggle,
    filtersContainer: $("#institution-filters"),
    filters: {
      institutionType: $("#institution-type"),
      foundationType: $("#foundation-type"),
      educationOffice: $("#education-office"),
      district: $("#district"),
    },
  },
  onSelectionChange: (item) => {
    state.origin = item;
    updateCalculateAvailability();
    setFormError();
  },
});

const destinationPicker = createDestinationPicker({
  api,
  map,
  elements: {
    input: controls.destination,
    listbox: controls.destinationResults,
    status: controls.destinationNote,
    retryButton: controls.destinationSearchRetry,
  },
  onSelectionChange: (item) => {
    state.destination = item;
    updateCalculateAvailability();
    setFormError();
  },
});
function formatMoney(value) {
  return value == null ? "비용 정보 없음" : `${new Intl.NumberFormat("ko-KR").format(value)}원`;
}

function formatDistance(value) {
  return value >= 1000 ? `${(value / 1000).toFixed(1)}km` : `${value}m`;
}

function formatDuration(seconds) {
  const minutes = Math.round(seconds / 60);
  return minutes >= 60 ? `${Math.floor(minutes / 60)}시간 ${minutes % 60}분` : `${minutes}분`;
}

function errorMessage(error) {
  if (error instanceof ApiError) {
    return error.message === "PLACE_PROVIDER_UNAVAILABLE"
      ? "출장지 검색 서비스를 잠시 이용할 수 없습니다."
      : "요청을 처리하지 못했습니다. 잠시 후 다시 시도해 주세요.";
  }
  return "네트워크 연결을 확인한 뒤 다시 시도해 주세요.";
}

function setFormError(message = "") {
  controls.formError.textContent = message;
  controls.formError.hidden = !message;
}

function updateCalculateAvailability() {
  controls.calculateButton.disabled = !(state.origin && state.destination);
}

function setDefaultDates() {
  const date = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Seoul",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date());
  $("#starts-date").value = date;
  $("#returns-date").value = date;
}

function requestPayload() {
  return {
    originSiteId: state.origin.siteId,
    destination: {
      name: state.destination.name,
      address: state.destination.roadAddress || state.destination.lotAddress,
      latitude: state.destination.latitude,
      longitude: state.destination.longitude,
    },
    startsAt: `${$("#starts-date").value}T${$("#starts-time").value}:00+09:00`,
    endsAt: `${$("#returns-date").value}T${$("#returns-time").value}:00+09:00`,
    tripPattern: "ROUND_TRIP",
    vehicleUse: $("#vehicle-use").value,
    carAssumptions: {
      fuelType: $("#fuel-type").value,
      efficiencyKmPerLiter: Number($("#efficiency").value),
      parkingCostKrw: Number($("#parking-cost").value),
    },
    hasOtherLocalTripsToday: $("#other-trips").checked,
    previousAllowanceKrw: controls.otherTrips.checked
      ? Number(controls.previousAllowance.value)
      : 0,
  };
}

function sortRoutes(routes) {
  const keys = {
    time: (route) => [route.durationSeconds, route.distanceMeters],
    distance: (route) => [route.distanceMeters, route.durationSeconds],
    cost: (route) => [route.mobilityCostKrw == null ? Infinity : route.mobilityCostKrw, route.durationSeconds],
  };
  return [...routes].sort((first, second) => {
    const [firstPrimary, firstSecondary] = keys[state.sort](first);
    const [secondPrimary, secondSecondary] = keys[state.sort](second);
    return firstPrimary - secondPrimary || firstSecondary - secondSecondary;
  });
}

function bestLabelsFor(route, leg) {
  return Object.entries(bestName)
    .filter(([key]) => leg.best[key] === route.id)
    .map(([, label]) => label);
}

function routeKey(direction, routeId) {
  return `${direction}:${routeId}`;
}

function directionalRoutes({ sorted = false } = {}) {
  return state.preview.routeLegs.flatMap((leg) =>
    (sorted ? sortRoutes(leg.routes) : leg.routes).map((route) => ({
      direction: leg.direction,
      key: routeKey(leg.direction, route.id),
      leg,
      route,
    })),
  );
}

function warningText(warning) {
  return {
    PARKING_COST_ESTIMATED: "주차비는 예상값입니다",
    DISTANCE_EVIDENCE_UNAVAILABLE: "분류 경로 정보 확인 필요",
    PARTIAL_MOBILITY_COST: "이동비 일부 정보 확인 필요",
  }[warning] || "경로 정보 일부를 확인해 주세요";
}

function renderRoutes() {
  const entries = directionalRoutes({ sorted: true });
  controls.routeCount.textContent = `${entries.length}개`;
  controls.routeList.replaceChildren();
  entries.forEach(({ direction, key, leg, route }) => {
    const card = document.createElement("button");
    const selected = key === state.activeRouteIds[direction];
    card.type = "button";
    const routeMode = { TRANSIT: "transit", CAR: "car", WALK: "walk" }[route.mode] || "unknown";
    card.className = `route-card mode-${routeMode}`;
    card.dataset.routeId = key;
    card.setAttribute("aria-current", String(selected));
    const top = document.createElement("span");
    top.className = "route-top";
    const title = document.createElement("strong");
    title.textContent = `${directionName[direction] || direction} · ${modeName[route.mode] || "이동 경로"} · ${formatDuration(route.durationSeconds)}`;
    const badges = document.createElement("span");
    badges.className = "route-badges";
    bestLabelsFor(route, leg).forEach((label) => {
      const badge = document.createElement("span");
      badge.className = "route-badge";
      badge.textContent = label;
      badges.append(badge);
    });
    top.append(title, badges);

    const metrics = document.createElement("span");
    metrics.className = "route-metrics";
    [
      ["소요시간", formatDuration(route.durationSeconds)],
      ["거리", formatDistance(route.distanceMeters)],
      ["예상 이동비", formatMoney(route.mobilityCostKrw)],
    ].forEach(([label, value]) => {
      const metric = document.createElement("span");
      const metricLabel = document.createElement("span");
      const metricValue = document.createElement("b");
      metricLabel.textContent = label;
      metricValue.textContent = value;
      metric.append(metricLabel, metricValue);
      metrics.append(metric);
    });

    card.append(top, metrics);
    if (route.warnings.length) {
      const warning = document.createElement("span");
      warning.className = "route-warning";
      warning.textContent = `⚠ ${route.warnings.map(warningText).join(" · ")}`;
      card.append(warning);
    }
    const source = document.createElement("span");
    source.className = "route-source";
    source.textContent = `${route.source} 기준`;
    card.append(source);
    card.addEventListener("click", () => selectRoute(direction, key));
    controls.routeList.append(card);
  });
}

function selectRoute(direction, routeId) {
  state.activeRouteIds[direction] = routeId;
  map.setActiveRoutes(Object.values(state.activeRouteIds));
  renderRoutes();
}

function renderSummary() {
  const {
    coverage,
    classification,
    classificationDistanceMeters,
    classificationDistanceBasis,
    mobilityCost,
    allowance,
  } = state.preview;
  $("#coverage-status").textContent = coverage.status === "SEOUL" ? "서울 경계 내" : coverage.status;
  $("#classification-result").textContent = {
    LOCAL: "관내출장",
    NON_LOCAL_EXPECTED: "관외 예상",
  }[classification] || "판정 보류";
  $("#classification-distance").textContent = classification === "NON_LOCAL_EXPECTED"
    ? "기관의 최종 관외 판단과 지급 기준을 확인하세요."
    : classificationDistanceMeters == null
      ? "분류 경로 확인 필요"
      : classificationDistanceBasis === "ONE_WAY_LOWER_BOUND"
        ? `분류 편도 하한 ${formatDistance(classificationDistanceMeters)}`
        : `분류 왕복 ${formatDistance(classificationDistanceMeters)}`;
  $("#mobility-cost").textContent = formatMoney(mobilityCost.amountKrw);
  $("#mobility-status").textContent = mobilityCost.status === "ESTIMATED" ? "예상값" : "경로 기준";
  const allowanceNeedsReview = allowance.status === "REVIEW_REQUIRED" || allowance.amountKrw == null;
  $("#allowance-amount").textContent = allowanceNeedsReview ? "여비 판정 보류" : formatMoney(allowance.amountKrw);
  $("#allowance-status").textContent = allowanceNeedsReview ? "신분·규정을 확인하세요" : "지급 확정액 아님";
}

function renderPreview() {
  state.activeRouteIds = Object.fromEntries(
    state.preview.routeLegs.flatMap((leg) => {
      const routeId = leg.best.fastestRouteId || leg.routes[0]?.id;
      return routeId ? [[leg.direction, routeKey(leg.direction, routeId)]] : [];
    }),
  );
  controls.results.hidden = false;
  renderSummary();
  renderRoutes();
  map.showRoutes({
    origin: state.preview.origin,
    destination: state.destination,
    routes: directionalRoutes().map(({ key, route }) => ({ ...route, id: key })),
    classificationPath: state.preview.classificationPath,
  });
  map.setActiveRoutes(Object.values(state.activeRouteIds));
}

async function calculate(event) {
  event.preventDefault();
  if (!state.origin || !state.destination) {
    setFormError("출발 기관과 출장지를 모두 선택하세요.");
    return;
  }
  if (!controls.form.checkValidity()) {
    setFormError("출발·복귀 일시와 자동차 이용 가정을 확인하세요.");
    return;
  }
  const button = controls.calculateButton;
  button.disabled = true;
  button.textContent = "경로를 계산하고 있습니다";
  setFormError();
  try {
    state.preview = await api.preview(requestPayload());
    renderPreview();
  } catch (error) {
    setFormError(errorMessage(error));
  } finally {
    updateCalculateAvailability();
    button.textContent = "▣ 경로 계산";
  }
}

function bindSortTabs() {
  document.querySelectorAll("[data-sort]").forEach((tab) => {
    tab.addEventListener("click", () => {
      state.sort = tab.dataset.sort;
      document.querySelectorAll("[data-sort]").forEach((item) => item.setAttribute("aria-selected", String(item === tab)));
      renderRoutes();
    });
  });
}

function bindMapControls() {
  [["#seoul-layer", "seoul"], ["#support-layer", "support"]].forEach(([selector, name]) => {
    $(selector).addEventListener("change", (event) => map.setBoundary(name, event.target.checked, api.geodata));
  });
  controls.mapCollapse.addEventListener("click", () => {
    const expanded = controls.mapCollapse.getAttribute("aria-expanded") !== "true";
    controls.mapCollapse.setAttribute("aria-expanded", String(expanded));
    controls.mapCollapse.textContent = expanded ? "지도 접기" : "지도 펼치기";
    $(".map-stage").classList.toggle("is-expanded", expanded);
  });
}

function updatePreviousAllowanceControl() {
  const enabled = controls.otherTrips.checked;
  controls.previousAllowanceField.hidden = !enabled;
  controls.previousAllowance.disabled = !enabled;
  if (!enabled) controls.previousAllowance.value = "0";
}

async function initialize() {
  setDefaultDates();
  institutionPicker.initialize();
  destinationPicker.initialize();
  controls.form.addEventListener("submit", calculate);
  bindSortTabs();
  bindMapControls();
  controls.otherTrips.addEventListener("change", updatePreviousAllowanceControl);
  updatePreviousAllowanceControl();
  updateCalculateAvailability();
  try {
    const bootstrap = await api.bootstrap();
    await map.initialize(bootstrap.map.javascriptKey);
  } catch {
    map.setStatus("지도 설정을 확인하지 못했습니다. 입력과 경로 결과는 계속 사용할 수 있습니다.");
  }
}

window.addEventListener("pagehide", () => {
  institutionPicker.destroy();
  destinationPicker.destroy();
}, { once: true });

initialize();
