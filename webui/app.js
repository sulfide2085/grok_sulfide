"use strict";

const state = {
  cursor: 0,
  running: false,
  status: null,
  config: null,
  editingPresetId: "",
  inventoryRequestId: 0,
  pollErrorShown: false,
};

const $ = (id) => document.getElementById(id);

async function api(path, options = {}) {
  const request = { ...options, headers: { ...(options.headers || {}) } };
  if (request.method && request.method !== "GET") {
    request.headers["X-Grok-WebUI"] = "1";
    request.headers["Content-Type"] = "application/json";
  }
  const response = await fetch(path, request);
  const payload = await response.json().catch(() => ({ ok: false, error: `HTTP ${response.status}` }));
  if (!response.ok || !payload.ok) {
    throw new Error(payload.error || `HTTP ${response.status}`);
  }
  return payload.data;
}

function toast(message, type = "") {
  const item = document.createElement("div");
  item.className = `toast ${type}`.trim();
  item.textContent = message;
  $("toastRegion").appendChild(item);
  window.setTimeout(() => item.remove(), 3600);
}

function formatTime(iso) {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleTimeString("zh-CN", { hour12: false });
}

function durationLabel(startedAt, running) {
  if (!startedAt) return "未运行";
  const elapsed = Math.max(0, Math.floor((Date.now() - new Date(startedAt).getTime()) / 1000));
  const hours = Math.floor(elapsed / 3600);
  const minutes = Math.floor((elapsed % 3600) / 60);
  const seconds = elapsed % 60;
  const value = [hours, minutes, seconds].map((part) => String(part).padStart(2, "0")).join(":");
  return running ? value : `结束于 ${formatTime(state.status?.ended_at)}`;
}

function setRunState(status) {
  state.status = status;
  state.running = Boolean(status.running);
  const badge = $("runState");
  badge.className = "status-badge";
  if (status.running) {
    badge.classList.add("running");
    badge.lastChild.textContent = "运行中";
  } else if (typeof status.exit_code === "number" && status.exit_code !== 0) {
    badge.classList.add("failed");
    badge.lastChild.textContent = "异常结束";
  } else {
    badge.classList.add("idle");
    badge.lastChild.textContent = "空闲";
  }

  $("accountCount").textContent = String(status.accounts?.total ?? 0);
  $("accountsPath").textContent = status.accounts?.path || "accounts_cli.txt";
  $("cpaCount").textContent = String(status.cpa?.count ?? 0);
  $("cpaPath").textContent = status.cpa?.path || "cpa_auths";
  $("mailCount").textContent = String(status.mailbox?.count ?? 0);
  $("mailPath").textContent = status.mailbox?.path || "mail_credentials.txt";
  $("processId").textContent = status.pid ? String(status.pid) : "—";
  $("runDuration").textContent = durationLabel(status.started_at, status.running);

  $("startButton").disabled = status.running;
  $("stopButton").disabled = !status.running;
  $("saveConfigButton").disabled = status.running;
  $("lastExitState").textContent = status.running
    ? `PID ${status.pid} · ${formatTime(status.started_at)} 启动`
    : typeof status.exit_code === "number"
      ? `退出码 ${status.exit_code} · ${formatTime(status.ended_at)}`
      : "等待任务";

  $("configState").textContent = status.config_exists ? "config.json 已加载" : "使用示例配置";
  renderReadiness(status);
  renderAccounts(status.accounts?.items || []);
}

function renderReadiness(status) {
  const notice = $("readinessNotice");
  const warnings = [];
  const preset = getPreset($("runPresetInput")?.value);
  const values = preset?.values || state.config?.values || {};
  const secrets = preset?.secrets || state.config?.secrets || {};
  const method = values.registration_method || "browser";
  const provider = presetProvider(values);
  if (!status.config_exists) warnings.push("尚未保存 config.json");
  const mailbox = preset?.mailbox || status.mailbox;
  if (["hotmail", "outlook"].includes(provider) && !(mailbox?.count > 0)) {
    warnings.push("Hotmail 凭据文件为空或不存在");
  }
  if (method === "protocol" && provider === "moemail" && !secrets.protocol_moemail_api_key) {
    warnings.push("MoeMail API Key 未配置");
  }
  notice.hidden = warnings.length === 0;
  notice.textContent = warnings.join("；");
}

