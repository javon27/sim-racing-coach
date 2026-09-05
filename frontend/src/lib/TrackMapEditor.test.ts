import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, cleanup, fireEvent, waitFor } from "@testing-library/svelte";
import {
  getLapMap,
  saveLapMap,
  setMapRotation,
  startMapCapture,
  cancelMapCapture,
  mapCaptureStatus,
  buildRibbon,
  rebuildFromCapture,
  saveRibbon,
} from "./api";
import TrackMapEditor, { nearestPathIndex, nearestDistIndex } from "./TrackMapEditor.svelte";

vi.mock("./api", async () => {
  const actual = await vi.importActual<typeof import("./api")>("./api");
  return {
    ...actual,
    getLapMap: vi.fn(),
    saveLapMap: vi.fn(),
    setMapRotation: vi.fn(),
    startMapCapture: vi.fn(),
    cancelMapCapture: vi.fn(),
    mapCaptureStatus: vi.fn(),
    buildRibbon: vi.fn(),
    rebuildFromCapture: vi.fn(),
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

  it("opens with the SAVED map (avg=0), not a re-average", async () => {
    render(TrackMapEditor, { props: { lapId: 7, open: true } });
    expect(await screen.findByText("First")).toBeTruthy();
    expect(getLapMap).toHaveBeenCalledWith(7); // no avg=1 — shows the frozen/canonical ribbon
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

  it("prefills an added corner's name with Turn N", async () => {
    const { container } = render(TrackMapEditor, { props: { lapId: 7, open: true } });
    await screen.findByText("First");

    await fireEvent.click(screen.getByText("Add corner"));
    await fireEvent.pointerDown(container.querySelector("svg")!);

    // jsdom has no CTM -> click maps to apex_dist 0 -> renumbers to T1 -> "Turn 1".
    const names = [...container.querySelectorAll("input.name")].map((el) => (el as HTMLInputElement).value);
    expect(names).toContain("Turn 1");
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

  it("accepts a letter label (8A): text input, renders T8A, saves + survives round-trip", async () => {
    const { container } = render(TrackMapEditor, { props: { lapId: 7, open: true } });
    await screen.findByText("First");

    const lbl = screen.getAllByLabelText("Corner label")[0] as HTMLInputElement;
    expect(lbl.type).toBe("text"); // NOT number — must accept letters

    await fireEvent.input(lbl, { target: { value: "8A" } });

    // Map draws the label, not the positional number.
    const labels = [...container.querySelectorAll("text.corner-lbl")].map((e) => e.textContent);
    expect(labels).toContain("T8A");

    // Saved payload carries the label; a backend that echoes it keeps T8A after save.
    vi.mocked(saveLapMap).mockResolvedValueOnce({
      ...fixture,
      corners: fixture.corners.map((c, i) => (i === 0 ? { ...c, label: "8A" } : { ...c, label: String(c.number) })),
    } as never);
    await fireEvent.click(screen.getByText("Save"));

    const [, body] = vi.mocked(saveLapMap).mock.calls[0];
    expect(body.corners[0].label).toBe("8A");
    await waitFor(() =>
      expect([...container.querySelectorAll("text.corner-lbl")].map((e) => e.textContent)).toContain("T8A"),
    );
  });

  it("adds a second apex and saves both in the corner's apexes", async () => {
    render(TrackMapEditor, { props: { lapId: 7, open: true } });
    await screen.findByText("First");

    await fireEvent.click(screen.getAllByLabelText("Add apex")[0]);
    await fireEvent.click(screen.getByText("Save"));

    const [, body] = vi.mocked(saveLapMap).mock.calls[0];
    expect(body.corners[0].apexes).toHaveLength(2);
    expect(body.corners[1].apexes).toHaveLength(1); // untouched corner stays single
  });

  it("add-section: enter mode, click the line to place a spanning section, name + save it", async () => {
    const { container } = render(TrackMapEditor, { props: { lapId: 7, open: true } });
    await screen.findByText("First");

    // Click "Add section" to enter add-section mode, then a pointerdown on the
    // svg places a section at the clicked point (click-to-place, like add-corner).
    await fireEvent.click(screen.getByText("Add section"));
    await fireEvent.pointerDown(container.querySelector("svg")!);

    // findBy* — the sections list renders reactively after the click.
    await fireEvent.input(await screen.findByLabelText("Section name"), { target: { value: "Back Straight" } });
    expect(container.querySelector("text.section-lbl")?.textContent).toBe("Back Straight");

    await fireEvent.click(screen.getByText("Save"));
    const [, body] = vi.mocked(saveLapMap).mock.calls[0];
    expect(body.sections).toHaveLength(1);
    expect(body.sections[0].name).toBe("Back Straight");
    // Span is a real range around the clicked point, not a zero/degenerate one.
    expect(body.sections[0].end_dist).toBeGreaterThan(body.sections[0].start_dist);
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
    vi.mocked(rebuildFromCapture).mockReset().mockResolvedValue(ribbonFix);
    vi.mocked(saveRibbon).mockReset().mockResolvedValue({ saved: true, slug: "monza" });
    cap.laps = [];
    vi.mocked(mapCaptureStatus).mockReset().mockImplementation(async () => ({ ...cap, laps: [...cap.laps] }));
  });
  afterEach(cleanup);

  it("point-cloud capture: build with no designation, preview, and save", async () => {
    const onSaved = vi.fn();
    const onClose = vi.fn();
    render(TrackMapEditor, { props: { lapId: 7, open: true, onSaved, onClose, pollMs: 5 } });
    await screen.findByText("First"); // editor loaded

    await fireEvent.click(screen.getByText("(Re)generate map layout"));
    expect(startMapCapture).toHaveBeenCalled();
    await screen.findByText(/full track width/i);

    // Build is disabled until at least one lap is buffered.
    const buildBtn = await screen.findByText("Build");
    expect((buildBtn as HTMLButtonElement).disabled).toBe(true);

    cap.laps = [
      { idx: 0, samples: 500, length: 5000 },
      { idx: 1, samples: 480, length: 4800 },
    ];
    await waitFor(() => expect((screen.getByText("Build") as HTMLButtonElement).disabled).toBe(false));

    await fireEvent.click(screen.getByText("Build"));
    expect(buildRibbon).toHaveBeenCalledWith(); // no left/right args

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
    await screen.findByText(/full track width/i);

    await fireEvent.click(screen.getByText("Cancel capture"));
    expect(cancelMapCapture).toHaveBeenCalled();
    expect(await screen.findByText("First")).toBeTruthy(); // back in the normal editor
  });

  it("cancels mapping mode on unmount while capturing (no silent buffering)", async () => {
    const { unmount } = render(TrackMapEditor, { props: { lapId: 7, open: true, pollMs: 5 } });
    await screen.findByText("First");
    await fireEvent.click(screen.getByText("(Re)generate map layout"));
    await screen.findByText(/full track width/i);

    unmount();
    expect(cancelMapCapture).toHaveBeenCalled();
  });

  it("cancels mapping mode when open flips false while capturing", async () => {
    const { rerender } = render(TrackMapEditor, { props: { lapId: 7, open: true, pollMs: 5 } });
    await screen.findByText("First");
    await fireEvent.click(screen.getByText("(Re)generate map layout"));
    await screen.findByText(/full track width/i);

    await rerender({ lapId: 7, open: false });
    await waitFor(() => expect(cancelMapCapture).toHaveBeenCalled());
  });

  it("Rebuild from saved capture: calls rebuildFromCapture and previews the result", async () => {
    render(TrackMapEditor, { props: { lapId: 7, open: true } });
    await screen.findByText("First");

    await fireEvent.click(screen.getByText("Rebuild from saved capture"));

    expect(rebuildFromCapture).toHaveBeenCalled();
    const svg = (await screen.findByText("Save ribbon")).closest(".modal")!.querySelector("svg")!;
    expect(svg.querySelector("path")).toBeTruthy(); // the band
    expect(screen.getAllByText("T1").length).toBeGreaterThan(0);
  });

  it("Rebuild shows a hint when there's no saved capture (422)", async () => {
    vi.mocked(rebuildFromCapture).mockRejectedValue(new Error("API request failed: POST /mapcapture/rebuild -> 422"));
    render(TrackMapEditor, { props: { lapId: 7, open: true } });
    await screen.findByText("First");

    await fireEvent.click(screen.getByText("Rebuild from saved capture"));
    expect(await screen.findByText(/no saved capture for this track/i)).toBeTruthy();
  });

  // Regression: on a fresh/unmapped session (lapId=null) starting a capture must
  // NOT be torn down by the teardown effect — it used to cancelGen() the instant
  // genPhase became "capturing", killing the capture and leaking laps to the store.
  it("fresh session (lapId=null): capture starts and is NOT auto-cancelled", async () => {
    render(TrackMapEditor, { props: { lapId: null, open: true, pollMs: 5 } });
    // No lap map -> the capture-entry panel, not a dead "Loading…".
    await fireEvent.click(await screen.findByText("(Re)generate map layout"));

    expect(startMapCapture).toHaveBeenCalled();
    await screen.findByText(/full track width/i); // entered + stayed in capturing
    expect(cancelMapCapture).not.toHaveBeenCalled(); // the bug: instant cancel

    cap.laps = [{ idx: 0, samples: 500, length: 5000 }];
    await waitFor(() => expect(screen.getByText(/1 lap\(s\) buffered/i)).toBeTruthy());
    expect(cancelMapCapture).not.toHaveBeenCalled(); // still alive while buffering
  });
});

describe("TrackMapEditor — rotation", () => {
  beforeEach(() => {
    vi.mocked(getLapMap).mockReset().mockResolvedValue({ ...fixture, rotation: 0 });
    vi.mocked(setMapRotation).mockReset();
  });
  afterEach(cleanup);

  it("shows the baked rotation and has no Apply button", async () => {
    render(TrackMapEditor, { props: { lapId: 7, open: true } });
    await screen.findByText("First");
    expect((screen.getByLabelText("Rotation degrees") as HTMLInputElement).value).toBe("0");
    expect(screen.queryByText("Apply")).toBeNull();
  });

  it("persists the ENTERED angle once (1x) — preview nets to identity, not 2x", async () => {
    // Backend bakes once and echoes rotation=entered.
    vi.mocked(setMapRotation).mockResolvedValue({ ...fixture, rotation: 40 });
    const { container } = render(TrackMapEditor, { props: { lapId: 7, open: true, rotDebounceMs: 0 } });
    await screen.findByText("First");

    await fireEvent.input(screen.getByLabelText("Rotation degrees"), { target: { value: "40" } });

    // Client sends exactly what the user entered (not 80).
    await waitFor(() => expect(setMapRotation).toHaveBeenCalledWith(7, 40));
    // After the rebaked geometry merges (rotation=40, angle=40) the preview group
    // is identity — so displayed = baked(40) + 0 = 40, i.e. 1x.
    await waitFor(() => {
      const g = container.querySelector("g[transform]") as SVGGElement;
      expect(g.getAttribute("transform")).toMatch(/^rotate\(0[,\s]/);
    });
  });

  it("does not clobber in-progress corner edits when rotation persists", async () => {
    vi.mocked(setMapRotation).mockResolvedValue({ ...fixture, rotation: 40 }); // server corners = First/Second
    render(TrackMapEditor, { props: { lapId: 7, open: true, rotDebounceMs: 0 } });
    await screen.findByText("First");

    const nameInput = screen.getAllByLabelText("Corner name")[0] as HTMLInputElement;
    await fireEvent.input(nameInput, { target: { value: "Zed" } });

    await fireEvent.input(screen.getByLabelText("Rotation degrees"), { target: { value: "40" } });
    await waitFor(() => expect(setMapRotation).toHaveBeenCalled());

    // The edited name survives — rotation merged only the geometry, not corners.
    expect((screen.getAllByLabelText("Corner name")[0] as HTMLInputElement).value).toBe("Zed");
  });

  it("reverts the preview to identity when a rotation persist fails (drags realign)", async () => {
    vi.mocked(setMapRotation).mockRejectedValue(new Error("boom"));
    const { container } = render(TrackMapEditor, { props: { lapId: 7, open: true, rotDebounceMs: 0 } });
    await screen.findByText("First");

    await fireEvent.input(screen.getByLabelText("Rotation degrees"), { target: { value: "40" } });
    await waitFor(() => expect(setMapRotation).toHaveBeenCalled());

    // On failure angle reverts to the baked rotation (0) → preview group is
    // identity again, so drag hit-tests against the unrotated path still line up.
    await waitFor(() => {
      const g = container.querySelector("g[transform]") as SVGGElement;
      expect(g.getAttribute("transform")).toMatch(/^rotate\(0[,\s]/);
    });
  });
});
