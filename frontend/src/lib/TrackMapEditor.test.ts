import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, cleanup, fireEvent } from "@testing-library/svelte";
import { getLapMap, saveLapMap } from "./api";
import TrackMapEditor, { nearestPathIndex, nearestDistIndex } from "./TrackMapEditor.svelte";

vi.mock("./api", async () => {
  const actual = await vi.importActual<typeof import("./api")>("./api");
  return { ...actual, getLapMap: vi.fn(), saveLapMap: vi.fn() };
});

const fixture = {
  width: 100,
  height: 60,
  path: [
    [0, 0],
    [10, 20],
    [30, 40],
    [50, 50],
  ] as [number, number][],
  dist: [0, 100, 200, 300],
  sectors: [100],
  corners: [
    { number: 1, name: "First", direction: "right", x: 10, y: 20, apex_dist: 100, start_dist: 80, end_dist: 120 },
    { number: 2, name: "Second", direction: "left", x: 30, y: 40, apex_dist: 200, start_dist: 180, end_dist: 220 },
  ],
};

describe("nearestPathIndex", () => {
  const path = fixture.path;
  it("returns the index of the closest path point", () => {
    expect(nearestPathIndex(path, 0, 0)).toBe(0);
    expect(nearestPathIndex(path, 11, 21)).toBe(1); // near [10,20]
    expect(nearestPathIndex(path, 49, 49)).toBe(3); // near [50,50]
  });
});

describe("nearestDistIndex", () => {
  it("maps a lap_distance to the closest dist[] entry", () => {
    expect(nearestDistIndex(fixture.dist, 0)).toBe(0);
    expect(nearestDistIndex(fixture.dist, 190)).toBe(2); // closest to 200
    expect(nearestDistIndex(fixture.dist, 260)).toBe(3); // closest to 300
  });
});

describe("TrackMapEditor", () => {
  beforeEach(() => {
    vi.mocked(getLapMap).mockReset();
    vi.mocked(saveLapMap).mockReset();
    vi.mocked(getLapMap).mockResolvedValue(fixture);
    vi.mocked(saveLapMap).mockResolvedValue(fixture);
  });
  afterEach(cleanup);

  it("does not render when closed", () => {
    render(TrackMapEditor, { props: { lapId: 7, open: false } });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(getLapMap).not.toHaveBeenCalled();
  });

  it("fetches the averaged outline on open", async () => {
    render(TrackMapEditor, { props: { lapId: 7, open: true } });
    expect(await screen.findByText("First")).toBeTruthy();
    expect(getLapMap).toHaveBeenCalledWith(7, true);
  });

  it("shows direction read-only (no input to change it)", async () => {
    const { container } = render(TrackMapEditor, { props: { lapId: 7, open: true } });
    await screen.findByText("First");
    // direction rendered as text, not an editable control
    expect(screen.getByText("right")).toBeTruthy();
    expect(screen.getByText("left")).toBeTruthy();
    const inputs = [...container.querySelectorAll("input")];
    // only number + name inputs per corner; none is a direction field
    expect(inputs.some((el) => el.getAttribute("aria-label") === "Corner name")).toBe(true);
    expect(inputs.some((el) => (el.getAttribute("aria-label") ?? "").toLowerCase().includes("direction"))).toBe(false);
  });

  it("adds and removes corners in the list", async () => {
    const { container } = render(TrackMapEditor, { props: { lapId: 7, open: true } });
    await screen.findByText("First");
    expect(container.querySelectorAll("li").length).toBe(2);

    await fireEvent.click(screen.getByText("Add corner"));
    await fireEvent.pointerDown(container.querySelector("svg")!);
    expect(container.querySelectorAll("li").length).toBe(3);

    await fireEvent.click(screen.getAllByLabelText("Remove corner")[0]);
    expect(container.querySelectorAll("li").length).toBe(2);
  });

  it("saves corners + sectors without direction", async () => {
    render(TrackMapEditor, { props: { lapId: 7, open: true } });
    await screen.findByText("First");

    await fireEvent.click(screen.getByText("Save"));

    expect(saveLapMap).toHaveBeenCalledTimes(1);
    const [id, body] = vi.mocked(saveLapMap).mock.calls[0];
    expect(id).toBe(7);
    expect(body.sectors).toEqual([100]);
    expect(body.corners).toHaveLength(2);
    for (const c of body.corners) {
      expect(c).not.toHaveProperty("direction");
      expect(c).toHaveProperty("apex_dist");
      expect(c).toHaveProperty("number");
      expect(c).toHaveProperty("name");
    }
  });
});