function renderAccounts(items) {
  const body = $("accountsTableBody");
  body.replaceChildren();
  if (!items.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 3;
    cell.className = "empty-cell";
    cell.textContent = "暂无账号";
    row.appendChild(cell);
    body.appendChild(row);
    return;
  }
  for (const item of items) {
    const row = document.createElement("tr");
    const index = document.createElement("td");
    index.textContent = String(item.index);
    const email = document.createElement("td");
    email.textContent = item.email;
    email.title = item.email;
    const cpa = document.createElement("td");
    const marker = document.createElement("span");
    marker.className = `cpa-state ${item.has_cpa ? "ready" : ""}`.trim();
    marker.textContent = item.has_cpa ? "已生成" : "待生成";
    cpa.appendChild(marker);
    row.append(index, email, cpa);
    body.appendChild(row);
  }
}

function appendLogs(items) {
  if (!items.length) return;
  const terminal = $("terminal");
  const nearBottom = terminal.scrollHeight - terminal.scrollTop - terminal.clientHeight < 80;
  terminal.querySelector(".terminal-empty")?.remove();
  const fragment = document.createDocumentFragment();
  for (const item of items) {
    const row = document.createElement("div");
    row.className = `log-line ${item.kind || "output"}`;
    const time = document.createElement("span");
    time.className = "log-time";
    time.textContent = formatTime(item.time);
    const text = document.createElement("span");
    text.className = "log-text";
    text.textContent = item.text;
    row.append(time, text);
    fragment.appendChild(row);
  }
  terminal.appendChild(fragment);
  while (terminal.children.length > 1200) terminal.firstElementChild?.remove();
  if ($("autoScrollInput").checked && nearBottom) terminal.scrollTop = terminal.scrollHeight;
}

async function refreshStatus(showError = false) {
  try {
    const data = await api("/api/status");
    setRunState(data);
    state.pollErrorShown = false;
  } catch (error) {
    if (showError || !state.pollErrorShown) toast(error.message, "error");
    state.pollErrorShown = true;
  }
}

async function refreshLogs() {
  try {
    const data = await api(`/api/logs?after=${state.cursor}`);
    appendLogs(data.items || []);
    state.cursor = data.cursor || state.cursor;
  } catch (error) {
    if (!state.pollErrorShown) toast(error.message, "error");
    state.pollErrorShown = true;
  }
}

function setValue(id, value) {
  const element = $(id);
  if (element.type === "checkbox") element.checked = Boolean(value);
  else element.value = value ?? "";
}

function getPreset(id) {
  return (state.config?.presets || []).find((preset) => preset.id === id) || null;
}

function presetProvider(values) {
  if ((values.registration_method || "browser") === "protocol") {
    return values.protocol_email_provider || "outlook";
  }
  const provider = values.email_provider || "hotmail";
  return provider === "hotmail" ? "outlook" : provider;
}

function updateProviderFields() {
  const method = $("registrationMethodConfig").value;
  const select = $("emailProviderConfig");
  const moeMailOption = [...select.options].find((option) => option.value === "moemail");
  if (moeMailOption) {
    moeMailOption.disabled = method !== "protocol";
    moeMailOption.hidden = method !== "protocol";
  }
  if (method !== "protocol" && select.value === "moemail") select.value = "outlook";
  const provider = select.value;
  document.querySelectorAll(".provider-fields").forEach((section) => {
    section.hidden = !section.classList.contains(`provider-${provider}-fields`);
  });
  document.querySelectorAll(".protocol-only-fields").forEach((section) => {
    section.hidden = method !== "protocol";
  });
}

