const LOGIN_PATH = "/auth/kakao/start";

export function createAuthController({ api, elements, onSessionChange = () => {} }) {
  let authenticatedValue = false;
  let destroyed = false;
  let initializeRevision = 0;
  let meController = null;
  let privateTrigger = null;

  function notifySessionChange(unavailable = false) {
    Promise.resolve(onSessionChange({
      authenticated: authenticatedValue,
      unavailable,
    })).catch(() => {});
  }

  function renderAuthenticated(authenticated, status) {
    authenticatedValue = authenticated;
    elements.loginButtons.forEach((button) => { button.hidden = authenticated; });
    elements.logoutButton.hidden = !authenticated;
    elements.status.textContent = status;
  }

  function closePrivateDialog() {
    if (elements.privateDialog.open) elements.privateDialog.close();
  }

  function login() {
    window.location.assign(LOGIN_PATH);
  }

  function showPrivateLogin(event) {
    if (authenticatedValue) return;
    privateTrigger = event.currentTarget;
    elements.privateDialog.showModal();
  }

  function onPrivateDialogClose() {
    privateTrigger?.focus();
    privateTrigger = null;
  }

  function closePrivateDialogFromButton() {
    closePrivateDialog();
  }

  async function initialize() {
    const revision = ++initializeRevision;
    meController?.abort();
    meController = new AbortController();
    renderAuthenticated(false, "로그인 상태를 확인하고 있습니다.");
    try {
      const me = await api.me({ signal: meController.signal });
      if (destroyed || revision !== initializeRevision) return;
      if (me?.authenticated === true) {
        renderAuthenticated(true, "Kakao 로그인 상태입니다.");
        notifySessionChange();
        return;
      }
      renderAuthenticated(false, "로그인하지 않아도 계산할 수 있습니다.");
      notifySessionChange();
    } catch (error) {
      if (destroyed || revision !== initializeRevision || error?.name === "AbortError") return;
      renderAuthenticated(false, "로그인 상태를 확인하지 못했습니다. 공개 계산은 계속 사용할 수 있습니다.");
      notifySessionChange(true);
    }
  }

  async function logout() {
    try {
      await api.logout();
    } catch {
      elements.status.textContent = "로그아웃을 완료하지 못했습니다. 다시 시도해 주세요.";
      return false;
    }
    initializeRevision += 1;
    renderAuthenticated(false, "로그아웃했습니다. 로그인하지 않아도 계산할 수 있습니다.");
    notifySessionChange();
    return true;
  }

  async function deleteMyData() {
    try {
      await api.deleteMyData();
    } catch {
      elements.status.textContent = "내 데이터를 삭제하지 못했습니다. 다시 시도해 주세요.";
      return false;
    }
    initializeRevision += 1;
    renderAuthenticated(false, "내 데이터를 삭제했습니다. 로그인하지 않아도 계산할 수 있습니다.");
    notifySessionChange();
    return true;
  }

  const handleLogout = () => { void logout(); };
  elements.loginButtons.forEach((button) => button.addEventListener("click", login));
  elements.logoutButton.addEventListener("click", handleLogout);
  elements.historyButton.addEventListener("click", showPrivateLogin);
  elements.settingsButton.addEventListener("click", showPrivateLogin);
  elements.privateCloseButton.addEventListener("click", closePrivateDialogFromButton);
  elements.privateDialog.addEventListener("close", onPrivateDialogClose);

  return {
    authenticated: () => authenticatedValue,
    deleteMyData,
    destroy() {
      if (destroyed) return;
      destroyed = true;
      initializeRevision += 1;
      meController?.abort();
      closePrivateDialog();
      elements.loginButtons.forEach((button) => button.removeEventListener("click", login));
      elements.logoutButton.removeEventListener("click", handleLogout);
      elements.historyButton.removeEventListener("click", showPrivateLogin);
      elements.settingsButton.removeEventListener("click", showPrivateLogin);
      elements.privateCloseButton.removeEventListener("click", closePrivateDialogFromButton);
      elements.privateDialog.removeEventListener("close", onPrivateDialogClose);
    },
    initialize,
    login,
    logout,
  };
}
