import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/svelte";
import {
  getSettings,
  putSettings,
  setApiKey,
  testApiKey,
  clearData,
  ingestHistory,
  type SettingsResponse,
} from "./api";
import { applyTheme } from "./theme";
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
  };
});

vi.mock("./theme", async () => {
  const actual = await vi.importActual<typeof import("./theme")>("./theme");
  return { ...actual, applyTheme: vi.fn() };
});

function baseSettings(overrides: Partial<SettingsResponse> = {}): SettingsResponse {
  return {
    units: "metric",
    input_device: "wheel",
    model: "sonnet",
    corner_naming: "names",
    driver: { braking_style: null, balance_preference: null, risk_tolerance: null },
    api_key_set: false,
    data_dir: "C:/data",
    app_version: "0.1.0",
    lmu_build: "1.0",
    lmu_dir: null,
    lmu_player_names: [],
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
    localStorage.clear();
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

  it("Reset confirms before calling clearData", async () => {
    vi.mocked(getSettings).mockResolvedValue(baseSettings());
    vi.mocked(clearData).mockResolvedValue({ cleared: true });

    render(SettingsScreen);

    await fireEvent.click(await screen.findByRole("button", { name: /reset \/ clear data/i }));
    expect(clearData).not.toHaveBeenCalled();

    await fireEvent.click(await screen.findByRole("button", { name: /confirm/i }));

    expect(clearData).toHaveBeenCalled();
    expect(await screen.findByText(/data cleared/i)).toBeTruthy();
  });
});
