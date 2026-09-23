import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { LiveField, type LiveFrame } from "./live";

class FakeWS {
  static last: FakeWS | null = null;
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  close = vi.fn(() => { this.onclose?.(); });
  constructor(public url: string) { FakeWS.last = this; }
}

const snap: LiveFrame = { live: true, player: { lap_dist: 500, cls: "GT3" }, next_corner: null };

beforeEach(() => { vi.stubGlobal("WebSocket", FakeWS as unknown as typeof WebSocket); });
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe("LiveField", () => {
  it("exposes the latest snapshot to subscribers", () => {
    const lf = new LiveField();
    let seen: LiveFrame | null = null;
    lf.subscribe((v) => { seen = v.snapshot; });
    lf.start();
    FakeWS.last!.onopen?.();
    FakeWS.last!.onmessage?.({ data: JSON.stringify(snap) });
    expect(seen!.player!.lap_dist).toBe(500);
    expect(lf.connected).toBe(true);
  });

  it("drops the last frame when the socket closes, so nothing stays 'live' frozen", () => {
    const lf = new LiveField();
    lf.subscribe(() => {});
    lf.start();
    FakeWS.last!.onopen?.();
    FakeWS.last!.onmessage?.({ data: JSON.stringify(snap) });
    expect(lf.snapshot).not.toBeNull();
    FakeWS.last!.onclose?.();
    expect(lf.snapshot).toBeNull();
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
