import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/svelte";
import { getLapAnalysis } from "./api";
import CornerPanel from "./CornerPanel.svelte";

vi.mock("./api", async () => {
  const actual = await vi.importActual<typeof import("./api")>("./api");
  return { ...actual, getLapAnalysis: vi.fn() };
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
  min_grip: null,
  min_spd_delta: 0.9,
};

describe("CornerPanel", () => {
  beforeEach(() => {
    vi.mocked(getLapAnalysis).mockReset();
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
    const delta = await screen.findByText("+0.90s");
    expect(delta.className).toContain("up");
    // Brake shown as metres BEFORE apex (apex_dist 1990 - brake_point 1980 = 10m),
    // not the raw distance from start/finish (1980m).
    expect(screen.getByText("10m")).toBeTruthy();
    expect(screen.queryByText("1980m")).toBeNull();
  });

  it("renders a left-turn arrow and a down delta for a negative min_spd_delta", async () => {
    vi.mocked(getLapAnalysis).mockResolvedValue({
      lap_time: 216.0,
      corners: [{ ...cornerFixture, direction: "left", min_spd_delta: -0.2 }],
    });

    render(CornerPanel, { props: { lapId: 9 } });

    expect(await screen.findByText(/T4 Mulsanne Corner/)).toBeTruthy();
    expect(screen.getByText("↰", { exact: false })).toBeTruthy();
    const delta = await screen.findByText("-0.20s");
    expect(delta.className).toContain("down");
  });

  it("shows a graceful no-analysis message when the fetch is rejected (e.g. a result-imported lap with no telemetry)", async () => {
    vi.mocked(getLapAnalysis).mockRejectedValue(new Error("API request failed: GET /laps/9/analysis -> 404"));

    render(CornerPanel, { props: { lapId: 9 } });

    expect(await screen.findByText(/no analysis for this lap/i)).toBeTruthy();
  });
});
