import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/svelte";
import { getSessions, getSessionLaps } from "./api";
import LapList from "./LapList.svelte";

vi.mock("./api", async () => {
  const actual = await vi.importActual<typeof import("./api")>("./api");
  return { ...actual, getSessions: vi.fn(), getSessionLaps: vi.fn() };
});

describe("LapList", () => {
  beforeEach(() => {
    vi.mocked(getSessions).mockReset();
    vi.mocked(getSessionLaps).mockReset();
  });

  afterEach(cleanup);

  it("renders laps for the most recent session and calls onselect on click", async () => {
    vi.mocked(getSessions).mockResolvedValue([
      { session_id: "s1", track: "Le Mans", car: "Corvette Z06", laps: 2, best: 216.0, started: 1, last: 2 },
    ]);
    vi.mocked(getSessionLaps).mockResolvedValue([
      { id: 9, lap_time: 216.0, valid: 1, ts: 100 },
    ]);

    const onselect = vi.fn();
    render(LapList, { props: { onselect } });

    const row = await screen.findByText(/Lap 9/);
    expect(row).toBeTruthy();

    await fireEvent.click(row);
    expect(onselect).toHaveBeenCalledWith({ lap_id: 9 });
  });

  it("shows an empty state when there are no laps yet", async () => {
    vi.mocked(getSessions).mockResolvedValue([]);

    render(LapList);

    expect(await screen.findByText(/no laps yet/i)).toBeTruthy();
  });

  it("retries through transient failures and eventually renders laps", async () => {
    vi.mocked(getSessions)
      .mockRejectedValueOnce(new Error("network"))
      .mockRejectedValueOnce(new Error("network"))
      .mockResolvedValue([
        { session_id: "s1", track: "Le Mans", car: "Corvette Z06", laps: 1, best: 216.0, started: 1, last: 2 },
      ]);
    vi.mocked(getSessionLaps).mockResolvedValue([{ id: 9, lap_time: 216.0, valid: 1, ts: 100 }]);

    render(LapList, { props: { retryTries: 5, retryDelayMs: 5 } });

    expect(await screen.findByText(/Lap 9/)).toBeTruthy();
  });

  it("shows an unreachable state with a Retry button when all attempts fail, and Retry re-fetches", async () => {
    vi.mocked(getSessions).mockRejectedValue(new Error("network"));

    render(LapList, { props: { retryTries: 2, retryDelayMs: 5 } });

    const retryBtn = await screen.findByRole("button", { name: /retry/i });
    expect(screen.getByText(/can't reach the coach server/i)).toBeTruthy();

    vi.mocked(getSessions).mockResolvedValue([
      { session_id: "s1", track: "Le Mans", car: "Corvette Z06", laps: 1, best: 216.0, started: 1, last: 2 },
    ]);
    vi.mocked(getSessionLaps).mockResolvedValue([{ id: 9, lap_time: 216.0, valid: 1, ts: 100 }]);

    await fireEvent.click(retryBtn);

    expect(await screen.findByText(/Lap 9/)).toBeTruthy();
  });
});
