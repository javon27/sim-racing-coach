import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/svelte";
import { getStatus, getSettings } from "./api";
import SessionBar from "./SessionBar.svelte";

vi.mock("./api", async () => {
  const actual = await vi.importActual<typeof import("./api")>("./api");
  return { ...actual, getStatus: vi.fn(), getSettings: vi.fn() };
});

describe("SessionBar", () => {
  beforeEach(() => {
    vi.mocked(getStatus).mockReset();
    vi.mocked(getSettings).mockReset();
  });

  afterEach(cleanup);

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

  it("retries through transient failures and eventually renders data", async () => {
    vi.mocked(getStatus)
      .mockRejectedValueOnce(new Error("network"))
      .mockRejectedValueOnce(new Error("network"))
      .mockResolvedValue({
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

    render(SessionBar, { props: { retryTries: 5, retryDelayMs: 5 } });

    expect(await screen.findByText("Le Mans")).toBeTruthy();
  });

  it("shows an unreachable state with a Retry button when all attempts fail, and Retry re-fetches", async () => {
    vi.mocked(getStatus).mockRejectedValue(new Error("network"));
    vi.mocked(getSettings).mockRejectedValue(new Error("network"));

    render(SessionBar, { props: { retryTries: 2, retryDelayMs: 5 } });

    const retryBtn = await screen.findByRole("button", { name: /retry/i });
    expect(screen.getByText(/can't reach the coach server/i)).toBeTruthy();

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

    await fireEvent.click(retryBtn);

    expect(await screen.findByText("Le Mans")).toBeTruthy();
  });

  it("polls getStatus periodically and reflects updated data", async () => {
    vi.mocked(getStatus).mockResolvedValueOnce({
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

    render(SessionBar, { props: { pollMs: 5 } });

    expect(await screen.findByText("Le Mans")).toBeTruthy();

    vi.mocked(getStatus).mockResolvedValue({
      track: "Sebring",
      car: "Corvette Z06",
      on_track: true,
      has_map: true,
      lmu_connected: true,
    });

    expect(await screen.findByText("Sebring", {}, { timeout: 2000 })).toBeTruthy();
  });

  it("keeps last good data and does not go unreachable on a transient poll failure", async () => {
    vi.mocked(getStatus).mockResolvedValueOnce({
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

    render(SessionBar, { props: { pollMs: 5 } });

    expect(await screen.findByText("Le Mans")).toBeTruthy();

    // Establish a persistent good default for further polls, then fail exactly the next one.
    vi.mocked(getStatus).mockResolvedValue({
      track: "Le Mans",
      car: "Corvette Z06",
      on_track: false,
      has_map: true,
      lmu_connected: true,
    });
    vi.mocked(getStatus).mockRejectedValueOnce(new Error("transient"));

    await vi.waitFor(() => expect(vi.mocked(getStatus).mock.calls.length).toBeGreaterThan(1), {
      timeout: 2000,
    });

    expect(screen.getByText("Le Mans")).toBeTruthy();
    expect(screen.queryByText(/can't reach the coach server/i)).toBeNull();
  });

  it("stops polling after unmount", async () => {
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

    const { unmount } = render(SessionBar, { props: { pollMs: 5 } });

    expect(await screen.findByText("Le Mans")).toBeTruthy();
    await vi.waitFor(() => expect(vi.mocked(getStatus).mock.calls.length).toBeGreaterThan(1), {
      timeout: 2000,
    });

    unmount();
    const callsAtUnmount = vi.mocked(getStatus).mock.calls.length;
    await new Promise((r) => setTimeout(r, 50));
    expect(vi.mocked(getStatus).mock.calls.length).toBe(callsAtUnmount);
  });
});
