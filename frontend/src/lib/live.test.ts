import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { LiveField, type FieldSnapshot } from "./live";

class FakeWS {
  static last: FakeWS | null = null;
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  close = vi.fn(() => { this.onclose?.(); });
  constructor(public url: string) { FakeWS.last = this; }
}

const snap: FieldSnapshot = {
  session: { live: true, phase: "race", current_lap: 9, max_laps: 24, elapsed: 100,
    end_time: 0, cur_s1: null, cur_s2: null, best_s1: null, best_s2: null,
    last_lap: null, best_lap: null },
  cars: [],
};

beforeEach(() => { vi.stubGlobal("WebSocket", FakeWS as unknown as typeof WebSocket); });
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe("LiveField", () => {
  it("exposes the latest snapshot to subscribers", () => {
    const lf = new LiveField();
    let seen: FieldSnapshot | null = null;
    lf.subscribe((v) => { seen = v.snapshot; });
    lf.start();
    FakeWS.last!.onopen?.();
    FakeWS.last!.onmessage?.({ data: JSON.stringify(snap) });
    expect(seen!.session.current_lap).toBe(9);
    expect(lf.connected).toBe(true);
  });

  it("reconnects after the socket closes", () => {
    vi.useFakeTimers();
    const lf = new LiveField();
    lf.subscribe(() => {});
    lf.start();
    const first = FakeWS.last!;
    first.onclose?.();
    expect(lf.connected).toBe(false);
    vi.advanceTimersByTime(2000);
    expect(FakeWS.last).not.toBe(first); // a new socket was opened
    vi.useRealTimers();
  });
});
