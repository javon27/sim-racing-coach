import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/svelte";
import { getSetup } from "./api";
import SetupPanel from "./SetupPanel.svelte";

vi.mock("./api", async () => {
  const actual = await vi.importActual<typeof import("./api")>("./api");
  return { ...actual, getSetup: vi.fn() };
});

describe("SetupPanel", () => {
  beforeEach(() => {
    vi.mocked(getSetup).mockReset();
  });

  afterEach(cleanup);

  it("renders a setup row per key in the flat dict", async () => {
    vi.mocked(getSetup).mockResolvedValue({ front_arb: "P3", rear_arb: "P1" });

    render(SetupPanel);

    expect(await screen.findByText("front_arb")).toBeTruthy();
    expect(screen.getByText("P3")).toBeTruthy();
    expect(screen.getByText("rear_arb")).toBeTruthy();
    expect(screen.getByText("P1")).toBeTruthy();
  });

  it("shows a no-setup message when the dict is empty", async () => {
    vi.mocked(getSetup).mockResolvedValue({});

    render(SetupPanel);

    expect(await screen.findByText(/no setup.*lmu not connected/i)).toBeTruthy();
  });

  it("shows a graceful no-setup message when the fetch is rejected", async () => {
    vi.mocked(getSetup).mockRejectedValue(new Error("API request failed: GET /setup -> 404"));

    render(SetupPanel);

    expect(await screen.findByText(/no setup.*lmu not connected/i)).toBeTruthy();
  });
});
