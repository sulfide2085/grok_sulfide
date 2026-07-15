/** Tiny API client with token query + headers. */

export function createApi(getToken) {
  function withToken(path) {
    const token = getToken();
    if (!token) return path;
    const joiner = path.includes("?") ? "&" : "?";
    return `${path}${joiner}token=${encodeURIComponent(token)}`;
  }

  async function api(path, options = {}) {
    const request = { ...options, headers: { ...(options.headers || {}) } };
    const token = getToken();
    if (token) request.headers["X-Grok-WebUI-Token"] = token;
    if (request.method && request.method !== "GET") {
      request.headers["X-Grok-WebUI"] = "1";
      request.headers["Content-Type"] = "application/json";
    }
    const response = await fetch(withToken(path), request);
    const payload = await response
      .json()
      .catch(() => ({ ok: false, error: `HTTP ${response.status}` }));
    if (!response.ok || !payload.ok) {
      throw new Error(payload.error || `HTTP ${response.status}`);
    }
    return payload.data;
  }

  return { api, withToken };
}
