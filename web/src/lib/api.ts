export class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
  }
}

export const UNAUTHORIZED_EVENT = "stigmergy:unauthorized";

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = {};
  const isForm = body instanceof FormData;
  if (method !== "GET") headers["X-Stigmergy-CSRF"] = "1";
  if (body !== undefined && !isForm) headers["Content-Type"] = "application/json";
  let response: Response;
  try {
    response = await fetch(`/admin/api/${path}`, {
      method,
      headers,
      credentials: "same-origin",
      body: body === undefined ? undefined : isForm ? body : JSON.stringify(body),
    });
  } catch {
    throw new ApiError("The server is unreachable.", 0);
  }
  if (response.status === 401 && path !== "session") {
    window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
  }
  let data: unknown = null;
  try {
    data = await response.json();
  } catch {
    // A non-JSON body is reported below by status.
  }
  if (!response.ok) {
    const message = (data as { error?: string } | null)?.error ?? `Request failed (${response.status}).`;
    throw new ApiError(message, response.status);
  }
  return data as T;
}

export const api = {
  get: <T,>(path: string) => request<T>("GET", path),
  post: <T,>(path: string, body: unknown = {}) => request<T>("POST", path, body),
  put: <T,>(path: string, body: unknown = {}) => request<T>("PUT", path, body),
  del: <T,>(path: string) => request<T>("DELETE", path),
  form: <T,>(path: string, body: FormData) => request<T>("POST", path, body),
};

export const enc = encodeURIComponent;
