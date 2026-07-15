/** DOM helpers and small UI utilities. */

export const $ = (id) => document.getElementById(id);

export function toast(message, type = "") {
  const region = $("toastRegion");
  if (!region) return;
  const item = document.createElement("div");
  item.className = `toast ${type}`.trim();
  item.textContent = message;
  region.appendChild(item);
  window.setTimeout(() => item.remove(), 3600);
}

export function formatTime(iso) {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleTimeString("zh-CN", { hour12: false });
}

export function durationLabel(startedAt, running, endedAt) {
  if (!startedAt) return "未运行";
  const elapsed = Math.max(0, Math.floor((Date.now() - new Date(startedAt).getTime()) / 1000));
  const hours = Math.floor(elapsed / 3600);
  const minutes = Math.floor((elapsed % 3600) / 60);
  const seconds = elapsed % 60;
  const value = [hours, minutes, seconds].map((part) => String(part).padStart(2, "0")).join(":");
  return running ? value : `结束于 ${formatTime(endedAt)}`;
}

export function setValue(id, value) {
  const element = $(id);
  if (!element) return;
  if (element.type === "checkbox") element.checked = Boolean(value);
  else element.value = value ?? "";
}

export function switchTab(name) {
  document.querySelectorAll(".tab-button").forEach((button) => {
    const active = button.dataset.tab === name;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  });
  const runPanel = $("runPanel");
  const configPanel = $("configPanel");
  if (runPanel) {
    runPanel.hidden = name !== "run";
    runPanel.classList.toggle("active", name === "run");
  }
  if (configPanel) {
    configPanel.hidden = name !== "config";
    configPanel.classList.toggle("active", name === "config");
  }
}
