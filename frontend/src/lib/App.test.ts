import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup, within, waitFor } from "@testing-library/svelte";
import {
  getHealth,
  getStatus,
  getSettings,
  getSessions,
  getSessionsSummary,
  getSessionLaps,
  getLapAnalysis,
  getLapInputs,
  getSetup,
  getChats,
  getSessionChats,
  getChatMessages,
  getChatUsage,
  getCurrentMap,
  getSessionOptimal,
} from "./api";
import { eventFeed } from "./events";
import App from "./App.svelte";

const LIVE_STATUS = {
  track: "Le Mans",
  car: "Corvette Z06",
  on_track: false,
  activity: null,
  has_map: true,
  lmu_connected: true,
  session_id: "s1",
  lap_id: 5,
};

// App starts the shared live feed on mount; stub it so tests don't open a real
// WebSocket (keep the module's other exports — CLASS_COLOR etc. — intact).
vi.mock("./live", async () => {
  const actual = await vi.importActual<typeof import("./live")>("./live");
  return {
    ...actual,
    liveField: { start() {}, stop() {}, subscribe: () => () => {}, snapshot: null },
  };
});

vi.mock("./api", async () => {
  const actual = await vi.importActual<typeof import("./api")>("./api");
  return {
    ...actual,
    getHealth: vi.fn(),
    getStatus: vi.fn(),
    getSettings: vi.fn(),
    getSessions: vi.fn(),
    getSessionsSummary: vi.fn(),
    getSessionLaps: vi.fn(),
    getSessionOptimal: vi.fn(),
    getLapAnalysis: vi.fn(),
    getLapInputs: vi.fn(),
    getSetup: vi.fn(),
    getChats: vi.fn(),
    getSessionChats: vi.fn(),
    getChatMessages: vi.fn(),
    getChatUsage: vi.fn(),
    getCurrentMap: vi.fn(),
  };
});

describe("App shell", () => {
  afterEach(cleanup);

  // CornerSetupTabs (issue #36 fix) keeps the Setup panel mounted once
  // opened, but mounts it lazily on first visit — these tests never touch
  // the Setup tab, so getSetup() isn't actually called; mock stays as a
  // defensive default in case a future test opens it.
  beforeEach(() => {
    vi.mocked(getSetup).mockResolvedValue({});
  });

  // The shell mounts after the splash finishes its boot (health + prefetch),
  // so these await the shell rather than querying synchronously.
  it("renders the nav items and a theme toggle — and no Pit Wall (#151)", async () => {
    const { container } = render(App);
    await screen.findByRole("navigation"); // shell mounts past the splash
    const nav = within(container.querySelector(".nav") as HTMLElement);

    expect(nav.getByText("Coach")).toBeTruthy();
    expect(nav.getByText("Sessions")).toBeTruthy();
    expect(nav.getByText("Tracks")).toBeTruthy();
    expect(nav.getByText("Settings")).toBeTruthy();
    expect(nav.getByText(/^Theme ·/)).toBeTruthy();
    expect(nav.queryByText("Pit Wall")).toBeNull();
    // Coach is the landing screen and still mounts.
    expect(nav.getByText("Coach").closest(".nav-item")?.classList.contains("on")).toBe(true);
  });

  it("has a draggable resizer for the lap pane", async () => {
    const { container } = render(App);
    await waitFor(() => expect(container.querySelector(".rail-resizer")).not.toBeNull());
    const resizer = container.querySelector(".rail-resizer");
    expect(resizer!.getAttribute("role")).toBe("separator");
  });
});

