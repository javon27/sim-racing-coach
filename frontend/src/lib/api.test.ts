import { describe, it, expect, vi, beforeEach } from "vitest";
import { getSessions, putSettings, setApiKey, testApiKey, clearData, ingestHistory, getChatUsage,
         rebuildFromCapture } from "./api";

const BASE = "http://127.0.0.1:8765";
const JSON_HEADERS = { "Content-Type": "application/json" };

describe("api client", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("getSessions requests {base}/sessions with filter params and returns the page", async () => {
    const payload = {
      rows: [
        { session_id: "s1", track: "Le Mans", car: "Corvette Z06", laps: 2, best: 216.0, started: 1, last: 2 },
      ],
      total: 1,
      tracks: ["Le Mans"],
      cars: ["Corvette Z06"],
    };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => payload,
    });
    vi.stubGlobal("fetch", fetchMock);

    const result = await getSessions({ track: "Le Mans", offset: 30, limit: 30 });

    expect(fetchMock).toHaveBeenCalledWith(`${BASE}/sessions?track=Le+Mans&offset=30&limit=30`, undefined);
    expect(result).toEqual(payload);
  });

  it("getChatUsage fetches {base}/chats/:id/usage and returns tokens + cost", async () => {
    const payload = {
      usage: { input: 3500, output: 400, cache_read: 0, cache_write: 0, total: 3900, messages: 2 },
      cost: 0.011,
      cost_complete: true,
    };
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => payload });
    vi.stubGlobal("fetch", fetchMock);

    const result = await getChatUsage("chat-1");

    expect(fetchMock).toHaveBeenCalledWith(`${BASE}/chats/chat-1/usage`, undefined);
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

  it("putSettings PUTs {base}/settings with the config body and returns the parsed settings", async () => {
    const cfg = { units: "imperial" };
    const payload = { units: "imperial", api_key_set: true };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => payload,
    });
    vi.stubGlobal("fetch", fetchMock);

    const result = await putSettings(cfg);

    expect(fetchMock).toHaveBeenCalledWith(`${BASE}/settings`, {
      method: "PUT",
      headers: JSON_HEADERS,
      body: JSON.stringify(cfg),
    });
    expect(result).toEqual(payload);
  });

  it("setApiKey POSTs {base}/settings/api-key with the key and returns api_key_set", async () => {
    const payload = { api_key_set: true };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => payload,
    });
    vi.stubGlobal("fetch", fetchMock);

    const result = await setApiKey("sk-test");

    expect(fetchMock).toHaveBeenCalledWith(`${BASE}/settings/api-key`, {
      method: "POST",
      headers: JSON_HEADERS,
      body: JSON.stringify({ key: "sk-test" }),
    });
    expect(result).toEqual(payload);
  });

  it("testApiKey POSTs {base}/settings/test-key and returns the result", async () => {
    const payload = { ok: true, error: null };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => payload,
    });
    vi.stubGlobal("fetch", fetchMock);

    const result = await testApiKey();

    expect(fetchMock).toHaveBeenCalledWith(`${BASE}/settings/test-key`, {
      method: "POST",
      headers: JSON_HEADERS,
      body: JSON.stringify({}),
    });
    expect(result).toEqual(payload);
  });

  it("clearData DELETEs {base}/data with confirm:true and returns cleared", async () => {
    const payload = { cleared: true };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => payload,
    });
    vi.stubGlobal("fetch", fetchMock);

    const result = await clearData();

    expect(fetchMock).toHaveBeenCalledWith(`${BASE}/data`, {
      method: "DELETE",
      headers: JSON_HEADERS,
      body: JSON.stringify({ confirm: true }),
    });
    expect(result).toEqual(payload);
  });

  it("ingestHistory POSTs {base}/ingest and returns the import summary", async () => {
    const payload = { scanned: 10, imported: 8, skipped: 1, unmatched: 1, errors: 0 };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => payload,
    });
    vi.stubGlobal("fetch", fetchMock);

    const result = await ingestHistory();

    expect(fetchMock).toHaveBeenCalledWith(`${BASE}/ingest`, {
      method: "POST",
      headers: JSON_HEADERS,
      body: JSON.stringify({}),
    });
    expect(result).toEqual(payload);
  });

  it("rebuildFromCapture binds to the track by SLUG when there is no lap (#26)", async () => {
    // The Tracks screen edits a track with no driven lap. Without a slug the
    // sidecar falls back to the LIVE track (or the newest saved capture) and
    // rebuilds a different track's geometry over the one being edited.
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({}) });
    vi.stubGlobal("fetch", fetchMock);

    await rebuildFromCapture(null, "autodromo_nazionale_monza");

    expect(fetchMock).toHaveBeenCalledWith(`${BASE}/mapcapture/rebuild`, {
      method: "POST",
      headers: JSON_HEADERS,
      body: JSON.stringify({ slug: "autodromo_nazionale_monza" }),
    });
  });

  it("rebuildFromCapture still binds by lap when editing a session's lap", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({}) });
    vi.stubGlobal("fetch", fetchMock);

    await rebuildFromCapture(7);

    expect(fetchMock.mock.calls[0][1].body).toBe(JSON.stringify({ lap_id: 7 }));
  });

  it("throws on a non-2xx response for a mutating call", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: false,
      status: 400,
      json: async () => ({ detail: "bad" }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(clearData()).rejects.toThrow();
  });
});