function fillPresetValues(preset) {
  const values = preset?.values || {};
  const secrets = preset?.secrets || {};
  state.editingPresetId = preset?.id || "";
  setValue("presetNameConfig", preset?.name || "");
  setValue("registrationMethodConfig", values.registration_method || "browser");
  setValue("emailProviderConfig", presetProvider(values));
  setValue("mailFileConfig", values.hotmail_accounts_file || "mail_credentials.txt");
  setValue("proxyConfig", values.proxy || "");
  setValue("emailProxyConfig", values.email_proxy || "direct");
  setValue("cpaProxyConfig", values.cpa_proxy || "");
  setValue("cpaExportConfig", values.cpa_export_enabled !== false);
  setValue("cpaHeadedConfig", values.cpa_headless === false);
  setValue("cpaUploadConfig", Boolean(values.cpa_management_upload_enabled));
  setValue("cpaSshUploadConfig", Boolean(values.cpa_ssh_upload_enabled));
  setValue("cpaDirConfig", values.cpa_auth_dir || "cpa_auths");
  setValue("cpaBaseConfig", values.cpa_base_url || "");
  setValue("cpaManagementBaseConfig", values.cpa_management_base || "");
  setValue("cpaSshHostConfig", values.cpa_ssh_host || "example-ssh-host");
  setValue("cpaSshDirConfig", values.cpa_ssh_auth_dir || "/path/to/cliproxyapi/auths");
  setValue("cpaSshChmodConfig", values.cpa_ssh_chmod || "600");
  setValue("protocolMoeMailBaseConfig", values.protocol_moemail_base_url || "");
  setValue("protocolMoeMailDomainConfig", values.protocol_moemail_domain || "");
  setValue("protocolProxyConfig", values.protocol_proxy || "");
  setValue("cloudflareBaseConfig", values.cloudflare_api_base || "");
  setValue("cloudmailUrlConfig", values.cloudmail_url || "");
  setValue("cloudmailAdminConfig", values.cloudmail_admin_email || "");
  $("cpaManagementKeyConfig").placeholder = secrets.cpa_management_key
    ? "已配置，留空保持现有值"
    : "未配置";
  $("protocolMoeMailKeyConfig").placeholder = secrets.protocol_moemail_api_key
    ? "已配置，留空保持现有值"
    : "未配置";
  $("protocolYesCaptchaKeyConfig").placeholder = secrets.protocol_yescaptcha_key
    ? "已配置，留空保持现有值"
    : "未配置";
  for (const [id, key] of [
    ["duckmailKeyConfig", "duckmail_api_key"],
    ["yydsKeyConfig", "yyds_api_key"],
    ["yydsJwtConfig", "yyds_jwt"],
    ["cloudflareKeyConfig", "cloudflare_api_key"],
    ["cloudmailPasswordConfig", "cloudmail_password"],
  ]) {
    $(id).value = "";
    $(id).placeholder = secrets[key] ? "已配置，留空保持现有值" : "未配置";
  }
  updateProviderFields();
}

function renderPresetOptions(config) {
  for (const id of ["runPresetInput", "presetSelectConfig"]) {
    const select = $(id);
    select.replaceChildren();
    for (const preset of config.presets || []) {
      const option = document.createElement("option");
      option.value = preset.id;
      option.textContent = preset.name;
      select.appendChild(option);
    }
    select.value = config.active_preset_id || config.presets?.[0]?.id || "";
  }
}

function updateRunPreset() {
  const preset = getPreset($("runPresetInput").value);
  if (!preset) return;
  const method = preset.values.registration_method === "protocol" ? "协议" : "浏览器";
  const provider = presetProvider(preset.values);
  $("presetSummary").textContent = `${method} · ${provider}`;
  $("threadsInput").value = String(preset.values.register_threads ?? 1);
  $("mintWorkersInput").value = String(preset.values.cpa_mint_workers ?? -1);
  const isOutlook = ["outlook", "hotmail"].includes(provider);
  $("aliasRunSection").hidden = !isOutlook;
  $("aliasEnabledInput").checked = isOutlook && (preset.values.hotmail_alias_mode || "primary") !== "primary";
  $("aliasLimitInput").value = String(preset.values.hotmail_max_aliases_per_account ?? 1);
  updateAliasControls();
  renderReadiness(state.status || {});
}

function updateAliasControls() {
  const enabled = $("aliasEnabledInput").checked;
  $("aliasLimitField").hidden = !enabled;
  refreshMailInventory();
}

