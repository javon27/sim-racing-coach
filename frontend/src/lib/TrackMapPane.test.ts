import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, cleanup, fireEvent, waitFor } from "@testing-library/svelte";
import { tick } from "svelte";
import { getSessionLaps, getLapMap, getStatus, getCurrentMap, type StatusResponse, type LapRow } from "./api";
import { eventFeed } from "./events";
import TrackMapPane from "./TrackMapPane.svelte";

vi.mock("./api", async () => {
  const actual = await vi.importActual<typeof import("./api")>("./api");
  return { ...actual, getSessionLaps: vi.fn(), getLapMap: vi.fn(), getStatus: vi.fn(), getCurrentMap: vi.fn() };
});

/** #176: getSessionLaps now resolves { laps, optimal } -- this pane only reads the
    rows, so `optimal` defaults to null. */
function withOpt(laps: LapRow[]) {
  return { laps, optimal: null as number | null };
}

// #151: a controllable stand-in for the shared /live feed (App owns the real one).
const live = vi.hoisted(() => {
  let snapshot: unknown = null;
  const listeners = new Set<(v: { snapshot: unknown }) => void>();
  return {
    get snapshot() {
      return snapshot;
    },
    subscribe(run: (v: { snapshot: unknown }) => void) {
      listeners.add(run);
      run({ snapshot });
      return () => listeners.delete(run);
    },
    emit(s: unknown) {
      snapshot = s;
      for (const l of listeners) l({ snapshot: s });
    },
  };
});
vi.mock("./live", async () => {
  const actual = await vi.importActual<typeof import("./live")>("./live");
  return { ...actual, liveField: live };
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

type Next = { label: string; name: string; direction: string } | null;
// One trimmed /live frame: the player's position and the next corner only.
const onTrackFrame = (lapDist: number, next: Next) => ({
  live: true,
  player: { lap_dist: lapDist, cls: "GT3" },
  next_corner: next,
});
const offTrackFrame = () => ({ live: false, player: null, next_corner: null });
const liveStatus = { ...noStatus, track: "Live Track", on_track: true, activity: "track", has_map: true };

describe("TrackMapPane", () => {
  beforeEach(() => {
    live.emit(null);
    vi.mocked(getSessionLaps).mockReset();
    vi.mocked(getLapMap).mockReset();
    vi.mocked(getStatus).mockReset();
    vi.mocked(getCurrentMap).mockReset();
    vi.mocked(getSessionLaps).mockResolvedValue(withOpt([{ id: 9, lap_time: 216, valid: 1, ts: 100 }]));
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
    // #106: the live track/map come off the shared feed; the pane fetches neither.
    expect(getStatus).not.toHaveBeenCalled();
    expect(getCurrentMap).not.toHaveBeenCalled();
  });

  it("loads the live track's map as soon as the track is detected, with no lap yet", async () => {
    eventFeed.push("status", { ...noStatus, track: "Live Track", on_track: true, activity: "track", has_map: true });
    eventFeed.push("map", { ...mapFixture, track_name: "Live Track" });
    const { container } = render(TrackMapPane, { props: { sessionId: undefined } });
    expect(await screen.findByText("Live Track")).toBeTruthy();
    expect(await screen.findByText("T1")).toBeTruthy();
    expect(container.querySelector("polyline")).toBeTruthy();
    expect(getSessionLaps).not.toHaveBeenCalled();
  });

  it("shows the build-me hint when a live track is detected but has no stored map", async () => {
    eventFeed.push("status", { ...noStatus, track: "New Track", on_track: true, activity: "track", has_map: false });
    eventFeed.push("map", {});
    render(TrackMapPane, { props: { sessionId: undefined } });
    expect(await screen.findByText("New Track")).toBeTruthy();
    expect(await screen.findByText(/no map for this track yet/i)).toBeTruthy();
  });

  it("picks up a track detected after mount (a frame, not a poll)", async () => {
    render(TrackMapPane, { props: { sessionId: undefined } });
    expect(screen.getByText(/no map yet/i)).toBeTruthy();

    eventFeed.push("status", { ...noStatus, track: "Live Track", on_track: true, activity: "track", has_map: true });
    eventFeed.push("map", { ...mapFixture, track_name: "Live Track" });

    expect(await screen.findByText("Live Track")).toBeTruthy();
    expect(await screen.findByText("T1")).toBeTruthy();
  });

  it("resolves the session's first lap and renders its canonical map", async () => {
    const { container } = render(TrackMapPane, { props: { sessionId: "s1" } });
    expect(await screen.findByText("T1")).toBeTruthy();
    expect(getSessionLaps).toHaveBeenCalledWith("s1");
    expect(getLapMap).toHaveBeenCalledWith(9); // TrackMap fetches the resolved lap
    expect(container.querySelector("polyline")).toBeTruthy();
  });

  it("never fetches the live map itself — the feed sends one only on a track change (#98/#106)", async () => {
    const live = { ...noStatus, track: "Live Track", car: "GT3", on_track: true, activity: "track", has_map: true };
    eventFeed.push("status", live);
    eventFeed.push("map", { ...mapFixture, track_name: "Live Track" });

    const { container } = render(TrackMapPane, { props: { sessionId: undefined } });
    expect(await screen.findByText("Live Track")).toBeTruthy();
    const points = () => container.querySelector("polyline")?.getAttribute("points");
    const drawn = points();
    expect(drawn).toBeTruthy();

    // Status keeps ticking (1 Hz on the real feed) with no map frame: the ~130 KB
    // map is neither re-fetched nor re-drawn, so the user's zoom/pan survives.
    for (let i = 0; i < 5; i++) eventFeed.push("status", live);
    await tick(); // flush, or "unchanged" would just mean "not rendered yet"
    expect(points()).toBe(drawn);

    // A track change is what makes the sidecar send a new map frame.
    eventFeed.push("status", { ...live, track: "Other Track" });
    eventFeed.push("map", { ...mapFixture, track_name: "Other Track" });
    expect(await screen.findByText("Other Track")).toBeTruthy();

    expect(getStatus).not.toHaveBeenCalled();
    expect(getCurrentMap).not.toHaveBeenCalled();
  });

  it("#151: on track the rail map is live — the live map, ONLY the player's car, and the next corner", async () => {
    eventFeed.push("status", liveStatus);
    eventFeed.push("map", { ...mapFixture, track_name: "Live Track" });
    live.emit(onTrackFrame(100, { label: "1", name: "First", direction: "right" }));
    // A past session is pinned in the rail: live still wins while on track.
    const { container } = render(TrackMapPane, { props: { sessionId: "s1" } });

    expect(await screen.findByText("Next: T1 First (R)")).toBeTruthy();
    expect(await screen.findByText("Live Track")).toBeTruthy();
    const dots = container.querySelectorAll("circle.car-dot");
    expect(dots.length).toBe(1); // one dot: the player's
    expect(dots[0].classList.contains("player")).toBe(true);
    expect(dots[0].getAttribute("cx")).toBe("10"); // lap_dist 100 -> path point [10, 20]
    expect(getLapMap).not.toHaveBeenCalled(); // the pinned session's map is not what's drawn
  });

  it("#151: off track it shows the selected session's map as before — no car, no callout", async () => {
    live.emit(offTrackFrame());
    const { container } = render(TrackMapPane, { props: { sessionId: "s1" } });
    expect(await screen.findByText("T1")).toBeTruthy();
    expect(getLapMap).toHaveBeenCalledWith(9);
    expect(container.querySelector("circle.car-dot")).toBeNull();
    expect(screen.queryByText(/^Next:/)).toBeNull();
  });

  it("#151: leaving the track drops the car and the callout", async () => {
    eventFeed.push("status", liveStatus);
    eventFeed.push("map", { ...mapFixture, track_name: "Live Track" });
    live.emit(onTrackFrame(100, { label: "1", name: "First", direction: "right" }));
    const { container } = render(TrackMapPane, { props: { sessionId: undefined } });
    expect(await screen.findByText("Next: T1 First (R)")).toBeTruthy();

    live.emit(offTrackFrame());
    await tick();
    expect(screen.queryByText(/^Next:/)).toBeNull();
    expect(container.querySelector("circle.car-dot")).toBeNull();
    expect(screen.getByText("T1")).toBeTruthy(); // no session pinned: still the live track's map
  });

  it("#151: no callout when the server resolves no corner (e.g. no corner map)", async () => {
    eventFeed.push("status", liveStatus);
    eventFeed.push("map", { ...mapFixture, track_name: "Live Track" });
    live.emit(onTrackFrame(100, null));
    const { container } = render(TrackMapPane, { props: { sessionId: undefined } });
    await waitFor(() => expect(container.querySelector("circle.car-dot")).not.toBeNull());
    expect(screen.queryByText(/^Next:/)).toBeNull();
  });

  it("#151: the expand button asks the owner to widen the rail — no overlay dialog", async () => {
    const onexpand = vi.fn();
    render(TrackMapPane, { props: { sessionId: "s1", expanded: false, onexpand } });
    await screen.findByText("T1");
    expect(screen.queryByLabelText("Enlarge track map")).toBeNull(); // the 🔍 overlay is gone
    await fireEvent.click(screen.getByLabelText("Expand track map"));
    expect(onexpand).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("#151: expanded, the button reads Collapse and the map fits its box", async () => {
    const { container } = render(TrackMapPane, { props: { sessionId: "s1", expanded: true, onexpand: () => {} } });
    await screen.findByText("T1");
    expect(screen.getByLabelText("Collapse track map").getAttribute("aria-pressed")).toBe("true");
    expect(container.querySelector(".panel.fill")).not.toBeNull();
  });
});
