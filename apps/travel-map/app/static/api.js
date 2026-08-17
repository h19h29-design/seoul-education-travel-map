const API_ROOT = "/api/v1";

class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function request(path, options = {}) {
  const response = await fetch(`${API_ROOT}${path}`, {
    ...options,
    headers: { Accept: "application/json", ...options.headers },
  });
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    throw new ApiError(payload?.error?.code || "요청을 처리하지 못했습니다.", response.status);
  }
  return payload;
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
};

export { ApiError };
