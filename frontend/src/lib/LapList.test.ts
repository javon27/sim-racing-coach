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

  it("renders per-sector delta bars colored vs the fastest lap, omits them when null", async () => {
    vi.mocked(getStatus).mockResolvedValue(status("S1", 6));
    // Reference = fastest lap with sectors = lap9 (91.2s): S1 30.0, S2 30.5, S3 30.0.
    vi.mocked(getSessionLaps).mockResolvedValue([
      { id: 9, lap_time: 91.2, valid: 1, ts: 100, s1: 30.0, s2: 30.5, s3: 30.0 },
      { id: 10, lap_time: 91.5, valid: 1, ts: 200, s1: 30.5, s2: 30.0, s3: 30.5 },
      { id: 11, lap_time: 92.0, valid: 1, ts: 300, s1: null, s2: null, s3: null },
    ]);

    const { container } = render(LapList);

    await screen.findByText("Lap 1");
    const secRows = container.querySelectorAll(".lapsec");
    expect(secRows.length).toBe(2); // only laps with sectors show delta bars
    expect(secRows[0].querySelectorAll("i").length).toBe(3); // one bar per sector

    // Lap 9 IS the reference -> all sectors match -> pb.
    const bars9 = secRows[0].querySelectorAll("i");
    expect([...bars9].map((b) => b.className)).toEqual(["pb", "pb", "pb"]);
    // Lap 10 vs lap9: S1 slower (up/red), S2 FASTER (down/green), S3 slower (up).
    const bars10 = secRows[1].querySelectorAll("i");
    expect([...bars10].map((b) => b.className)).toEqual(["up", "down", "up"]);
  });

  it("expanding a lap's chevron reveals the sector delta detail", async () => {
    vi.mocked(getStatus).mockResolvedValue(status("S1", 6));
    vi.mocked(getSessionLaps).mockResolvedValue([
      { id: 9, lap_time: 91.2, valid: 1, ts: 100, s1: 30.0, s2: 30.5, s3: 30.0 },
      { id: 10, lap_time: 91.5, valid: 1, ts: 200, s1: 30.5, s2: 30.0, s3: 30.5 },
    ]);

    const { container } = render(LapList);
    await screen.findByText("Lap 1");

    // Second lap's detail vs the fastest lap (lap9): S1 +0.50, S2 −0.50 (faster!), S3 +0.50.
    const wraps = container.querySelectorAll(".lapwrap");
    expect(wraps[1].classList.contains("open")).toBe(false);

    const chev = wraps[1].querySelector(".chev") as HTMLButtonElement;
    expect(chev.getAttribute("aria-expanded")).toBe("false");
    await fireEvent.click(chev);

    expect(wraps[1].classList.contains("open")).toBe(true);
    expect(chev.getAttribute("aria-expanded")).toBe("true");
    const cells = wraps[1].querySelectorAll(".seccell .sv");
    expect(cells[0].textContent).toBe("+0.50");
    expect(cells[1].textContent).toBe("−0.50");
    expect(cells[2].textContent).toBe("+0.50");
  });

  it("row click still selects the lap even though a chevron button now nests in the row", async () => {
    vi.mocked(getStatus).mockResolvedValue(status("S1", 6));
    vi.mocked(getSessionLaps).mockResolvedValue([
      { id: 9, lap_time: 91.2, valid: 1, ts: 100, s1: 30.0, s2: 30.5, s3: 30.0 },
    ]);

    const onselect = vi.fn();
    const { container } = render(LapList, { props: { onselect } });
    await screen.findByText("Lap 1");

    const row = container.querySelector(".lap") as HTMLElement;
    await fireEvent.click(row);
    expect(onselect).toHaveBeenCalledWith({ lap_id: 9, session_id: "S1" });

    // Clicking the chevron toggles detail without selecting.
    onselect.mockClear();
    const chev = container.querySelector(".chev") as HTMLButtonElement;
    await fireEvent.click(chev);
    expect(onselect).not.toHaveBeenCalled();
    expect(container.querySelector(".lapwrap")!.classList.contains("open")).toBe(true);
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