function renderMailInventory(inventory) {
  const aliasMode = inventory.mode === "alias";
  $("mailInventorySummary").textContent = aliasMode
    ? `${inventory.available_mailboxes} 个邮箱可用 · 可生成 ${inventory.alias_capacity} 个别名账号`
    : `${inventory.primary_available} 个未注册主邮箱可用`;
  const list = $("mailInventoryList");
  list.replaceChildren();
  for (const item of inventory.items || []) {
    const row = document.createElement("div");
    row.className = "mail-inventory-row";
    const email = document.createElement("span");
    email.className = "mail-inventory-email";
    email.textContent = item.email;
    const used = document.createElement("span");
    used.textContent = aliasMode ? `已用 ${item.aliases_used}` : "主邮箱";
    const remaining = document.createElement("span");
    remaining.className = "mail-inventory-number";
    remaining.textContent = aliasMode ? `剩 ${item.remaining}` : "可用";
    row.append(email, used, remaining);
    list.appendChild(row);
  }
  if (!inventory.items?.length) {
    const row = document.createElement("div");
    row.className = "mail-inventory-row";
    row.textContent = "当前模式没有可用邮箱";
    list.appendChild(row);
  }
}

async function refreshMailInventory() {
  if ($("aliasRunSection").hidden) return;
  const presetId = $("runPresetInput").value;
  const enabled = $("aliasEnabledInput").checked;
  const limit = Math.max(1, Math.min(Number($("aliasLimitInput").value) || 1, 1000));
  const requestId = ++state.inventoryRequestId;
  try {
    const inventory = await api(
      `/api/mail-inventory?preset_id=${encodeURIComponent(presetId)}`
      + `&alias_enabled=${enabled ? "1" : "0"}&alias_limit=${limit}`,
    );
    if (requestId !== state.inventoryRequestId) return;
    renderMailInventory(inventory);
  } catch (error) {
    $("mailInventorySummary").textContent = error.message;
  }
}

function fillConfig(config) {
  state.config = config;
  renderPresetOptions(config);
  const active = getPreset(config.active_preset_id) || config.presets?.[0] || {
    id: "",
    name: "默认配置",
    values: config.values || {},
    secrets: config.secrets || {},
  };
  fillPresetValues(active);
  $("mailboxState").textContent = config.mailbox?.exists
    ? `${config.mailbox.count} 条 · ${config.mailbox.path}`
    : "文件不存在";
  updateRunPreset();
  renderReadiness(state.status || { config_exists: config.exists, mailbox: config.mailbox });
}

async function refreshConfig(showError = false) {
  try {
    fillConfig(await api("/api/config"));
  } catch (error) {
    if (showError) toast(error.message, "error");
  }
}

function readRunForm() {
  const mode = document.querySelector('input[name="mode"]:checked')?.value || "extra";
  const presetId = $("runPresetInput").value;
  const preset = getPreset(presetId);
  return {
    mode,
    preset_id: presetId,
    registration_method: preset?.values?.registration_method || "browser",
    alias_enabled: $("aliasEnabledInput").checked,
    alias_limit: Number($("aliasLimitInput").value) || 1,
    amount: Number($("amountInput").value),
    threads: Number($("threadsInput").value),
    mint_workers: Number($("mintWorkersInput").value),
    browser_recycle_every: Number($("recycleInput").value),
    accounts_file: $("accountsFileInput").value.trim() || "accounts_cli.txt",
    fast: $("fastInput").checked,
    no_browser_reuse: !$("browserReuseInput").checked,
    cookie_snapshot: $("cookieSnapshotInput").checked,
  };
}

