import { api, ApiError } from "./api.js";
import { createAuthController } from "./auth.js";
import { createDestinationPicker } from "./destination-picker.js";
import { createInstitutionPicker } from "./institution-picker.js";
import { KakaoMapController } from "./kakao-map.js";
import { createHelpPanels } from "./help.js";
import { createHistoryPanel } from "./history.js";
import { createRouteResults } from "./route-results.js";
import { createScheduleController } from "./schedule.js";
import { createSettingsPanel } from "./settings.js";
import { createTripForm } from "./trip-form.js";

const $ = (selector) => document.querySelector(selector);
const ANONYMOUS_PUBLIC_SETTINGS = Object.freeze({
  defaultDurationMinutes: 4 * 60,
  defaultOriginSiteId: null,
  defaultTripPattern: "ROUND_TRIP",
  efficiencyKmPerLiter: 10,
  fuelType: "GASOLINE",
  parkingCostKrw: 0,
  routeSort: "time",
  vehicleUse: "NONE",
});
const controls = {
  authStatus: $("#auth-status"),
  allowanceAmount: $("#allowance-amount"),
  allowanceStatus: $("#allowance-status"),
  calculateButton: $("#calculate-button"),
  classificationDistance: $("#classification-distance"),
  classificationResult: $("#classification-result"),
  classificationWarning: $("#classification-warning"),
  coverageStatus: $("#coverage-status"),
  destination: $("#destination-search"),
  destinationNote: $("#destination-selection"),
  destinationResults: $("#destination-results"),
  destinationSearchRetry: $("#destination-search-retry"),
  durationHours: $("#duration-hours"),
  durationMinutes: $("#duration-minutes"),
  efficiency: $("#efficiency"),
  endDate: $("#returns-date"),
  endTime: $("#returns-time"),
  facetsRetry: $("#institution-facets-retry"),
  facetsStatus: $("#institution-facets-status"),
  filtersContainer: $("#institution-filters"),
  filtersToggle: $("#institution-filters-toggle"),
  form: $("#trip-form"),
  formError: $("#form-error"),
  fuelType: $("#fuel-type"),
  helpButton: $("#help-button"),
  helpDialog: $("#help-dialog"),
  historyButton: $("#history-button"),
  historyCloseButton: $("#history-close-button"),
  historyDeleteAllButton: $("#history-delete-all"),
  historyDetailCloseButton: $("#history-detail-close-button"),
  historyDetailDeleteButton: $("#history-detail-delete"),
  historyDetailDialog: $("#history-detail-dialog"),
  historyDetailRecalculateButton: $("#history-detail-recalculate"),
  historyDetailContent: $("#history-detail-content"),
  historyDialog: $("#history-dialog"),
  historyLoadMore: $("#history-load-more"),
  historyRows: $("#history-rows"),
  historyStatus: $("#history-status"),
  historyRecalculationStatus: $("#history-recalculation-status"),
  loginButton: $("#login-button"),
  logoutButton: $("#logout-button"),
  map: $("#map"),
  mapCollapse: $("#map-collapse"),
  mapStatus: $("#map-status"),
  menuButton: $("#mobile-menu-button"),
  mobilityCost: $("#mobility-cost"),
  mobilityStatus: $("#mobility-status"),
  mobilityWarning: $("#mobility-warning"),
  origin: $("#origin-search"),
  originLoadMore: $("#origin-load-more"),
  originNote: $("#origin-selection"),
  originResults: $("#origin-results"),
  originSearchRetry: $("#origin-search-retry"),
  otherTrips: $("#other-trips"),
  parkingCost: $("#parking-cost"),
  previousAllowance: $("#previous-allowance"),
  previousAllowanceField: $("#previous-allowance-field"),
  policyButton: $("#policy-button"),
  policyDialog: $("#policy-dialog"),
  privateAuthDialog: $("#private-auth-dialog"),
  privateAuthCloseButton: $("#private-auth-close-button"),
  privateAuthLoginButton: $("#private-auth-login-button"),
  quickDurationButtons: [...document.querySelectorAll("[data-duration-hours]")],
  results: $("#results"),
  routeCount: $("#route-count"),
  routeList: $("#route-list"),
  startDate: $("#starts-date"),
  startTime: $("#starts-time"),
  tripPattern: [...document.querySelectorAll("input[name='trip-pattern']")],
  utilityNav: $("#utility-nav"),
  settingsButton: $("#settings-button"),
  settingsDialog: $("#settings-dialog"),
  vehicleUse: $("#vehicle-use"),
  deleteDataDialog: $("#delete-data-dialog"),
  boundaries: {
    seoul: $("#seoul-layer"),
    support: $("#support-layer"),
  },
  filters: {
    district: $("#district"),
    educationOffice: $("#education-office"),
    foundationType: $("#foundation-type"),
    institutionType: $("#institution-type"),
  },
};

