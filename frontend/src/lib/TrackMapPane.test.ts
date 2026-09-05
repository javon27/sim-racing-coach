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

  it("shows the empty state with no session, but still offers Generate map", () => {
    render(TrackMapPane, { props: { sessionId: undefined } });
    expect(screen.getByText(/no map yet/i)).toBeTruthy();
    expect(screen.getByText("Generate map")).toBeTruthy(); // capture entry with no lap
    expect(getSessionLaps).not.toHaveBeenCalled();
  });

  it("opens the capture editor from Generate map with no session", async () => {
    render(TrackMapPane, { props: { sessionId: undefined } });
    await fireEvent.click(screen.getByText("Generate map"));
    expect(await screen.findByRole("dialog")).toBeTruthy();
  });

  it("resolves the session's first lap and renders its canonical map", async () => {
    const { container } = render(TrackMapPane, { props: { sessionId: "s1" } });
    expect(await screen.findByText("T1")).toBeTruthy();
    expect(getSessionLaps).toHaveBeenCalledWith("s1");
    expect(getLapMap).toHaveBeenCalledWith(9); // TrackMap fetches the resolved lap
    expect(container.querySelector("polyline")).toBeTruthy();
  });

  it("collapses and expands, persisting the state", async () => {
    render(TrackMapPane, { props: { sessionId: "s1" } });
    await screen.findByText("Track map");

    await fireEvent.click(screen.getByLabelText("Hide track map"));
    expect(screen.queryByText("Track map")).toBeNull();
    expect(localStorage.getItem("trackmap.collapsed")).toBe("1");

    await fireEvent.click(screen.getByLabelText("Show track map"));
    expect(screen.getByText("Track map")).toBeTruthy();
    expect(localStorage.getItem("trackmap.collapsed")).toBe("0");
  });

  it("starts collapsed when localStorage says so", () => {
    localStorage.setItem("trackmap.collapsed", "1");
    render(TrackMapPane, { props: { sessionId: "s1" } });
    expect(screen.queryByText("Track map")).toBeNull();
    expect(screen.getByLabelText("Show track map")).toBeTruthy();
  });

  it("opens the editor modal from the Edit map button", async () => {
    render(TrackMapPane, { props: { sessionId: "s1" } });
    await screen.findByText("Edit map");

    await fireEvent.click(screen.getByText("Edit map"));
    expect(await screen.findByRole("dialog")).toBeTruthy();
  });
});
