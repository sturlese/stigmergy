const TOKEN_KEY = "stigmergy-admin-token";

export function token() {
  return sessionStorage.getItem(TOKEN_KEY) || "";
}

export function setToken(value) {
  sessionStorage.setItem(TOKEN_KEY, value);
}

export function clearToken() {
  sessionStorage.removeItem(TOKEN_KEY);
}

async function request(method, path, body, form = false) {
  const headers = { Authorization: `Bearer ${token()}` };
  if (body !== undefined && !form) headers["Content-Type"] = "application/json";
  let response;
  try {
    response = await fetch(`./api/${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : (form ? body : JSON.stringify(body)),
    });
  } catch {
    throw new Error("The server is unreachable.");
  }
  if (response.status === 401) {
    clearToken();
    window.location.reload();
    throw new Error("The admin token was refused.");
  }
  let data;
  try {
    data = await response.json();
  } catch {
    throw new Error(`The server returned ${response.status} without JSON.`);
  }
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status}).`);
  return data;
}

export const api = {
  get: (path) => request("GET", path),
  post: (path, body = {}) => request("POST", path, body),
  form: (path, body) => request("POST", path, body, true),
};
