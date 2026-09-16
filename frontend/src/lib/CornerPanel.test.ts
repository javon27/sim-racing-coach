import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/svelte";
import { getLapAnalysis, getSettings } from "./api";
import CornerPanel from "./CornerPanel.svelte";

vi.mock("./api", async () => {
  const actual = await vi.importActual<typeof import("./api")>("./api");
  return { ...actual, getLapAnalysis: vi.fn(), getSettings: vi.fn() };
});

const cornerFixture = {
  corner_no: 4,
  name: "Mulsanne Corner",
  direction: "right",
  apex_dist: 1990,
  entry_spd: 60,
  apex_spd: 27,
  min_spd: 26.7,
  exit_spd: 40,
  brake_point: 1980,
  throttle_point: 2010,
  trailbrake_overlap: 0.12,
  peak_brake: 0.9,
  peak_lat_g: 1.4,
  peak_brake_g: null,
  peak_accel_g: null,
  peak_steer: null,
  abs_pct: null,
  tc_pct: null,
  track_edge_min: null,
  min_spd_delta: 0.9,
};

describe("CornerPanel", () => {
  beforeEach(() => {
    vi.mocked(getLapAnalysis).mockReset();
    vi.mocked(getSettings).mockReset();
    vi.mocked(getSettings).mockResolvedValue({ units: "metric" } as never);
  });

  afterEach(cleanup);

  it("shows a select-a-lap message when lapId is null", () => {
    render(CornerPanel, { props: { lapId: null } });
    expect(screen.getByText(/select a lap/i)).toBeTruthy();
    expect(getLapAnalysis).not.toHaveBeenCalled();
  });

  it("renders corner rows with name, direction arrow, and colored delta", async () => {
    vi.mocked(getLapAnalysis).mockResolvedValue({
      lap_time: 216.0,
      corners: [cornerFixture],
    });

    render(CornerPanel, { props: { lapId: 9 } });

    expect(await screen.findByText(/T4 Mulsanne Corner/)).toBeTruthy();
    expect(screen.getByText("↱", { exact: false })).toBeTruthy();
    // +0.9 km/h = carried MORE speed than best -> faster -> green (down class).
    const delta = await screen.findByText("+0.9");
    expect(delta.className).toContain("down");
    // Brake shown as metres BEFORE apex (apex_dist 1990 - brake_point 1980 = 10m),
    // not the raw distance from start/finish (1980m).
    expect(screen.getByText("10m")).toBeTruthy();
    expect(screen.queryByText("1980m")).toBeNull();
  });

  it("renders an em dash, not a number, when there is no brake point", async () => {
    // brake_point null = the driver never braked here. `apex_dist - null` is
    // apex_dist in JS, so a guard on the SUBTRACTION being positive would print the
    // apex distance as if it were a brake figure.
    vi.mocked(getLapAnalysis).mockResolvedValue({
      lap_time: 216.0,
      corners: [{ ...cornerFixture, brake_point: null }],
    });

    render(CornerPanel, { props: { lapId: 9 } });

    expect(await screen.findByText(/T4 Mulsanne Corner/)).toBeTruthy();
    expect(screen.getByText("—")).toBeTruthy();
    expect(screen.queryByText("1990m")).toBeNull();
  });

  it("renders a left-turn arrow and a red delta for a negative (slower) min_spd_delta", async () => {
    vi.mocked(getLapAnalysis).mockResolvedValue({
      lap_time: 216.0,
      corners: [{ ...cornerFixture, direction: "left", min_spd_delta: -0.2 }],
    });

    render(CornerPanel, { props: { lapId: 9 } });

    expect(await screen.findByText(/T4 Mulsanne Corner/)).toBeTruthy();
    expect(screen.getByText("↰", { exact: false })).toBeTruthy();
    // −0.2 km/h = less speed than best -> slower -> red (up class).
    const delta = await screen.findByText("−0.2");
    expect(delta.className).toContain("up");
  });

  it("shows a graceful no-analysis message when the fetch is rejected (e.g. a result-imported lap with no telemetry)", async () => {
    vi.mocked(getLapAnalysis).mockRejectedValue(new Error("API request failed: GET /laps/9/analysis -> 404"));

    render(CornerPanel, { props: { lapId: 9 } });

    expect(await screen.findByText(/no analysis for this lap/i)).toBeTruthy();
  });
  it("a partial lap's untouched corners render as dashes, never a measured-looking 0", async () => {
    // #77: an out/in/spin lap is stored and selectable. The corners it never reached
    // come back all-null (the sidecar maps NaN to null), and `null * 100` is 0 in JS,
    // so an unguarded cell would print "0%" trail-braking for a corner with no
    // telemetry at all -- a fabricated number on a screen whose rule is measured-only.
    vi.mocked(getLapAnalysis).mockResolvedValue({
      lap_time: null,
      corners: [
        { ...cornerFixture, corner_no: 1, name: "Left Hairpin", direction: "left",
          apex_dist: null, entry_spd: null, apex_spd: null, min_spd: null,
          exit_spd: null, brake_point: null, throttle_point: null,
          trailbrake_overlap: null, peak_brake: null, peak_lat_g: null,
          min_spd_delta: null },
        cornerFixture,
      ],
    } as never);

    render(CornerPanel, { props: { lapId: 9 } });

    await screen.findByText(/T1 Left Hairpin/);
    const row = screen.getByText(/T1 Left Hairpin/).closest("tr")!;
    const cells = [...row.querySelectorAll("td")].map((td) => td.textContent?.trim());
    expect(cells.slice(1)).toEqual(["—", "—", "—", "—"]);
    // The corner the lap DID reach still reads its real numbers.
    expect(screen.getByText("12%")).toBeTruthy();
  });
});