describe("Coach screen assembly", () => {
  beforeEach(() => {
    localStorage.removeItem("coachRail.expanded");
    // The screens read live state off the /events feed (#106), not getStatus.
    // Seeding the frame before render means every subscriber gets it on replay.
    eventFeed.push("status", LIVE_STATUS);
    vi.mocked(getStatus).mockResolvedValue(LIVE_STATUS);
    vi.mocked(getSettings).mockResolvedValue({
      units: "metric",
      input_device: "wheel",
      model: "sonnet",
      corner_naming: "names",
      driver: { braking_style: null, balance_preference: null, risk_tolerance: null },
      voice: {
        enabled: false, ptt_key: "F9", ptt_wheel: null, ptt_gamepad: null, ptt_wheel_name: null, ptt_gamepad_name: null, tts_voice: null, tts_rate: 1.0,
        tts_engine: "windows", kokoro_voice: "af_heart",
        vad_threshold: 0.02, vad_silence_ms: 1000, vad_max_s: 20, conversation_idle_s: 30, mic_device_id: null,
      },
      api_key_set: true,
      data_dir: "x",
      app_version: "0.1",
      lmu_build: null,
      lmu_dir: null,
      lmu_player_names: [],
    });
    vi.mocked(getHealth).mockResolvedValue({ status: "ok" });
    vi.mocked(getSessions).mockResolvedValue({
      rows: [
        { session_id: "s1", track: "Le Mans", car: "Corvette Z06", laps: 1, best: 216.0, started: 1, last: 2 },
      ],
      total: 1,
      tracks: ["Le Mans"],
      cars: ["Corvette Z06"],
    });
    vi.mocked(getSessionsSummary).mockResolvedValue({
      count: 1,
      cars: 1,
      personal_best: 216.0,
      consistency: null,
      improvement: null,
      series: [],
    });
    vi.mocked(getSessionLaps).mockResolvedValue([{ id: 9, lap_time: 216.0, valid: 1, ts: 100 }]);
    vi.mocked(getSessionOptimal).mockResolvedValue({ optimal: null });
    vi.mocked(getLapAnalysis).mockResolvedValue({ lap_time: 216.0, corners: [] });
    vi.mocked(getLapInputs).mockResolvedValue({ lap: {}, best: null, corners: [] });
    vi.mocked(getSetup).mockResolvedValue({});
    vi.mocked(getChats).mockResolvedValue([]);
    vi.mocked(getSessionChats).mockResolvedValue([]);
  });

  afterEach(cleanup);

  it("renders the session bar, lap list, corner/setup tabs, and chat composer together", async () => {
    const { container } = render(App);
    await screen.findByRole("navigation"); // wait for the shell to mount past the splash
    const bar = container.querySelector(".bar") as HTMLElement;

    expect(await within(bar).findByText("Le Mans")).toBeTruthy(); // session bar
    expect(await screen.findByText("Lap 1")).toBeTruthy(); // lap list
    expect(screen.getByText("Corners")).toBeTruthy(); // tabs
    expect(screen.getByText("Setup")).toBeTruthy();
    expect(screen.getByPlaceholderText(/ask your engineer/i)).toBeTruthy(); // chat composer
    expect(await screen.findByText(/inputs.*lap vs best/i)).toBeTruthy(); // inputs panel (#95)
  });

  it("#106: nothing polls once the shell has booted", async () => {
    render(App);
    await screen.findByRole("navigation"); // shell mounts past the splash
    await screen.findByText("Lap 1"); // everything has loaded its first data

    // A quiet window longer than the 2 s period the status/map pollers ran at.
    // (LapList's 5 s poll is pinned in LapList.test.ts instead: it called
    // getStatus on mount, so "never called at all" catches it there with no wait.
    // getStatus can't be asserted here — Splash and Settings call it one-shot.)
    vi.mocked(getStatus).mockClear();
    vi.mocked(getSessionLaps).mockClear();
    vi.mocked(getCurrentMap).mockClear();
    await new Promise((r) => setTimeout(r, 2600));

    expect(vi.mocked(getStatus)).not.toHaveBeenCalled();
    expect(vi.mocked(getSessionLaps)).not.toHaveBeenCalled();
    expect(vi.mocked(getCurrentMap)).not.toHaveBeenCalled();
  });

  it("dragging the chat bar resizes the Inputs panel (#95 round 2)", async () => {
    const flat = (v: number[]) => ({ lo: [...v], hi: [...v], mean: [...v] });
    vi.mocked(getLapInputs).mockResolvedValue({
      lap: {
        lap_id: 9, lap_time: 216.0, length: 1000, dist: [0, 500, 1000],
        throttle: flat([1, 0, 1]), brake: flat([0, 1, 0]), steering: flat([0, 0, 0]),
      },
      best: null,
      corners: [],
    });
    const { container } = render(App);
    await screen.findByRole("navigation"); // shell mounts past the splash

    await fireEvent.click(await screen.findByText("Lap 1")); // selects lap 9, feeds InputsPanel real channels
    const panel = (await screen.findByTestId("inputs-panel-body")).closest(".panel") as HTMLElement;
    await waitFor(() => expect(panel.style.getPropertyValue("--inputs-h")).toBe("220px"));

    const bar = container.querySelector(".chat-bar") as HTMLElement;
    await fireEvent.pointerDown(bar, { clientY: 300 });
    await fireEvent.pointerMove(bar, { clientY: 360 });

    expect(panel.style.getPropertyValue("--inputs-h")).toBe("280px");
  });

  it("flows a selected lap's id into the corner panel and highlights the row", async () => {
    render(App);
    await screen.findByRole("navigation"); // shell mounts past the splash

    const lapRow = await screen.findByText("Lap 1");
    const cell = lapRow.closest(".lap") as HTMLElement; // grid container keeps best/selected
    const selectBtn = lapRow.closest(".lap-select") as HTMLElement; // left hit zone (real button)
    expect(selectBtn.getAttribute("aria-pressed")).toBe("false"); // not selected yet

    await fireEvent.click(lapRow);

    // The clicked lap's real id (9) flows into the corner analysis...
    expect(vi.mocked(getLapAnalysis)).toHaveBeenCalledWith(9);
    // ...and the row is highlighted as selected (no "Lap N selected" hint text).
    await vi.waitFor(() => expect(selectBtn.getAttribute("aria-pressed")).toBe("true"));
    expect(cell.className).toContain("selected");
  });

  it("switches to the Sessions screen when the Sessions nav item is clicked", async () => {
    const { container } = render(App);
    await screen.findByRole("navigation"); // shell mounts past the splash
    const nav = within(container.querySelector(".nav") as HTMLElement);

    await fireEvent.click(nav.getByText("Sessions"));

    expect(await screen.findByRole("heading", { name: "Sessions" })).toBeTruthy();
  });

  it("switches to the Settings screen when the Settings nav item is clicked", async () => {
    const { container } = render(App);
    await screen.findByRole("navigation"); // shell mounts past the splash
    const nav = within(container.querySelector(".nav") as HTMLElement);

    await fireEvent.click(nav.getByText("Settings"));

    expect(await screen.findByText("Display")).toBeTruthy();
  });

  it("switches to the Tracks screen when the Tracks nav item is clicked", async () => {
    const { container } = render(App);
    await screen.findByRole("navigation"); // shell mounts past the splash
    const nav = within(container.querySelector(".nav") as HTMLElement);

    await fireEvent.click(nav.getByText("Tracks"));

    expect(await screen.findByRole("heading", { name: "Tracks" })).toBeTruthy();
  });

  it("deep-links a clicked session row into the Coach screen and loads that session's laps", async () => {
    const { container } = render(App);
    await screen.findByRole("navigation"); // shell mounts past the splash
    const nav = within(container.querySelector(".nav") as HTMLElement);

    await fireEvent.click(nav.getByText("Sessions"));
    const sessionRow = await screen.findByRole("button", { name: /Le Mans/i });
    await fireEvent.click(sessionRow);

    // Back on the Coach screen, with laps loaded for the deep-linked session.
    // (selection resolves the linked chat first, so the switch is async.)
    await waitFor(() =>
      expect(nav.getByText("Coach").closest(".nav-item")?.classList.contains("on")).toBe(true),
    );
    expect(await screen.findByText("Lap 1")).toBeTruthy();
    expect(vi.mocked(getSessionLaps)).toHaveBeenCalledWith("s1");
    expect(vi.mocked(getSessionChats)).toHaveBeenCalledWith("s1");
  });

  it("#109/#167: a new live session unpins the lap list to follow it but keeps the chat; New session clears it", async () => {
    // #167: the chat used to reset here too -- an online server that read offline
    // between races flipped the live session id and wiped the driver's thread.
    vi.mocked(getSessionChats).mockResolvedValue([
      { chat_id: "c1", started: 1, last: 2, message_count: 1, tracks: "Le Mans" },
    ]);
    vi.mocked(getChatMessages).mockResolvedValue([
      { ts: 1, session_id: "s1", track: "Le Mans", car: "Corvette Z06", lap_id: 9,
        question: "how was race 1?", answer: "Race 1 went well.", tool_calls: null },
    ]);
    vi.mocked(getChatUsage).mockResolvedValue({
      usage: { input: 0, output: 0, cache_read: 0, cache_write: 0, total: 0, messages: 1 },
      cost: 0, cost_complete: true,
    } as never);
    const { container } = render(App);
    await screen.findByRole("navigation"); // shell mounts past the splash
    const nav = within(container.querySelector(".nav") as HTMLElement);

    await fireEvent.click(nav.getByText("Sessions"));
    const sessionRow = await screen.findByRole("button", { name: /Le Mans/i });
    await fireEvent.click(sessionRow);

    // Pinned on the deep-linked session s1, with its chat open.
    await waitFor(() =>
      expect(nav.getByText("Coach").closest(".nav-item")?.classList.contains("on")).toBe(true),
    );
    expect(await screen.findByText("Lap 1")).toBeTruthy();
    expect(vi.mocked(getSessionLaps)).toHaveBeenCalledWith("s1");
    expect(await screen.findByText("Race 1 went well.")).toBeTruthy();

    // A new live session starts (s2), with a laps list distinguishable from s1's.
    vi.mocked(getSessionLaps).mockImplementation(async (sid: string) =>
      sid === "s2"
        ? [{ id: 20, lap_time: 200.0, valid: 1, ts: 500 }]
        : [{ id: 9, lap_time: 216.0, valid: 1, ts: 100 }],
    );
    eventFeed.push("status", { ...LIVE_STATUS, on_track: true, session_id: "s2", lap_id: 7 });

    // The lap list followed the new live session...
    await screen.findByText("3:20.000");
    expect(vi.mocked(getSessionLaps)).toHaveBeenCalledWith("s2");
    // ...but the chat thread is still there (#167).
    expect(screen.getByText("Race 1 went well.")).toBeTruthy();
    expect(screen.queryByText(/new chat — ask a question/i)).toBeNull();

    // The explicit New session button still starts an empty chat.
    await fireEvent.click(screen.getByRole("button", { name: /new session/i }));
    expect(await screen.findByText(/new chat — ask a question/i)).toBeTruthy();
    expect(screen.queryByText("Race 1 went well.")).toBeNull();
  });

  it("#151: expanding the rail map widens the rail, never overlays the chat, and is remembered", async () => {
    const { container } = render(App);
    await screen.findByRole("navigation"); // shell mounts past the splash
    const main = container.querySelector(".main") as HTMLElement;
    const rail = container.querySelector("aside.rail") as HTMLElement;
    expect(main.classList.contains("rail-wide")).toBe(false);
    expect(rail.style.getPropertyValue("--map-h")).toBe("200px");

    await fireEvent.click(await screen.findByLabelText("Expand track map"));
    expect(main.classList.contains("rail-wide")).toBe(true);
    expect(rail.style.getPropertyValue("--map-h")).toBe(`${Math.round(window.innerHeight * 0.5)}px`);
    // The chat column is still the rail's grid sibling (pushed narrower by CSS), and
    // nothing is layered over it: no dialog, no overlay.
    const chatCol = container.querySelector(".coach-main") as HTMLElement;
    expect(chatCol.parentElement).toBe(main);
    expect(within(chatCol).getByPlaceholderText(/ask your engineer/i)).toBeTruthy();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(localStorage.getItem("coachRail.expanded")).toBe("true");

    await fireEvent.click(screen.getByLabelText("Collapse track map"));
    expect(main.classList.contains("rail-wide")).toBe(false);
    expect(rail.style.getPropertyValue("--map-h")).toBe("200px");
    expect(localStorage.getItem("coachRail.expanded")).toBe("false");
  });

  it("#151: the expanded rail is restored for this viewer", async () => {
    localStorage.setItem("coachRail.expanded", "true");
    const { container } = render(App);
    await screen.findByRole("navigation");
    expect((container.querySelector(".main") as HTMLElement).classList.contains("rail-wide")).toBe(true);
    expect(await screen.findByLabelText("Collapse track map")).toBeTruthy();
  });

  it("#151: with storage blocked the rail still renders and expands (just not remembered)", async () => {
    const get = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    const set = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    try {
      const { container } = render(App);
      await screen.findByRole("navigation");
      const main = container.querySelector(".main") as HTMLElement;
      expect(main.classList.contains("rail-wide")).toBe(false);
      await fireEvent.click(await screen.findByLabelText("Expand track map"));
      expect(main.classList.contains("rail-wide")).toBe(true);
    } finally {
      get.mockRestore();
      set.mockRestore();
    }
  });
});
