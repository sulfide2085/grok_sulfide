/** Persist / restore run-panel temporary settings (non-secret). */
import { $, toast } from "./dom.js";
import { loadJson, removeKey, RUN_DRAFT_KEY, saveJson } from "./storage.js";

const DRAFT_VERSION = 1;

export function collectRunDraft() {
  const mode = document.querySelector('input[name="mode"]:checked')?.value || "extra";
  return {
    v: DRAFT_VERSION,
    saved_at: new Date().toISOString(),
    preset_id: $("runPresetInput")?.value || "",
    mode,
    amount: Number($("amountInput")?.value) || 1,
    threads: Number($("threadsInput")?.value) || 1,
    mint_workers: Number($("mintWorkersInput")?.value) || -1,
    browser_recycle_every: Number($("recycleInput")?.value) || 25,
    accounts_file: ($("accountsFileInput")?.value || "").trim() || "accounts_cli.txt",
    alias_enabled: Boolean($("aliasEnabledInput")?.checked),
    alias_limit: Number($("aliasLimitInput")?.value) || 1,
    fast: Boolean($("fastInput")?.checked),
    browser_reuse: Boolean($("browserReuseInput")?.checked),
    cookie_snapshot: Boolean($("cookieSnapshotInput")?.checked),
    auto_scroll: Boolean($("autoScrollInput")?.checked),
  };
}

export function saveRunDraft(silent = true) {
  const ok = saveJson(RUN_DRAFT_KEY, collectRunDraft());
  if (!silent) toast(ok ? "临时运行配置已记住" : "无法写入本地存储", ok ? "success" : "error");
  return ok;
}

export function loadRunDraft() {
  const draft = loadJson(RUN_DRAFT_KEY, null);
  if (!draft || draft.v !== DRAFT_VERSION) return null;
  return draft;
}

export function applyRunDraft(draft, { preferPreset = true } = {}) {
  if (!draft) return false;
  if (preferPreset && draft.preset_id && $("runPresetInput")) {
    const has = [...$("runPresetInput").options].some((o) => o.value === draft.preset_id);
    if (has) $("runPresetInput").value = draft.preset_id;
  }
  if (draft.mode) {
    const radio = document.querySelector(`input[name="mode"][value="${draft.mode}"]`);
    if (radio) radio.checked = true;
  }
  if ($("amountInput") && draft.amount != null) $("amountInput").value = String(draft.amount);
  if ($("threadsInput") && draft.threads != null) $("threadsInput").value = String(draft.threads);
  if ($("mintWorkersInput") && draft.mint_workers != null) {
    $("mintWorkersInput").value = String(draft.mint_workers);
  }
  if ($("recycleInput") && draft.browser_recycle_every != null) {
    $("recycleInput").value = String(draft.browser_recycle_every);
  }
  if ($("accountsFileInput") && draft.accounts_file) {
    $("accountsFileInput").value = draft.accounts_file;
  }
  if ($("aliasEnabledInput") && draft.alias_enabled != null) {
    $("aliasEnabledInput").checked = Boolean(draft.alias_enabled);
  }
  if ($("aliasLimitInput") && draft.alias_limit != null) {
    $("aliasLimitInput").value = String(draft.alias_limit);
  }
  if ($("fastInput") && draft.fast != null) $("fastInput").checked = Boolean(draft.fast);
  if ($("browserReuseInput") && draft.browser_reuse != null) {
    $("browserReuseInput").checked = Boolean(draft.browser_reuse);
  }
  if ($("cookieSnapshotInput") && draft.cookie_snapshot != null) {
    $("cookieSnapshotInput").checked = Boolean(draft.cookie_snapshot);
  }
  if ($("autoScrollInput") && draft.auto_scroll != null) {
    $("autoScrollInput").checked = Boolean(draft.auto_scroll);
  }
  // sync amount label / limits
  const extra = (document.querySelector('input[name="mode"]:checked')?.value || "extra") === "extra";
  if ($("amountLabel")) $("amountLabel").textContent = extra ? "新增数量" : "目标总数";
  if ($("amountInput")) {
    $("amountInput").min = extra ? "1" : "0";
    $("amountInput").max = extra ? "10000" : "100000";
  }
  return true;
}

export function clearRunDraft() {
  removeKey(RUN_DRAFT_KEY);
  toast("已清除记住的临时运行配置");
}

export function bindRunDraftAutosave(onChange) {
  const ids = [
    "runPresetInput",
    "amountInput",
    "threadsInput",
    "mintWorkersInput",
    "recycleInput",
    "accountsFileInput",
    "aliasEnabledInput",
    "aliasLimitInput",
    "fastInput",
    "browserReuseInput",
    "cookieSnapshotInput",
    "autoScrollInput",
  ];
  const handler = () => {
    saveRunDraft(true);
    if (typeof onChange === "function") onChange();
  };
  for (const id of ids) {
    const el = $(id);
    if (!el) continue;
    el.addEventListener("change", handler);
    el.addEventListener("input", handler);
  }
  document.querySelectorAll('input[name="mode"]').forEach((input) => {
    input.addEventListener("change", handler);
  });
}
