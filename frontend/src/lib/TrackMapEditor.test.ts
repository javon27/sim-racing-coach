import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, cleanup, fireEvent, waitFor } from "@testing-library/svelte";
import {
  getLapMap,
  saveLapMap,
  startMapCapture,
  cancelMapCapture,
  mapCaptureStatus,
  buildRibbon,
  saveRibbon,
} from "./api";
import TrackMapEditor, { nearestPathIndex, nearestDistIndex } from "./TrackMapEditor.svelte";

vi.mock("./api", async () => {
  const actual = await vi.importActual<typeof import("./api")>("./api");
  return {
    ...actual,
    getLapMap: vi.fn(),
    saveLapMap: vi.fn(),
    startMapCapture: vi.fn(),
    cancelMapCapture: vi.fn(),
    mapCaptureStatus: vi.fn(),
    buildRibbon: vi.fn(),
    saveRibbon: vi.fn(),
  };
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

  it("renumbers corners sequentially by track order after add", async () => {
    const { container } = render(TrackMapEditor, { props: { lapId: 7, open: true } });
    await screen.findByText("First");

    await fireEvent.click(screen.getByText("Add corner"));
    await fireEvent.pointerDown(container.querySelector("svg")!);

    // A corner added anywhere renumbers the set 1..N in apex order — no gaps,
    // no "T15" appended last.
    const nums = [...container.querySelectorAll(".num")].map((e) => e.textContent);
    expect(nums).toEqual(["T1", "T2", "T3"]);
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

const ribbonFix = {
  width: 100,
  height: 60,
  left: [[0, 0], [10, 10]] as [number, number][],
  right: [[0, 20], [10, 30]] as [number, number][],
  center: [[0, 10], [10, 20]] as [number, number][],
  corners: [
    { number: 1, name: "Turn 1", direction: "left", apex_dist: 50, start_dist: 40, end_dist: 60, x: 5, y: 15 },
  ],
  sectors: [] as number[],
};

describe("TrackMapEditor — (re)generate map layout", () => {
  // Shared mutable status; tests push buffered laps to drive the poll progression.
  const cap = { active: true, live_track: "Monza", laps: [] as { idx: number; samples: number; length: number }[] };

  beforeEach(() => {
    vi.mocked(getLapMap).mockReset().mockResolvedValue(fixture);
    vi.mocked(saveLapMap).mockReset().mockResolvedValue(fixture);
    vi.mocked(startMapCapture).mockReset().mockResolvedValue({ active: true });
    vi.mocked(cancelMapCapture).mockReset().mockResolvedValue({ active: false });
    vi.mocked(buildRibbon).mockReset().mockResolvedValue(ribbonFix);
    vi.mocked(saveRibbon).mockReset().mockResolvedValue({ saved: true, slug: "monza" });
    cap.laps = [];
    vi.mocked(mapCaptureStatus).mockReset().mockImplementation(async () => ({ ...cap, laps: [...cap.laps] }));
  });
  afterEach(cleanup);

  it("guides left->right capture, builds the ribbon, and saves it", async () => {
    const onSaved = vi.fn();
    const onClose = vi.fn();
    render(TrackMapEditor, { props: { lapId: 7, open: true, onSaved, onClose, pollMs: 5 } });
    await screen.findByText("First"); // editor loaded

    await fireEvent.click(screen.getByText("(Re)generate map layout"));
    expect(startMapCapture).toHaveBeenCalled();
    await screen.findByText(/LEFT edge/i);

    // First boundary lap buffered -> advance to the RIGHT edge step.
    cap.laps = [{ idx: 0, samples: 500, length: 5000 }];
    await screen.findByText(/RIGHT edge/i);

    // Second lap buffered -> designation step with a Build button.
    cap.laps = [
      { idx: 0, samples: 500, length: 5000 },
      { idx: 1, samples: 480, length: 4800 },
    ];
    const buildBtn = await screen.findByText("Build ribbon");
    await fireEvent.click(buildBtn);

    expect(buildRibbon).toHaveBeenCalledWith(0, 1);
    // Preview renders the ribbon (filled band) + its solved corner.
    const svg = (await screen.findByText("Save ribbon")).closest(".modal")!.querySelector("svg")!;
    expect(svg.querySelector("path")).toBeTruthy(); // the band
    expect(screen.getAllByText("T1").length).toBeGreaterThan(0); // svg label + list row

    await fireEvent.click(screen.getByText("Save ribbon"));
    await waitFor(() => expect(saveRibbon).toHaveBeenCalled());
    expect(onSaved).toHaveBeenCalled();
    expect(onClose).toHaveBeenCalled();
  });

  it("cancelling the capture calls cancelMapCapture and returns to the editor", async () => {
    render(TrackMapEditor, { props: { lapId: 7, open: true, pollMs: 5 } });
    await screen.findByText("First");

    await fireEvent.click(screen.getByText("(Re)generate map layout"));
    await screen.findByText(/LEFT edge/i);

    await fireEvent.click(screen.getByText("Cancel capture"));
    expect(cancelMapCapture).toHaveBeenCalled();
    expect(await screen.findByText("First")).toBeTruthy(); // back in the normal editor
  });
});
