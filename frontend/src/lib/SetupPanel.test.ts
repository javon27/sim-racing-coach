import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/svelte";
import { getSetup, captureSetup, getLapSetup, getSessionSetup } from "./api";
import SetupPanel from "./SetupPanel.svelte";

vi.mock("./api", async () => {
  const actual = await vi.importActual<typeof import("./api")>("./api");
  return {
    ...actual,
    getSetup: vi.fn(),
    captureSetup: vi.fn(),
    getLapSetup: vi.fn(),
    getSessionSetup: vi.fn(),
  };
});

// A payload mixing adjustable (isFreeSetting:true) entries with the junk LMU
// returns for a car: fixed/non-adjustable/unavailable settings.
const mixedPayload = {
  VM_BRAKE_BALANCE: { isFreeSetting: true, stringValue: "48.0:52.0", value: 48 },
  VM_FRONT_WING: { isFreeSetting: true, stringValue: "Standard", value: 2 },
  VM_STEERING_LOCK: { isFreeSetting: false, stringValue: "Fixed", value: 0 },
  VM_SOMETHING_NA: { isFreeSetting: false, stringValue: "N/A", value: 0 },
  VM_DETACHED_PART: { available: false, stringValue: "Detached", value: 0 },
};

describe("SetupPanel", () => {
  beforeEach(() => {
    vi.mocked(getSetup).mockReset();
    vi.mocked(captureSetup).mockReset();
    vi.mocked(captureSetup).mockResolvedValue({ saved: true });
    vi.mocked(getLapSetup).mockReset();
    vi.mocked(getSessionSetup).mockReset();
  });

  afterEach(cleanup);

  it("renders only isFreeSetting entries and drops the junk", async () => {
    vi.mocked(getSetup).mockResolvedValue(mixedPayload);

    render(SetupPanel);

    // adjustable ones show
    expect(await screen.findByText("Brake Balance")).toBeTruthy();
    expect(screen.getByText("Front Wing")).toBeTruthy();
    // junk is absent (neither prettified label nor its value)
    expect(screen.queryByText("Steering Lock")).toBeNull();
    expect(screen.queryByText("Fixed")).toBeNull();
    expect(screen.queryByText("N/A")).toBeNull();
    expect(screen.queryByText("Detached")).toBeNull();
  });

  it("shows a curated readable name and its stringValue", async () => {
    vi.mocked(getSetup).mockResolvedValue(mixedPayload);

    render(SetupPanel);

    expect(await screen.findByText("Brake Balance")).toBeTruthy();
    expect(screen.getByText("48.0:52.0")).toBeTruthy();
  });

  it("shows the not-connected message when the dict is empty", async () => {
    vi.mocked(getSetup).mockResolvedValue({});

    render(SetupPanel);

    expect(await screen.findByText(/no setup.*lmu not connected/i)).toBeTruthy();
  });

  it("shows the not-connected message when the fetch is rejected", async () => {
    vi.mocked(getSetup).mockRejectedValue(new Error("API request failed: GET /setup -> 404"));

    render(SetupPanel);

    expect(await screen.findByText(/no setup.*lmu not connected/i)).toBeTruthy();
  });

  it("shows a distinct note when the payload is non-empty but all junk", async () => {
    vi.mocked(getSetup).mockResolvedValue({
      VM_STEERING_LOCK: { isFreeSetting: false, stringValue: "Fixed", value: 0 },
      VM_DETACHED_PART: { available: false, stringValue: "Detached", value: 0 },
    });

    render(SetupPanel);

    expect(await screen.findByText(/no adjustable settings/i)).toBeTruthy();
  });
  // #188: a selected lap shows the setup it was driven on, labelled with the lap.
  it("shows the selected lap's stored setup, not the live one", async () => {
    vi.mocked(getSetup).mockResolvedValue(mixedPayload);
    vi.mocked(getLapSetup).mockResolvedValue({
      lap_id: 7,
      lap_time: 101.234,
      setup: { VM_REAR_WING: { isFreeSetting: true, stringValue: "P9" } },
    });

    render(SetupPanel, { props: { lapId: 7 } });

    expect(await screen.findByText("P9")).toBeTruthy();
    expect(screen.getByText(/setup on lap 1:41\.234/i)).toBeTruthy();
    expect(getLapSetup).toHaveBeenCalledWith(7);
    expect(getSetup).not.toHaveBeenCalled();
    expect(captureSetup).not.toHaveBeenCalled();
  });

  it("says no setup was recorded for a lap driven before per-lap setups", async () => {
    vi.mocked(getLapSetup).mockResolvedValue({ lap_id: 3, lap_time: 99, setup: null });

    render(SetupPanel, { props: { lapId: 3 } });

    expect(await screen.findByText(/no setup recorded for this lap/i)).toBeTruthy();
  });

  it("a reviewed session with no lap selected shows its fastest lap's setup", async () => {
    vi.mocked(getSessionSetup).mockResolvedValue({
      lap_id: 4,
      lap_time: 95.5,
      setup: { VM_BRAKE_BALANCE: { isFreeSetting: true, stringValue: "55.0:45.0" } },
    });

    render(SetupPanel, { props: { lapId: null, sessionId: "s1" } });

    expect(await screen.findByText("55.0:45.0")).toBeTruthy();
    expect(screen.getByText(/setup on fastest lap 1:35\.500/i)).toBeTruthy();
    expect(getSessionSetup).toHaveBeenCalledWith("s1");
    expect(getSetup).not.toHaveBeenCalled();
  });

  it("a session with no recorded setup says so", async () => {
    vi.mocked(getSessionSetup).mockResolvedValue({});

    render(SetupPanel, { props: { lapId: null, sessionId: "s1" } });

    expect(await screen.findByText(/no setup recorded for this lap/i)).toBeTruthy();
  });
});
