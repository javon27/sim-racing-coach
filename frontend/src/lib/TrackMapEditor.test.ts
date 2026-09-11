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
  saveMapCloud,
  getTrackMap,
} from "./api";
import TrackMapEditor, { nearestPathIndex, nearestDistIndex, layoutLabels, wrapLabel, localOutward, type LabelItem } from "./TrackMapEditor.svelte";

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
    saveMapCloud: vi.fn(),
    getTrackMap: vi.fn(),
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

describe("wrapLabel", () => {
  it("keeps a short name on one line", () => expect(wrapLabel("Rivazza")).toEqual(["Rivazza"]));
  it("splits a long name into two balanced lines at a word boundary", () =>
    expect(wrapLabel("Variante della Roggia")).toEqual(["Variante", "della Roggia"]));
});

describe("localOutward", () => {
  it("points to the outside of the bend (opposite the curvature)", () => {
    // right-angle bend at index 1 ([10,0]); inside points up-left, so outside is down-right.
    const path: [number, number][] = [[0, 0], [10, 0], [10, 10], [0, 10]];
    const o = localOutward(path, 1, 1);
    expect(o.ox).toBeCloseTo(Math.SQRT1_2, 5);
    expect(o.oy).toBeCloseTo(-Math.SQRT1_2, 5);
    expect(o.curv).toBeGreaterThan(0);
  });
  it("reports ~zero curvature on a straight", () => {
    expect(localOutward([[0, 0], [10, 0], [20, 0]], 1, 1).curv).toBeLessThan(1e-6);
  });
});

