import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/svelte";
import { getStatus, getSessionLaps } from "./api";
import LapList from "./LapList.svelte";

vi.mock("./api", async () => {
  const actual = await vi.importActual<typeof import("./api")>("./api");
  return { ...actual, getStatus: vi.fn(), getSessionLaps: vi.fn() };
});

function status(session_id: string | null, lap_id: number | null) {
  return {
    track: session_id ? "Le Mans" : null,
    car: session_id ? "Corvette Z06" : null,
    on_track: session_id !== null,
    has_map: false,
    lmu_connected: false,
    session_id,
    lap_id,
  };
}

describe("LapList", () => {
  beforeEach(() => {
    vi.mocked(getStatus).mockReset();
    vi.mocked(getSessionLaps).mockReset();
  });

  afterEach(cleanup);

  it("cold start: no live session yet -> empty state, does not fetch laps", async () => {
    vi.mocked(getStatus).mockResolvedValue(status(null, null));

    render(LapList);

    expect(await screen.findByText(/no laps yet/i)).toBeTruthy();
    expect(vi.mocked(getSessionLaps)).not.toHaveBeenCalled();
  });

  it("live: renders the live session's laps and calls onselect on click", async () => {
    vi.mocked(getStatus).mockResolvedValue(status("S1", 5));
    vi.mocked(getSessionLaps).mockResolvedValue([{ id: 9, lap_time: 216.0, valid: 1, ts: 100 }]);

    const onselect = vi.fn();
    render(LapList, { props: { onselect } });

    const row = await screen.findByText("Lap 1");
    expect(vi.mocked(getSessionLaps)).toHaveBeenCalledWith("S1");

    await fireEvent.click(row);
    expect(onselect).toHaveBeenCalledWith({ lap_id: 9, session_id: "S1" });
  });

  it("renders per-lap sector times when present, omits them when null", async () => {
    vi.mocked(getStatus).mockResolvedValue(status("S1", 6));
    vi.mocked(getSessionLaps).mockResolvedValue([
      { id: 9, lap_time: 91.2, valid: 1, ts: 100, s1: 30.1, s2: 30.2, s3: 30.9 },
      { id: 10, lap_time: 92.0, valid: 1, ts: 200, s1: null, s2: null, s3: null },
    ]);

    const { container } = render(LapList);

    await screen.findByText("Lap 1");
    const sectorEls = container.querySelectorAll(".sectors");
    expect(sectorEls.length).toBe(1); // only the lap with sectors shows a sector line
    expect(sectorEls[0].textContent).toContain("30.1");
    expect(sectorEls[0].textContent).toContain("30.9");
  });

  it("groups laps by phase with per-phase numbering that restarts each phase", async () => {
    vi.mocked(getStatus).mockResolvedValue(status("S1", 4));
    vi.mocked(getSessionLaps).mockResolvedValue([
      { id: 1, lap_time: 95.0, valid: 1, ts: 100, s1: null, s2: null, s3: null, phase: "practice" },
      { id: 2, lap_time: 94.0, valid: 1, ts: 200, s1: null, s2: null, s3: null, phase: "practice" },
      { id: 3, lap_time: 93.0, valid: 1, ts: 300, s1: null, s2: null, s3: null, phase: "race" },
      { id: 4, lap_time: 92.5, valid: 1, ts: 400, s1: null, s2: null, s3: null, phase: "race" },
    ]);

    const { container } = render(LapList);

    await screen.findByText("PRACTICE");
    expect(screen.getByText("RACE")).toBeTruthy();
    // Numbering restarts per phase: two "Lap 1" rows (one per phase).
    expect(screen.getAllByText("Lap 1")).toHaveLength(2);
    expect(container.querySelectorAll(".phase-head")).toHaveLength(2);
  });

  it("shows a best-lap header with the fastest lap time", async () => {
    vi.mocked(getStatus).mockResolvedValue(status("S1", 6));
    vi.mocked(getSessionLaps).mockResolvedValue([
      { id: 9, lap_time: 216.0, valid: 1, ts: 100 },
      { id: 10, lap_time: 210.5, valid: 1, ts: 200 },
    ]);

    const { container } = render(LapList);

    await screen.findByText("Lap 2");
    const head = container.querySelector(".best-head");
    expect(head).not.toBeNull();
    expect(head!.textContent).toContain("Best");
    expect(head!.textContent).toContain("3:30.500");
  });

  it("review hold then snap: holds on the deep-linked session until a new live lap arrives", async () => {
    vi.mocked(getStatus).mockResolvedValue(status("S1", 5)); // live session already running
    vi.mocked(getSessionLaps).mockImplementation(async (sid: string) =>
      sid === "OLD"
        ? [{ id: 42, lap_time: 210.0, valid: 1, ts: 300 }]
        : [{ id: 9, lap_time: 216.0, valid: 1, ts: 100 }],
    );

    const { container } = render(LapList, { props: { sessionId: "OLD", pollMs: 5 } });

    // Holds on OLD (3:30.000) while live lap_id stays at the baseline (5).
    // Numbering is per-phase positional now, so discriminate by the row's time.
    await vi.waitFor(() =>
      expect(container.querySelector(".lap .t")?.textContent).toBe("3:30.000"),
    );

    // A new live lap arrives -> snap to the live session (3:36.000).
    vi.mocked(getStatus).mockResolvedValue(status("S1", 6));
    await vi.waitFor(
      () => expect(container.querySelector(".lap .t")?.textContent).toBe("3:36.000"),
      { timeout: 2000 },
    );
  });

  it("retries through transient failures and eventually renders laps", async () => {
    vi.mocked(getStatus)
      .mockRejectedValueOnce(new Error("network"))
      .mockRejectedValueOnce(new Error("network"))
      .mockResolvedValue(status("S1", 5));
    vi.mocked(getSessionLaps).mockResolvedValue([{ id: 9, lap_time: 216.0, valid: 1, ts: 100 }]);

    render(LapList, { props: { retryTries: 5, retryDelayMs: 5 } });

    expect(await screen.findByText("Lap 1")).toBeTruthy();
  });

  it("shows an unreachable state with a Retry button when all attempts fail, and Retry re-fetches", async () => {
    vi.mocked(getStatus).mockRejectedValue(new Error("network"));

    render(LapList, { props: { retryTries: 2, retryDelayMs: 5 } });

    const retryBtn = await screen.findByRole("button", { name: /retry/i });
    expect(screen.getByText(/can't reach the coach server/i)).toBeTruthy();

    vi.mocked(getStatus).mockResolvedValue(status("S1", 5));
    vi.mocked(getSessionLaps).mockResolvedValue([{ id: 9, lap_time: 216.0, valid: 1, ts: 100 }]);

    await fireEvent.click(retryBtn);

    expect(await screen.findByText("Lap 1")).toBeTruthy();
  });

  it("polls for new laps periodically and shows them", async () => {
    vi.mocked(getStatus).mockResolvedValue(status("S1", 5));
    vi.mocked(getSessionLaps).mockResolvedValueOnce([{ id: 9, lap_time: 216.0, valid: 1, ts: 100 }]);

    render(LapList, { props: { pollMs: 5 } });

    expect(await screen.findByText("Lap 1")).toBeTruthy();
    expect(screen.queryByText("Lap 2")).toBeNull();

    vi.mocked(getSessionLaps).mockResolvedValue([
      { id: 9, lap_time: 216.0, valid: 1, ts: 100 },
      { id: 10, lap_time: 214.0, valid: 1, ts: 200 },
    ]);

    expect(await screen.findByText("Lap 2", {}, { timeout: 2000 })).toBeTruthy();
  });

  it("keeps last good laps and does not go unreachable on a transient poll failure", async () => {
    vi.mocked(getStatus).mockResolvedValue(status("S1", 5));
    vi.mocked(getSessionLaps).mockResolvedValue([{ id: 9, lap_time: 216.0, valid: 1, ts: 100 }]);

    render(LapList, { props: { pollMs: 5 } });

    expect(await screen.findByText("Lap 1")).toBeTruthy();

    vi.mocked(getStatus).mockResolvedValueOnce(status("S1", 5));
    vi.mocked(getStatus).mockRejectedValueOnce(new Error("transient"));

    await vi.waitFor(() => expect(vi.mocked(getStatus).mock.calls.length).toBeGreaterThan(2), {
      timeout: 2000,
    });

    expect(screen.getByText("Lap 1")).toBeTruthy();
    expect(screen.queryByText(/can't reach the coach server/i)).toBeNull();
  });

  it("stops polling after unmount", async () => {
    vi.mocked(getStatus).mockResolvedValue(status("S1", 5));
    vi.mocked(getSessionLaps).mockResolvedValue([{ id: 9, lap_time: 216.0, valid: 1, ts: 100 }]);

    const { unmount } = render(LapList, { props: { pollMs: 5 } });

    expect(await screen.findByText("Lap 1")).toBeTruthy();
    await vi.waitFor(() => expect(vi.mocked(getStatus).mock.calls.length).toBeGreaterThan(1), {
      timeout: 2000,
    });

    unmount();
    const callsAtUnmount = vi.mocked(getStatus).mock.calls.length;
    await new Promise((r) => setTimeout(r, 50));
    expect(vi.mocked(getStatus).mock.calls.length).toBe(callsAtUnmount);
  });
});
