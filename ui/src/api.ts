// Thin client for the AI-OS control-plane API.

const TOKEN_KEY = "aios.token";

export function getToken(): string | null {
  try { return localStorage.getItem(TOKEN_KEY); } catch { return null; }
}
export function setToken(token: string | null) {
  try { token ? localStorage.setItem(TOKEN_KEY, token) : localStorage.removeItem(TOKEN_KEY); } catch { /* storage unavailable */ }
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) { super(message); this.status = status; }
}

let onUnauthorized: () => void = () => {};
export function setUnauthorizedHandler(fn: () => void) { onUnauthorized = fn; }

async function request(method: string, path: string, body?: unknown, params?: Record<string, any>) {
  const url = new URL("/api/v1" + path, window.location.origin);
  for (const [k, v] of Object.entries(params ?? {})) if (v !== undefined && v !== null && v !== "") url.searchParams.set(k, String(v));
  const headers: Record<string, string> = {};
  const token = getToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(url, { method, headers, body: body !== undefined ? JSON.stringify(body) : undefined });
  if (res.status === 401 && !path.startsWith("/auth/")) onUnauthorized();
  const text = await res.text();
  let data: any = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!res.ok) {
    let detail = data?.detail ?? res.statusText;
    if (Array.isArray(detail)) detail = detail.map((d: any) => `${(d.loc ?? []).slice(1).join(".")}: ${d.msg}`).join("; ");
    throw new ApiError(res.status, String(detail));
  }
  return data;
}

export const api = {
  get: (path: string, params?: Record<string, any>) => request("GET", path, undefined, params),
  post: (path: string, body: unknown = {}) => request("POST", path, body),
  put: (path: string, body: unknown = {}) => request("PUT", path, body),
  patch: (path: string, body: unknown = {}) => request("PATCH", path, body),
  del: (path: string, params?: Record<string, any>) => request("DELETE", path, undefined, params),
};

export type LiveEvent = {
  id: number; ts: string; namespace_id: string; kind: string; level: string; actor: string; subject: string; message: string; data: any;
};

/** Subscribe to the server-sent event stream. Uses fetch so the bearer token stays in a header, not in the URL. */
export function streamEvents(onEvent: (e: LiveEvent) => void, onState: (connected: boolean) => void): () => void {
  let stopped = false;
  let controller: AbortController | null = null;
  let last = 0;

  async function run() {
    while (!stopped) {
      controller = new AbortController();
      try {
        const res = await fetch(`/api/v1/events/stream${last ? `?after=${last}` : ""}`, {
          headers: { Authorization: `Bearer ${getToken()}` }, signal: controller.signal,
        });
        if (!res.ok || !res.body) throw new Error(`stream ${res.status}`);
        onState(true);
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          let idx;
          while ((idx = buffer.indexOf("\n\n")) >= 0) {
            const chunk = buffer.slice(0, idx);
            buffer = buffer.slice(idx + 2);
            const line = chunk.split("\n").find((l) => l.startsWith("data: "));
            if (!line) continue;
            try { const ev = JSON.parse(line.slice(6)); last = ev.id; onEvent(ev); } catch { /* ignore malformed frame */ }
          }
        }
      } catch { /* dropped connection: retry below */ }
      onState(false);
      if (!stopped) await new Promise((r) => setTimeout(r, 2000));
    }
  }
  run();
  return () => { stopped = true; controller?.abort(); };
}
