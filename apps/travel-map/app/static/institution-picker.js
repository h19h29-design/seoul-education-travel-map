import { createCombobox } from "./combobox.js";

const institutionTypeName = {
  KINDERGARTEN: "유치원",
  ELEMENTARY_SCHOOL: "초등학교",
  MIDDLE_SCHOOL: "중학교",
  HIGH_SCHOOL: "고등학교",
  SPECIAL_SCHOOL: "특수학교",
  OTHER: "기타",
};
const foundationTypeName = {
  NATIONAL: "국립",
  PUBLIC: "공립",
  PRIVATE: "사립",
};

function isAbortError(error) {
  return error?.name === "AbortError";
}

function isInstitution(item) {
  return item
    && typeof item.siteId === "string"
    && typeof item.displayName === "string"
    && item.displayName.trim().length > 0
    && typeof item.roadAddress === "string"
    && Number.isFinite(item.coordinate?.latitude)
    && Number.isFinite(item.coordinate?.longitude);
}

function institutionDetails(item) {
  const type = institutionTypeName[item.institutionType] || item.institutionType;
  const foundation = foundationTypeName[item.foundationType] || item.foundationType;
  return [type, foundation, item.district, item.roadAddress]
    .filter((value) => typeof value === "string" && value)
    .join(" · ");
}

function renderInstitution(item) {
  const option = document.createElement("li");
  const name = document.createElement("strong");
  const details = document.createElement("span");
  name.className = "suggestion-primary";
  name.textContent = item.displayName;
  details.className = "suggestion-secondary";
  details.textContent = institutionDetails(item);
  option.append(name, details);
  return option;
}

function replaceSelectOptions(select, options) {
  const all = document.createElement("option");
  all.value = "";
  all.textContent = "전체";
  select.replaceChildren(all, ...options.map((item) => {
    const option = document.createElement("option");
    option.value = item.value;
    option.textContent = `${item.label} (${item.count})`;
    return option;
  }));
}

