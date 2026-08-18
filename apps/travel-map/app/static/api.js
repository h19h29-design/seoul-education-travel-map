const API_ROOT = "/api/v1";

class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function request(path, options = {}) {
  const url = new URL(`${API_ROOT}${path}`, window.location.origin);
  const method = String(options.method ?? "GET").toUpperCase();
  const headers = new Headers({ Accept: "application/json" });
  new Headers(options.headers ?? {}).forEach((value, name) => headers.set(name, value));
  if (url.origin === window.location.origin && ["POST", "PUT", "DELETE"].includes(method)) {
    const csrf = readCookie("__Host-travel_csrf");
    if (csrf !== null) headers.set("X-CSRF-Token", csrf);
  }
  const response = await fetch(url, {
    ...options,
    credentials: "same-origin",
    headers,
    method,
  });
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    throw new ApiError(payload?.error?.code || "요청을 처리하지 못했습니다.", response.status);
  }
  return payload;
}

function readCookie(name) {
  for (const item of document.cookie.split(";")) {
    const separator = item.indexOf("=");
    if (separator < 0 || item.slice(0, separator).trim() !== name) continue;
    try {
      return decodeURIComponent(item.slice(separator + 1));
    } catch {
      return null;
    }
  }
  return null;
}

function queryString(values, includeEmpty = new Set()) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) {
    if (value !== undefined && value !== null && (value !== "" || includeEmpty.has(key))) {
      query.set(key, String(value));
    }
  }
  return query.toString();
}

export const api = {
  bootstrap: () => request("/bootstrap"),
  institutionFacets: (options = {}) => request("/institutions/facets", options),
  institutions: (filters, options = {}) => request(`/institutions?${queryString({
    q: filters.q,
    limit: filters.limit ?? 20,
    offset: filters.offset ?? 0,
    institution_type: filters.institutionType,
    foundation_type: filters.foundationType,
    education_office: filters.educationOffice,
    district: filters.district,
  }, new Set(["q"]))}`, options),
  places: (query, options = {}) => request(`/places?${queryString({ q: query })}`, options),
  currentPolicy: (options = {}) => request("/policy/current", options),
  reversePlace: ({ latitude, longitude }, options = {}) => request(`/places/reverse?${queryString({ latitude, longitude })}`, options),
  preview: (payload) => request("/trips/preview", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  }),
  geodata: (name) => request(`/geodata/${name}`),
  me: (options = {}) => request("/me", options),
  settings: (options = {}) => request("/me/settings", options),
  history: ({ cursor = null, limit = 50 } = {}, options = {}) => request(`/me/history?${queryString({
    cursor,
    limit,
  })}`, options),
  historyDetail: (id, options = {}) => request(`/me/history/${encodeURIComponent(id)}`, options),
  deleteHistory: (id) => request(`/me/history/${encodeURIComponent(id)}`, { method: "DELETE" }),
  deleteAllHistory: () => request("/me/history", { method: "DELETE" }),
  logout: () => request("/auth/logout", { method: "POST" }),
  deleteMyData: () => request("/me/data", { method: "DELETE" }),
};

export { ApiError };
