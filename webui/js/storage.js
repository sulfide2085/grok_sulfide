/** Local temporary UI preferences (not secrets). */
export const RUN_DRAFT_KEY = "grok_webui_run_draft_v1";
export const UI_PREFS_KEY = "grok_webui_ui_prefs_v1";
export const TOKEN_KEY = "grok_webui_token";

export function loadJson(key, fallback = null) {
  try {
    const raw = window.localStorage.getItem(key);
    if (!raw) return fallback;
    return JSON.parse(raw);
  } catch (_) {
    return fallback;
  }
}

export function saveJson(key, value) {
  try {
    window.localStorage.setItem(key, JSON.stringify(value));
    return true;
  } catch (_) {
    return false;
  }
}

export function removeKey(key) {
  try {
    window.localStorage.removeItem(key);
  } catch (_) {
    /* ignore */
  }
}

export function readTokenFromUrl() {
  try {
    const params = new URLSearchParams(window.location.search || "");
    const token = (params.get("token") || params.get("access_token") || "").trim();
    if (token) {
      window.sessionStorage.setItem(TOKEN_KEY, token);
      return token;
    }
    return (window.sessionStorage.getItem(TOKEN_KEY) || "").trim();
  } catch (_) {
    return "";
  }
}
