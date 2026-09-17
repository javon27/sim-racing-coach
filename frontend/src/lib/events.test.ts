import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { EventFeed } from "./events";

class FakeWS {
  static last: FakeWS | null = null;
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  close = vi.fn(() => {
    this.onclose?.();
  });
  constructor(public url: string) {
    FakeWS.last = this;
  }
}

function frame(topic: string, data: unknown) {
  return { data: JSON.stringify({ topic, data }) };
}

const STATUS = {
  track: "Le Mans", car: "Corvette Z06", car_model: "Corvette Z06",
  on_track: true, activity: "track", has_map: true, lmu_connected: true,
  session_id: "s1", lap_id: 5,
};

beforeEach(() => {
  FakeWS.last = null;
  vi.stubGlobal("WebSocket", FakeWS as unknown as typeof WebSocket);
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("EventFeed", () => {
  it("connects to /events and routes each frame to its own topic store", () => {
    const feed = new EventFeed();
    feed.start();
    expect(FakeWS.last!.url).toMatch(/\/events$/);

    const ws = FakeWS.last!;
    ws.onopen?.();
    ws.onmessage!(frame("status", STATUS));
    ws.onmessage!(frame("laps", { session_id: "s1", laps: [{ id: 9, lap_time: 90 }] }));
    ws.onmessage!(frame("map", { width: 10, height: 10, path: [[0, 0]], dist: [0], sectors: [], corners: [] }));
    ws.onmessage!(frame("mapcapture", { active: true, live_track: "Monza", laps: [{ idx: 0, samples: 3, length: 20 }] }));
    ws.onmessage!(frame("voice_setup", { installed: false, installing: true, progress_bytes: 1, total_bytes: 10, error: null }));

    expect(feed.status.value?.track).toBe("Le Mans");
    expect(feed.laps.value?.session_id).toBe("s1");
    expect(feed.laps.value?.laps).toHaveLength(1);
    expect(feed.map.value).toMatchObject({ width: 10 });
    expect(feed.mapcapture.value?.laps[0].samples).toBe(3);
    expect(feed.voiceSetup.value?.installing).toBe(true);
  });

  it("notifies subscribers of the topic they subscribed to, and only that one", () => {
    const feed = new EventFeed();
    const seenStatus: (unknown | null)[] = [];
    const seenMap: (unknown | null)[] = [];
    feed.status.subscribe((v) => seenStatus.push(v));
    feed.map.subscribe((v) => seenMap.push(v));
    feed.start();

    FakeWS.last!.onmessage!(frame("status", STATUS));

    expect(seenStatus).toHaveLength(2); // initial null replay + the frame
    expect(seenMap).toHaveLength(1); // replay only
  });

  it("replays the current value to a late subscriber", () => {
    const feed = new EventFeed();
    feed.start();
    FakeWS.last!.onmessage!(frame("status", STATUS));

    let seen: unknown = undefined;
    feed.status.subscribe((v) => (seen = v));
    expect(seen).toMatchObject({ track: "Le Mans" });
  });

  it("ignores unparseable frames and unknown topics without throwing", () => {
    const feed = new EventFeed();
    feed.start();
    FakeWS.last!.onmessage!({ data: "not json" });
    FakeWS.last!.onmessage!(frame("nonsense", { a: 1 }));
    expect(feed.status.value).toBeNull();
  });

  it("flips connected on the first frame and back on close", () => {
    const feed = new EventFeed();
    const seen: boolean[] = [];
    feed.connected.subscribe((v) => seen.push(v));
    feed.start();

    expect(feed.connected.value).toBe(false); // open alone is not liveness
    FakeWS.last!.onopen?.();
    expect(feed.connected.value).toBe(false);

    FakeWS.last!.onmessage!(frame("status", STATUS));
    expect(feed.connected.value).toBe(true);

    FakeWS.last!.onclose?.();
    expect(feed.connected.value).toBe(false);
    expect(seen).toEqual([false, true, false]);
  });

  it("reconnects after the socket closes", () => {
    vi.useFakeTimers();
    try {
      const feed = new EventFeed();
      feed.start();
      const first = FakeWS.last!;
      first.onclose?.();
      vi.advanceTimersByTime(2000);
      expect(FakeWS.last).not.toBe(first);
    } finally {
      vi.useRealTimers();
    }
  });

  it("stop() closes the socket and does not reconnect", () => {
    vi.useFakeTimers();
    try {
      const feed = new EventFeed();
      feed.start();
      const first = FakeWS.last!;
      feed.stop();
      vi.advanceTimersByTime(5000);
      expect(FakeWS.last).toBe(first);
      expect(feed.connected.value).toBe(false);
    } finally {
      vi.useRealTimers();
    }
  });

  it("reset() clears every topic (test seam for the app-lifetime singleton)", () => {
    const feed = new EventFeed();
    feed.start();
    FakeWS.last!.onmessage!(frame("status", STATUS));
    FakeWS.last!.onmessage!(frame("laps", { session_id: "s1", laps: [] }));

    feed.reset();

    expect(feed.status.value).toBeNull();
    expect(feed.laps.value).toBeNull();
    expect(feed.connected.value).toBe(false);
  });

  it("start() is idempotent: a second call does not open a second socket", () => {
    const feed = new EventFeed();
    feed.start();
    const first = FakeWS.last!;
    feed.start();
    expect(FakeWS.last).toBe(first);
    expect(first.close).not.toHaveBeenCalled();
  });

  it("push() routes a frame the same way the socket does", () => {
    const feed = new EventFeed();
    feed.push("status", STATUS);
    expect(feed.status.value?.session_id).toBe("s1");
    expect(feed.connected.value).toBe(true);
  });
});
