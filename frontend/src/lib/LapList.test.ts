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

    const row = await screen.findByText(/Lap 9/);
    expect(vi.mocked(getSessionLaps)).toHaveBeenCalledWith("S1");

    await fireEvent.click(row);
    expect(onselect).toHaveBeenCalledWith({ lap_id: 9, session_id: "S1" });
  });

  it("shows a best-lap header with the fastest lap time", async () => {
    vi.mocked(getStatus).mockResolvedValue(status("S1", 6));
    vi.mocked(getSessionLaps).mockResolvedValue([
      { id: 9, lap_time: 216.0, valid: 1, ts: 100 },
      { id: 10, lap_time: 210.5, valid: 1, ts: 200 },
    ]);

    const { container } = render(LapList);

    await screen.findByText(/Lap 10/);
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

    render(LapList, { props: { sessionId: "OLD", pollMs: 5 } });

    // Holds on OLD while live lap_id stays at the baseline (5).
    expect(await screen.findByText(/Lap 42/)).toBeTruthy();
    expect(screen.queryByText(/Lap 9/)).toBeNull();

    // A new live lap arrives -> snap to the live session.
    vi.mocked(getStatus).mockResolvedValue(status("S1", 6));
    expect(await screen.findByText(/Lap 9/, {}, { timeout: 2000 })).toBeTruthy();
  });

  it("retries through transient failures and eventually renders laps", async () => {
    vi.mocked(getStatus)
      .mockRejectedValueOnce(new Error("network"))
      .mockRejectedValueOnce(new Error("network"))
      .mockResolvedValue(status("S1", 5));
    vi.mocked(getSessionLaps).mockResolvedValue([{ id: 9, lap_time: 216.0, valid: 1, ts: 100 }]);

    render(LapList, { props: { retryTries: 5, retryDelayMs: 5 } });

    expect(await screen.findByText(/Lap 9/)).toBeTruthy();
  });

  it("shows an unreachable state with a Retry button when all attempts fail, and Retry re-fetches", async () => {
    vi.mocked(getStatus).mockRejectedValue(new Error("network"));

    render(LapList, { props: { retryTries: 2, retryDelayMs: 5 } });

    const retryBtn = await screen.findByRole("button", { name: /retry/i });
    expect(screen.getByText(/can't reach the coach server/i)).toBeTruthy();

    vi.mocked(getStatus).mockResolvedValue(status("S1", 5));
    vi.mocked(getSessionLaps).mockResolvedValue([{ id: 9, lap_time: 216.0, valid: 1, ts: 100 }]);

    await fireEvent.click(retryBtn);

    expect(await screen.findByText(/Lap 9/)).toBeTruthy();
  });

  it("polls for new laps periodically and shows them", async () => {
    vi.mocked(getStatus).mockResolvedValue(status("S1", 5));
    vi.mocked(getSessionLaps).mockResolvedValueOnce([{ id: 9, lap_time: 216.0, valid: 1, ts: 100 }]);

    render(LapList, { props: { pollMs: 5 } });

    expect(await screen.findByText(/Lap 9/)).toBeTruthy();
    expect(screen.queryByText(/Lap 10/)).toBeNull();

    vi.mocked(getSessionLaps).mockResolvedValue([
      { id: 9, lap_time: 216.0, valid: 1, ts: 100 },
      { id: 10, lap_time: 214.0, valid: 1, ts: 200 },
    ]);

    expect(await screen.findByText(/Lap 10/, {}, { timeout: 2000 })).toBeTruthy();
  });

  it("keeps last good laps and does not go unreachable on a transient poll failure", async () => {
    vi.mocked(getStatus).mockResolvedValue(status("S1", 5));
    vi.mocked(getSessionLaps).mockResolvedValue([{ id: 9, lap_time: 216.0, valid: 1, ts: 100 }]);

    render(LapList, { props: { pollMs: 5 } });

    expect(await screen.findByText(/Lap 9/)).toBeTruthy();

    vi.mocked(getStatus).mockResolvedValueOnce(status("S1", 5));
    vi.mocked(getStatus).mockRejectedValueOnce(new Error("transient"));

    await vi.waitFor(() => expect(vi.mocked(getStatus).mock.calls.length).toBeGreaterThan(2), {
      timeout: 2000,
    });

    expect(screen.getByText(/Lap 9/)).toBeTruthy();
    expect(screen.queryByText(/can't reach the coach server/i)).toBeNull();
  });

  it("stops polling after unmount", async () => {
    vi.mocked(getStatus).mockResolvedValue(status("S1", 5));
    vi.mocked(getSessionLaps).mockResolvedValue([{ id: 9, lap_time: 216.0, valid: 1, ts: 100 }]);

    const { unmount } = render(LapList, { props: { pollMs: 5 } });

    expect(await screen.findByText(/Lap 9/)).toBeTruthy();
    await vi.waitFor(() => expect(vi.mocked(getStatus).mock.calls.length).toBeGreaterThan(1), {
      timeout: 2000,
    });

    unmount();
    const callsAtUnmount = vi.mocked(getStatus).mock.calls.length;
    await new Promise((r) => setTimeout(r, 50));
    expect(vi.mocked(getStatus).mock.calls.length).toBe(callsAtUnmount);
  });
});
