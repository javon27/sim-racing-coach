import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, cleanup, fireEvent } from "@testing-library/svelte";
import { getLapAnalysis, getSetup } from "./api";
import CornerSetupTabs from "./CornerSetupTabs.svelte";

vi.mock("./api", async () => {
  const actual = await vi.importActual<typeof import("./api")>("./api");
  return { ...actual, getLapAnalysis: vi.fn(), getSetup: vi.fn() };
});

describe("CornerSetupTabs", () => {
  beforeEach(() => {
    vi.mocked(getLapAnalysis).mockReset();
    vi.mocked(getSetup).mockReset();
    vi.mocked(getSetup).mockResolvedValue({ front_arb: "P3" });
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
    expect(await screen.findByText("front_arb")).toBeTruthy();
    expect(screen.queryByText(/select a lap/i)).toBeNull();

    await fireEvent.click(screen.getByText("Corners"));
    expect(screen.getByText(/select a lap/i)).toBeTruthy();
    expect(screen.queryByText("front_arb")).toBeNull();
  });
});
