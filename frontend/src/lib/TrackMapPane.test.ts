import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, cleanup, fireEvent } from "@testing-library/svelte";
import { getSessionLaps, getLapMap } from "./api";
import TrackMapPane from "./TrackMapPane.svelte";

vi.mock("./api", async () => {
  const actual = await vi.importActual<typeof import("./api")>("./api");
  return { ...actual, getSessionLaps: vi.fn(), getLapMap: vi.fn() };
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

describe("TrackMapPane", () => {
  beforeEach(() => {
    vi.mocked(getSessionLaps).mockReset();
    vi.mocked(getLapMap).mockReset();
    vi.mocked(getSessionLaps).mockResolvedValue([{ id: 9, lap_time: 216, valid: 1, ts: 100 }]);
    vi.mocked(getLapMap).mockResolvedValue(mapFixture);
    try {
      localStorage.clear();
    } catch {
      /* jsdom always has it */
    }
  });

  afterEach(cleanup);

  it("shows the empty state with no session, pointing editing to Track view", () => {
    render(TrackMapPane, { props: { sessionId: undefined } });
    expect(screen.getByText(/no map yet/i)).toBeTruthy();
    // Editing/capture moved to Track view — the rail no longer offers Generate map.
    expect(screen.queryByText("Generate map")).toBeNull();
    expect(getSessionLaps).not.toHaveBeenCalled();
  });

  it("resolves the session's first lap and renders its canonical map", async () => {
    const { container } = render(TrackMapPane, { props: { sessionId: "s1" } });
    expect(await screen.findByText("T1")).toBeTruthy();
    expect(getSessionLaps).toHaveBeenCalledWith("s1");
    expect(getLapMap).toHaveBeenCalledWith(9); // TrackMap fetches the resolved lap
    expect(container.querySelector("polyline")).toBeTruthy();
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
