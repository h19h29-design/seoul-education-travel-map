function isAbortError(error) {
  return error instanceof DOMException
    ? error.name === "AbortError"
    : error?.name === "AbortError";
}

function responseItems(response) {
  if (Array.isArray(response)) return response;
  return Array.isArray(response?.items) ? response.items : [];
}

export function createCombobox({
  input,
  listbox,
  status,
  debounceMs,
  minLength,
  search,
  renderOption,
  onSelect,
  onInvalidate,
  onInput = () => {},
  onResults = () => {},
  retryButton = null,
}) {
  let activeIndex = -1;
  let controller = null;
  let debounceTimer = null;
  let destroyed = false;
  let items = [];
  let pendingEnter = false;
  let pendingMove = 0;
  let renderedQuery = null;
  let requestId = 0;
  let selectedItem = null;
  let waitForFreshOptions = false;

  function announce(message) {
    status.textContent = message;
  }

  function setRetryVisible(visible) {
    if (retryButton) retryButton.hidden = !visible;
  }

  function cancelPending() {
    requestId += 1;
    if (debounceTimer !== null) {
      window.clearTimeout(debounceTimer);
      debounceTimer = null;
    }
    controller?.abort();
    controller = null;
    return requestId;
  }

  function closeList({ clearItems = false } = {}) {
    activeIndex = -1;
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
    if (clearItems) {
      items = [];
      listbox.replaceChildren();
    } else {
      listbox.querySelectorAll('[role="option"]').forEach((option) => {
        option.setAttribute("aria-selected", "false");
      });
    }
    listbox.hidden = true;
  }

  function invalidateSelection() {
    if (selectedItem === null) return;
    selectedItem = null;
    onInvalidate();
  }

  function choose(item) {
    pendingEnter = false;
    pendingMove = 0;
    waitForFreshOptions = false;
    cancelPending();
    selectedItem = item;
    closeList({ clearItems: true });
    setRetryVisible(false);
    const message = onSelect(item);
    announce(typeof message === "string" && message
      ? message
      : "후보를 선택했습니다.");
  }

  function optionElement(item, index) {
    const option = renderOption(item, index);
    option.id = `${input.id}-option-${requestId}-${index}`;
    option.setAttribute("role", "option");
    option.setAttribute("aria-selected", "false");
    option.tabIndex = -1;
    option.addEventListener("pointerdown", (event) => event.preventDefault());
    option.addEventListener("click", () => choose(item));
    return option;
  }

  function showResults(nextItems, {
    total = nextItems.length,
    message = null,
  } = {}) {
    waitForFreshOptions = false;
    renderedQuery = input.value.trim();
    items = [...nextItems];
    activeIndex = -1;
    listbox.replaceChildren(
      ...items.map((item, index) => optionElement(item, index)),
    );
    setRetryVisible(false);
    if (items.length) {
      listbox.hidden = false;
      input.setAttribute("aria-expanded", "true");
      announce(message || `총 ${total}개의 검색 결과가 있습니다.`);
      if (pendingMove !== 0) {
        const move = pendingMove;
        pendingMove = 0;
        moveActive(move);
        if (pendingEnter && activeIndex >= 0) {
          pendingEnter = false;
          choose(items[activeIndex]);
        }
      }
    } else {
      closeList({ clearItems: true });
      announce(message || "검색 결과 0개입니다.");
    }
  }

  function append(nextItems, {
    total = items.length + nextItems.length,
    message = null,
  } = {}) {
    const start = items.length;
    items.push(...nextItems);
    listbox.append(
      ...nextItems.map((item, index) => optionElement(item, start + index)),
    );
    if (items.length) {
      listbox.hidden = false;
      input.setAttribute("aria-expanded", "true");
    }
    announce(message || `총 ${total}개의 검색 결과가 있습니다.`);
  }

  async function performSearch(id, query) {
    if (destroyed || id !== requestId) return null;
    const activeController = new AbortController();
    controller = activeController;
    try {
      const response = await search(query, {
        requestId: id,
        signal: activeController.signal,
      });
      if (destroyed || id !== requestId) return null;
      controller = null;
      onResults(response, id);
      const nextItems = responseItems(response);
      showResults(nextItems, {
        total: Number.isInteger(response?.total) ? response.total : nextItems.length,
      });
      return response;
    } catch (error) {
      if (destroyed || id !== requestId || isAbortError(error)) return null;
      controller = null;
      waitForFreshOptions = false;
      closeList({ clearItems: true });
      setRetryVisible(true);
      announce("검색 결과를 불러오지 못했습니다. 다시 시도해 주세요.");
      return null;
    }
  }

  function beginSearch({ defer = false } = {}) {
    const id = cancelPending();
    const query = input.value.trim();
    setRetryVisible(false);
    if (query.length < minLength) {
      waitForFreshOptions = false;
      closeList({ clearItems: true });
      announce(`${minLength}자 이상 입력해 검색하세요.`);
      return Promise.resolve(null);
    }
    announce("검색하고 있습니다.");
    if (defer && debounceMs > 0) {
      return new Promise((resolve) => {
        debounceTimer = window.setTimeout(() => {
          debounceTimer = null;
          void performSearch(id, query).then(resolve);
        }, debounceMs);
      });
    }
    return performSearch(id, query);
  }

  function handleInput() {
    const query = input.value.trim();
    const hadStaleOptions = items.length > 0 && renderedQuery !== query;
    pendingEnter = false;
    pendingMove = 0;
    invalidateSelection();
    closeList({ clearItems: true });
    renderedQuery = null;
    waitForFreshOptions = hadStaleOptions;
    onInput();
    void beginSearch({ defer: true });
  }

  function handleFocus() {
    if (input.value.trim().length >= minLength && listbox.hidden) {
      void beginSearch();
    }
  }

  function moveActive(increment) {
    const options = [...listbox.querySelectorAll('[role="option"]')];
    if (!options.length) {
      if (waitForFreshOptions) return;
      pendingMove = increment;
      void beginSearch();
      return;
    }
    if (listbox.hidden) {
      listbox.hidden = false;
      input.setAttribute("aria-expanded", "true");
    }
    activeIndex = (activeIndex + increment + options.length) % options.length;
    options.forEach((option, index) => {
      option.setAttribute("aria-selected", String(index === activeIndex));
    });
    input.setAttribute("aria-activedescendant", options[activeIndex].id);
    options[activeIndex].scrollIntoView({ block: "nearest" });
  }

  function handleKeydown(event) {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      moveActive(event.key === "ArrowDown" ? 1 : -1);
      return;
    }
    if (event.key === "Enter" && activeIndex >= 0) {
      event.preventDefault();
      items[activeIndex] && choose(items[activeIndex]);
      return;
    }
    if (event.key === "Enter" && pendingMove !== 0) {
      event.preventDefault();
      pendingEnter = true;
      return;
    }
    if (event.key === "Enter" && waitForFreshOptions) {
      event.preventDefault();
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      pendingEnter = false;
      pendingMove = 0;
      waitForFreshOptions = false;
      cancelPending();
      closeList();
    }
  }

  function clear({
    message = "후보를 검색해 선택하세요.",
    preserveInput = false,
  } = {}) {
    pendingEnter = false;
    pendingMove = 0;
    renderedQuery = null;
    waitForFreshOptions = false;
    cancelPending();
    invalidateSelection();
    if (!preserveInput) input.value = "";
    closeList({ clearItems: true });
    setRetryVisible(false);
    announce(message);
  }

  function select(item) {
    choose(item);
  }

  function destroy() {
    if (destroyed) return;
    destroyed = true;
    cancelPending();
    invalidateSelection();
    input.removeEventListener("focus", handleFocus);
    input.removeEventListener("input", handleInput);
    input.removeEventListener("keydown", handleKeydown);
    retryButton?.removeEventListener("click", handleRetry);
    closeList({ clearItems: true });
  }

  function handleRetry() {
    void beginSearch();
  }

  input.addEventListener("focus", handleFocus);
  input.addEventListener("input", handleInput);
  input.addEventListener("keydown", handleKeydown);
  retryButton?.addEventListener("click", handleRetry);

  return {
    append,
    clear,
    destroy,
    searchNow: () => beginSearch(),
    select,
    selected: () => selectedItem,
    setStatus: announce,
    showResults,
  };
}
