import { describe, it, expect, vi, afterEach } from "vitest";
import { render, waitFor, cleanup } from "@testing-library/svelte";

const getInputTraces = vi.fn(async () => ({ last: null, best: null }));

// Stub the network deps before importing the component.
vi.mock("./api", () => ({
  getCurrentMap: vi.fn(async () => ({})),
  getInputTraces: (...a: unknown[]) => getInputTraces(...(a as [])),
}));

import PitWall from "./PitWall.svelte";
import { liveField } from "./live";
import type { FieldSnapshot } from "./live";

function snapshot(lap: number): FieldSnapshot {
  return {
    session: {
      live: true, phase: "practice", current_lap: lap, max_laps: null, track_len: 4000,
      elapsed: 0, end_time: 0, cur_s1: null, cur_s2: null, best_s1: null, best_s2: null,
      last_s1: null, last_s2: null, last_lap: null, best_lap: null,
    },
    cars: [],
  };
}

const TRACE = {
  lap_id: 4, lap_time: 92.5, length: 1000,
  dist: [0, 500, 1000], throttle: [1, 0, 1], brake: [0, 1, 0], steering: [0, 0, 0],
};

afterEach(() => {
  cleanup();
  liveField.snapshot = null;
  getInputTraces.mockReset();
  getInputTraces.mockResolvedValue({ last: null, best: null });
  vi.restoreAllMocks();
});

describe("PitWall", () => {
  it("shows the waiting state when not live", async () => {
    const { getByText } = render(PitWall);
    await waitFor(() => expect(getByText(/waiting for lmu/i)).toBeTruthy());
  });

  it("does NOT stop the shared live field on destroy (it streams app-wide)", async () => {
    const stop = vi.spyOn(liveField, "stop");
    const { unmount } = render(PitWall);
    unmount();
    expect(stop).not.toHaveBeenCalled();
  });

  it("draws the driver-input graphs from the sidecar's traces", async () => {
    getInputTraces.mockResolvedValue({ last: TRACE, best: null } as never);
    const { getByTestId } = render(PitWall);
    await waitFor(() => expect(getByTestId("trace-throttle-last")).toBeTruthy());
    expect(getByTestId("trace-brake-last").getAttribute("d")).toContain("M0.0");
  });

  it("refetches the traces when the lap counter ticks, not on every live frame", async () => {
    liveField.snapshot = snapshot(3);
    render(PitWall);
    await waitFor(() => expect(getInputTraces).toHaveBeenCalledTimes(1));

    // Another frame of the SAME lap must not refetch (the traces are ~40 KB).
    liveField.snapshot = snapshot(3);
    liveField.stop(); // the only public way to push a notify to subscribers
    await waitFor(() => expect(getInputTraces).toHaveBeenCalledTimes(1));

    // A completed lap must.
    liveField.snapshot = snapshot(4);
    liveField.stop();
    await waitFor(() => expect(getInputTraces).toHaveBeenCalledTimes(2));
  });
});
