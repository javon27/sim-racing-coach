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

  it("collapses and expands the map body, persisting the state", async () => {
    const { container } = render(TrackMapPane, { props: { sessionId: "s1" } });
    await screen.findByText("T1"); // map body present

    // The title doubles as the collapse toggle; the title stays, the body hides.
    await fireEvent.click(screen.getByLabelText("Hide track map"));
    expect(container.querySelector("polyline")).toBeNull();
    expect(screen.getByText("Track map")).toBeTruthy();
    expect(localStorage.getItem("trackmap.collapsed")).toBe("1");

    await fireEvent.click(screen.getByLabelText("Show track map"));
    expect(await screen.findByText("T1")).toBeTruthy();
    expect(localStorage.getItem("trackmap.collapsed")).toBe("0");
  });

  it("starts collapsed when localStorage says so (body hidden, title shown)", () => {
    localStorage.setItem("trackmap.collapsed", "1");
    const { container } = render(TrackMapPane, { props: { sessionId: "s1" } });
    expect(screen.getByText("Track map")).toBeTruthy();
    expect(container.querySelector("polyline")).toBeNull();
    expect(screen.getByLabelText("Show track map")).toBeTruthy();
  });

  it("opens the map in a read-only VIEW modal from the magnifier", async () => {
    render(TrackMapPane, { props: { sessionId: "s1" } });
    await fireEvent.click(await screen.findByLabelText("Enlarge track map"));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toBeTruthy();
    // View mode: header reads "Track map" (not "Edit"), no Save, offers a pencil.
    expect(screen.getByLabelText("Edit map")).toBeTruthy(); // the pencil
    expect(screen.queryByText("Save")).toBeNull();
  });
});