function readConfigForm() {
  const clearSecrets = [];
  if ($("clearManagementKeyConfig").checked) clearSecrets.push("cpa_management_key");
  if ($("clearProtocolSecretsConfig").checked) {
    clearSecrets.push(
      "protocol_moemail_api_key",
      "protocol_yescaptcha_key",
      "duckmail_api_key",
      "yyds_api_key",
      "yyds_jwt",
      "cloudflare_api_key",
      "cloudmail_password",
    );
  }
  const registrationMethod = $("registrationMethodConfig").value;
  const provider = $("emailProviderConfig").value;
  const browserProvider = provider === "outlook" ? "hotmail" : provider;
  return {
    preset_id: state.editingPresetId,
    preset_name: $("presetNameConfig").value.trim() || "未命名预设",
    values: {
      registration_method: registrationMethod,
      protocol_email_provider: provider,
      email_provider: browserProvider === "moemail" ? "hotmail" : browserProvider,
      hotmail_accounts_file: $("mailFileConfig").value.trim(),
      proxy: $("proxyConfig").value.trim(),
      email_proxy: $("emailProxyConfig").value.trim(),
      cpa_proxy: $("cpaProxyConfig").value.trim(),
      register_threads: Number($("threadsInput").value),
      cpa_export_enabled: $("cpaExportConfig").checked,
      cpa_auth_dir: $("cpaDirConfig").value.trim(),
      cpa_base_url: $("cpaBaseConfig").value.trim(),
      cpa_management_upload_enabled: $("cpaUploadConfig").checked,
      cpa_management_base: $("cpaManagementBaseConfig").value.trim(),
      cpa_management_key: $("cpaManagementKeyConfig").value,
      cpa_ssh_upload_enabled: $("cpaSshUploadConfig").checked,
      cpa_ssh_host: $("cpaSshHostConfig").value.trim(),
      cpa_ssh_auth_dir: $("cpaSshDirConfig").value.trim(),
      cpa_ssh_chmod: $("cpaSshChmodConfig").value,
      protocol_moemail_base_url: $("protocolMoeMailBaseConfig").value.trim(),
      protocol_moemail_domain: $("protocolMoeMailDomainConfig").value.trim(),
      protocol_moemail_api_key: $("protocolMoeMailKeyConfig").value,
      protocol_yescaptcha_key: $("protocolYesCaptchaKeyConfig").value,
      protocol_proxy: $("protocolProxyConfig").value.trim(),
      duckmail_api_key: $("duckmailKeyConfig").value,
      yyds_api_key: $("yydsKeyConfig").value,
      yyds_jwt: $("yydsJwtConfig").value,
      cloudflare_api_base: $("cloudflareBaseConfig").value.trim(),
      cloudflare_api_key: $("cloudflareKeyConfig").value,
      cloudmail_url: $("cloudmailUrlConfig").value.trim(),
      cloudmail_admin_email: $("cloudmailAdminConfig").value.trim(),
      cloudmail_password: $("cloudmailPasswordConfig").value,
      cpa_headless: !$("cpaHeadedConfig").checked,
      cpa_force_standalone: true,
      cpa_mint_workers: Number($("mintWorkersInput").value),
    },
    clear_secrets: clearSecrets,
  };
}

function switchTab(name) {
  document.querySelectorAll(".tab-button").forEach((button) => {
    const active = button.dataset.tab === name;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  });
  $("runPanel").hidden = name !== "run";
  $("configPanel").hidden = name !== "config";
  $("runPanel").classList.toggle("active", name === "run");
  $("configPanel").classList.toggle("active", name === "config");
}

async function startTask(event) {
  event.preventDefault();
  $("startButton").disabled = true;
  try {
    setRunState(await api("/api/start", { method: "POST", body: JSON.stringify(readRunForm()) }));
    toast("任务已启动", "success");
    await refreshLogs();
  } catch (error) {
    toast(error.message, "error");
    await refreshStatus();
  }
}

async function stopTask() {
  $("stopButton").disabled = true;
  try {
    setRunState(await api("/api/stop", { method: "POST", body: "{}" }));
    toast("停止请求已完成", "success");
    await refreshLogs();
  } catch (error) {
    toast(error.message, "error");
  } finally {
    await refreshStatus();
  }
}

async function saveConfig(event) {
  event.preventDefault();
  $("saveConfigButton").disabled = true;
  try {
    const config = await api("/api/config", {
      method: "POST",
      body: JSON.stringify(readConfigForm()),
    });
    $("cpaManagementKeyConfig").value = "";
    $("clearManagementKeyConfig").checked = false;
    for (const id of [
      "protocolMoeMailKeyConfig",
      "protocolYesCaptchaKeyConfig",
      "duckmailKeyConfig",
      "yydsKeyConfig",
      "yydsJwtConfig",
      "cloudflareKeyConfig",
      "cloudmailPasswordConfig",
    ]) $(id).value = "";
    $("clearProtocolSecretsConfig").checked = false;
    fillConfig(config);
    toast("配置已保存", "success");
    await refreshStatus();
  } catch (error) {
    toast(error.message, "error");
  } finally {
    $("saveConfigButton").disabled = state.running;
  }
}

