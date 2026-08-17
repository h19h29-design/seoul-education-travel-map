import { createCombobox } from "./combobox.js";

function isAbortError(error) {
  return error?.name === "AbortError";
}

function isPlace(item) {
  return item
    && typeof item.name === "string"
    && item.name.trim().length > 0
    && (typeof item.roadAddress === "string" || typeof item.lotAddress === "string")
    && Number.isFinite(item.latitude)
    && Number.isFinite(item.longitude);
}

function addresses(item) {
  return [...new Set([item.roadAddress, item.lotAddress]
    .filter((value) => typeof value === "string" && value.trim()))];
}

function renderDestination(item) {
  const option = document.createElement("li");
  const name = document.createElement("strong");
  const address = document.createElement("span");
  name.className = "suggestion-primary";
  name.textContent = item.name;
  address.className = "suggestion-secondary";
  address.textContent = addresses(item).join(" · ");
  option.append(name, address);
  return option;
}

export function createDestinationPicker({
  api,
  map,
  elements,
  onSelectionChange,
}) {
  let destroyed = false;
  let initialized = false;
  let reverseController = null;
  let reverseRequestId = 0;

  function cancelReverse() {
    reverseRequestId += 1;
    reverseController?.abort();
    reverseController = null;
  }

  function clearDestinationAuthority() {
    map.clearDestinationCandidate();
    onSelectionChange(null);
  }

  const combobox = createCombobox({
    input: elements.input,
    listbox: elements.listbox,
    status: elements.status,
    debounceMs: 250,
    minLength: 2,
    retryButton: elements.retryButton,
    onInput: cancelReverse,
    search: async (query, { signal }) => {
      const response = await api.places(query, { signal });
      return {
        ...response,
        items: response.items.filter(isPlace),
        total: response.items.filter(isPlace).length,
      };
    },
    renderOption: renderDestination,
    onSelect: (item) => {
      cancelReverse();
      elements.input.value = item.name;
      map.showDestinationCandidate(item);
      onSelectionChange(item);
      return `${item.name}을(를) 선택했습니다. ${addresses(item).join(" · ")}`;
    },
    onInvalidate: clearDestinationAuthority,
  });

  async function reverseDestination(point) {
    cancelReverse();
    combobox.clear({
      message: "지도에서 선택한 위치의 주소를 확인하고 있습니다.",
    });
    clearDestinationAuthority();
    const id = ++reverseRequestId;
    const activeController = new AbortController();
    reverseController = activeController;
    try {
      const response = await api.reversePlace(point, { signal: activeController.signal });
      if (destroyed || id !== reverseRequestId) return;
      reverseController = null;
      if (!isPlace(response.item)) {
        combobox.setStatus("선택한 위치의 주소를 확인할 수 없습니다.");
        return;
      }
      elements.input.value = response.item.name;
      combobox.showResults([response.item], {
        message: "지도 위치의 주소를 확인했습니다. 후보를 클릭하거나 Enter로 선택하세요.",
        total: 1,
      });
      elements.input.focus();
    } catch (error) {
      if (destroyed || id !== reverseRequestId || isAbortError(error)) return;
      reverseController = null;
      combobox.setStatus("선택한 위치의 주소를 불러오지 못했습니다.");
    }
  }

  function initialize() {
    if (initialized) return;
    initialized = true;
    map.setClickHandler(reverseDestination);
  }

  function clear() {
    cancelReverse();
    combobox.clear({ message: "출장지를 검색해 선택하세요." });
    clearDestinationAuthority();
  }

  function selectResolved(item) {
    if (!isPlace(item)) return false;
    combobox.select(item);
    return true;
  }

  function confirmReverseCandidate(item) {
    if (!isPlace(item)) return false;
    cancelReverse();
    combobox.clear({ message: "지도 위치 후보를 확인해 선택하세요." });
    clearDestinationAuthority();
    elements.input.value = item.name;
    combobox.showResults([item], {
      message: "지도 위치 후보를 클릭하거나 Enter로 선택하세요.",
      total: 1,
    });
    return true;
  }

  async function setQueryAndSearch(query) {
    cancelReverse();
    combobox.clear({ message: "출장지를 검색합니다." });
    clearDestinationAuthority();
    elements.input.value = String(query).slice(0, 80);
    return combobox.searchNow();
  }

  function destroy() {
    if (destroyed) return;
    destroyed = true;
    cancelReverse();
    map.setClickHandler(null);
    combobox.destroy();
    clearDestinationAuthority();
  }

  return {
    clear,
    confirmReverseCandidate,
    destroy,
    initialize,
    selected: combobox.selected,
    selectResolved,
    setQueryAndSearch,
  };
}
