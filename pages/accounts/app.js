const bridge = window.AstrBotPluginPage;

const stateLabels = {
  anonymous: "未登录",
  connected: "已登录",
  waiting: "等待扫码",
  scanned: "已扫码",
  confirmed: "登录成功",
  expired: "二维码过期",
  failed: "登录失败",
  cancelled: "已取消",
};

const terminalStates = new Set(["confirmed", "expired", "failed", "cancelled"]);

const elements = {
  refresh: document.getElementById("refresh"),
  message: document.getElementById("page-message"),
  ffmpeg: document.getElementById("ffmpeg-status"),
  dialog: document.getElementById("login-dialog"),
  loginState: document.getElementById("login-state"),
  qrCode: document.getElementById("qr-code"),
  closeLogin: document.getElementById("close-login"),
  cancelLogin: document.getElementById("cancel-login"),
  account: document.querySelector(".account-card"),
};

let activeLogin = null;
let subscriptionId = null;
let loginGeneration = 0;
let pageBusy = false;

function messageFrom(error, fallback) {
  return error instanceof Error && error.message ? error.message : fallback;
}

function showMessage(text, tone = "error") {
  elements.message.textContent = text;
  elements.message.dataset.tone = tone;
  elements.message.hidden = !text;
}

function setBusy(button, busy) {
  if (!button) return;
  button.disabled = busy;
  button.dataset.busy = String(busy);
}

function renderAccount(account) {
  const state = String(account.state || "anonymous");
  const name = account.display_name || (state === "connected" ? "已登录" : "未登录");
  const badge = elements.account.querySelector('[data-role="state"]');
  const nameNode = elements.account.querySelector('[data-role="name"]');
  const loginButton = elements.account.querySelector('[data-action="login"]');
  const logoutButton = elements.account.querySelector('[data-action="logout"]');

  badge.textContent = stateLabels[state] || "状态未知";
  badge.dataset.state = state;
  nameNode.textContent = name;
  loginButton.hidden = state === "connected";
  logoutButton.hidden = state !== "connected";
}

function renderHealth(health) {
  const ffmpeg = health?.ffmpeg;
  if (!ffmpeg) {
    elements.ffmpeg.textContent = "状态未知";
    elements.ffmpeg.dataset.state = "unknown";
    return;
  }
  const state = String(ffmpeg.state || "unknown");
  elements.ffmpeg.textContent = ffmpeg.message || (state === "ready" ? "ffmpeg 可用" : "ffmpeg 不可用");
  elements.ffmpeg.dataset.state = state;
}

function renderStatus(payload) {
  renderAccount(payload?.account || { state: "anonymous" });
  renderHealth(payload?.health);

  if (payload?.storage?.state === "error") {
    showMessage("账号凭证文件不可读取，请在服务器上检查数据目录。", "error");
  }
}

async function refreshStatus() {
  setBusy(elements.refresh, true);
  try {
    const payload = await bridge.apiGet("accounts/status");
    renderStatus(payload);
    if (payload?.storage?.state !== "error") {
      showMessage("");
    }
  } catch (error) {
    showMessage(messageFrom(error, "无法读取账号状态。"));
  } finally {
    setBusy(elements.refresh, false);
  }
}

function openDialog() {
  elements.loginState.textContent = "正在生成二维码";
  elements.loginState.dataset.state = "waiting";
  elements.qrCode.hidden = true;
  elements.qrCode.removeAttribute("src");
  if (!elements.dialog.open) elements.dialog.showModal();
}

function setLoginSnapshot(snapshot) {
  if (!snapshot || !activeLogin || snapshot.session_id !== activeLogin.session_id) return;

  const state = String(snapshot.state || "waiting");
  const message = snapshot.message || stateLabels[state] || "正在处理登录";
  activeLogin = { ...activeLogin, state, message };
  elements.loginState.textContent = message;
  elements.loginState.dataset.state = state;

  if (state === "confirmed") {
    const sessionId = activeLogin.session_id;
    window.setTimeout(() => {
      if (activeLogin?.session_id === sessionId) void closeLogin(false);
    }, 700);
    void refreshStatus();
  }
}

