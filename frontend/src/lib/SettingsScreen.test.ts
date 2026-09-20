import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup, waitFor, within } from "@testing-library/svelte";
import { isTauri, captureNext, listDevices, pushBinds } from "./voice-shell";
import {
  getSettings,
  putSettings,
  setApiKey,
  testApiKey,
  clearData,
  ingestHistory,
  getStatus,
  getProfile,
  putProfileSummary,
  addMemory,
  updateMemory,
  distillProfile,
  voiceStatus,
  voiceSetup,
  kokoroStatus,
  kokoroSetup,
  ttsCacheStats,
  ttsCacheClear,
  speak,
  type SettingsResponse,
  type DriverProfileData,
  type StatusResponse,
} from "./api";
import { applyTheme } from "./theme";
import { eventFeed } from "./events";
import SettingsScreen from "./SettingsScreen.svelte";

vi.mock("./api", async () => {
  const actual = await vi.importActual<typeof import("./api")>("./api");
  return {
    ...actual,
    getSettings: vi.fn(),
    putSettings: vi.fn(),
    setApiKey: vi.fn(),
    testApiKey: vi.fn(),
    clearData: vi.fn(),
    ingestHistory: vi.fn(),
    getStatus: vi.fn(),
    getProfile: vi.fn(),
    putProfileSummary: vi.fn(),
    addMemory: vi.fn(),
    updateMemory: vi.fn(),
    distillProfile: vi.fn(),
    voiceStatus: vi.fn(),
    voiceSetup: vi.fn(),
    kokoroStatus: vi.fn(),
    kokoroSetup: vi.fn(),
    ttsCacheStats: vi.fn(),
    ttsCacheClear: vi.fn(),
    speak: vi.fn(),
  };
});

vi.mock("./theme", async () => {
  const actual = await vi.importActual<typeof import("./theme")>("./theme");
  return { ...actual, applyTheme: vi.fn() };
});

vi.mock("./voice-shell", async () => {
  const actual = await vi.importActual<typeof import("./voice-shell")>("./voice-shell");
  return {
    ...actual,
    isTauri: vi.fn(() => false),
    captureNext: vi.fn(),
    pushBinds: vi.fn(),
    attachShell: vi.fn(),
    listDevices: vi.fn(() => Promise.resolve([])),
  };
});

function baseSettings(overrides: Partial<SettingsResponse> = {}): SettingsResponse {
  return {
    units: "metric",
    input_device: "wheel",
    model: "sonnet",
    corner_naming: "names",
    show_cost: true,
    log_level: "INFO",
    driver: { braking_style: null, balance_preference: null, risk_tolerance: null },
    voice: {
      enabled: false, ptt_key: "F9", ptt_wheel: null, ptt_gamepad: null, ptt_wheel_name: null, ptt_gamepad_name: null, tts_voice: null, tts_rate: 1.0,
      tts_engine: "windows", kokoro_voice: "af_heart", ptt_focus_gate: true,
      vad_threshold: 0.02, vad_silence_ms: 1000, vad_max_s: 20, conversation_idle_s: 30, mic_device_id: null,
    },
    api_key_set: false,
    data_dir: "C:/data",
    app_version: "0.1.0",
    lmu_build: "1.0",
    lmu_dir: null,
    lmu_player_names: [],
    ...overrides,
  };
}

function baseStatus(overrides: Partial<StatusResponse> = {}): StatusResponse {
  return {
    track: null,
    car: null,
    on_track: false,
    activity: null,
    has_map: false,
    lmu_connected: false,
    session_id: "sess-1",
    lap_id: null,
    ...overrides,
  };
}

function baseProfile(overrides: Partial<DriverProfileData> = {}): DriverProfileData {
  return {
    prefs: { braking_style: null, balance_preference: null, risk_tolerance: null },
    summary: { text: "", status: "active" },
    memories: { active: [], pending: [] },
    ...overrides,
  };
}

