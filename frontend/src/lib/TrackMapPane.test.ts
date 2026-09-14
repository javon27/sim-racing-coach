import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, cleanup, fireEvent } from "@testing-library/svelte";
import { getSessionLaps, getLapMap, getStatus, getCurrentMap, type StatusResponse } from "./api";
import TrackMapPane from "./TrackMapPane.svelte";

vi.mock("./api", async () => {
  const actual = await vi.importActual<typeof import("./api")>("./api");
  return { ...actual, getSessionLaps: vi.fn(), getLapMap: vi.fn(), getStatus: vi.fn(), getCurrentMap: vi.fn() };
});

const mapFixture = {
  width: 100,
  height: 60,
  path: [
    [0, 0],
    [10, 20],
    [30, 40],
  ] as [number, number][],
  dist: [0, 100, 200],
  sectors: [],
  corners: [
    { number: 1, name: "First", direction: "right", x: 10, y: 20, apex_dist: 100, start_dist: 80, end_dist: 120 },
  ],
};

const noStatus: StatusResponse = {
  track: null,
  car: null,
  on_track: false,
  activity: null,
  has_map: false,
  lmu_connected: false,
  session_id: null,
  lap_id: null,
};

describe("TrackMapPane", () => {
  beforeEach(() => {
    vi.mocked(getSessionLaps).mockReset();
    vi.mocked(getLapMap).mockReset();
    vi.mocked(getStatus).mockReset();
    vi.mocked(getCurrentMap).mockReset();
    vi.mocked(getSessionLaps).mockResolvedValue([{ id: 9, lap_time: 216, valid: 1, ts: 100 }]);
    vi.mocked(getLapMap).mockResolvedValue(mapFixture);
    vi.mocked(getStatus).mockResolvedValue(noStatus);
    vi.mocked(getCurrentMap).mockResolvedValue({});
    try {
      localStorage.clear();
    } catch {
      /* jsdom always has it */
    }
  });

  afterEach(cleanup);

  it("shows the empty state with no session and no live track, pointing editing to Track view", async () => {
    render(TrackMapPane, { props: { sessionId: undefined } });
    expect(screen.getByText(/no map yet/i)).toBeTruthy();
    // Editing/capture moved to Track view — the rail no longer offers Generate map.
    expect(screen.queryByText("Generate map")).toBeNull();
    expect(getSessionLaps).not.toHaveBeenCalled();
    await vi.waitFor(() => expect(getStatus).toHaveBeenCalled());
  });

  it("loads the live track's map as soon as the track is detected, with no lap yet", async () => {
    vi.mocked(getStatus).mockResolvedValue({ ...noStatus, track: "Live Track", on_track: true, activity: "track", has_map: true });
    vi.mocked(getCurrentMap).mockResolvedValue({ ...mapFixture, track_name: "Live Track" });
    const { container } = render(TrackMapPane, { props: { sessionId: undefined } });
    expect(await screen.findByText("Live Track")).toBeTruthy();
    expect(await screen.findByText("T1")).toBeTruthy();
    expect(container.querySelector("polyline")).toBeTruthy();
    expect(getSessionLaps).not.toHaveBeenCalled();
  });

  it("shows the build-me hint when a live track is detected but has no stored map", async () => {
    vi.mocked(getStatus).mockResolvedValue({ ...noStatus, track: "New Track", on_track: true, activity: "track", has_map: false });
    vi.mocked(getCurrentMap).mockResolvedValue({});
    render(TrackMapPane, { props: { sessionId: undefined } });
    expect(await screen.findByText("New Track")).toBeTruthy();
    expect(await screen.findByText(/no map for this track yet/i)).toBeTruthy();
  });

  it("resolves the session's first lap and renders its canonical map", async () => {
    const { container } = render(TrackMapPane, { props: { sessionId: "s1" } });
    expect(await screen.findByText("T1")).toBeTruthy();
    expect(getSessionLaps).toHaveBeenCalledWith("s1");
    expect(getLapMap).toHaveBeenCalledWith(9); // TrackMap fetches the resolved lap
    expect(container.querySelector("polyline")).toBeTruthy();
  });

  it("only re-fetches the live map when the polled track/car changes (#98)", async () => {
    vi.useFakeTimers();
    try {
      vi.mocked(getStatus).mockResolvedValue({ ...noStatus, track: "Live Track", car: "GT3", on_track: true, activity: "track", has_map: true });
      vi.mocked(getCurrentMap).mockResolvedValue({ ...mapFixture, track_name: "Live Track" });

      render(TrackMapPane, { props: { sessionId: undefined } });
      await vi.waitFor(() => expect(getCurrentMap).toHaveBeenCalledTimes(1));

      // 5 more unchanged status ticks -> still just the one map fetch.
      for (let i = 0; i < 5; i++) {
        await vi.advanceTimersByTimeAsync(2000);
      }
      expect(getStatus).toHaveBeenCalledTimes(6); // initial poll + 5 ticks
      expect(getCurrentMap).toHaveBeenCalledTimes(1);

      // Track changes -> exactly one more map fetch.
      vi.mocked(getStatus).mockResolvedValue({ ...noStatus, track: "Other Track", car: "GT3", on_track: true, activity: "track", has_map: true });
      vi.mocked(getCurrentMap).mockResolvedValue({ ...mapFixture, track_name: "Other Track" });
      await vi.advanceTimersByTimeAsync(2000);
      expect(getCurrentMap).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it("opens the map strictly read-only from the magnifier", async () => {
    render(TrackMapPane, { props: { sessionId: "s1" } });
    await fireEvent.click(await screen.findByLabelText("Enlarge track map"));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toBeTruthy();
    // Read-only: no pencil to reveal edit tools, no Save. Editing lives in Track view.
    expect(screen.queryByLabelText("Edit map")).toBeNull();
    expect(screen.queryByText("Save")).toBeNull();
  });
});