function renderQr(dataUrl) {
  if (typeof dataUrl !== "string" || !dataUrl.startsWith("data:image/png;base64,")) {
    throw new Error("服务端未返回可用二维码。");
  }
  elements.qrCode.src = dataUrl;
  elements.qrCode.hidden = false;
}

async function subscribeLoginEvents(sessionId, generation) {
  const id = await bridge.subscribeSSE(
    `accounts/login/${sessionId}/events`,
    {
      onMessage(event) {
        if (generation !== loginGeneration) return;
        setLoginSnapshot(event.parsed);
      },
      onError() {
        if (generation === loginGeneration && activeLogin && !terminalStates.has(activeLogin.state)) {
          elements.loginState.textContent = "登录状态连接已断开";
          elements.loginState.dataset.state = "failed";
        }
      },
    },
  );
  if (generation !== loginGeneration) {
    await bridge.unsubscribeSSE(id);
    return;
  }
  subscriptionId = id;
}

async function startLogin(button) {
  if (pageBusy) return;
  pageBusy = true;
  setBusy(button, true);
  showMessage("");
  await closeLogin(true);
  const generation = ++loginGeneration;
  openDialog();

  try {
    const snapshot = await bridge.apiPost("accounts/login", {});
    activeLogin = snapshot;
    renderQr(snapshot.qr_data_url);
    setLoginSnapshot(snapshot);
    await subscribeLoginEvents(snapshot.session_id, generation);
  } catch (error) {
    elements.loginState.textContent = messageFrom(error, "无法创建登录二维码。");
    elements.loginState.dataset.state = "failed";
  } finally {
    pageBusy = false;
    setBusy(button, false);
  }
}

async function closeLogin(cancel) {
  const login = activeLogin;
  const currentSubscription = subscriptionId;
  activeLogin = null;
  subscriptionId = null;

  if (currentSubscription !== null) {
    try {
      await bridge.unsubscribeSSE(currentSubscription);
    } catch {
      // The iframe may already be unloading.
    }
  }
  if (cancel && login && !terminalStates.has(login.state)) {
    try {
      await bridge.apiPost(`accounts/login/${login.session_id}/cancel`, {});
    } catch {
      // A completed or expired login needs no additional UI error.
    }
  }
  if (elements.dialog.open) elements.dialog.close();
}

async function logout(button) {
  setBusy(button, true);
  showMessage("");
  try {
    await bridge.apiPost("accounts/logout", {});
    await refreshStatus();
  } catch (error) {
    showMessage(messageFrom(error, "无法退出该账号。"));
  } finally {
    setBusy(button, false);
  }
}

function bindActions() {
  elements.account.querySelector('[data-action="login"]').addEventListener("click", (event) => {
    void startLogin(event.currentTarget);
  });
  elements.account.querySelector('[data-action="logout"]').addEventListener("click", (event) => {
    void logout(event.currentTarget);
  });
  elements.refresh.addEventListener("click", () => void refreshStatus());
  elements.closeLogin.addEventListener("click", () => void closeLogin(true));
  elements.cancelLogin.addEventListener("click", () => void closeLogin(true));
  elements.dialog.addEventListener("cancel", (event) => {
    event.preventDefault();
    void closeLogin(true);
  });
  window.addEventListener("beforeunload", () => {
    loginGeneration += 1;
    void closeLogin(false);
  });
}

async function boot() {
  if (!bridge) {
    showMessage("AstrBot 页面桥接不可用。", "error");
    return;
  }
  try {
    const context = await bridge.ready();
    document.title = context?.pageTitle || "我想听歌！- 音乐账号";
    bindActions();
    await refreshStatus();
  } catch (error) {
    showMessage(messageFrom(error, "无法初始化账号管理页面。"));
  }
}

void boot();
