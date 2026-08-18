function control(dialog, selector) {
  const element = dialog.querySelector(selector);
  if (!element) throw new Error("settings control is unavailable");
  return element;
}

function selectedRouteSort(dialog) {
  return dialog.querySelector("input[name='settings-route-sort']:checked")?.value ?? null;
}

function setValue(dialog, selector, value) {
  const input = control(dialog, selector);
  if (typeof value === "string" || Number.isFinite(value)) input.value = String(value);
}

function isSettings(value) {
  return value && typeof value === "object";
}

export function settingsPayloadFrom(dialog, defaultOriginSiteId) {
  const settings = {
    defaultOriginSiteId,
    defaultTripPattern: control(dialog, "#settings-trip-pattern").value,
    defaultDurationMinutes: Number.parseInt(control(dialog, "#settings-duration-minutes").value, 10),
    vehicleUse: control(dialog, "#settings-vehicle-use").value,
    fuelType: control(dialog, "#settings-fuel-type").value,
    efficiencyKmPerLiter: Number(control(dialog, "#settings-efficiency").value),
    parkingCostKrw: Number.parseInt(control(dialog, "#settings-parking-cost").value, 10),
    routeSort: selectedRouteSort(dialog),
  };
  return settings;
}

export function createSettingsPanel({
  api,
  confirmationDialog,
  deleteMyData,
  dialog,
  institutionPicker,
  isAuthenticated,
  onSettingsApplied = () => {},
  trigger,
}) {
  const form = control(dialog, "#settings-form");
  const status = control(dialog, "#settings-status");
  const originSummary = control(dialog, "#settings-origin-summary");
  const useCurrentOrigin = control(dialog, "#settings-use-current-origin");
  const clearDefaultOrigin = control(dialog, "#settings-clear-default-origin");
  const closeButton = control(dialog, "#settings-close-button");
  const saveButton = control(dialog, "#settings-save-button");
  const deleteButton = control(dialog, "#settings-delete-data-button");
  const deleteCancelButton = control(confirmationDialog, "#delete-data-cancel-button");
  const deleteKeepButton = control(confirmationDialog, "#delete-data-keep-button");
  const deleteConfirmButton = control(confirmationDialog, "#delete-data-confirm-button");
  let current = null;
  let defaultOriginSiteId = null;
  let destroyed = false;
  let loadRevision = 0;
  let loadController = null;
  let reopenSettingsAfterConfirmation = false;

  function setStatus(message = "") {
    status.textContent = message;
    status.hidden = !message;
  }

  function clearPrivateValues() {
    current = null;
    defaultOriginSiteId = null;
    form.reset();
    originSummary.textContent = "기본 근무지를 저장하지 않습니다.";
    setStatus();
  }

  function activeResolvedOrigin(response) {
    const origin = response?.resolvedDefaultOrigin;
    return origin && typeof origin.siteId === "string" && typeof origin.displayName === "string"
      ? origin
      : null;
  }

  function renderOriginSummary(response) {
    const origin = activeResolvedOrigin(response);
    if (origin) {
      originSummary.textContent = `${origin.displayName}을(를) 기본 근무지로 사용합니다.`;
      return;
    }
    if (response?.settings?.defaultOriginSiteId) {
      originSummary.textContent = "기본 근무지는 현재 사용할 수 없습니다. 다시 선택해 저장하세요.";
      return;
    }
    originSummary.textContent = "기본 근무지를 저장하지 않습니다.";
  }

  function populate(response) {
    const settings = response?.settings;
    if (!isSettings(settings)) throw new Error("settings response is unavailable");
    defaultOriginSiteId = typeof settings.defaultOriginSiteId === "string"
      ? settings.defaultOriginSiteId
      : null;
    setValue(dialog, "#settings-trip-pattern", settings.defaultTripPattern);
    setValue(dialog, "#settings-duration-minutes", settings.defaultDurationMinutes);
    setValue(dialog, "#settings-vehicle-use", settings.vehicleUse);
    setValue(dialog, "#settings-fuel-type", settings.fuelType);
    setValue(dialog, "#settings-efficiency", settings.efficiencyKmPerLiter);
    setValue(dialog, "#settings-parking-cost", settings.parkingCostKrw);
    const routeSort = typeof settings.routeSort === "string" ? settings.routeSort : "";
    dialog.querySelectorAll("input[name='settings-route-sort']").forEach((input) => {
      input.checked = input.value === routeSort;
    });
    renderOriginSummary(response);
    setStatus(response.source === "DEFAULT" ? "서버 기본값" : "저장한 기본 설정");
  }

  function apply(response) {
    const settings = response?.settings;
    if (!isSettings(settings)) throw new Error("settings response is unavailable");
    current = response;
    populate(response);
    onSettingsApplied(response);
  }

  async function load() {
    if (destroyed || !isAuthenticated()) return null;
    const revision = ++loadRevision;
    loadController?.abort();
    const controller = new AbortController();
    loadController = controller;
    try {
      const response = await api.settings({ signal: controller.signal });
      if (destroyed || revision !== loadRevision || !isAuthenticated()) return null;
      apply(response);
      return response;
    } finally {
      if (loadController === controller) loadController = null;
    }
  }

  async function save() {
    if (destroyed || !isAuthenticated() || !form.reportValidity()) return null;
    saveButton.disabled = true;
    setStatus("설정을 저장하고 있습니다.");
    try {
      const saved = await api.replaceSettings(settingsPayloadFrom(dialog, defaultOriginSiteId));
      if (destroyed || !isAuthenticated()) return null;
      apply(saved);
      return await load();
    } catch {
      if (!destroyed && isAuthenticated()) setStatus("설정을 저장하지 못했습니다. 다시 시도해 주세요.");
      return null;
    } finally {
      saveButton.disabled = false;
    }
  }

  function open() {
    if (destroyed || !isAuthenticated()) return;
    if (!dialog.open) dialog.showModal();
    dialog.querySelector("[data-dialog-heading]")?.focus();
  }

  function close() {
    if (dialog.open) dialog.close();
  }

  function useCurrent() {
    const origin = institutionPicker.selected?.();
    if (!origin || typeof origin.siteId !== "string" || typeof origin.displayName !== "string") {
      setStatus("먼저 현재 화면에서 출발 기관을 선택하세요.");
      return;
    }
    defaultOriginSiteId = origin.siteId;
    originSummary.textContent = `${origin.displayName}을(를) 기본 근무지로 저장합니다.`;
    setStatus("현재 출발 기관을 기본 근무지로 지정했습니다.");
  }

  function clearDefault() {
    defaultOriginSiteId = null;
    originSummary.textContent = "기본 근무지를 저장하지 않습니다.";
    setStatus("기본 근무지를 해제했습니다.");
  }

  function openDeleteConfirmation() {
    if (destroyed || !isAuthenticated()) return;
    reopenSettingsAfterConfirmation = dialog.open;
    close();
    if (!confirmationDialog.open) confirmationDialog.showModal();
    confirmationDialog.querySelector("[data-dialog-heading]")?.focus();
  }

  function closeDeleteConfirmation() {
    if (confirmationDialog.open) confirmationDialog.close();
  }

  function restoreSettingsAfterConfirmation() {
    if (!reopenSettingsAfterConfirmation || destroyed || !isAuthenticated()) return;
    reopenSettingsAfterConfirmation = false;
    open();
    deleteButton.focus();
  }

  async function confirmDelete() {
    deleteConfirmButton.disabled = true;
    const deleted = await deleteMyData();
    deleteConfirmButton.disabled = false;
    if (deleted) {
      reopenSettingsAfterConfirmation = false;
      closeDeleteConfirmation();
      close();
    }
  }

  function onFormSubmit(event) {
    event.preventDefault();
    void save();
  }

  function onDialogClose() {
    if (!confirmationDialog.open) trigger.focus();
  }

  function onConfirmationClose() {
    restoreSettingsAfterConfirmation();
  }

  trigger.addEventListener("click", open);
  closeButton.addEventListener("click", close);
  form.addEventListener("submit", onFormSubmit);
  useCurrentOrigin.addEventListener("click", useCurrent);
  clearDefaultOrigin.addEventListener("click", clearDefault);
  deleteButton.addEventListener("click", openDeleteConfirmation);
  deleteCancelButton.addEventListener("click", closeDeleteConfirmation);
  deleteKeepButton.addEventListener("click", closeDeleteConfirmation);
  deleteConfirmButton.addEventListener("click", () => { void confirmDelete(); });
  dialog.addEventListener("close", onDialogClose);
  confirmationDialog.addEventListener("close", onConfirmationClose);

  return {
    destroy() {
      if (destroyed) return;
      destroyed = true;
      loadRevision += 1;
      loadController?.abort();
      closeDeleteConfirmation();
      close();
      trigger.removeEventListener("click", open);
      closeButton.removeEventListener("click", close);
      form.removeEventListener("submit", onFormSubmit);
      useCurrentOrigin.removeEventListener("click", useCurrent);
      clearDefaultOrigin.removeEventListener("click", clearDefault);
      deleteButton.removeEventListener("click", openDeleteConfirmation);
      deleteCancelButton.removeEventListener("click", closeDeleteConfirmation);
      deleteKeepButton.removeEventListener("click", closeDeleteConfirmation);
      dialog.removeEventListener("close", onDialogClose);
      confirmationDialog.removeEventListener("close", onConfirmationClose);
    },
    load,
    save,
    setAuthenticated(authenticated) {
      if (authenticated) return;
      loadRevision += 1;
      loadController?.abort();
      reopenSettingsAfterConfirmation = false;
      closeDeleteConfirmation();
      close();
      clearPrivateValues();
    },
  };
}
