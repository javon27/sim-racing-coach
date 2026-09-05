import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup, within, waitFor } from "@testing-library/svelte";
import {
  getStatus,
  getSettings,
  getSessions,
  getSessionLaps,
  getLapAnalysis,
  getSetup,
  getChats,
  getSessionChats,
} from "./api";
import App from "./App.svelte";

vi.mock("./api", async () => {
  const actual = await vi.importActual<typeof import("./api")>("./api");
  return {
    ...actual,
    getStatus: vi.fn(),
    getSettings: vi.fn(),
    getSessions: vi.fn(),
    getSessionLaps: vi.fn(),
    getLapAnalysis: vi.fn(),
    getSetup: vi.fn(),
    getChats: vi.fn(),
    getSessionChats: vi.fn(),
  };
});

describe("App shell", () => {
  afterEach(cleanup);

  it("renders the three nav items and a theme toggle", () => {
    const { container } = render(App);
    const nav = within(container.querySelector(".nav") as HTMLElement);

    expect(nav.getByText("Coach")).toBeTruthy();
    expect(nav.getByText("Sessions")).toBeTruthy();
    expect(nav.getByText("Pit Wall")).toBeTruthy();
    expect(nav.getByText(/^Theme ·/)).toBeTruthy();
  });

  it("has a draggable resizer for the lap pane", () => {
    const { container } = render(App);
    const resizer = container.querySelector(".rail-resizer");
    expect(resizer).not.toBeNull();
    expect(resizer!.getAttribute("role")).toBe("separator");
  });
});

describe("Coach screen assembly", () => {
  beforeEach(() => {
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
    vi.mocked(getSessions).mockResolvedValue([
      { session_id: "s1", track: "Le Mans", car: "Corvette Z06", laps: 1, best: 216.0, started: 1, last: 2 },
    ]);
    vi.mocked(getSessionLaps).mockResolvedValue([{ id: 9, lap_time: 216.0, valid: 1, ts: 100 }]);
    vi.mocked(getLapAnalysis).mockResolvedValue({ lap_time: 216.0, corners: [] });
    vi.mocked(getSetup).mockResolvedValue({});
    vi.mocked(getChats).mockResolvedValue([]);
    vi.mocked(getSessionChats).mockResolvedValue([]);
  });

  afterEach(cleanup);

  it("renders the session bar, lap list, corner/setup tabs, and chat composer together", async () => {
    const { container } = render(App);
    const bar = container.querySelector(".bar") as HTMLElement;

    expect(await within(bar).findByText("Le Mans")).toBeTruthy(); // session bar
    expect(await screen.findByText("Lap 1")).toBeTruthy(); // lap list
    expect(screen.getByText("Corners")).toBeTruthy(); // tabs
    expect(screen.getByText("Setup")).toBeTruthy();
    expect(screen.getByPlaceholderText(/ask your engineer/i)).toBeTruthy(); // chat composer
  });

  it("flows a selected lap's id into the corner panel and highlights the row", async () => {
    render(App);

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
    const nav = within(container.querySelector(".nav") as HTMLElement);

    await fireEvent.click(nav.getByText("Sessions"));

    expect(await screen.findByRole("heading", { name: "Sessions" })).toBeTruthy();
  });

  it("switches to the Settings screen when the Settings nav item is clicked", async () => {
    const { container } = render(App);
    const nav = within(container.querySelector(".nav") as HTMLElement);

    await fireEvent.click(nav.getByText("Settings"));

    expect(await screen.findByText("Display")).toBeTruthy();
  });

  it("deep-links a clicked session row into the Coach screen and loads that session's laps", async () => {
    const { container } = render(App);
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
});