describe("layoutLabels", () => {
  const bounds: [number, number, number, number] = [-10, -10, 110, 70];
  const mk = (anchor: [number, number], text: string, extra: Partial<LabelItem> = {}): LabelItem =>
    ({ anchor, text, outward: [0, -1], fontSize: 3, gap: 2, wrap: false, priority: 0, ...extra });
  // half-extents of a placed label's rendered box (same formula the layout uses)
  const box = (r: { lines: string[]; fontSize: number }) => {
    const w = Math.max(...r.lines.map((l) => l.length)) * r.fontSize * 0.6, h = r.lines.length * r.fontSize * 1.02;
    return { hw: w / 2 + r.fontSize * 0.15, hh: h / 2 + r.fontSize * 0.12 };
  };

  it("keeps every label box inside the bounds", () => {
    const items = [mk([0, 0], "T1"), mk([100, 0], "T2"), mk([0, 60], "T3"), mk([100, 60], "T4"),
      mk([50, 30], "Long Section Name", { wrap: true, priority: 1 })];
    const out = layoutLabels(items, { bounds, markers: items.map((i) => i.anchor), line: [] });
    for (const r of out) {
      const b = box(r);
      expect(r.cx - b.hw).toBeGreaterThanOrEqual(bounds[0] - 0.01);
      expect(r.cx + b.hw).toBeLessThanOrEqual(bounds[2] + 0.01);
      expect(r.cy - b.hh).toBeGreaterThanOrEqual(bounds[1] - 0.01);
      expect(r.cy + b.hh).toBeLessThanOrEqual(bounds[3] + 0.01);
    }
  });

  it("never covers another dot and never overlaps another label", () => {
    const items = [mk([48, 30], "T1"), mk([52, 30], "T2"), mk([50, 33], "T3"), mk([50, 27], "T4")];
    const markers = items.map((i) => i.anchor);
    const out = layoutLabels(items, { bounds, markers, line: [] });
    out.forEach((r, i) => {
      const b = box(r);
      markers.forEach((m, j) => { if (j !== i) expect(Math.abs(m[0] - r.cx) < b.hw && Math.abs(m[1] - r.cy) < b.hh).toBe(false); });
    });
    for (let i = 0; i < out.length; i++)
      for (let j = i + 1; j < out.length; j++) {
        const a = box(out[i]), b = box(out[j]);
        const ox = Math.min(out[i].cx + a.hw, out[j].cx + b.hw) - Math.max(out[i].cx - a.hw, out[j].cx - b.hw);
        const oy = Math.min(out[i].cy + a.hh, out[j].cy + b.hh) - Math.max(out[i].cy - a.hh, out[j].cy - b.hh);
        expect(ox > 1 && oy > 1).toBe(false);
      }
  });

  it("draws a leader only as a last resort", () => {
    // Open dot: a clean ring slot exists, so no leader.
    expect(layoutLabels([mk([50, 30], "T1")], { bounds, markers: [[50, 30]], line: [] })[0].leader).toBeNull();
    // A label bigger than the frame: no ring slot can fit inside the bounds, so a
    // leader connects the displaced label back to its dot.
    const tiny: [number, number, number, number] = [0, 0, 12, 12];
    const stuck = layoutLabels([mk([6, 6], "Long Name", { fontSize: 8, gap: 4 })], { bounds: tiny, markers: [[6, 6]], line: [] });
    expect(stuck[0].leader).not.toBeNull();
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
    await fireEvent.pointerDown(container.querySelector('svg[role="application"]')!);
    expect(container.querySelectorAll("li").length).toBe(3);

    await fireEvent.click(screen.getAllByLabelText("Remove corner")[0]);
    expect(container.querySelectorAll("li").length).toBe(2);
  });

  it("prefills an added corner's name with Turn N", async () => {
    const { container } = render(TrackMapEditor, { props: { lapId: 7, open: true } });
    await screen.findByText("First");

    await fireEvent.click(screen.getByText("Add corner"));
    await fireEvent.pointerDown(container.querySelector('svg[role="application"]')!);

    // jsdom has no CTM -> click maps to apex_dist 0 -> renumbers to T1 -> "Turn 1".
    const names = [...container.querySelectorAll("input.name")].map((el) => (el as HTMLInputElement).value);
    expect(names).toContain("Turn 1");
  });

  it("renumbers corners sequentially by track order after add", async () => {
    const { container } = render(TrackMapEditor, { props: { lapId: 7, open: true } });
    await screen.findByText("First");

    await fireEvent.click(screen.getByText("Add corner"));
    await fireEvent.pointerDown(container.querySelector('svg[role="application"]')!);

    // A corner added anywhere renumbers the set 1..N in apex order — no gaps,
    // no "T15" appended last.
    const nums = [...container.querySelectorAll(".num")].map((e) => e.textContent);
    expect(nums).toEqual(["T1", "T2", "T3"]);
  });

  it("surfaces the server's 422 detail on a failed save", async () => {
    vi.mocked(saveLapMap).mockRejectedValueOnce(
      new Error("API request failed: PUT /laps/7/map -> 422: corners must be a non-empty list"),
    );
    render(TrackMapEditor, { props: { lapId: 7, open: true } });
    await screen.findByText("First");

    await fireEvent.click(screen.getByText("Save"));
    expect(await screen.findByText(/corners must be a non-empty list/i)).toBeTruthy();
  });

  it("Add sectors seeds the game's real boundaries when captured", async () => {
    vi.mocked(getLapMap).mockResolvedValueOnce({ ...fixture, sectors: [], detected_sectors: [150, 250] });
    render(TrackMapEditor, { props: { lapId: 7, open: true } });
    await screen.findByText("First");
    await fireEvent.click(screen.getByText("Add sectors"));
    await fireEvent.click(screen.getByText("Save"));
    const [, body] = vi.mocked(saveLapMap).mock.calls[0];
    expect(body.sectors).toEqual([150, 250]); // real boundaries, not thirds (100,200)
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
    await fireEvent.pointerDown(container.querySelector('svg[role="application"]')!);

    // findBy* — the sections list renders reactively after the click.
    await fireEvent.input(await screen.findByLabelText("Section name"), { target: { value: "Back Straight" } });
    // Long names wrap onto two <tspan> lines; joining them recovers the full name.
    expect([...container.querySelectorAll("text.section-lbl tspan")].map((t) => t.textContent).join(" ")).toBe("Back Straight");

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
    vi.mocked(saveMapCloud).mockReset().mockResolvedValue({ saved: true, slug: "monza" });
    cap.laps = [];
    vi.mocked(mapCaptureStatus).mockReset().mockImplementation(async () => ({ ...cap, laps: [...cap.laps] }));
  });
  afterEach(cleanup);

  it("point-cloud capture: build with no designation, preview, and save", async () => {
    const onSaved = vi.fn();
    const onClose = vi.fn();
    render(TrackMapEditor, { props: { lapId: 7, open: true, onSaved, onClose, pollMs: 5 } });
    await screen.findByText("First"); // editor loaded

    await fireEvent.click(screen.getByText("Generate map layout"));
    expect(startMapCapture).toHaveBeenCalled();
    await screen.findByText(/three full laps/i);

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
    const svg = (await screen.findByText("Save ribbon")).closest(".modal")!.querySelector('svg[role="img"]')!;
    expect(svg.querySelector("path")).toBeTruthy(); // the band
    expect(screen.getAllByText("T1").length).toBeGreaterThan(0); // svg label + list row

    await fireEvent.click(screen.getByText("Save ribbon"));
    await waitFor(() => expect(saveRibbon).toHaveBeenCalled());
    expect(onSaved).toHaveBeenCalled();
    expect(onClose).toHaveBeenCalled();
  });

  it("'Save capture only' persists the cloud without rebuilding geometry", async () => {
    const onSaved = vi.fn();
    const onClose = vi.fn();
    render(TrackMapEditor, { props: { lapId: 7, open: true, onSaved, onClose, pollMs: 5 } });
    await screen.findByText("First"); // map (with corners) loaded

    await fireEvent.click(screen.getByText("Generate map layout"));
    await screen.findByText(/three full laps/i);

    // Needs at least two passes; disabled until then.
    const btn = await screen.findByText("Save capture only");
    expect((btn as HTMLButtonElement).disabled).toBe(true);
    cap.laps = [
      { idx: 0, samples: 500, length: 5000 },
      { idx: 1, samples: 480, length: 4800 },
    ];
    await waitFor(() => expect((screen.getByText("Save capture only") as HTMLButtonElement).disabled).toBe(false));

    await fireEvent.click(screen.getByText("Save capture only"));
    await waitFor(() => expect(saveMapCloud).toHaveBeenCalled());
    expect(saveRibbon).not.toHaveBeenCalled(); // geometry untouched
    expect(onClose).toHaveBeenCalled();
  });

  it("cancelling the capture calls cancelMapCapture and returns to the editor", async () => {
    render(TrackMapEditor, { props: { lapId: 7, open: true, pollMs: 5 } });
    await screen.findByText("First");

    await fireEvent.click(screen.getByText("Generate map layout"));
    await screen.findByText(/three full laps/i);

    await fireEvent.click(screen.getByText("Cancel capture"));
    expect(cancelMapCapture).toHaveBeenCalled();
    expect(await screen.findByText("First")).toBeTruthy(); // back in the normal editor
  });

  it("cancels mapping mode on unmount while capturing (no silent buffering)", async () => {
    const { unmount } = render(TrackMapEditor, { props: { lapId: 7, open: true, pollMs: 5 } });
    await screen.findByText("First");
    await fireEvent.click(screen.getByText("Generate map layout"));
    await screen.findByText(/three full laps/i);

    unmount();
    expect(cancelMapCapture).toHaveBeenCalled();
  });

  it("cancels mapping mode when open flips false while capturing", async () => {
    const { rerender } = render(TrackMapEditor, { props: { lapId: 7, open: true, pollMs: 5 } });
    await screen.findByText("First");
    await fireEvent.click(screen.getByText("Generate map layout"));
    await screen.findByText(/three full laps/i);

    await rerender({ lapId: 7, open: false });
    await waitFor(() => expect(cancelMapCapture).toHaveBeenCalled());
  });

  it("Rebuild from saved capture: calls rebuildFromCapture and previews the result", async () => {
    render(TrackMapEditor, { props: { lapId: 7, open: true } });
    await screen.findByText("First");

    await fireEvent.click(screen.getByText("Rebuild from saved capture"));

    expect(rebuildFromCapture).toHaveBeenCalled();
    const svg = (await screen.findByText("Save ribbon")).closest(".modal")!.querySelector('svg[role="img"]')!;
    expect(svg.querySelector("path")).toBeTruthy(); // the band
    expect(screen.getAllByText("T1").length).toBeGreaterThan(0);
  });

  it("Rebuild says how many laps back the geometry, before Save", async () => {
    // A map off 1-3 clouds is thin (#41), and the rebuild may be falling back to the
    // track's own live laps -- the driver has to see the count before committing it.
    vi.mocked(rebuildFromCapture).mockResolvedValue({ ...ribbonFix, lap_count: 3 });
    render(TrackMapEditor, { props: { lapId: 7, open: true } });
    await screen.findByText("First");

    await fireEvent.click(screen.getByText("Rebuild from saved capture"));
    const footer = (await screen.findByText("Save ribbon")).closest("footer")!;
    expect(footer.textContent).toContain("from 3 laps");
    // No elevation-lap warning: width/banking are gated server-side on real lateral
    // spread, so a lap count says nothing about whether they are right (#69).
    expect(footer.textContent).not.toMatch(/elevation|banking/i);
  });

  it("Rebuild omits the lap count when the server didn't send one", async () => {
    vi.mocked(rebuildFromCapture).mockResolvedValue(ribbonFix);   // no lap_count
    render(TrackMapEditor, { props: { lapId: 7, open: true } });
    await screen.findByText("First");

    await fireEvent.click(screen.getByText("Rebuild from saved capture"));
    const footer = (await screen.findByText("Save ribbon")).closest("footer")!;
    expect(footer.textContent).not.toMatch(/\blaps?\b/);          // not "from undefined laps"
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
    // (Heading and button share this text here, so target the button.)
    await fireEvent.click(await screen.findByRole("button", { name: "Generate map layout" }));

    expect(startMapCapture).toHaveBeenCalled();
    await screen.findByText(/three full laps/i); // entered + stayed in capturing
    expect(cancelMapCapture).not.toHaveBeenCalled(); // the bug: instant cancel

    cap.laps = [{ idx: 0, samples: 500, length: 5000 }];
    await waitFor(() => expect(screen.getByText(/1 lap\(s\) buffered/i)).toBeTruthy());
    expect(cancelMapCapture).not.toHaveBeenCalled(); // still alive while buffering
  });
});

