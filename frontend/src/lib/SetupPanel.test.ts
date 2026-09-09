import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/svelte";
import { getSetup, captureSetup } from "./api";
import SetupPanel from "./SetupPanel.svelte";

vi.mock("./api", async () => {
  const actual = await vi.importActual<typeof import("./api")>("./api");
  return { ...actual, getSetup: vi.fn(), captureSetup: vi.fn() };
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
});
