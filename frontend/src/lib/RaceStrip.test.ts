import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/svelte";
import { getSessionRace } from "./api";
import RaceStrip from "./RaceStrip.svelte";

vi.mock("./api", async () => {
  const actual = await vi.importActual<typeof import("./api")>("./api");
  return { ...actual, getSessionRace: vi.fn() };
});

describe("RaceStrip", () => {
  beforeEach(() => {
    vi.mocked(getSessionRace).mockReset();
  });

  afterEach(cleanup);

  it("renders position and incident count for a populated result", async () => {
    vi.mocked(getSessionRace).mockResolvedValue({
      position: 3,
      class_position: 3,
      finish_status: "Finished",
      incidents: ["contact", "off-track"],
      penalties: [],
    });

    render(RaceStrip, { props: { sessionId: "S1" } });

    expect(await screen.findByText("P3")).toBeTruthy();
    expect(screen.getByText("2 incidents")).toBeTruthy();
    expect(screen.getByText("Finished")).toBeTruthy();
    expect(vi.mocked(getSessionRace)).toHaveBeenCalledWith("S1");
  });

  it("shows class position when it differs from overall", async () => {
    vi.mocked(getSessionRace).mockResolvedValue({
      position: 3,
      class_position: 2,
      finish_status: null,
      incidents: [],
      penalties: [],
    });

    render(RaceStrip, { props: { sessionId: "S1" } });

    expect(await screen.findByText("P3 (class P2)")).toBeTruthy();
  });

  it("renders nothing for an empty result ({})", async () => {
    vi.mocked(getSessionRace).mockResolvedValue({});

    const { container } = render(RaceStrip, { props: { sessionId: "S1" } });

    await vi.waitFor(() => expect(vi.mocked(getSessionRace)).toHaveBeenCalled());
    expect(container.querySelector(".race-strip")).toBeNull();
  });

  it("renders nothing and does not call the API when sessionId is undefined", async () => {
    const { container } = render(RaceStrip);

    await new Promise((r) => setTimeout(r, 10));
    expect(container.querySelector(".race-strip")).toBeNull();
    expect(vi.mocked(getSessionRace)).not.toHaveBeenCalled();
  });

  it("renders nothing when the fetch fails", async () => {
    vi.mocked(getSessionRace).mockRejectedValue(new Error("network"));

    const { container } = render(RaceStrip, { props: { sessionId: "S1" } });

    await vi.waitFor(() => expect(vi.mocked(getSessionRace)).toHaveBeenCalled());
    expect(container.querySelector(".race-strip")).toBeNull();
  });
});
