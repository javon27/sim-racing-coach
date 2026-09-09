import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, cleanup, fireEvent } from "@testing-library/svelte";
import { getLapAnalysis, getSetup, captureSetup } from "./api";
import CornerSetupTabs from "./CornerSetupTabs.svelte";

vi.mock("./api", async () => {
  const actual = await vi.importActual<typeof import("./api")>("./api");
  return { ...actual, getLapAnalysis: vi.fn(), getSetup: vi.fn(), captureSetup: vi.fn() };
});

describe("CornerSetupTabs", () => {
  beforeEach(() => {
    vi.mocked(getLapAnalysis).mockReset();
    vi.mocked(getSetup).mockReset();
    vi.mocked(captureSetup).mockReset();
    vi.mocked(captureSetup).mockResolvedValue({ saved: true });
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
    expect(screen.getByText(/select a lap/i).closest("[hidden]")).toBeTruthy();

    await fireEvent.click(screen.getByText("Corners"));
    expect(screen.getByText(/select a lap/i).closest("[hidden]")).toBeNull();
    expect(screen.getByText("Front Anti-roll Bar").closest("[hidden]")).toBeTruthy();
  });

  it("keeps the Setup panel mounted across tab switches (issue #36: no refetch/reload flash)", async () => {
    render(CornerSetupTabs, { props: { lapId: null } });

    await fireEvent.click(screen.getByText("Setup"));
    expect(await screen.findByText("Front Anti-roll Bar")).toBeTruthy();
    expect(getSetup).toHaveBeenCalledTimes(1);

    await fireEvent.click(screen.getByText("Corners"));
    await fireEvent.click(screen.getByText("Setup"));
    expect(screen.getByText("Front Anti-roll Bar")).toBeTruthy();
    expect(getSetup).toHaveBeenCalledTimes(1); // still mounted, no re-fetch
  });

  it("no longer has a Map tab (the canonical map moved to the Coach right pane)", () => {
    render(CornerSetupTabs, { props: { lapId: null } });
    expect(screen.queryByText("Map")).toBeNull();
  });
});
