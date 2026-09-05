import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, cleanup, fireEvent } from "@testing-library/svelte";
import { getLapAnalysis, getSetup, getLapMap } from "./api";
import CornerSetupTabs from "./CornerSetupTabs.svelte";

vi.mock("./api", async () => {
  const actual = await vi.importActual<typeof import("./api")>("./api");
  return { ...actual, getLapAnalysis: vi.fn(), getSetup: vi.fn(), getLapMap: vi.fn() };
});

describe("CornerSetupTabs", () => {
  beforeEach(() => {
    vi.mocked(getLapAnalysis).mockReset();
    vi.mocked(getSetup).mockReset();
    vi.mocked(getLapMap).mockReset();
    vi.mocked(getLapMap).mockResolvedValue({});
    vi.mocked(getLapAnalysis).mockResolvedValue({ lap_time: 0, corners: [] });
    // Real /setup shape: only isFreeSetting entries render (SetupPanel filters).
    vi.mocked(getSetup).mockResolvedValue({
      VM_FRONT_ANTISWAY: { isFreeSetting: true, stringValue: "P3" },
    });
  });

  afterEach(cleanup);

  it("defaults to the Corners tab", () => {
    render(CornerSetupTabs, { props: { lapId: null } });

    expect(screen.getByText(/select a lap/i)).toBeTruthy();
    expect(screen.getByText("Corners").className).toContain("on");
  });

  it("switches to Setup content when the Setup tab is clicked, and back", async () => {
    render(CornerSetupTabs, { props: { lapId: null } });

    await fireEvent.click(screen.getByText("Setup"));
    expect(await screen.findByText("Front Anti-roll Bar")).toBeTruthy();
    expect(screen.queryByText(/select a lap/i)).toBeNull();

    await fireEvent.click(screen.getByText("Corners"));
    expect(screen.getByText(/select a lap/i)).toBeTruthy();
    expect(screen.queryByText("Front Anti-roll Bar")).toBeNull();
  });

  it("shows the track map view when the Map tab is clicked", async () => {
    render(CornerSetupTabs, { props: { lapId: null } });

    await fireEvent.click(screen.getByText("Map"));
    expect(await screen.findByText(/select a lap to see the track map/i)).toBeTruthy();
  });

  it("has no Edit map button without a selected lap", async () => {
    render(CornerSetupTabs, { props: { lapId: null } });
    await fireEvent.click(screen.getByText("Map"));
    expect(screen.queryByText("Edit map")).toBeNull();
  });

  it("opens the editor modal from the Map tab when a lap is selected", async () => {
    render(CornerSetupTabs, { props: { lapId: 7 } });

    await fireEvent.click(screen.getByText("Map"));
    await fireEvent.click(screen.getByText("Edit map"));
    expect(await screen.findByRole("dialog")).toBeTruthy();
  });
});
