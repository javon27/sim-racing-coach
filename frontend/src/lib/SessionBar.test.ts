import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/svelte";
import { getSettings, type StatusResponse } from "./api";
import { eventFeed } from "./events";
import SessionBar from "./SessionBar.svelte";

vi.mock("./api", async () => {
  const actual = await vi.importActual<typeof import("./api")>("./api");
  return { ...actual, getSettings: vi.fn() };
});

const SETTINGS = {
  units: "metric",
  input_device: "wheel",
  model: "sonnet",
  corner_naming: "names",
  driver: { braking_style: null, balance_preference: null, risk_tolerance: null },
  api_key_set: true,
  data_dir: "x",
  app_version: "0.1",
  lmu_build: null,
};

function status(over: Partial<StatusResponse> = {}): StatusResponse {
  return {
    track: "Le Mans",
    car: "Corvette Z06",
    on_track: false,
    activity: null,
    has_map: true,
    lmu_connected: true,
    session_id: "s1",
    lap_id: 1,
    ...over,
  };
}

/** A status frame off the /events feed — what used to be a getStatus() poll tick. */
function pushStatus(over: Partial<StatusResponse> = {}) {
  eventFeed.push("status", status(over));
}

describe("SessionBar", () => {
  beforeEach(() => {
    vi.mocked(getSettings).mockReset();
    vi.mocked(getSettings).mockResolvedValue(SETTINGS as never);
  });

  afterEach(cleanup);

  it("renders track/car from the /events feed and units from the API", async () => {
    render(SessionBar);
    pushStatus();

    expect(await screen.findByText("Le Mans")).toBeTruthy();
    expect(screen.getByText("Corvette Z06")).toBeTruthy();
    expect(await screen.findByText("metric")).toBeTruthy();
  });

  it("shows the live driving state (On track / Garage / In menus / LMU offline)", async () => {
    let view = render(SessionBar);
    pushStatus({ on_track: true, activity: "track" });
    expect(await screen.findByText("On track")).toBeTruthy();
    view.unmount();

    // Garage: detected live even before any lap is completed.
    view = render(SessionBar);
    pushStatus({ activity: "garage" });
    expect(await screen.findByText("Garage")).toBeTruthy();
    view.unmount();

    // In a session menu (connected, no player state).
    view = render(SessionBar);
    pushStatus({ track: null, car: null, activity: null });
    expect(await screen.findByText("In menus")).toBeTruthy();
    view.unmount();

    render(SessionBar);
    pushStatus({ track: null, car: null, activity: null, lmu_connected: false });
    expect(await screen.findByText("LMU offline")).toBeTruthy();
  });

  it("retries through transient settings failures and eventually renders units", async () => {
    vi.mocked(getSettings)
      .mockRejectedValueOnce(new Error("network"))
      .mockRejectedValueOnce(new Error("network"))
      .mockResolvedValue(SETTINGS as never);

    render(SessionBar, { props: { retryTries: 5, retryDelayMs: 5 } });
    pushStatus();

    expect(await screen.findByText("Le Mans")).toBeTruthy();
    expect(await screen.findByText("metric")).toBeTruthy();
  });

  it("shows an unreachable state with a Retry button when no frame arrives, and Retry recovers", async () => {
    // Nothing pushed: the feed has never delivered a frame, so after the grace
    // window (retryTries x retryDelayMs) the header says the server is gone.
    render(SessionBar, { props: { retryTries: 2, retryDelayMs: 5 } });

    const retryBtn = await screen.findByRole("button", { name: /retry/i });
    expect(screen.getByText(/no server/i)).toBeTruthy();

    await fireEvent.click(retryBtn);
    pushStatus();

    expect(await screen.findByText("Le Mans")).toBeTruthy();
    expect(screen.queryByText(/no server/i)).toBeNull();
  });

  it("re-renders when the next status frame arrives (no polling)", async () => {
    render(SessionBar);
    pushStatus();
    expect(await screen.findByText("Le Mans")).toBeTruthy();

    pushStatus({ track: "Sebring", on_track: true });

    expect(await screen.findByText("Sebring")).toBeTruthy();
  });

  it("keeps last good data and does not go unreachable when the socket blips", async () => {
    render(SessionBar, { props: { retryTries: 20, retryDelayMs: 50 } });
    pushStatus();
    expect(await screen.findByText("Le Mans")).toBeTruthy();

    // The socket drops, then a frame arrives well inside the grace window.
    eventFeed.connected.set(false);
    pushStatus();

    expect(screen.getByText("Le Mans")).toBeTruthy();
    expect(screen.queryByText(/no server/i)).toBeNull();
  });

  it("does not re-fetch settings on every frame (#98)", async () => {
    render(SessionBar);
    pushStatus();

    expect(await screen.findByText("Le Mans")).toBeTruthy();
    expect(vi.mocked(getSettings).mock.calls.length).toBe(1); // the one-shot mount fetch

    for (let i = 0; i < 20; i++) pushStatus({ lap_id: i });
    expect(await screen.findByText("Le Mans")).toBeTruthy();

    expect(vi.mocked(getSettings).mock.calls.length).toBe(1);
  });

  it("re-fetches settings when a save announces itself", async () => {
    render(SessionBar);
    pushStatus();
    expect(await screen.findByText("metric")).toBeTruthy();

    vi.mocked(getSettings).mockResolvedValue({ ...SETTINGS, units: "imperial" } as never);
    window.dispatchEvent(new Event("settings-changed"));

    expect(await screen.findByText("imperial")).toBeTruthy();
  });

  it("unsubscribes from the feed on unmount", async () => {
    const before = eventFeed.status.subscriberCount;
    const { unmount } = render(SessionBar);
    pushStatus();
    await screen.findByText("Le Mans");
    expect(eventFeed.status.subscriberCount).toBeGreaterThan(before);

    unmount();

    expect(eventFeed.status.subscriberCount).toBe(before);
    expect(eventFeed.connected.subscriberCount).toBe(0);
  });
});