function newPreset() {
  state.editingPresetId = `preset_${Date.now().toString(36)}`;
  $("presetNameConfig").value = "新预设";
  $("presetSelectConfig").value = "";
  toast("编辑完成后保存新预设");
}

async function deletePreset() {
  const presetId = state.editingPresetId;
  if (!presetId || !getPreset(presetId)) {
    toast("当前是尚未保存的新预设");
    return;
  }
  try {
    const config = await api("/api/config", {
      method: "POST",
      body: JSON.stringify({ delete_preset_id: presetId }),
    });
    fillConfig(config);
    toast("预设已删除", "success");
  } catch (error) {
    toast(error.message, "error");
  }
}

async function clearLogs() {
  try {
    await api("/api/logs/clear", { method: "POST", body: "{}" });
    state.cursor = 0;
    $("terminal").replaceChildren();
    const empty = document.createElement("div");
    empty.className = "terminal-empty";
    empty.textContent = "暂无日志";
    $("terminal").appendChild(empty);
  } catch (error) {
    toast(error.message, "error");
  }
}

function exportLogs() {
  const lines = [...$("terminal").querySelectorAll(".log-line")].map((row) => {
    const time = row.querySelector(".log-time")?.textContent || "";
    const text = row.querySelector(".log-text")?.textContent || "";
    return `[${time}] ${text}`;
  });
  if (!lines.length) {
    toast("没有可导出的日志");
    return;
  }
  const blob = new Blob([`${lines.join("\n")}\n`], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `grok-sulfide-${new Date().toISOString().replaceAll(":", "-")}.log`;
  link.click();
  URL.revokeObjectURL(url);
}

function bindEvents() {
  $("runPresetInput").addEventListener("change", updateRunPreset);
  $("aliasEnabledInput").addEventListener("change", updateAliasControls);
  $("aliasLimitInput").addEventListener("input", refreshMailInventory);
  $("presetSelectConfig").addEventListener("change", () => {
    const preset = getPreset($("presetSelectConfig").value);
    if (preset) fillPresetValues(preset);
  });
  $("registrationMethodConfig").addEventListener("change", updateProviderFields);
  $("emailProviderConfig").addEventListener("change", updateProviderFields);
  $("newPresetButton").addEventListener("click", newPreset);
  $("deletePresetButton").addEventListener("click", deletePreset);
  document.querySelectorAll(".tab-button").forEach((button) => {
    button.addEventListener("click", () => switchTab(button.dataset.tab));
  });
  document.querySelectorAll('input[name="mode"]').forEach((input) => {
    input.addEventListener("change", () => {
      const extra = input.value === "extra" && input.checked;
      if (!input.checked) return;
      $("amountLabel").textContent = extra ? "新增数量" : "目标总数";
      $("amountInput").min = extra ? "1" : "0";
      $("amountInput").max = extra ? "10000" : "100000";
      if (extra && Number($("amountInput").value) < 1) $("amountInput").value = "1";
    });
  });
  $("runForm").addEventListener("submit", startTask);
  $("stopButton").addEventListener("click", stopTask);
  $("configForm").addEventListener("submit", saveConfig);
  $("refreshButton").addEventListener("click", async () => {
    await Promise.all([refreshConfig(true), refreshStatus(true), refreshLogs()]);
  });
  $("clearLogsButton").addEventListener("click", clearLogs);
  $("exportLogsButton").addEventListener("click", exportLogs);
}

async function initialize() {
  bindEvents();
  await refreshConfig(true);
  await refreshStatus(true);
  await refreshLogs();
  window.setInterval(refreshLogs, 700);
  window.setInterval(refreshStatus, 1500);
  window.setInterval(() => {
    if (state.status) $("runDuration").textContent = durationLabel(state.status.started_at, state.status.running);
  }, 1000);
}

initialize();
