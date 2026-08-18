const classificationLabel = {
  LOCAL: "관내 출장",
  NON_LOCAL_EXPECTED: "관외 예상",
};

const directionLabel = {
  OUTBOUND: "가는 길",
  RETURN: "돌아오는 길",
};

const modeLabel = {
  CAR: "자동차",
  TRANSIT: "대중교통",
  WALK: "도보",
};

function formatMoney(value) {
  return Number.isFinite(value) ? `${new Intl.NumberFormat("ko-KR").format(value)}원` : "지급액 확인 필요";
}

function formatDistance(value) {
  return value >= 1000 ? `${(value / 1000).toFixed(1)}km` : `${value}m`;
}

function formatDuration(seconds) {
  const minutes = Math.round(seconds / 60);
  return minutes >= 60 ? `${Math.floor(minutes / 60)}시간 ${minutes % 60}분` : `${minutes}분`;
}

function text(value, fallback = "—") {
  return typeof value === "string" && value.trim() ? value : fallback;
}

function clearDialog(dialog) {
  if (dialog.open) dialog.close();
}

export function createHistoryPanel({ api, elements, tripForm, onDraftApplied = () => {} }) {
  let activeDetail = null;
  let authenticated = false;
  let destroyed = false;
  let loadedOnce = false;
  let loading = false;
  let nextCursor = null;
  let refreshAfterSavedPreview = false;
  let listTrigger = null;
  let detailTrigger = null;
  let restoreListAfterDetail = false;
  let historyGeneration = 0;
  let refreshPending = false;

  function setStatus(message = "") {
    elements.status.textContent = message;
    elements.status.hidden = !message;
  }

  function focusAfterClose(trigger) {
    if (trigger instanceof HTMLElement && trigger.isConnected) trigger.focus();
  }

  function closeDetail() {
    clearDialog(elements.detailDialog);
  }

  function closeList() {
    clearDialog(elements.dialog);
  }

  function renderRow(item) {
    const row = document.createElement("article");
    row.className = "history-row";
    const heading = document.createElement("h3");
    heading.textContent = `${text(item.destinationName, "저장된 출장")} · ${classificationLabel[item.classification] || "분류 확인 필요"}`;
    const summary = document.createElement("p");
    summary.textContent = `${text(item.originName, "출발 기관")} → ${text(item.destinationName, "출장지")} · ${formatMoney(item.allowanceKrw)}`;
    const actions = document.createElement("div");
    actions.className = "history-actions";
    const detail = document.createElement("button");
    detail.type = "button";
    detail.className = "secondary-action";
    detail.textContent = "상세 보기";
    detail.addEventListener("click", () => { void openDetail(item.id, detail); });
    const recalculate = document.createElement("button");
    recalculate.type = "button";
    recalculate.className = "secondary-action";
    recalculate.textContent = "다시 계산";
    recalculate.addEventListener("click", () => { void applyDraft(item.id); });
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "secondary-action history-delete";
    remove.textContent = "삭제";
    remove.addEventListener("click", () => { void deleteOne(item.id); });
    actions.append(detail, recalculate, remove);
    row.append(heading, summary, actions);
    elements.rows.append(row);
  }

  function renderDetail(detail) {
    elements.detailContent.replaceChildren();
    const heading = document.createElement("p");
    heading.className = "history-detail-heading";
    heading.textContent = `${classificationLabel[detail.item?.classification] || "분류 확인 필요"} · ${formatMoney(detail.item?.allowanceKrw)}`;
    const routeList = document.createElement("ul");
    routeList.className = "history-route-summary";
    (Array.isArray(detail.routeSummary) ? detail.routeSummary : []).forEach((leg) => {
      const route = document.createElement("li");
      route.textContent = [
        directionLabel[leg.direction] || "이동 경로",
        modeLabel[leg.mode] || "이동 수단 확인 필요",
        formatDuration(leg.durationSeconds),
        formatDistance(leg.distanceMeters),
        formatMoney(leg.mobilityCostKrw),
      ].join(" · ");
      routeList.append(route);
    });
    const rules = document.createElement("p");
    rules.className = "history-rule-summary";
    const ruleSet = text(detail.ruleSetId, "규정 정보 없음");
    const effectiveFrom = text(detail.effectiveFrom, "적용일 정보 없음");
    rules.textContent = `${ruleSet} · ${effectiveFrom}부터 적용`;
    elements.detailContent.append(heading, routeList, rules);
  }

  async function loadNext() {
    if (!authenticated || loading || (loadedOnce && nextCursor === null)) return;
    loading = true;
    const requestGeneration = historyGeneration;
    try {
      const page = await api.history({ cursor: nextCursor, limit: 50 });
      if (destroyed || !authenticated || requestGeneration !== historyGeneration) return;
      const items = Array.isArray(page?.items) ? page.items : [];
      items.forEach(renderRow);
      nextCursor = typeof page?.nextCursor === "string" ? page.nextCursor : null;
      loadedOnce = true;
      elements.loadMore.hidden = nextCursor === null;
      setStatus(items.length ? "" : "보관 중인 계산 이력이 없습니다.");
    } catch {
      if (destroyed || !authenticated || requestGeneration !== historyGeneration) return;
      elements.loadMore.hidden = true;
      setStatus("계산 이력을 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.");
    } finally {
      loading = false;
      if (refreshPending && authenticated && !destroyed) {
        refreshPending = false;
        void loadNext();
      }
    }
  }

  async function refresh() {
    if (!authenticated) return;
    historyGeneration += 1;
    activeDetail = null;
    loadedOnce = false;
    nextCursor = null;
    elements.rows.replaceChildren();
    elements.loadMore.hidden = true;
    setStatus("");
    if (loading) {
      refreshPending = true;
      return;
    }
    await loadNext();
  }

  function refreshForSavedPreview() {
    if (!authenticated) {
      refreshAfterSavedPreview = true;
      return;
    }
    void refresh();
  }

  async function openDetail(id, trigger = null) {
    if (!authenticated || typeof id !== "string") return;
    try {
      const detail = await api.historyDetail(id);
      if (destroyed || !authenticated) return;
      activeDetail = detail;
      detailTrigger = trigger;
      renderDetail(detail);
      restoreListAfterDetail = true;
      closeList();
      elements.detailDialog.showModal();
    } catch {
      setStatus("계산 상세를 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.");
    }
  }

  async function applyDraft(id) {
    if (!authenticated || typeof id !== "string") return;
    try {
      const detail = await api.historyDetail(id);
      if (destroyed || !authenticated) return;
      const applied = await tripForm.applyRecalculationDraft(
        detail.recalculationDraft,
        detail.resolvedOrigin,
      );
      restoreListAfterDetail = false;
      closeDetail();
      closeList();
      onDraftApplied({ ...applied, canPreview: false });
    } catch {
      setStatus("다시 계산할 이력을 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.");
    }
  }

  async function deleteOne(id) {
    if (!authenticated || typeof id !== "string") return;
    try {
      await api.deleteHistory(id);
      restoreListAfterDetail = false;
      closeDetail();
      await refresh();
    } catch {
      setStatus("계산 이력을 삭제하지 못했습니다. 잠시 후 다시 시도해 주세요.");
    }
  }

  async function deleteAll() {
    if (!authenticated) return;
    try {
      await api.deleteAllHistory();
      restoreListAfterDetail = false;
      closeDetail();
      await refresh();
    } catch {
      setStatus("계산 이력을 삭제하지 못했습니다. 잠시 후 다시 시도해 주세요.");
    }
  }

  function showList(event) {
    if (!authenticated || destroyed) return;
    listTrigger = event.currentTarget;
    elements.dialog.showModal();
    void refresh();
  }

  function setAuthenticated(nextAuthenticated) {
    authenticated = nextAuthenticated === true;
    if (authenticated) {
      if (refreshAfterSavedPreview) {
        refreshAfterSavedPreview = false;
        void refresh();
      }
      return;
    }
    historyGeneration += 1;
    refreshPending = false;
    refreshAfterSavedPreview = false;
    restoreListAfterDetail = false;
    closeDetail();
    closeList();
    activeDetail = null;
    loadedOnce = false;
    nextCursor = null;
    elements.rows.replaceChildren();
    elements.detailContent.replaceChildren();
    elements.loadMore.hidden = true;
    setStatus("");
  }

  const onListClose = () => focusAfterClose(listTrigger);
  const onDetailClose = () => {
    const restore = restoreListAfterDetail && authenticated && !destroyed;
    restoreListAfterDetail = false;
    if (restore) elements.dialog.showModal();
    focusAfterClose(detailTrigger);
  };
  const onCloseList = () => closeList();
  const onCloseDetail = () => closeDetail();
  const onLoadMore = () => { void loadNext(); };
  const onDeleteAll = () => { void deleteAll(); };
  const onDetailRecalculate = () => {
    if (typeof activeDetail?.item?.id === "string") void applyDraft(activeDetail.item.id);
  };
  const onDetailDelete = () => {
    if (typeof activeDetail?.item?.id === "string") void deleteOne(activeDetail.item.id);
  };

  function initialize() {
    elements.button.addEventListener("click", showList);
    elements.closeButton.addEventListener("click", onCloseList);
    elements.detailCloseButton.addEventListener("click", onCloseDetail);
    elements.loadMore.addEventListener("click", onLoadMore);
    elements.deleteAllButton.addEventListener("click", onDeleteAll);
    elements.detailRecalculateButton.addEventListener("click", onDetailRecalculate);
    elements.detailDeleteButton.addEventListener("click", onDetailDelete);
    elements.dialog.addEventListener("close", onListClose);
    elements.detailDialog.addEventListener("close", onDetailClose);
  }

  function destroy() {
    if (destroyed) return;
    destroyed = true;
    historyGeneration += 1;
    refreshPending = false;
    restoreListAfterDetail = false;
    closeDetail();
    closeList();
    elements.button.removeEventListener("click", showList);
    elements.closeButton.removeEventListener("click", onCloseList);
    elements.detailCloseButton.removeEventListener("click", onCloseDetail);
    elements.loadMore.removeEventListener("click", onLoadMore);
    elements.deleteAllButton.removeEventListener("click", onDeleteAll);
    elements.detailRecalculateButton.removeEventListener("click", onDetailRecalculate);
    elements.detailDeleteButton.removeEventListener("click", onDetailDelete);
    elements.dialog.removeEventListener("close", onListClose);
    elements.detailDialog.removeEventListener("close", onDetailClose);
  }

  return {
    applyDraft,
    deleteAll,
    deleteOne,
    destroy,
    initialize,
    loadNext,
    openDetail,
    refresh,
    refreshForSavedPreview,
    setAuthenticated,
  };
}
