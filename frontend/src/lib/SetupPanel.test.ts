import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/svelte";
import { getSetup, captureSetup, getLapSetup, getSessionSetup, type LapSetup } from "./api";
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
  it("shows the selected lap's stored setup, labelled by lap number then time", async () => {
    vi.mocked(getSetup).mockResolvedValue(mixedPayload);
    vi.mocked(getLapSetup).mockResolvedValue({
      lap_id: 7,
      lap_no: 12,
      lap_time: 101.234,
      setup: { VM_REAR_WING: { isFreeSetting: true, stringValue: "P9" } },
    });

    render(SetupPanel, { props: { lapId: 7 } });

    expect(await screen.findByText("P9")).toBeTruthy();
    expect(screen.getByText("Setup on Lap 12 · 1:41.234")).toBeTruthy();
    expect(getLapSetup).toHaveBeenCalledWith(7);
    expect(getSetup).not.toHaveBeenCalled();
    expect(captureSetup).not.toHaveBeenCalled();
  });

  it("rounds the lap time before splitting minutes (59.9996 is 1:00.000)", async () => {
    vi.mocked(getLapSetup).mockResolvedValue({
      lap_id: 7,
      lap_no: 3,
      lap_time: 59.9996,
      setup: { VM_REAR_WING: { isFreeSetting: true, stringValue: "P9" } },
    });

    render(SetupPanel, { props: { lapId: 7 } });

    expect(await screen.findByText("Setup on Lap 3 · 1:00.000")).toBeTruthy();
  });

  it("says no setup was recorded for a lap driven before per-lap setups", async () => {
    vi.mocked(getLapSetup).mockResolvedValue({ lap_id: 3, lap_no: 2, lap_time: 99, setup: null });

    render(SetupPanel, { props: { lapId: 3 } });

    expect(await screen.findByText(/no setup recorded for this lap/i)).toBeTruthy();
  });

  it("a failed fetch is an error, not 'no setup recorded'", async () => {
    vi.mocked(getLapSetup).mockRejectedValue(new Error("API request failed: GET -> 404"));

    render(SetupPanel, { props: { lapId: 3 } });

    expect(await screen.findByText(/couldn't load the setup/i)).toBeTruthy();
    expect(screen.queryByText(/no setup recorded/i)).toBeNull();
  });

  it("a reviewed session with no lap selected shows its fastest lap's setup", async () => {
    vi.mocked(getSessionSetup).mockResolvedValue({
      lap_id: 4,
      lap_no: 5,
      lap_time: 95.5,
      fastest: true,
      setup: { VM_BRAKE_BALANCE: { isFreeSetting: true, stringValue: "55.0:45.0" } },
    });

    render(SetupPanel, { props: { lapId: null, sessionId: "s1" } });

    expect(await screen.findByText("55.0:45.0")).toBeTruthy();
    expect(screen.getByText("Setup on fastest lap (Lap 5 · 1:35.500)")).toBeTruthy();
    expect(getSessionSetup).toHaveBeenCalledWith("s1");
    expect(getSetup).not.toHaveBeenCalled();
  });

  it("names a session's lap 'fastest lap with a recorded setup' when a faster one has none", async () => {
    vi.mocked(getSessionSetup).mockResolvedValue({
      lap_id: 4,
      lap_no: 5,
      lap_time: 95.5,
      fastest: false,
      setup: { VM_BRAKE_BALANCE: { isFreeSetting: true, stringValue: "55.0:45.0" } },
    });

    render(SetupPanel, { props: { lapId: null, sessionId: "s1" } });

    expect(
      await screen.findByText("Setup on fastest lap with a recorded setup (Lap 5 · 1:35.500)"),
    ).toBeTruthy();
  });

  it("a session with no recorded setup says so for the session", async () => {
    vi.mocked(getSessionSetup).mockResolvedValue({});

    render(SetupPanel, { props: { lapId: null, sessionId: "s1" } });

    expect(await screen.findByText("No setup recorded for this session.")).toBeTruthy();
  });

  it("a stale response resolving after a newer selection's is dropped", async () => {
    let resolveOld!: (v: LapSetup) => void;
    let resolveNew!: (v: LapSetup) => void;
    vi.mocked(getLapSetup)
      .mockReturnValueOnce(new Promise((r) => (resolveOld = r)))
      .mockReturnValueOnce(new Promise((r) => (resolveNew = r)));
    const row = (v: string) => ({ VM_REAR_WING: { isFreeSetting: true, stringValue: v } });

    const { rerender } = render(SetupPanel, { props: { lapId: 1 } });
    await rerender({ lapId: 2 });
    resolveNew({ lap_id: 2, lap_no: 2, lap_time: 90, setup: row("NEW") });
    expect(await screen.findByText("NEW")).toBeTruthy();
    resolveOld({ lap_id: 1, lap_no: 1, lap_time: 91, setup: row("OLD") });
    await new Promise((r) => setTimeout(r, 0));

    expect(screen.queryByText("OLD")).toBeNull();
    expect(screen.getByText("NEW")).toBeTruthy();
  });
});
