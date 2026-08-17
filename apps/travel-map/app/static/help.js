function appendPolicyRow(container, label, value) {
  const term = document.createElement("dt");
  term.textContent = label;
  const description = document.createElement("dd");
  description.textContent = value;
  container.append(term, description);
}

function validSourceUrl(value) {
  try {
    const url = new URL(value);
    return url.protocol === "https:" && !url.username && !url.password ? url : null;
  } catch {
    return null;
  }
}

function renderPolicy(dialog, policy) {
  const details = dialog.querySelector("[data-policy-details]");
  const sources = dialog.querySelector("[data-policy-sources]");
  if (!(details instanceof HTMLElement) || !(sources instanceof HTMLElement)) return;

  details.replaceChildren();
  sources.replaceChildren();
  appendPolicyRow(details, "적용 규정", policy.profileLabel);
  appendPolicyRow(details, "규칙 버전", policy.ruleSetId);
  appendPolicyRow(details, "시행일", policy.effectiveFrom);
  appendPolicyRow(
    details,
    "관내출장 거리 기준",
    `${policy.localRoundTripExclusiveMeters.toLocaleString("ko-KR")}m 미만`,
  );
  appendPolicyRow(
    details,
    "실비 지급 검토 기준",
    `${policy.actualExpenseInclusiveMeters.toLocaleString("ko-KR")}m 이상`,
  );
  appendPolicyRow(
    details,
    "출장시간 기준",
    `${policy.fourHoursMinutes.toLocaleString("ko-KR")}분`,
  );
  appendPolicyRow(
    details,
    "4시간 미만 예상액",
    `${policy.underFourHoursKrw.toLocaleString("ko-KR")}원`,
  );
  appendPolicyRow(
    details,
    "4시간 이상 예상액",
    `${policy.fourHoursOrMoreKrw.toLocaleString("ko-KR")}원`,
  );
  appendPolicyRow(
    details,
    "관용 차량 공제 기준",
    `${policy.officialVehicleDeductionKrw.toLocaleString("ko-KR")}원`,
  );

  policy.sourceRefs
    .map(validSourceUrl)
    .filter((source) => source !== null)
    .forEach((source, index) => {
      const link = document.createElement("a");
      link.href = source.href;
      link.rel = "noopener noreferrer";
      link.target = "_blank";
      link.textContent = `공식 출처 ${index + 1}`;
      sources.append(link);
    });
}

export function createHelpPanels({
  helpButton,
  policyButton,
  helpDialog,
  policyDialog,
  api,
}) {
  const dialogs = [helpDialog, policyDialog];
  const triggers = new Map([
    [helpDialog, helpButton],
    [policyDialog, policyButton],
  ]);
  let activeTrigger = null;
  let destroyed = false;
  let policyRequestController = null;
  let policyRequestRevision = 0;
  let suppressFocusFor = null;

  const invalidatePolicyRequest = () => {
    policyRequestRevision += 1;
    policyRequestController?.abort();
    policyRequestController = null;
  };

  const closeDialog = (dialog, restoreFocus = true) => {
    if (!dialog.open) return;
    if (dialog === policyDialog) invalidatePolicyRequest();
    if (!restoreFocus) suppressFocusFor = dialog;
    dialog.close();
  };

  const closeAll = (restoreFocus = true) => {
    dialogs.forEach((dialog) => closeDialog(dialog, restoreFocus));
  };

  const onClose = (event) => {
    if (event.currentTarget === policyDialog) invalidatePolicyRequest();
    if (event.currentTarget === suppressFocusFor) {
      suppressFocusFor = null;
      activeTrigger = null;
      return;
    }
    const trigger = activeTrigger ?? triggers.get(event.currentTarget);
    activeTrigger = null;
    trigger?.focus();
  };

  const openDialog = (dialog, trigger) => {
    closeAll(false);
    activeTrigger = trigger;
    if (!dialog.open) dialog.showModal();
    const heading = dialog.querySelector("[data-dialog-heading]");
    if (heading instanceof HTMLElement) heading.focus();
  };

  const openHelp = () => openDialog(helpDialog, helpButton);

  const openPolicy = async () => {
    openDialog(policyDialog, policyButton);
    const status = policyDialog.querySelector("[data-policy-status]");
    const content = policyDialog.querySelector("[data-policy-content]");
    if (!(status instanceof HTMLElement) || !(content instanceof HTMLElement)) return;
    invalidatePolicyRequest();
    const requestRevision = policyRequestRevision;
    const controller = new AbortController();
    policyRequestController = controller;
    const requestIsCurrent = () => (
      !destroyed
      && policyDialog.open
      && requestRevision === policyRequestRevision
      && policyRequestController === controller
    );
    status.hidden = false;
    status.textContent = "현재 적용 규정을 불러오고 있습니다.";
    content.hidden = true;
    try {
      const policy = await api.currentPolicy({ signal: controller.signal });
      if (!requestIsCurrent()) return;
      renderPolicy(policyDialog, policy);
      status.hidden = true;
      content.hidden = false;
    } catch {
      if (!requestIsCurrent() || controller.signal.aborted) return;
      status.textContent = "현재 규정 정보를 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.";
    } finally {
      if (policyRequestController === controller) policyRequestController = null;
    }
  };

  const onHelpClick = () => openHelp();
  const onPolicyClick = () => { void openPolicy(); };
  const closeButtons = dialogs.flatMap((dialog) => (
    [...dialog.querySelectorAll("[data-dialog-close]")]
  ));
  const onCloseButtonClick = (event) => {
    const dialog = event.currentTarget.closest("dialog");
    if (dialog instanceof HTMLDialogElement) closeDialog(dialog);
  };

  return {
    initialize() {
      helpButton.addEventListener("click", onHelpClick);
      policyButton.addEventListener("click", onPolicyClick);
      dialogs.forEach((dialog) => dialog.addEventListener("close", onClose));
      closeButtons.forEach((button) => button.addEventListener("click", onCloseButtonClick));
    },
    openHelp,
    openPolicy,
    closeAll,
    destroy() {
      destroyed = true;
      invalidatePolicyRequest();
      helpButton.removeEventListener("click", onHelpClick);
      policyButton.removeEventListener("click", onPolicyClick);
      dialogs.forEach((dialog) => dialog.removeEventListener("close", onClose));
      closeButtons.forEach((button) => button.removeEventListener("click", onCloseButtonClick));
      closeAll(false);
    },
  };
}
