import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/svelte";
import { getLapMap } from "./api";
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
    expect(container.querySelectorAll("text").length).toBe(2);
  });

  it("shows the no-map note when the fetch returns an empty object", async () => {
    vi.mocked(getLapMap).mockResolvedValue({});

    render(TrackMap, { props: { lapId: 7 } });

    expect(await screen.findByText(/no track map for this lap/i)).toBeTruthy();
  });

  it("shows the no-map note when the fetch is rejected", async () => {
    vi.mocked(getLapMap).mockRejectedValue(new Error("API request failed"));

    render(TrackMap, { props: { lapId: 7 } });

    expect(await screen.findByText(/no track map for this lap/i)).toBeTruthy();
  });
});
