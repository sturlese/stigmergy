// The one fetch seam. Every call sends the admin bearer token; a 401 clears the stored token and
// reloads into the login screen (revocation is instant server-side — the UI follows suit).

const TOKEN_KEY = "stigmergy-ops-token";

export function storedToken() {
  return sessionStorage.getItem(TOKEN_KEY) || "";
}

export function storeToken(token) {
  sessionStorage.setItem(TOKEN_KEY, token);
}

export function clearToken() {
  sessionStorage.removeItem(TOKEN_KEY);
}

export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

// Set by the shell once a session is live: a mid-session 401 (revoked token) lands back at the
// login screen. During login itself there is no handler — the failure surfaces as a message.
let unauthorizedHandler = null;

export function onUnauthorized(handler) {
  unauthorizedHandler = handler;
}

async function call(method, path, body) {
  const headers = { Authorization: `Bearer ${storedToken()}` };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let response;
  try {
    response = await fetch(`./api/${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new ApiError(0, "the server is unreachable — is it still running?");
  }
  if (response.status === 401) {
    clearToken();
    if (unauthorizedHandler) unauthorizedHandler();
    throw new ApiError(401, "the token was refused — it may have been rotated or revoked");
  }
  let data = null;
  try {
    data = await response.json();
  } catch {
    throw new ApiError(response.status, `the server answered ${response.status} with no JSON body`);
  }
  if (!response.ok) {
    throw new ApiError(response.status, (data && data.error) || `the server answered ${response.status}`);
  }
  return data;
}

export const api = {
  get: (path) => call("GET", path),
  post: (path, body) => call("POST", path, body ?? {}),
};