describe("SettingsScreen", () => {
  beforeEach(() => {
    vi.mocked(getSettings).mockReset();
    vi.mocked(putSettings).mockReset();
    vi.mocked(setApiKey).mockReset();
    vi.mocked(testApiKey).mockReset();
    vi.mocked(clearData).mockReset();
    vi.mocked(ingestHistory).mockReset();
    vi.mocked(applyTheme).mockReset();
    vi.mocked(getStatus).mockReset().mockResolvedValue(baseStatus());
    vi.mocked(getProfile).mockReset().mockResolvedValue(baseProfile());
    vi.mocked(putProfileSummary).mockReset();
    vi.mocked(addMemory).mockReset();
    vi.mocked(updateMemory).mockReset();
    vi.mocked(distillProfile).mockReset();
    vi.mocked(voiceStatus).mockReset();
    vi.mocked(voiceSetup).mockReset();
    vi.mocked(kokoroStatus).mockReset();
    vi.mocked(kokoroSetup).mockReset();
    vi.mocked(ttsCacheStats).mockReset().mockResolvedValue({ files: 0, bytes: 0 });
    vi.mocked(ttsCacheClear).mockReset();
    vi.mocked(speak).mockReset();
    vi.mocked(isTauri).mockReset().mockReturnValue(false);
    vi.mocked(captureNext).mockReset();
    vi.mocked(pushBinds).mockReset();
    vi.mocked(listDevices).mockReset().mockResolvedValue([]);
    localStorage.clear();
    delete (navigator as { mediaDevices?: unknown }).mediaDevices; // set per-test (#104 mic select)
  });

  afterEach(cleanup);

  it("renders current settings including api_key_set state", async () => {
    vi.mocked(getSettings).mockResolvedValue(baseSettings({ api_key_set: true }));

    render(SettingsScreen);

    expect(await screen.findAllByText(/set ✓/)).not.toHaveLength(0);
    expect(screen.getByText("0.1.0")).toBeTruthy();
    expect(screen.getByText("C:/data")).toBeTruthy();
  });

  it("changing units calls putSettings with units updated", async () => {
    vi.mocked(getSettings).mockResolvedValue(baseSettings({ units: "metric" }));
    vi.mocked(putSettings).mockResolvedValue(baseSettings({ units: "imperial" }));

    render(SettingsScreen);

    const select = await screen.findByDisplayValue("Metric");
    await fireEvent.change(select, { target: { value: "imperial" } });

    expect(putSettings).toHaveBeenCalledWith(
      expect.objectContaining({ units: "imperial" })
    );
  });

  it("switching to Verbose calls putSettings with log_level DEBUG", async () => {
    vi.mocked(getSettings).mockResolvedValue(baseSettings({ log_level: "INFO" }));
    vi.mocked(putSettings).mockResolvedValue(baseSettings({ log_level: "DEBUG" }));

    render(SettingsScreen);

    const select = await screen.findByDisplayValue("Normal");
    await fireEvent.change(select, { target: { value: "DEBUG" } });

    expect(putSettings).toHaveBeenCalledWith(
      expect.objectContaining({ log_level: "DEBUG" })
    );
  });

  it("renders the cost-estimate toggle, on by default", async () => {
    vi.mocked(getSettings).mockResolvedValue(baseSettings());

    render(SettingsScreen);

    const toggle = await screen.findByLabelText<HTMLInputElement>(/cost estimate/i);
    expect(toggle.checked).toBe(true);
  });

  it("turning the cost estimate off calls putSettings with show_cost false", async () => {
    vi.mocked(getSettings).mockResolvedValue(baseSettings({ show_cost: true }));
    vi.mocked(putSettings).mockResolvedValue(baseSettings({ show_cost: false }));

    render(SettingsScreen);

    const toggle = await screen.findByLabelText(/cost estimate/i);
    await fireEvent.click(toggle);

    expect(putSettings).toHaveBeenCalledWith(expect.objectContaining({ show_cost: false }));
  });

  it("entering a key and clicking Save calls setApiKey", async () => {
    vi.mocked(getSettings).mockResolvedValue(baseSettings());
    vi.mocked(setApiKey).mockResolvedValue({ api_key_set: true });

    render(SettingsScreen);

    const input = await screen.findByLabelText("API key");
    await fireEvent.input(input, { target: { value: "sk-ant-secret" } });
    await fireEvent.click(screen.getByRole("button", { name: /save key/i }));

    expect(setApiKey).toHaveBeenCalledWith("sk-ant-secret");
    expect(await screen.findByText(/key saved/i)).toBeTruthy();
  });

  it("clicking Test key shows the result", async () => {
    vi.mocked(getSettings).mockResolvedValue(baseSettings({ api_key_set: true }));
    vi.mocked(testApiKey).mockResolvedValue({ ok: true, error: null });

    render(SettingsScreen);

    await fireEvent.click(await screen.findByRole("button", { name: /test key/i }));

    expect(await screen.findByText(/✓ valid/)).toBeTruthy();
  });

  it("Import calls ingestHistory and shows the summary", async () => {
    vi.mocked(getSettings).mockResolvedValue(baseSettings());
    vi.mocked(ingestHistory).mockResolvedValue({
      scanned: 10,
      imported: 7,
      skipped: 1,
      unmatched: 2,
      errors: 0,
    });

    render(SettingsScreen);

    await fireEvent.click(await screen.findByRole("button", { name: /import history/i }));

    expect(ingestHistory).toHaveBeenCalled();
    expect(await screen.findByText(/imported 7 sessions, 2 unmatched/i)).toBeTruthy();
  });

  it("without Tauri, no Browse button and the LMU dir input is editable", async () => {
    vi.mocked(getSettings).mockResolvedValue(baseSettings({ lmu_dir: "C:/lmu" }));

    render(SettingsScreen);

    const input = (await screen.findByDisplayValue("C:/lmu")) as HTMLInputElement;
    expect(screen.queryByRole("button", { name: /browse/i })).toBeNull();

    await fireEvent.input(input, { target: { value: "C:/other" } });
    expect(input.value).toBe("C:/other");
  });

  it("Reset confirms before calling clearData", async () => {
    vi.mocked(getSettings).mockResolvedValue(baseSettings());
    vi.mocked(clearData).mockResolvedValue({ cleared: true });

    render(SettingsScreen);

    // The sidecar's poller re-imports LMU result logs after a reset (#81); say so up front.
    expect(await screen.findByText(/result logs are re-imported automatically/i)).toBeTruthy();
    await fireEvent.click(await screen.findByRole("button", { name: /reset \/ clear data/i }));
    expect(clearData).not.toHaveBeenCalled();

    await fireEvent.click(await screen.findByRole("button", { name: /confirm/i }));

    expect(clearData).toHaveBeenCalled();
    expect(await screen.findByText(/data cleared/i)).toBeTruthy();
  });

  it("has no standalone Driver profile section (folded into Driver Memories)", async () => {
    vi.mocked(getSettings).mockResolvedValue(baseSettings());

    render(SettingsScreen);

    await screen.findByText("Driver Memories");
    expect(screen.queryByText("Driver profile")).toBeNull();
  });

  it("renders active memories and the pending review queue", async () => {
    vi.mocked(getSettings).mockResolvedValue(baseSettings());
    vi.mocked(getProfile).mockResolvedValue(
      baseProfile({
        summary: { text: "Trail-brakes hard, working on exit throttle.", status: "active" },
        memories: {
          active: [
            {
              id: 1,
              text: "Brakes early into slow hairpins",
              status: "active",
              track: "Imola",
              car: null,
              source: "coach_tool",
              session_id: null,
              created_ts: 0,
              updated_ts: 0,
            },
          ],
          pending: [
            {
              id: 2,
              text: "Consistently understeers mid-corner in the rain",
              status: "pending",
              track: null,
              car: null,
              source: "distill",
              session_id: "sess-1",
              created_ts: 0,
              updated_ts: 0,
            },
          ],
        },
      }),
    );

    render(SettingsScreen);

    expect(await screen.findByText("Brakes early into slow hairpins")).toBeTruthy();
    expect(await screen.findByText("Consistently understeers mid-corner in the rain")).toBeTruthy();
    expect(screen.getByText(/pending review/i)).toBeTruthy();
  });

  it("approving a pending memory calls updateMemory with status active", async () => {
    vi.mocked(getSettings).mockResolvedValue(baseSettings());
    vi.mocked(getProfile).mockResolvedValue(
      baseProfile({
        memories: {
          active: [],
          pending: [
            {
              id: 7,
              text: "Late on throttle after the chicane",
              status: "pending",
              track: null,
              car: null,
              source: "coach_tool",
              session_id: null,
              created_ts: 0,
              updated_ts: 0,
            },
          ],
        },
      }),
    );
    vi.mocked(updateMemory).mockResolvedValue({ ok: true });

    render(SettingsScreen);

    await screen.findByText("Late on throttle after the chicane");
    await fireEvent.click(screen.getByRole("button", { name: /approve/i }));

    expect(updateMemory).toHaveBeenCalledWith(7, { status: "active" });
  });

  it("rejecting a pending memory retires it", async () => {
    vi.mocked(getSettings).mockResolvedValue(baseSettings());
    vi.mocked(getProfile).mockResolvedValue(
      baseProfile({
        memories: {
          active: [],
          pending: [
            {
              id: 9,
              text: "Overdrives cold tires on out-laps",
              status: "pending",
              track: null,
              car: null,
              source: "distill",
              session_id: null,
              created_ts: 0,
              updated_ts: 0,
            },
          ],
        },
      }),
    );
    vi.mocked(updateMemory).mockResolvedValue({ ok: true });

    render(SettingsScreen);

    await screen.findByText("Overdrives cold tires on out-laps");
    await fireEvent.click(screen.getByRole("button", { name: /reject/i }));

    expect(updateMemory).toHaveBeenCalledWith(9, { status: "retired" });
  });

  it("deleting an active memory retires it", async () => {
    vi.mocked(getSettings).mockResolvedValue(baseSettings());
    vi.mocked(getProfile).mockResolvedValue(
      baseProfile({
        memories: {
          active: [
            {
              id: 3,
              text: "Prefers a stiffer front ARB",
              status: "active",
              track: null,
              car: null,
              source: "manual",
              session_id: null,
              created_ts: 0,
              updated_ts: 0,
            },
          ],
          pending: [],
        },
      }),
    );
    vi.mocked(updateMemory).mockResolvedValue({ ok: true });

    render(SettingsScreen);

    await screen.findByText("Prefers a stiffer front ARB");
    await fireEvent.click(screen.getByRole("button", { name: /delete/i }));

    expect(updateMemory).toHaveBeenCalledWith(3, { status: "retired" });
  });

  it("adding a memory calls addMemory with the entered text", async () => {
    vi.mocked(getSettings).mockResolvedValue(baseSettings());
    vi.mocked(addMemory).mockResolvedValue({
      id: 5,
      text: "Runs a wider entry line at fast sweepers",
      status: "active",
      track: null,
      car: null,
    });

    render(SettingsScreen);

    await screen.findByText("Driver Memories");
    const input = screen.getByPlaceholderText(/add a memory/i);
    await fireEvent.input(input, { target: { value: "Runs a wider entry line at fast sweepers" } });
    await fireEvent.click(screen.getByRole("button", { name: /\+ add memory/i }));

    expect(addMemory).toHaveBeenCalledWith(
      expect.objectContaining({ text: "Runs a wider entry line at fast sweepers" }),
    );
  });

  it("shows a proposed summary for approval when the distill produced a pending one", async () => {
    vi.mocked(getSettings).mockResolvedValue(baseSettings());
    vi.mocked(getProfile).mockResolvedValue(
      baseProfile({ summary: { text: "New candidate summary text.", status: "pending" } }),
    );
    vi.mocked(putProfileSummary).mockResolvedValue({ text: "New candidate summary text.", status: "active" });

    render(SettingsScreen);

    expect(await screen.findByDisplayValue("New candidate summary text.")).toBeTruthy();
    expect(screen.getByText(/proposed/i)).toBeTruthy();

    await fireEvent.click(screen.getByRole("button", { name: /approve summary/i }));

    expect(putProfileSummary).toHaveBeenCalledWith("New candidate summary text.", "active");
  });

  it("does not auto-save a pending summary on blur -- only Approve persists it", async () => {
    vi.mocked(getSettings).mockResolvedValue(baseSettings());
    vi.mocked(getProfile).mockResolvedValue(
      baseProfile({ summary: { text: "New candidate summary text.", status: "pending" } }),
    );

    render(SettingsScreen);

    const textarea = await screen.findByDisplayValue("New candidate summary text.");
    await fireEvent.change(textarea, { target: { value: "Edited proposed text." } });

    expect(putProfileSummary).not.toHaveBeenCalled();
  });

  it("Review session learnings distills the current session and surfaces proposals", async () => {
    vi.mocked(getSettings).mockResolvedValue(baseSettings());
    vi.mocked(getStatus).mockResolvedValue(baseStatus({ session_id: "sess-42" }));
    vi.mocked(distillProfile).mockResolvedValue({
      memories: [
        { id: 11, text: "Braking earlier lap over lap in T4", status: "pending", track: null, car: null },
      ],
      summary: "Updated distilled summary.",
    });

    render(SettingsScreen);

    await screen.findByText("Driver Memories");
    await fireEvent.click(screen.getByRole("button", { name: /review session learnings/i }));

    expect(distillProfile).toHaveBeenCalledWith("sess-42");
    await waitFor(() =>
      expect(screen.getByText("Braking earlier lap over lap in T4")).toBeTruthy(),
    );
  });

  // --- Settings -> Voice (#104) ---

  const voiceDefaults = {
    enabled: false, ptt_key: "F9", ptt_wheel: null, ptt_gamepad: null, ptt_wheel_name: null, ptt_gamepad_name: null, tts_voice: null, tts_rate: 1,
    tts_engine: "windows" as const, kokoro_voice: "af_heart", ptt_focus_gate: true,
    vad_threshold: 0.02, vad_silence_ms: 1000, vad_max_s: 20, conversation_idle_s: 30, mic_device_id: null,
  };

  describe("Settings -> Voice", () => {
    it("enabling voice starts the model download and shows progress until ready", async () => {
      vi.mocked(getSettings).mockResolvedValue(baseSettings({ voice: voiceDefaults }));
      vi.mocked(putSettings).mockImplementation(async (cfg) => baseSettings(cfg as Partial<SettingsResponse>));
      vi.mocked(voiceStatus)
        .mockResolvedValue({ installed: false, installing: false, progress_bytes: 0, total_bytes: 100, error: null });
      vi.mocked(voiceSetup).mockResolvedValue({ started: true, installed: false, installing: true, progress_bytes: 0, total_bytes: 100, error: null });

      render(SettingsScreen);
      const enable = await screen.findByLabelText("Enable voice");
      await fireEvent.click(enable);

      const dialog = await screen.findByRole("dialog");
      const download = within(dialog).getByRole("button", { name: "Download" });
      await fireEvent.click(download);

      await waitFor(() => expect(voiceSetup).toHaveBeenCalled());
      expect(vi.mocked(putSettings).mock.calls.at(-1)![0]).toMatchObject({ voice: { enabled: true } });

      // Progress arrives on the /events feed (#106), not a 1 s /voice/status poll.
      eventFeed.push("voice_setup", { installed: false, installing: true, progress_bytes: 50, total_bytes: 100, error: null });
      expect(await screen.findByText("50%")).toBeTruthy();

      eventFeed.push("voice_setup", { installed: true, installing: false, progress_bytes: 100, total_bytes: 100, error: null });
      await waitFor(() => expect(screen.getByText(/Ready/)).toBeTruthy());

      // One /voice/status call only: the "is it already installed?" check on enable.
      expect(vi.mocked(voiceStatus).mock.calls.length).toBe(1);
    });

    it("a download that fails reports the feed's error in the dialog", async () => {
      vi.mocked(getSettings).mockResolvedValue(baseSettings({ voice: voiceDefaults }));
      vi.mocked(putSettings).mockImplementation(async (cfg) => baseSettings(cfg as Partial<SettingsResponse>));
      vi.mocked(voiceStatus).mockResolvedValue({ installed: false, installing: false, progress_bytes: 0, total_bytes: 100, error: null });
      vi.mocked(voiceSetup).mockResolvedValue({ started: true, installed: false, installing: true, progress_bytes: 0, total_bytes: 100, error: null });

      render(SettingsScreen);
      await fireEvent.click(await screen.findByLabelText("Enable voice"));
      const dialog = await screen.findByRole("dialog");
      await fireEvent.click(within(dialog).getByRole("button", { name: "Download" }));
      await waitFor(() => expect(voiceSetup).toHaveBeenCalled());

      eventFeed.push("voice_setup", { installed: false, installing: false, progress_bytes: 10, total_bytes: 100, error: "disk full" });

      expect(await screen.findByText("disk full")).toBeTruthy();
      expect(await screen.findByRole("button", { name: "Retry" })).toBeTruthy();
    });

    it("Not now reverts enable", async () => {
      vi.mocked(getSettings).mockResolvedValue(baseSettings({ voice: voiceDefaults }));
      vi.mocked(putSettings).mockImplementation(async (cfg) => baseSettings(cfg as Partial<SettingsResponse>));
      vi.mocked(voiceStatus).mockResolvedValue({ installed: false, installing: false, progress_bytes: 0, total_bytes: 100, error: null });

      render(SettingsScreen);
      const enable = await screen.findByLabelText("Enable voice");
      await fireEvent.click(enable);

      const dialog = await screen.findByRole("dialog");
      const notNow = within(dialog).getByRole("button", { name: "Not now" });
      await fireEvent.click(notNow);

      await waitFor(() =>
        expect(vi.mocked(putSettings).mock.calls.at(-1)![0]).toMatchObject({ voice: { enabled: false } }),
      );
      expect(voiceSetup).not.toHaveBeenCalled();
    });

    it("capture sets the PTT key from the next keydown", async () => {
      vi.mocked(getSettings).mockResolvedValue(baseSettings({ voice: voiceDefaults }));
      vi.mocked(voiceStatus).mockResolvedValue({ installed: true, installing: false, progress_bytes: 0, total_bytes: null, error: null });
      vi.mocked(putSettings).mockImplementation(async (cfg) =>
        baseSettings({ voice: { ...voiceDefaults, ...(cfg.voice as object) } }),
      );

      render(SettingsScreen);
      await fireEvent.click(await screen.findByRole("button", { name: "Capture" }));
      await fireEvent.keyDown(window, { code: "F7" });

      await waitFor(() =>
        expect(vi.mocked(putSettings).mock.calls.at(-1)![0]).toMatchObject({ voice: { ptt_key: "F7" } }),
      );
    });

    it("never shows a wake-word option", async () => {
      vi.mocked(getSettings).mockResolvedValue(baseSettings({ voice: voiceDefaults }));
      vi.mocked(voiceStatus).mockResolvedValue({ installed: true, installing: false, progress_bytes: 0, total_bytes: null, error: null });

      render(SettingsScreen);
      await screen.findByLabelText("Enable voice");

      expect(screen.queryByText(/wake word/i)).toBeNull();
    });

    it("never shows the wheel/gamepad PTT rows (slice 4)", async () => {
      vi.mocked(getSettings).mockResolvedValue(baseSettings({ voice: voiceDefaults }));
      vi.mocked(voiceStatus).mockResolvedValue({ installed: true, installing: false, progress_bytes: 0, total_bytes: null, error: null });

      render(SettingsScreen);
      await screen.findByLabelText("Enable voice");

      expect(screen.queryByText(/wheel/i)).toBeNull();
      expect(screen.queryByText(/gamepad/i)).toBeNull();
    });

    it("wheel/gamepad capture rows appear only under Tauri and save the captured bind", async () => {
      vi.mocked(isTauri).mockReturnValue(true);
      vi.mocked(captureNext).mockResolvedValue("winmm:0:7");
      vi.mocked(pushBinds).mockResolvedValue(null);
      vi.mocked(getSettings).mockResolvedValue(baseSettings({ voice: voiceDefaults }));
      vi.mocked(voiceStatus).mockResolvedValue({ installed: true, installing: false, progress_bytes: 0, total_bytes: null, error: null });
      vi.mocked(putSettings).mockImplementation(async (cfg) =>
        baseSettings({ voice: { ...voiceDefaults, ...(cfg.voice as object) } }),
      );

      render(SettingsScreen);
      const row = await screen.findByText("Wheel button");
      await fireEvent.click(within(row.closest(".field")!).getByRole("button", { name: "Capture" }));
      await waitFor(() =>
        expect(vi.mocked(putSettings).mock.calls.at(-1)![0]).toMatchObject({ voice: { ptt_wheel: "winmm:0:7" } }),
      );
    });

    it("shows the device name + button number for a bound wheel PTT (#104)", async () => {
      vi.mocked(isTauri).mockReturnValue(true);
      vi.mocked(listDevices).mockResolvedValue([{ device: "winmm:0", name: "Fanatec DD1" }]);
      vi.mocked(getSettings).mockResolvedValue(
        baseSettings({ voice: { ...voiceDefaults, ptt_wheel: "winmm:0:1" } }),
      );
      vi.mocked(voiceStatus).mockResolvedValue({ installed: true, installing: false, progress_bytes: 0, total_bytes: null, error: null });

      render(SettingsScreen);
      const row = await screen.findByText("Wheel button");

      await waitFor(() =>
        expect(within(row.closest(".field")!).getByText("Fanatec DD1 · button 2")).toBeTruthy(),
      );
    });

    it("capturing a gamepad bind stores the device name from listDevices (#104)", async () => {
      vi.mocked(isTauri).mockReturnValue(true);
      vi.mocked(captureNext).mockResolvedValue("winmm:1:0");
      vi.mocked(listDevices).mockResolvedValue([{ device: "winmm:1", name: "Xbox controller 2" }]);
      vi.mocked(pushBinds).mockResolvedValue(null);
      vi.mocked(getSettings).mockResolvedValue(baseSettings({ voice: voiceDefaults }));
      vi.mocked(voiceStatus).mockResolvedValue({ installed: true, installing: false, progress_bytes: 0, total_bytes: null, error: null });
      vi.mocked(putSettings).mockImplementation(async (cfg) =>
        baseSettings({ voice: { ...voiceDefaults, ...(cfg.voice as object) } }),
      );

      render(SettingsScreen);
      const row = await screen.findByText("Gamepad button");
      await fireEvent.click(within(row.closest(".field")!).getByRole("button", { name: "Capture" }));

      await waitFor(() =>
        expect(vi.mocked(putSettings).mock.calls.at(-1)![0]).toMatchObject({
          voice: { ptt_gamepad: "winmm:1:0", ptt_gamepad_name: "Xbox controller 2" },
        }),
      );
    });

    it("shows the stored name + not-connected hint when the bound device isn't currently listed (#104)", async () => {
      vi.mocked(isTauri).mockReturnValue(true);
      vi.mocked(listDevices).mockResolvedValue([]);
      vi.mocked(getSettings).mockResolvedValue(
        baseSettings({ voice: { ...voiceDefaults, ptt_gamepad: "winmm:1:0", ptt_gamepad_name: "Xbox controller 2" } }),
      );
      vi.mocked(voiceStatus).mockResolvedValue({ installed: true, installing: false, progress_bytes: 0, total_bytes: null, error: null });

      render(SettingsScreen);
      const row = await screen.findByText("Gamepad button");

      await waitFor(() =>
        expect(within(row.closest(".field")!).getByText("Xbox controller 2 · button 1 (not connected)")).toBeTruthy(),
      );
    });

    it("shows a re-capture notice for a stale winmm: bind, not for a live hid: bind (#144)", async () => {
      vi.mocked(isTauri).mockReturnValue(true);
      vi.mocked(getSettings).mockResolvedValue(
        baseSettings({
          voice: { ...voiceDefaults, ptt_wheel: "winmm:0:1", ptt_gamepad: "hid:046d:c276:7" },
        }),
      );
      vi.mocked(voiceStatus).mockResolvedValue({ installed: true, installing: false, progress_bytes: 0, total_bytes: null, error: null });

      render(SettingsScreen);
      await screen.findByText("Wheel button");

      expect(screen.getByText(/wheel bind was saved with an older version/i)).toBeTruthy();
      expect(screen.queryByText(/gamepad bind was saved with an older version/i)).toBeNull();
    });

    it("Clear resets both the bind and its stored name (#104)", async () => {
      vi.mocked(isTauri).mockReturnValue(true);
      vi.mocked(getSettings).mockResolvedValue(
        baseSettings({ voice: { ...voiceDefaults, ptt_gamepad: "winmm:1:0", ptt_gamepad_name: "Xbox controller 2" } }),
      );
      vi.mocked(voiceStatus).mockResolvedValue({ installed: true, installing: false, progress_bytes: 0, total_bytes: null, error: null });
      vi.mocked(putSettings).mockImplementation(async (cfg) =>
        baseSettings({ voice: { ...voiceDefaults, ...(cfg.voice as object) } }),
      );

      render(SettingsScreen);
      const row = await screen.findByText("Gamepad button");
      await fireEvent.click(within(row.closest(".field")!).getByRole("button", { name: "Clear" }));

      await waitFor(() =>
        expect(vi.mocked(putSettings).mock.calls.at(-1)![0]).toMatchObject({
          voice: { ptt_gamepad: null, ptt_gamepad_name: null },
        }),
      );
    });

    it("Microphone select lists System default plus each audioinput device", async () => {
      Object.defineProperty(navigator, "mediaDevices", {
        configurable: true,
        value: {
          enumerateDevices: vi.fn().mockResolvedValue([
            { deviceId: "d1", kind: "audioinput", label: "Headset Mic" },
            { deviceId: "d2", kind: "audioinput", label: "Webcam Mic" },
            { deviceId: "d3", kind: "videoinput", label: "Webcam" },
          ]),
          addEventListener: vi.fn(),
          removeEventListener: vi.fn(),
        },
      });
      vi.mocked(getSettings).mockResolvedValue(baseSettings({ voice: voiceDefaults }));
      vi.mocked(voiceStatus).mockResolvedValue({ installed: true, installing: false, progress_bytes: 0, total_bytes: null, error: null });

      render(SettingsScreen);
      const select = await screen.findByLabelText("Microphone");

      await waitFor(() => expect(within(select).getAllByRole("option")).toHaveLength(3));
      const labels = within(select).getAllByRole("option").map((o) => o.textContent);
      expect(labels).toEqual(["System default", "Headset Mic", "Webcam Mic"]);
    });

    it("choosing a microphone saves voice.mic_device_id", async () => {
      Object.defineProperty(navigator, "mediaDevices", {
        configurable: true,
        value: {
          enumerateDevices: vi.fn().mockResolvedValue([
            { deviceId: "d1", kind: "audioinput", label: "Headset Mic" },
          ]),
          addEventListener: vi.fn(),
          removeEventListener: vi.fn(),
        },
      });
      vi.mocked(getSettings).mockResolvedValue(baseSettings({ voice: voiceDefaults }));
      vi.mocked(putSettings).mockImplementation(async (cfg) =>
        baseSettings({ voice: { ...voiceDefaults, ...(cfg.voice as object) } }),
      );
      vi.mocked(voiceStatus).mockResolvedValue({ installed: true, installing: false, progress_bytes: 0, total_bytes: null, error: null });

      render(SettingsScreen);
      const select = await screen.findByLabelText("Microphone");
      await waitFor(() => expect(within(select).getAllByRole("option")).toHaveLength(2));

      await fireEvent.change(select, { target: { value: "d1" } });

      await waitFor(() =>
        expect(vi.mocked(putSettings).mock.calls.at(-1)![0]).toMatchObject({ voice: { mic_device_id: "d1" } }),
      );
    });

    it("with no mediaDevices the Microphone select still renders only System default", async () => {
      vi.mocked(getSettings).mockResolvedValue(baseSettings({ voice: voiceDefaults }));
      vi.mocked(voiceStatus).mockResolvedValue({ installed: true, installing: false, progress_bytes: 0, total_bytes: null, error: null });

      render(SettingsScreen);
      const select = await screen.findByLabelText("Microphone");

      expect(within(select).getAllByRole("option").map((o) => o.textContent)).toEqual(["System default"]);
    });

    // --- Settings -> Voice -> Kokoro engine ---

    it("Engine select offers Windows and Kokoro; picking Kokoro without the model opens the setup dialog and Not now reverts", async () => {
      vi.mocked(getSettings).mockResolvedValue(baseSettings({ voice: voiceDefaults }));
      vi.mocked(putSettings).mockImplementation(async (cfg) =>
        baseSettings({ voice: { ...voiceDefaults, ...(cfg.voice as object) } }),
      );
      vi.mocked(kokoroStatus).mockResolvedValue({
        engine: "kokoro", installed: false, installing: false, progress_bytes: 0, total_bytes: 340_000_000,
        error: null, voices: [],
      });

      render(SettingsScreen);
      const select = await screen.findByLabelText("Engine");
      expect(within(select).getAllByRole("option").map((o) => o.textContent)).toEqual(["Windows", "Kokoro"]);

      await fireEvent.change(select, { target: { value: "kokoro" } });

      const dialog = await screen.findByRole("dialog", { name: /kokoro/i });
      await waitFor(() =>
        expect(vi.mocked(putSettings).mock.calls.at(-1)![0]).toMatchObject({ voice: { tts_engine: "kokoro" } }),
      );

      const notNow = within(dialog).getByRole("button", { name: "Not now" });
      await fireEvent.click(notNow);

      await waitFor(() =>
        expect(vi.mocked(putSettings).mock.calls.at(-1)![0]).toMatchObject({ voice: { tts_engine: "windows" } }),
      );
      expect(kokoroSetup).not.toHaveBeenCalled();
    });

    it("Kokoro voice select lists the installed voices and Preview posts one fixed sentence", async () => {
      vi.mocked(getSettings).mockResolvedValue(
        baseSettings({ voice: { ...voiceDefaults, tts_engine: "kokoro" } }),
      );
      vi.mocked(kokoroStatus).mockResolvedValue({
        engine: "kokoro", installed: true, installing: false, progress_bytes: 0, total_bytes: null, error: null,
        voices: ["af_heart", "am_michael"],
      });
      vi.mocked(speak).mockResolvedValue(new ArrayBuffer(10));

      render(SettingsScreen);
      const select = await screen.findByLabelText("Voice");
      await waitFor(() => expect(within(select).getAllByRole("option")).toHaveLength(2));
      expect(within(select).getAllByRole("option").map((o) => o.textContent)).toEqual(["af_heart", "am_michael"]);

      await fireEvent.click(screen.getByRole("button", { name: "Preview" }));

      await waitFor(() =>
        expect(speak).toHaveBeenCalledWith(
          "Brake later into turn four, the fast left after the long straight.",
          expect.anything(),
          expect.anything(),
        ),
      );
    });

    it("shows the cached-audio readout and Clear removes it and refreshes stats", async () => {
      vi.mocked(getSettings).mockResolvedValue(
        baseSettings({ voice: { ...voiceDefaults, tts_engine: "kokoro" } }),
      );
      vi.mocked(kokoroStatus).mockResolvedValue({
        engine: "kokoro", installed: true, installing: false, progress_bytes: 0, total_bytes: null, error: null,
        voices: ["af_heart"],
      });
      vi.mocked(ttsCacheStats).mockResolvedValueOnce({ files: 58, bytes: 12_400_000 });

      render(SettingsScreen);
      expect(await screen.findByText(/Cached audio: 12\.4 MB \(58 clips\)/)).toBeTruthy();

      vi.mocked(ttsCacheClear).mockResolvedValue({ removed: 58 });
      vi.mocked(ttsCacheStats).mockResolvedValueOnce({ files: 0, bytes: 0 });
      await fireEvent.click(screen.getByRole("button", { name: "Clear" }));

      expect(await screen.findByText(/Cached audio: 0\.0 MB \(0 clips\)/)).toBeTruthy();
      expect(ttsCacheClear).toHaveBeenCalled();
    });

    it("voice_setup frames with engine kokoro drive the dialog progress", async () => {
      vi.mocked(getSettings).mockResolvedValue(baseSettings({ voice: voiceDefaults }));
      vi.mocked(putSettings).mockImplementation(async (cfg) =>
        baseSettings({ voice: { ...voiceDefaults, ...(cfg.voice as object) } }),
      );
      vi.mocked(kokoroStatus).mockResolvedValue({
        engine: "kokoro", installed: false, installing: false, progress_bytes: 0, total_bytes: 340_000_000,
        error: null, voices: [],
      });
      vi.mocked(kokoroSetup).mockResolvedValue({
        started: true, engine: "kokoro", installed: false, installing: true, progress_bytes: 0,
        total_bytes: 340_000_000, error: null, voices: [],
      });

      render(SettingsScreen);
      const select = await screen.findByLabelText("Engine");
      await fireEvent.change(select, { target: { value: "kokoro" } });

      const dialog = await screen.findByRole("dialog", { name: /kokoro/i });
      await fireEvent.click(within(dialog).getByRole("button", { name: "Download" }));
      await waitFor(() => expect(kokoroSetup).toHaveBeenCalled());

      eventFeed.push("voice_setup", {
        engine: "kokoro", installed: false, installing: true, progress_bytes: 170_000_000,
        total_bytes: 340_000_000, error: null, voices: [],
      });
      expect(await screen.findByText("50%")).toBeTruthy();

      eventFeed.push("voice_setup", {
        engine: "kokoro", installed: true, installing: false, progress_bytes: 340_000_000,
        total_bytes: 340_000_000, error: null, voices: ["af_heart"],
      });
      await waitFor(() => expect(screen.getByText(/Ready/)).toBeTruthy());
    });
  });
});
