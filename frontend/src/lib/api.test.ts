import { describe, it, expect, vi, beforeEach } from "vitest";
import { getSessions } from "./api";

const BASE = "http://127.0.0.1:8765";

describe("api client", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("getSessions requests {base}/sessions and returns the parsed array", async () => {
    const payload = [
      { session_id: "s1", track: "Le Mans", car: "Corvette Z06", laps: 2, best: 216.0, started: 1, last: 2 },
    ];
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => payload,
    });
    vi.stubGlobal("fetch", fetchMock);

    const result = await getSessions();

    expect(fetchMock).toHaveBeenCalledWith(`${BASE}/sessions`, undefined);
    expect(result).toEqual(payload);
  });

  it("throws on a non-2xx response", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: false,
      status: 500,
      json: async () => ({ detail: "boom" }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(getSessions()).rejects.toThrow();
  });
});
