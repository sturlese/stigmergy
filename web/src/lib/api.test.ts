import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, UNAUTHORIZED_EVENT } from "./api";

function respond(status: number, body: unknown) {
  return vi.fn(async () => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }));
}

afterEach(() => vi.unstubAllGlobals());

describe("api client", () => {
  it("sends the anti-CSRF header on every state change and never on reads", async () => {
    const fetch = respond(200, {});
    vi.stubGlobal("fetch", fetch);
    await api.get("me");
    await api.post("groups", { name: "x" });
    await api.del("session");
    const headers = fetch.mock.calls.map((call) => (call as unknown as [string, RequestInit])[1].headers as Record<string, string>);
    expect(headers[0]["X-Stigmergy-CSRF"]).toBeUndefined();
    expect(headers[1]["X-Stigmergy-CSRF"]).toBe("1");
    expect(headers[2]["X-Stigmergy-CSRF"]).toBe("1");
    for (const call of fetch.mock.calls) {
      const init = (call as unknown as [string, RequestInit])[1];
      expect(init.credentials).toBe("same-origin");
      expect((init.headers as Record<string, string>).Authorization).toBeUndefined();
    }
  });

  it("surfaces the server's message and announces an expired session", async () => {
    vi.stubGlobal("fetch", respond(401, { error: "unauthorized" }));
    const heard = vi.fn();
    window.addEventListener(UNAUTHORIZED_EVENT, heard);
    await expect(api.get("people")).rejects.toEqual(new ApiError("unauthorized", 401));
    expect(heard).toHaveBeenCalledTimes(1);
    window.removeEventListener(UNAUTHORIZED_EVENT, heard);
  });

  it("does not announce a refused sign-in as an expired session", async () => {
    vi.stubGlobal("fetch", respond(401, { error: "sign-in failed" }));
    const heard = vi.fn();
    window.addEventListener(UNAUTHORIZED_EVENT, heard);
    await expect(api.post("session", { token: "x" })).rejects.toBeInstanceOf(ApiError);
    expect(heard).not.toHaveBeenCalled();
    window.removeEventListener(UNAUTHORIZED_EVENT, heard);
  });
});