export function createInstitutionPicker({
  api,
  map,
  elements,
  onSelectionChange,
}) {
  const filterEntries = Object.entries(elements.filters);
  let destroyed = false;
  let facetController = null;
  let facetRequestId = 0;
  let initialized = false;
  let pageState = null;
  let paginationController = null;
  let paginationRequestId = 0;
  let paginationLoading = false;

  function selectedMessage(item) {
    return `${item.displayName}을(를) 선택했습니다. ${institutionDetails(item)}`;
  }

  function clearOriginAuthority() {
    map.clearOriginCandidate();
    onSelectionChange(null);
  }

  function cancelPagination() {
    paginationRequestId += 1;
    paginationController?.abort();
    paginationController = null;
    paginationLoading = false;
    elements.loadMoreButton.disabled = false;
  }

  function filters() {
    return {
      institutionType: elements.filters.institutionType.value,
      foundationType: elements.filters.foundationType.value,
      educationOffice: elements.filters.educationOffice.value,
      district: elements.filters.district.value,
    };
  }

  function updateLoadMore() {
    elements.loadMoreButton.hidden = pageState?.nextOffset == null;
    elements.loadMoreButton.disabled = paginationLoading;
  }

  function resetBaseSearchState() {
    cancelPagination();
    pageState = null;
    updateLoadMore();
  }

  const combobox = createCombobox({
    input: elements.input,
    listbox: elements.listbox,
    status: elements.status,
    debounceMs: 0,
    minLength: 0,
    retryButton: elements.retryButton,
    onInput: resetBaseSearchState,
    search: async (query, { signal }) => {
      resetBaseSearchState();
      const response = await api.institutions({
        q: query,
        limit: 20,
        offset: 0,
        ...filters(),
      }, { signal });
      return {
        ...response,
        items: response.items.filter(isInstitution),
      };
    },
    renderOption: renderInstitution,
    onResults: (response) => {
      pageState = {
        items: [...response.items],
        nextOffset: response.nextOffset,
        snapshotId: response.snapshotId,
        total: response.total,
      };
      updateLoadMore();
    },
    onSelect: (item) => {
      resetBaseSearchState();
      elements.input.value = item.displayName;
      map.showOriginCandidate(item);
      onSelectionChange(item);
      return selectedMessage(item);
    },
    onInvalidate: clearOriginAuthority,
  });

  function setFiltersDisabled(disabled) {
    filterEntries.forEach(([, select]) => { select.disabled = disabled; });
  }

  async function loadFacets() {
    facetRequestId += 1;
    const id = facetRequestId;
    facetController?.abort();
    const activeController = new AbortController();
    facetController = activeController;
    setFiltersDisabled(true);
    elements.facetsRetryButton.hidden = true;
    elements.facetsStatus.textContent = "기관 필터를 불러오고 있습니다.";
    try {
      const facets = await api.institutionFacets({ signal: activeController.signal });
      if (destroyed || id !== facetRequestId) return;
      replaceSelectOptions(elements.filters.institutionType, facets.institutionTypes);
      replaceSelectOptions(elements.filters.foundationType, facets.foundationTypes);
      replaceSelectOptions(elements.filters.educationOffice, facets.educationOffices);
      replaceSelectOptions(elements.filters.district, facets.districts);
      setFiltersDisabled(false);
      elements.facetsStatus.textContent = "현재 기관 자료에서 필터를 불러왔습니다.";
    } catch (error) {
      if (destroyed || id !== facetRequestId || isAbortError(error)) return;
      setFiltersDisabled(true);
      elements.facetsRetryButton.hidden = false;
      elements.facetsStatus.textContent = "기관 필터를 불러오지 못했습니다. 텍스트 검색은 계속 이용할 수 있습니다.";
    }
  }

  async function loadMore() {
    if (paginationLoading || pageState?.nextOffset == null) return;
    paginationLoading = true;
    updateLoadMore();
    const id = ++paginationRequestId;
    const activeController = new AbortController();
    paginationController = activeController;
    const query = elements.input.value.trim();
    const offset = pageState.nextOffset;
    const snapshotId = pageState.snapshotId;
    combobox.setStatus(`기관을 더 불러오고 있습니다. 총 ${pageState.total}개`);
    try {
      const response = await api.institutions({
        q: query,
        limit: 20,
        offset,
        ...filters(),
      }, { signal: activeController.signal });
      if (destroyed || id !== paginationRequestId) return;
      if (response.snapshotId !== snapshotId) {
        paginationLoading = false;
        updateLoadMore();
        await combobox.searchNow();
        return;
      }
      const knownSiteIds = new Set(pageState.items.map((item) => item.siteId));
      const additions = [];
      response.items.forEach((item) => {
        if (!isInstitution(item) || knownSiteIds.has(item.siteId)) return;
        knownSiteIds.add(item.siteId);
        additions.push(item);
      });
      pageState.items.push(...additions);
      pageState.nextOffset = response.nextOffset;
      pageState.total = response.total;
      combobox.append(additions, { total: response.total });
    } catch (error) {
      if (destroyed || id !== paginationRequestId || isAbortError(error)) return;
      combobox.setStatus("추가 기관을 불러오지 못했습니다. 기관 더 보기를 다시 눌러 주세요.");
    } finally {
      if (id === paginationRequestId) {
        paginationController = null;
        paginationLoading = false;
        updateLoadMore();
      }
    }
  }

  function handleFilterChange() {
    resetBaseSearchState();
    combobox.clear({
      message: "필터를 반영해 기관을 검색합니다.",
      preserveInput: true,
    });
    clearOriginAuthority();
    void combobox.searchNow();
  }

  function handleToggle() {
    const expanded = elements.filtersToggle.getAttribute("aria-expanded") !== "true";
    elements.filtersContainer.hidden = !expanded;
    elements.filtersToggle.setAttribute("aria-expanded", String(expanded));
    elements.filtersToggle.textContent = expanded
      ? "기관 검색 필터 닫기"
      : "기관 검색 필터 열기";
  }

  function initialize() {
    if (initialized) return;
    initialized = true;
    setFiltersDisabled(true);
    filterEntries.forEach(([, select]) => {
      select.addEventListener("change", handleFilterChange);
    });
    elements.filtersToggle.addEventListener("click", handleToggle);
    elements.loadMoreButton.addEventListener("click", loadMore);
    elements.facetsRetryButton.addEventListener("click", loadFacets);
    void loadFacets();
  }

  function clear() {
    resetBaseSearchState();
    combobox.clear({ message: "기관을 검색해 선택하세요." });
    clearOriginAuthority();
    updateLoadMore();
  }

  function selectResolved(item) {
    if (!isInstitution(item)) return false;
    combobox.select(item);
    return true;
  }

  function destroy() {
    if (destroyed) return;
    destroyed = true;
    facetRequestId += 1;
    facetController?.abort();
    cancelPagination();
    filterEntries.forEach(([, select]) => {
      select.removeEventListener("change", handleFilterChange);
    });
    elements.filtersToggle.removeEventListener("click", handleToggle);
    elements.loadMoreButton.removeEventListener("click", loadMore);
    elements.facetsRetryButton.removeEventListener("click", loadFacets);
    combobox.destroy();
    clearOriginAuthority();
  }

  return {
    clear,
    destroy,
    initialize,
    selectResolved,
    selected: combobox.selected,
  };
}
