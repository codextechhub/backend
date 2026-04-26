import axios from "axios";
import { EP } from "./endpoints";

const FALLBACK = import.meta.env.VITE_API_BASE_URL;

export function getBaseUrl() {
  return localStorage.getItem("vs_base_url") || FALLBACK;
}

const api = axios.create();

api.interceptors.request.use((config) => {
  config.baseURL = getBaseUrl();
  const token = localStorage.getItem("access_token");
  if (token) config.headers.Authorization = `Bearer ${token}`;
  return config;
});

// Single shared refresh promise — prevents race conditions when multiple
// requests 401 simultaneously (e.g. the stats row on the home page).
let _refreshPromise = null;

api.interceptors.response.use(
  (res) => res,
  async (error) => {
    const original = error.config;
    if (error.response?.status === 401 && !original._retry) {
      original._retry = true;
      const refresh = localStorage.getItem("refresh_token");
      if (refresh) {
        try {
          if (!_refreshPromise) {
            _refreshPromise = axios
              .post(`${getBaseUrl()}${EP.AUTH_REFRESH}`, { refresh })
              .finally(() => { _refreshPromise = null; });
          }
          const { data } = await _refreshPromise;
          localStorage.setItem("access_token", data.data.access);
          // Store rotated refresh token if the server returns one
          if (data.data.refresh) localStorage.setItem("refresh_token", data.data.refresh);
          original.headers.Authorization = `Bearer ${data.data.access}`;
          return api(original);
        } catch {
          // refresh failed — tokens may be stale, but not redirecting
        }
      }
    }
    return Promise.reject(error);
  }
);

export async function logout() {
  const refresh = localStorage.getItem("refresh_token");
  try {
    await api.post(EP.AUTH_LOGOUT, { refresh });
  } finally {
    localStorage.clear();
    window.location.href = "/";
  }
}

export default api;
