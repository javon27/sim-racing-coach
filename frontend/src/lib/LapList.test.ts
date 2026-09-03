import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/svelte";
import { getSessions, getSessionLaps } from "./api";
import LapList from "./LapList.svelte";

vi.mock("./api", () => ({
  getSessions: vi.fn(),
  getSessionLaps: vi.fn(),
}));

describe("LapList", () => {
  beforeEach(() => {
    vi.mocked(getSessions).mockReset();
    vi.mocked(getSessionLaps).mockReset();
  });

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
});
