// Shared helpers for talking to the FastAPI backend (same host resolution as the main page).
export function getApiBase(): string {
  if (typeof window === "undefined") return "http://127.0.0.1:8000/api";
  let host = window.location.hostname;
  if (host === "localhost") host = "127.0.0.1";
  return `${window.location.protocol}//${host}:8000/api`;
}

export const authHeaders = (): Record<string, string> => ({
  Authorization: "Bearer local-session",
  "Content-Type": "application/json",
});

export async function api<T = any>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(`${getApiBase()}${path}`, { ...init, headers: { ...authHeaders(), ...(init.headers || {}) } });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail || detail;
    } catch {
      /* non-JSON error body */
    }
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return res.json();
}
