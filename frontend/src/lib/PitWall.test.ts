import { describe, it, expect, vi, afterEach } from "vitest";
import { render, waitFor, cleanup } from "@testing-library/svelte";
import { tick } from "svelte";

const getInputTraces = vi.fn(async () => ({ last: null, best: null }));

// Stub the network deps before importing the component.
vi.mock("./api", () => ({
  getCurrentMap: vi.fn(async () => ({})),
  getInputTraces: (...a: unknown[]) => getInputTraces(...(a as [])),
}));

import PitWall from "./PitWall.svelte";
import { getCurrentMap } from "./api";
import { eventFeed } from "./events";
import { liveField } from "./live";
import type { FieldSnapshot } from "./live";

const MAP = {
  width: 100, height: 60,
  path: [[0, 0], [10, 20], [30, 40]] as [number, number][],
  dist: [0, 100, 200],
  sectors: [],
  corners: [],
  track_name: "Monza",
};

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

const band = (v: number[]) => ({ lo: [...v], hi: [...v] });
const TRACE = {
  lap_id: 4, lap_time: 92.5, length: 1000, dist: [0, 500, 1000],
  throttle: band([1, 0, 1]), brake: band([0, 1, 0]), steering: band([0, 0, 0]),
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

  it("takes the track map off /events instead of polling for it (#106)", async () => {
    liveField.snapshot = snapshot(1); // the map only draws while the session is live
    eventFeed.push("map", MAP);
    const { container } = render(PitWall);
    await waitFor(() => expect(container.querySelector("polyline")).toBeTruthy());
    expect(getCurrentMap).not.toHaveBeenCalled();
  });

  it("only swaps the map when the frame names a different track (zoom/pan survives)", async () => {
    liveField.snapshot = snapshot(1);
    eventFeed.push("map", MAP);
    const { container } = render(PitWall);
    const points = () => container.querySelector("polyline")?.getAttribute("points");
    await waitFor(() => expect(points()).toBeTruthy());
    const drawn = points();

    // Same track: the map is NOT re-assigned (handing TrackMap a new object resets
    // the user's zoom/pan), so even different geometry under the same name is ignored.
    eventFeed.push("map", { ...MAP, path: [[0, 0], [50, 50], [99, 59]] });
    await tick(); // let Svelte flush, so "unchanged" means it really was ignored
    expect(points()).toBe(drawn);

    // A different track is the one thing that swaps it.
    eventFeed.push("map", { ...MAP, track_name: "Spa", path: [[0, 0], [50, 50], [99, 59]] });
    await waitFor(() => expect(points()).not.toBe(drawn));
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
