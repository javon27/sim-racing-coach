import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, cleanup, waitFor } from "@testing-library/svelte";
import { getLapMap, type TrackMap as TrackMapData } from "./api";
import TrackMap from "./TrackMap.svelte";

vi.mock("./api", async () => {
  const actual = await vi.importActual<typeof import("./api")>("./api");
  return { ...actual, getLapMap: vi.fn() };
});

const mapFixture = {
  width: 100,
  height: 60,
  path: [
    [0, 0],
    [10, 20],
    [30, 40],
  ] as [number, number][],
  dist: [0, 50, 100],
  corners: [
    { number: 1, name: "First", direction: "right", x: 10, y: 20 },
    { number: 2, name: "Second", direction: "left", x: 30, y: 40 },
  ],
};

describe("TrackMap", () => {
  beforeEach(() => {
    vi.mocked(getLapMap).mockReset();
  });

  afterEach(cleanup);

  it("shows a select-a-lap message and does not fetch when lapId is null", () => {
    render(TrackMap, { props: { lapId: null } });
    expect(screen.getByText(/select a lap/i)).toBeTruthy();
    expect(getLapMap).not.toHaveBeenCalled();
  });

  it("renders an svg with a polyline and one label per corner", async () => {
    vi.mocked(getLapMap).mockResolvedValue(mapFixture);

    const { container } = render(TrackMap, { props: { lapId: 7 } });

    expect(await screen.findByText("T1")).toBeTruthy();
    expect(screen.getByText("T2")).toBeTruthy();
    expect(container.querySelector("svg")).toBeTruthy();
    expect(container.querySelector("polyline")).toBeTruthy();
    expect(container.querySelectorAll("text.corner-lbl").length).toBe(2);
    // Start/finish marker + direction arrow are drawn from the path.
    expect(container.querySelector("text.sf")).toBeTruthy();
    expect(container.querySelector("polygon")).toBeTruthy();
  });

  it("shows the no-map note when the fetch returns an empty object", async () => {
    vi.mocked(getLapMap).mockResolvedValue({});

    render(TrackMap, { props: { lapId: 7 } });

    expect(await screen.findByText(/no map for this track/i)).toBeTruthy();
  });

  it("shows the no-map note when the fetch is rejected", async () => {
    vi.mocked(getLapMap).mockRejectedValue(new Error("API request failed"));

    render(TrackMap, { props: { lapId: 7 } });

    expect(await screen.findByText(/no map for this track/i)).toBeTruthy();
  });

  it("renders a dot per live car, positioned by lap distance", async () => {
    vi.mocked(getLapMap).mockResolvedValue(mapFixture);

    const { container } = render(TrackMap, {
      props: {
        lapId: 1,
        cars: [
          { id: 1, lapDist: 0, cls: "GT3", isPlayer: false },
          { id: 2, lapDist: 0, cls: "GT4", isPlayer: false },
          { id: 3, lapDist: 100, cls: "Hypercar", isPlayer: true },
        ],
      },
    });

    await waitFor(() => expect(container.querySelectorAll(".car-dot").length).toBe(3));
    const player = container.querySelector(".car-dot.player");
    expect(player).not.toBeNull();
  });

  it("#151: draws a tick at each sector boundary from the map's sectors, none when absent", async () => {
    const withSectors = { ...mapFixture, sectors: [50] } as unknown as TrackMapData;
    const { container } = render(TrackMap, { props: { mapData: withSectors } });
    await waitFor(() => expect(container.querySelectorAll("rect.sector-tick").length).toBe(1));
    const tick = container.querySelector("rect.sector-tick") as SVGRectElement;
    // Sector at lap distance 50 sits on path point [10, 20].
    const cx = Number(tick.getAttribute("x")) + Number(tick.getAttribute("width")) / 2;
    const cy = Number(tick.getAttribute("y")) + Number(tick.getAttribute("height")) / 2;
    expect(cx).toBeCloseTo(10);
    expect(cy).toBeCloseTo(20);
    cleanup();

    const none = { ...mapFixture, sectors: [] } as unknown as TrackMapData;
    const { container: c2 } = render(TrackMap, { props: { mapData: none } });
    await waitFor(() => expect(c2.querySelector("polyline")).not.toBeNull());
    expect(c2.querySelectorAll("rect.sector-tick").length).toBe(0);
  });
});
