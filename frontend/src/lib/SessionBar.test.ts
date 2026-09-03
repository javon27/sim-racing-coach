import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/svelte";
import { getStatus, getSettings } from "./api";
import SessionBar from "./SessionBar.svelte";

vi.mock("./api", () => ({
  getStatus: vi.fn(),
  getSettings: vi.fn(),
}));

describe("SessionBar", () => {
  beforeEach(() => {
    vi.mocked(getStatus).mockReset();
    vi.mocked(getSettings).mockReset();
  });

  it("renders track/car + units from the API", async () => {
    vi.mocked(getStatus).mockResolvedValue({
      track: "Le Mans",
      car: "Corvette Z06",
      on_track: false,
      has_map: true,
      lmu_connected: true,
    });
    vi.mocked(getSettings).mockResolvedValue({
      units: "metric",
      input_device: "wheel",
      model: "sonnet",
      corner_naming: "names",
      driver: { braking_style: null, balance_preference: null, risk_tolerance: null },
      api_key_set: true,
      data_dir: "x",
      app_version: "0.1",
      lmu_build: null,
    });

    render(SessionBar);

    expect(await screen.findByText("Le Mans")).toBeTruthy();
    expect(screen.getByText("Corvette Z06")).toBeTruthy();
    expect(screen.getByText("metric")).toBeTruthy();
  });

  it("shows a server-not-reachable state when the fetch fails", async () => {
    vi.mocked(getStatus).mockRejectedValue(new Error("network"));
    vi.mocked(getSettings).mockRejectedValue(new Error("network"));

    render(SessionBar);

    expect(await screen.findByText(/not reachable/i)).toBeTruthy();
  });
});