describe("TrackMapEditor — Tracks-screen (by-slug) binding", () => {
  beforeEach(() => {
    vi.mocked(getTrackMap).mockReset().mockResolvedValue({});
    vi.mocked(rebuildFromCapture).mockReset().mockResolvedValue(ribbonFix);
  });
  afterEach(cleanup);

  it("rebuilds the SELECTED track, not the live one (#26)", async () => {
    // Editing from the Tracks screen: a slug, no lap. Sending neither lets the
    // sidecar fall back to the live sim / newest capture and write a different
    // track's geometry over this one.
    render(TrackMapEditor, { props: { lapId: null, slug: "autodromo_nazionale_monza", open: true } });

    await fireEvent.click(await screen.findByText("Rebuild from saved capture"));

    expect(rebuildFromCapture).toHaveBeenCalledWith(null, "autodromo_nazionale_monza");
  });
});

describe("TrackMapEditor — rotation", () => {
  beforeEach(() => {
    vi.mocked(getLapMap).mockReset().mockResolvedValue({ ...fixture, rotation: 0 });
    vi.mocked(setMapRotation).mockReset().mockResolvedValue({ ...fixture, rotation: 40 });
    vi.mocked(saveLapMap).mockReset().mockResolvedValue({ ...fixture, rotation: 40 });
  });
  afterEach(cleanup);

  it("shows the baked rotation and has no Apply button", async () => {
    render(TrackMapEditor, { props: { lapId: 7, open: true } });
    await screen.findByText("First");
    expect((screen.getByLabelText("Rotation degrees") as HTMLInputElement).value).toBe("0");
    expect(screen.queryByText("Apply")).toBeNull();
  });

  it("nudging previews live but does NOT persist until Save (once, 1x)", async () => {
    const { container } = render(TrackMapEditor, { props: { lapId: 7, open: true } });
    await screen.findByText("First");

    await fireEvent.input(screen.getByLabelText("Rotation degrees"), { target: { value: "40" } });

    // Live preview rotates by the entered angle (baked 0 + preview 40)...
    const g = container.querySelector("g[transform]") as SVGGElement;
    expect(g.getAttribute("transform")).toMatch(/^rotate\(40[,\s]/);
    // ...but nothing is persisted until Save.
    expect(setMapRotation).not.toHaveBeenCalled();

    await fireEvent.click(screen.getByText("Save"));
    await waitFor(() => expect(setMapRotation).toHaveBeenCalledWith(7, 40)); // once, on Save
    expect(vi.mocked(setMapRotation).mock.calls.length).toBe(1);
    expect(saveLapMap).toHaveBeenCalled();
  });

  it("Save persists the pending rotation AND the edited corners together", async () => {
    render(TrackMapEditor, { props: { lapId: 7, open: true } });
    await screen.findByText("First");

    await fireEvent.input(screen.getAllByLabelText("Corner name")[0], { target: { value: "Zed" } });
    await fireEvent.input(screen.getByLabelText("Rotation degrees"), { target: { value: "40" } });
    await fireEvent.click(screen.getByText("Save"));

    await waitFor(() => expect(setMapRotation).toHaveBeenCalledWith(7, 40));
    const [, body] = vi.mocked(saveLapMap).mock.calls[0];
    expect(body.corners[0].name).toBe("Zed"); // edited corner saved on the same Save
  });

  it("does not persist rotation on Save when the angle is unchanged", async () => {
    render(TrackMapEditor, { props: { lapId: 7, open: true } });
    await screen.findByText("First");
    await fireEvent.click(screen.getByText("Save"));
    await waitFor(() => expect(saveLapMap).toHaveBeenCalled());
    expect(setMapRotation).not.toHaveBeenCalled(); // angle == baked -> skip
  });

  it("shows Save failed when persisting the rotation throws", async () => {
    vi.mocked(setMapRotation).mockRejectedValue(new Error("boom"));
    render(TrackMapEditor, { props: { lapId: 7, open: true } });
    await screen.findByText("First");
    await fireEvent.input(screen.getByLabelText("Rotation degrees"), { target: { value: "40" } });
    await fireEvent.click(screen.getByText("Save"));
    expect(await screen.findByText("Save failed.")).toBeTruthy();
  });
});