const map = new KakaoMapController(controls.map, controls.mapStatus);
let tripForm;
let previewRevision = 0;
let destroyMobileMenu = () => {};
let authController;
let historyPanel;
let settingsPanel;
let appliedAuthenticatedSettings = false;
let settingsRevision = 0;

const helpPanels = createHelpPanels({
  api,
  helpButton: controls.helpButton,
  helpDialog: controls.helpDialog,
  policyButton: controls.policyButton,
  policyDialog: controls.policyDialog,
});

function setFormError(message = "") {
  controls.formError.textContent = message;
  controls.formError.hidden = !message;
}

function updateCalculateAvailability() {
  controls.calculateButton.disabled = !tripForm?.valid();
}

function invalidatePreview() {
  previewRevision += 1;
  routeResults.clear();
  controls.calculateButton.textContent = "▣ 경로 계산";
}

function setRouteSort(sort) {
  routeResults.setSort(sort);
  document.querySelectorAll("[data-sort]").forEach((tab) => {
    tab.setAttribute("aria-selected", String(tab.dataset.sort === sort));
  });
}

function resetAnonymousPublicSettings() {
  institutionPicker.clear();
  tripForm.applySettings(ANONYMOUS_PUBLIC_SETTINGS);
  setRouteSort(ANONYMOUS_PUBLIC_SETTINGS.routeSort);
  invalidatePreview();
  updateCalculateAvailability();
  appliedAuthenticatedSettings = false;
}

function seoulToday() {
  const parts = new Intl.DateTimeFormat("en-CA", {
    day: "2-digit",
    month: "2-digit",
    timeZone: "Asia/Seoul",
    year: "numeric",
  }).formatToParts(new Date());
  const value = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${value.year}-${value.month}-${value.day}`;
}

function setDefaultDates() {
  const date = seoulToday();
  controls.startDate.value = date;
  controls.endDate.value = date;
}

function errorMessage(error) {
  if (error instanceof ApiError) {
    return error.message === "PLACE_PROVIDER_UNAVAILABLE"
      ? "출장지 검색 서비스를 잠시 이용할 수 없습니다."
      : "요청을 처리하지 못했습니다. 잠시 후 다시 시도해 주세요.";
  }
  return "네트워크 연결을 확인한 뒤 다시 시도해 주세요.";
}

const schedule = createScheduleController({
  elements: {
    durationHours: controls.durationHours,
    durationMinutes: controls.durationMinutes,
    endDate: controls.endDate,
    endTime: controls.endTime,
    quickButtons: controls.quickDurationButtons,
    startDate: controls.startDate,
    startTime: controls.startTime,
    tripPattern: controls.tripPattern,
  },
  onValidityChange: updateCalculateAvailability,
});

const institutionPicker = createInstitutionPicker({
  api,
  map,
  elements: {
    facetsRetryButton: controls.facetsRetry,
    facetsStatus: controls.facetsStatus,
    filters: controls.filters,
    filtersContainer: controls.filtersContainer,
    filtersToggle: controls.filtersToggle,
    input: controls.origin,
    listbox: controls.originResults,
    loadMoreButton: controls.originLoadMore,
    retryButton: controls.originSearchRetry,
    status: controls.originNote,
  },
  onSelectionChange: () => {
    controls.historyRecalculationStatus.hidden = true;
    controls.historyRecalculationStatus.textContent = "";
    invalidatePreview();
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
    retryButton: controls.destinationSearchRetry,
    status: controls.destinationNote,
  },
  onSelectionChange: () => {
    invalidatePreview();
    updateCalculateAvailability();
    setFormError();
  },
});

const routeResults = createRouteResults({
  elements: {
    allowanceAmount: controls.allowanceAmount,
    allowanceStatus: controls.allowanceStatus,
    classificationDistance: controls.classificationDistance,
    classificationResult: controls.classificationResult,
    classificationWarning: controls.classificationWarning,
    coverageStatus: controls.coverageStatus,
    mobilityCost: controls.mobilityCost,
    mobilityStatus: controls.mobilityStatus,
    mobilityWarning: controls.mobilityWarning,
    results: controls.results,
    routeCount: controls.routeCount,
    routeList: controls.routeList,
  },
  map,
});

tripForm = createTripForm({
  destinationPicker,
  elements: {
    efficiency: controls.efficiency,
    form: controls.form,
    fuelType: controls.fuelType,
    onClearResult: routeResults.clear,
    otherTrips: controls.otherTrips,
    parkingCost: controls.parkingCost,
    previousAllowance: controls.previousAllowance,
    vehicleUse: controls.vehicleUse,
  },
  originPicker: institutionPicker,
  schedule,
});

async function applyAuthenticatedSettings({ authenticated }) {
  const revision = ++settingsRevision;
  historyPanel?.setAuthenticated(authenticated);
  settingsPanel?.setAuthenticated(authenticated);
  if (!authenticated) {
    if (appliedAuthenticatedSettings) resetAnonymousPublicSettings();
    return;
  }
  try {
    await settingsPanel?.load();
    if (revision !== settingsRevision || !authController.authenticated()) return;
  } catch {
    if (revision !== settingsRevision || !authController.authenticated()) return;
    controls.originNote.textContent = "기본 설정을 불러오지 못했습니다. 직접 입력해 계산할 수 있습니다.";
  }
}

authController = createAuthController({
  api,
  elements: {
    historyButton: controls.historyButton,
    loginButtons: [controls.loginButton, controls.privateAuthLoginButton],
    logoutButton: controls.logoutButton,
    privateCloseButton: controls.privateAuthCloseButton,
    privateDialog: controls.privateAuthDialog,
    settingsButton: controls.settingsButton,
    status: controls.authStatus,
  },
  onSessionChange: applyAuthenticatedSettings,
});

historyPanel = createHistoryPanel({
  api,
  elements: {
    button: controls.historyButton,
    closeButton: controls.historyCloseButton,
    deleteAllButton: controls.historyDeleteAllButton,
    detailCloseButton: controls.historyDetailCloseButton,
    detailDeleteButton: controls.historyDetailDeleteButton,
    detailContent: controls.historyDetailContent,
    detailDialog: controls.historyDetailDialog,
    detailRecalculateButton: controls.historyDetailRecalculateButton,
    dialog: controls.historyDialog,
    loadMore: controls.historyLoadMore,
    rows: controls.historyRows,
    status: controls.historyStatus,
  },
  onDraftApplied: () => {
    controls.historyRecalculationStatus.hidden = false;
    controls.historyRecalculationStatus.textContent = "출장지를 다시 선택하세요.";
    invalidatePreview();
    setFormError();
    updateCalculateAvailability();
    controls.destination.focus();
  },
  tripForm,
});

settingsPanel = createSettingsPanel({
  api,
  confirmationDialog: controls.deleteDataDialog,
  deleteMyData: () => authController.deleteMyData(),
  dialog: controls.settingsDialog,
  institutionPicker,
  isAuthenticated: () => authController.authenticated(),
  onSettingsApplied: (response) => {
    const settings = response.settings;
    appliedAuthenticatedSettings = true;
    tripForm.applySettings(settings, response.resolvedDefaultOrigin ?? null);
    setRouteSort(settings.routeSort);
    if (settings.defaultOriginSiteId && !response.resolvedDefaultOrigin) {
      controls.originNote.textContent = "기본 근무지를 다시 선택하세요.";
    }
    invalidatePreview();
    updateCalculateAvailability();
  },
  trigger: controls.settingsButton,
});

async function calculate(event) {
  event.preventDefault();
  if (!tripForm.valid()) {
    setFormError("출발 기관·출장지·일정과 자동차 이용 가정을 확인하세요.");
    updateCalculateAvailability();
    return;
  }
  const requestRevision = previewRevision;
  const requestOrigin = institutionPicker.selected();
  const requestDestination = destinationPicker.selected();
  const payload = tripForm.payload();
  if (!payload) return;
  const requestIsCurrent = () => (
    requestRevision === previewRevision
    && requestOrigin === institutionPicker.selected()
    && requestDestination === destinationPicker.selected()
  );
  controls.calculateButton.disabled = true;
  controls.calculateButton.textContent = "경로를 계산하고 있습니다";
  setFormError();
  try {
    const preview = await api.preview(payload);
    if (!requestIsCurrent()) return;
    routeResults.render(preview, requestDestination);
    if (!preview.warnings?.includes("HISTORY_NOT_SAVED")) {
      historyPanel.refreshForSavedPreview();
    }
  } catch (error) {
    if (!requestIsCurrent()) return;
    setFormError(errorMessage(error));
  } finally {
    if (!requestIsCurrent()) return;
    updateCalculateAvailability();
    controls.calculateButton.textContent = "▣ 경로 계산";
  }
}

function bindSortTabs() {
  document.querySelectorAll("[data-sort]").forEach((tab) => {
    tab.addEventListener("click", () => {
      setRouteSort(tab.dataset.sort);
    });
  });
}

function bindMapControls() {
  Object.entries(controls.boundaries).forEach(([name, input]) => {
    input.addEventListener("change", (event) => {
      map.setBoundary(name, event.target.checked, api.geodata);
    });
  });
  controls.mapCollapse.addEventListener("click", () => {
    const expanded = controls.mapCollapse.getAttribute("aria-expanded") !== "true";
    controls.mapCollapse.setAttribute("aria-expanded", String(expanded));
    controls.mapCollapse.textContent = expanded ? "지도 접기" : "지도 펼치기";
    $(".map-stage").classList.toggle("is-expanded", expanded);
  });
}

function bindMobileMenu() {
  const media = window.matchMedia("(max-width: 767px)");
  const setOpen = (open) => {
    const mobileOpen = media.matches && open;
    controls.menuButton.setAttribute("aria-expanded", String(mobileOpen));
    controls.utilityNav.classList.toggle("is-open", mobileOpen);
  };
  const onMenuClick = () => {
    if (!media.matches) return;
    setOpen(controls.menuButton.getAttribute("aria-expanded") !== "true");
  };
  const onMediaChange = () => setOpen(false);
  controls.menuButton.addEventListener("click", onMenuClick);
  media.addEventListener("change", onMediaChange);
  setOpen(false);
  return () => {
    controls.menuButton.removeEventListener("click", onMenuClick);
    media.removeEventListener("change", onMediaChange);
    setOpen(false);
  };
}

function updatePreviousAllowanceControl() {
  const enabled = controls.otherTrips.checked;
  controls.previousAllowanceField.hidden = !enabled;
  controls.previousAllowance.disabled = !enabled;
  if (!enabled) controls.previousAllowance.value = "0";
}

async function initialize() {
  setDefaultDates();
  schedule.applyDefaults({ durationMinutes: 4 * 60, tripPattern: "ROUND_TRIP" });
  institutionPicker.initialize();
  destinationPicker.initialize();
  controls.form.addEventListener("submit", calculate);
  bindSortTabs();
  bindMapControls();
  destroyMobileMenu = bindMobileMenu();
  helpPanels.initialize();
  historyPanel.initialize();
  controls.otherTrips.addEventListener("change", updatePreviousAllowanceControl);
  updatePreviousAllowanceControl();
  updateCalculateAvailability();
  void authController.initialize();
  try {
    const bootstrap = await api.bootstrap();
    await map.initialize(bootstrap.map.javascriptKey);
  } catch {
    map.setStatus("지도 설정을 확인하지 못했습니다. 입력과 경로 결과는 계속 사용할 수 있습니다.");
  }
}

window.addEventListener("pagehide", () => {
  schedule.destroy();
  institutionPicker.destroy();
  destinationPicker.destroy();
  helpPanels.destroy();
  historyPanel.destroy();
  settingsPanel.destroy();
  authController.destroy();
  destroyMobileMenu();
}, { once: true });

initialize();
