import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/svelte";
import { getStatus, getSessionLaps } from "./api";
import { eventFeed } from "./events";
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

/** A status frame off the /events feed — what used to be a getStatus() poll tick. */
function pushStatus(session_id: string | null, lap_id: number | null) {
  eventFeed.push("status", status(session_id, lap_id));
}

describe("LapList", () => {
  beforeEach(() => {
    vi.mocked(getStatus).mockReset();
    vi.mocked(getSessionLaps).mockReset();
  });

  afterEach(cleanup);

  it("cold start: no live session yet -> empty state, does not fetch laps", async () => {
    pushStatus(null, null);

    render(LapList);

    expect(await screen.findByText(/no laps yet/i)).toBeTruthy();
    expect(vi.mocked(getSessionLaps)).not.toHaveBeenCalled();
  });

  it("live: renders the live session's laps and calls onselect on click", async () => {
    pushStatus("S1", 5);
    vi.mocked(getSessionLaps).mockResolvedValue([{ id: 9, lap_time: 216.0, valid: 1, ts: 100 }]);

    const onselect = vi.fn();
    render(LapList, { props: { onselect } });

    const row = await screen.findByText("Lap 1");
    expect(vi.mocked(getSessionLaps)).toHaveBeenCalledWith("S1");

    await fireEvent.click(row);
    expect(onselect).toHaveBeenCalledWith({ lap_id: 9, session_id: "S1" });
  });

  it("clicked session: renders laps immediately even before any status frame (fast path)", async () => {
    // A deep-linked/clicked session must not be gated on live state arriving.
    vi.mocked(getSessionLaps).mockResolvedValue([{ id: 42, lap_time: 90.0, valid: 1, ts: 100 }]);

    render(LapList, { props: { sessionId: "S2" } });

    expect(await screen.findByText("Lap 1")).toBeTruthy();
    expect(vi.mocked(getSessionLaps)).toHaveBeenCalledWith("S2");
  });

  it("never calls getStatus — live state is a subscription, not a poll (#106)", async () => {
    pushStatus("S1", 5);
    vi.mocked(getSessionLaps).mockResolvedValue([{ id: 9, lap_time: 216.0, valid: 1, ts: 100 }]);

    render(LapList);
    await screen.findByText("Lap 1");

    for (let i = 6; i < 12; i++) pushStatus("S1", i);
    expect(vi.mocked(getStatus)).not.toHaveBeenCalled();
  });

  it("takes the live session's laps from the laps frame instead of re-fetching them", async () => {
    pushStatus("S1", 5);
    eventFeed.push("laps", { session_id: "S1", laps: [{ id: 9, lap_time: 216.0, valid: 1, ts: 100 }] });

    render(LapList);

    expect(await screen.findByText("Lap 1")).toBeTruthy();
    expect(vi.mocked(getSessionLaps)).not.toHaveBeenCalled();
  });

  it("appends laps from a laps frame with no status frame in between", async () => {
    pushStatus("S1", 5);
    eventFeed.push("laps", { session_id: "S1", laps: [{ id: 9, lap_time: 216.0, valid: 1, ts: 100 }] });
    render(LapList);
    await screen.findByText("Lap 1");

    eventFeed.push("laps", {
      session_id: "S1",
      laps: [
        { id: 9, lap_time: 216.0, valid: 1, ts: 100 },
        { id: 10, lap_time: 214.0, valid: 1, ts: 200 },
      ],
    });

    expect(await screen.findByText("Lap 2")).toBeTruthy();
  });

  it("ignores a laps frame for a session it is not following (pinned review)", async () => {
    vi.mocked(getSessionLaps).mockResolvedValue([{ id: 42, lap_time: 210.0, valid: 1, ts: 300 }]);
    const { container } = render(LapList, { props: { sessionId: "OLD" } });
    await screen.findByText("Lap 1");

    // The live session keeps producing laps while the driver reviews an old one.
    eventFeed.push("laps", {
      session_id: "S1",
      laps: [
        { id: 1, lap_time: 100.0, valid: 1, ts: 1 },
        { id: 2, lap_time: 101.0, valid: 1, ts: 2 },
      ],
    });

    expect(screen.queryByText("Lap 2")).toBeNull();
    expect(container.querySelector(".lap .t")?.textContent).toBe("3:30.000"); // still OLD's lap
  });

  it("renders per-sector delta bars colored vs the fastest lap, omits them when null", async () => {
    pushStatus("S1", 6);
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

  it("a faster mapping lap is neither the best lap nor the sector reference (#154)", async () => {
    pushStatus("S1", 6);
    vi.mocked(getSessionLaps).mockResolvedValue([
      { id: 9, lap_time: 80.0, valid: 1, ts: 100, s1: 26.0, s2: 27.0, s3: 27.0, mapping: true },
      { id: 10, lap_time: 91.2, valid: 1, ts: 200, s1: 30.0, s2: 30.5, s3: 30.0 },
    ]);

    const { container } = render(LapList);

    await screen.findByText("Lap 2");
    expect(container.querySelector(".lap.best")).toBe(container.querySelectorAll(".lap")[1]);
    const secRows = container.querySelectorAll(".lapsec");
    // Lap 10 is the reference: all its sectors match.
    expect([...secRows[1].querySelectorAll("i")].map((b) => b.className)).toEqual(["pb", "pb", "pb"]);
  });

  it("expanding a lap's chevron reveals the sector delta detail", async () => {
    pushStatus("S1", 6);
    vi.mocked(getSessionLaps).mockResolvedValue([
      { id: 9, lap_time: 91.2, valid: 1, ts: 100, s1: 30.0, s2: 30.5, s3: 30.0 },
      { id: 10, lap_time: 91.5, valid: 1, ts: 200, s1: 30.5, s2: 30.0, s3: 30.5 },
    ]);

    const { container } = render(LapList);
    await screen.findByText("Lap 1");

    // Second lap's detail vs the fastest lap (lap9): S1 +0.50, S2 −0.50 (faster!), S3 +0.50.
    const wraps = container.querySelectorAll(".lapwrap");
    expect(wraps[1].classList.contains("open")).toBe(false);

    const toggle = wraps[1].querySelector(".lap-expand") as HTMLButtonElement;
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    await fireEvent.click(toggle);

    expect(wraps[1].classList.contains("open")).toBe(true);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    const cells = wraps[1].querySelectorAll(".seccell .sv");
    expect(cells[0].textContent).toBe("+0.50");
    expect(cells[1].textContent).toBe("−0.50");
    expect(cells[2].textContent).toBe("+0.50");
  });

  it("left zone selects the lap; the right zone toggles detail without selecting", async () => {
    pushStatus("S1", 6);
    vi.mocked(getSessionLaps).mockResolvedValue([
      { id: 9, lap_time: 91.2, valid: 1, ts: 100, s1: 30.0, s2: 30.5, s3: 30.0 },
    ]);

    const onselect = vi.fn();
    const { container } = render(LapList, { props: { onselect } });
    await screen.findByText("Lap 1");

    const selectZone = container.querySelector(".lap-select") as HTMLElement;
    await fireEvent.click(selectZone);
    expect(onselect).toHaveBeenCalledWith({ lap_id: 9, session_id: "S1" });

    // The right (expand) zone toggles detail without selecting.
    onselect.mockClear();
    const toggle = container.querySelector(".lap-expand") as HTMLButtonElement;
    await fireEvent.click(toggle);
    expect(onselect).not.toHaveBeenCalled();
    expect(container.querySelector(".lapwrap")!.classList.contains("open")).toBe(true);
  });

  it("a lap with no sectors makes the whole row select (no expand zone)", async () => {
    pushStatus("S1", 6);
    vi.mocked(getSessionLaps).mockResolvedValue([
      { id: 9, lap_time: 216.0, valid: 1, ts: 100, s1: null, s2: null, s3: null },
    ]);

    const onselect = vi.fn();
    const { container } = render(LapList, { props: { onselect } });
    await screen.findByText("Lap 1");

    expect(container.querySelector(".lap-expand")).toBeNull();
    const selectZone = container.querySelector(".lap-select.full") as HTMLElement;
    expect(selectZone).not.toBeNull();
    await fireEvent.click(selectZone);
    expect(onselect).toHaveBeenCalledWith({ lap_id: 9, session_id: "S1" });
  });

  it("groups laps by phase with per-phase numbering that restarts each phase", async () => {
    pushStatus("S1", 4);
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

  it("a mapping lap stays in its real phase group and is tagged, not a MAPPING group (#154)", async () => {
    pushStatus("S1", 3);
    vi.mocked(getSessionLaps).mockResolvedValue([
      { id: 1, lap_time: 95.0, valid: 1, ts: 100, s1: null, s2: null, s3: null, phase: "race" },
      { id: 2, lap_time: 99.0, valid: 1, ts: 200, s1: null, s2: null, s3: null, phase: "race", mapping: true },
    ]);

    const { container } = render(LapList);

    await screen.findByText("RACE");
    expect(container.querySelectorAll(".phase-head")).toHaveLength(1); // one race group
    expect(screen.getByText("Lap 2")).toBeTruthy();                     // numbered in it
    expect(container.querySelectorAll(".maptag")).toHaveLength(1);      // only lap 2
  });

  it("shows the game's own lap number when every lap in the session has one", async () => {
    // Issue #77: a session with a garage return (or an out-lap) shows the game's
    // real numbers/gaps, not a plain 1..N re-count.
    pushStatus("S1", 9);
    vi.mocked(getSessionLaps).mockResolvedValue([
      { id: 1, lap_time: 95.0, valid: 1, ts: 100, lap_no: 2 },
      { id: 2, lap_time: 94.0, valid: 1, ts: 200, lap_no: 3 },
      { id: 3, lap_time: 93.0, valid: 1, ts: 300, lap_no: 4 },
      { id: 4, lap_time: 92.5, valid: 1, ts: 400, lap_no: 8 },
      { id: 5, lap_time: 92.0, valid: 1, ts: 500, lap_no: 9 },
    ]);

    render(LapList);

    for (const n of [2, 3, 4, 8, 9]) {
      expect(await screen.findByText(`Lap ${n}`)).toBeTruthy();
    }
    expect(screen.queryByText("Lap 1")).toBeNull();
    expect(screen.queryByText("Lap 5")).toBeNull();
  });

  it("falls back to 1..N numbering when any lap in the session lacks a lap_no (legacy)", async () => {
    pushStatus("S1", 3);
    vi.mocked(getSessionLaps).mockResolvedValue([
      { id: 1, lap_time: 95.0, valid: 1, ts: 100, lap_no: 2 },
      { id: 2, lap_time: 94.0, valid: 1, ts: 200, lap_no: null },
      { id: 3, lap_time: 93.0, valid: 1, ts: 300, lap_no: 4 },
    ]);

    render(LapList);

    for (const n of [1, 2, 3]) {
      expect(await screen.findByText(`Lap ${n}`)).toBeTruthy();
    }
  });

  it("shows a best-lap header with the fastest lap time", async () => {
    pushStatus("S1", 6);
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
    pushStatus("S1", 5); // live session already running
    vi.mocked(getSessionLaps).mockImplementation(async (sid: string) =>
      sid === "OLD"
        ? [{ id: 42, lap_time: 210.0, valid: 1, ts: 300 }]
        : [{ id: 9, lap_time: 216.0, valid: 1, ts: 100 }],
    );

    const onlive = vi.fn();
    const { container } = render(LapList, { props: { sessionId: "OLD", onlive } });

    // Holds on OLD (3:30.000) while live lap_id stays at the baseline (5).
    // Numbering is per-phase positional now, so discriminate by the row's time.
    await vi.waitFor(() =>
      expect(container.querySelector(".lap .t")?.textContent).toBe("3:30.000"),
    );
    pushStatus("S1", 5); // another tick at the baseline changes nothing
    expect(container.querySelector(".lap .t")?.textContent).toBe("3:30.000");

    // A new live lap arrives -> snap to the live session (3:36.000).
    pushStatus("S1", 6);
    await vi.waitFor(() =>
      expect(container.querySelector(".lap .t")?.textContent).toBe("3:36.000"),
    );
    // OLD -> S1 is a real switch of the followed session -> reported once.
    expect(onlive).toHaveBeenCalledTimes(1);
    expect(onlive).toHaveBeenCalledWith({ session_id: "S1" });
  });

  it("a pinned past session is queried once, not once per status frame", async () => {
    pushStatus("S1", 5);
    vi.mocked(getSessionLaps).mockImplementation(async (sid: string) =>
      sid === "OLD"
        ? [{ id: 42, lap_time: 210.0, valid: 1, ts: 300 }]
        : [{ id: 9, lap_time: 216.0, valid: 1, ts: 100 }],
    );
    const { container } = render(LapList, { props: { sessionId: "OLD" } });
    await vi.waitFor(() =>
      expect(container.querySelector(".lap .t")?.textContent).toBe("3:30.000"),
    );
    const before = vi.mocked(getSessionLaps).mock.calls.filter(([sid]) => sid === "OLD").length;
    // Three more 1 Hz status frames while the hold stays (same live session, same lap).
    pushStatus("S1", 5);
    pushStatus("S1", 5);
    pushStatus("S1", 5);
    await new Promise((r) => setTimeout(r, 30));
    const after = vi.mocked(getSessionLaps).mock.calls.filter(([sid]) => sid === "OLD").length;
    expect(after).toBe(before);
    expect(container.querySelector(".lap .t")?.textContent).toBe("3:30.000");
  });

  it("pinned session, live session id changes: onlive fires once with the new id (snaps even before the baseline lap advances)", async () => {
    pushStatus("S1", 5);
    vi.mocked(getSessionLaps).mockImplementation(async (sid: string) =>
      sid === "OLD"
        ? [{ id: 42, lap_time: 210.0, valid: 1, ts: 300 }]
        : sid === "S1"
          ? [{ id: 9, lap_time: 216.0, valid: 1, ts: 100 }]
          : [{ id: 10, lap_time: 220.0, valid: 1, ts: 100 }], // S2
    );

    const onlive = vi.fn();
    const { container } = render(LapList, { props: { sessionId: "OLD", onlive } });

    await vi.waitFor(() =>
      expect(container.querySelector(".lap .t")?.textContent).toBe("3:30.000"), // OLD
    );

    // The live session id itself changes (same lap_id 5) -> the new-session-id
    // rule alone must end the hold, even though no new lap has arrived.
    pushStatus("S2", 5);
    await vi.waitFor(
      () => expect(container.querySelector(".lap .t")?.textContent).toBe("3:40.000"), // S2
    );
    expect(onlive).toHaveBeenCalledTimes(1);
    expect(onlive).toHaveBeenCalledWith({ session_id: "S2" });
  });

  it("no pinned session, live session changes: onlive fires with the new id", async () => {
    pushStatus("S1", 5);
    vi.mocked(getSessionLaps).mockImplementation(async (sid: string) =>
      sid === "S1"
        ? [{ id: 9, lap_time: 216.0, valid: 1, ts: 100 }]
        : [{ id: 10, lap_time: 220.0, valid: 1, ts: 100 }],
    );

    const onlive = vi.fn();
    const { container } = render(LapList, { props: { onlive } });

    await vi.waitFor(() =>
      expect(container.querySelector(".lap .t")?.textContent).toBe("3:36.000"), // S1
    );

    pushStatus("S2", 6);
    await vi.waitFor(
      () => expect(container.querySelector(".lap .t")?.textContent).toBe("3:40.000"), // S2
    );
    expect(onlive).toHaveBeenCalledTimes(1);
    expect(onlive).toHaveBeenCalledWith({ session_id: "S2" });
  });

  it("cold start does not emit onlive (no prior followed session to switch away from)", async () => {
    pushStatus(null, null);
    vi.mocked(getSessionLaps).mockResolvedValue([{ id: 9, lap_time: 216.0, valid: 1, ts: 100 }]);

    const onlive = vi.fn();
    render(LapList, { props: { onlive } });
    await screen.findByText(/no laps yet/i);

    pushStatus("S1", 1);

    await screen.findByText("Lap 1");
    expect(onlive).not.toHaveBeenCalled();
  });

  it("recovers from a transient lap-fetch failure on the next frame", async () => {
    pushStatus("S1", 5);
    vi.mocked(getSessionLaps)
      .mockRejectedValueOnce(new Error("network"))
      .mockResolvedValue([{ id: 9, lap_time: 216.0, valid: 1, ts: 100 }]);

    render(LapList);
    await screen.findByText(/connecting/i);

    pushStatus("S1", 6);

    expect(await screen.findByText("Lap 1")).toBeTruthy();
  });

  it("shows an unreachable state with a Retry button when nothing arrives, and Retry recovers", async () => {
    // No frame at all: after the grace window (retryTries x retryDelayMs) the
    // list says the server is unreachable rather than spinning forever.
    render(LapList, { props: { retryTries: 2, retryDelayMs: 5 } });

    const retryBtn = await screen.findByRole("button", { name: /retry/i });
    expect(screen.getByText(/can't reach the coach server/i)).toBeTruthy();

    vi.mocked(getSessionLaps).mockResolvedValue([{ id: 9, lap_time: 216.0, valid: 1, ts: 100 }]);
    await fireEvent.click(retryBtn);
    pushStatus("S1", 5);

    expect(await screen.findByText("Lap 1")).toBeTruthy();
  });

  it("shows new laps when the next frame arrives", async () => {
    pushStatus("S1", 5);
    vi.mocked(getSessionLaps).mockResolvedValueOnce([{ id: 9, lap_time: 216.0, valid: 1, ts: 100 }]);

    render(LapList);

    expect(await screen.findByText("Lap 1")).toBeTruthy();
    expect(screen.queryByText("Lap 2")).toBeNull();

    vi.mocked(getSessionLaps).mockResolvedValue([
      { id: 9, lap_time: 216.0, valid: 1, ts: 100 },
      { id: 10, lap_time: 214.0, valid: 1, ts: 200 },
    ]);
    pushStatus("S1", 6);

    expect(await screen.findByText("Lap 2")).toBeTruthy();
  });

  it("keeps last good laps and does not go unreachable on a transient failure", async () => {
    pushStatus("S1", 5);
    vi.mocked(getSessionLaps).mockResolvedValue([{ id: 9, lap_time: 216.0, valid: 1, ts: 100 }]);

    render(LapList);
    expect(await screen.findByText("Lap 1")).toBeTruthy();

    vi.mocked(getSessionLaps).mockRejectedValueOnce(new Error("transient"));
    pushStatus("S1", 6);
    await vi.waitFor(() => expect(vi.mocked(getSessionLaps).mock.calls.length).toBeGreaterThan(1));

    expect(screen.getByText("Lap 1")).toBeTruthy();
    expect(screen.queryByText(/can't reach the coach server/i)).toBeNull();
  });

  it("unsubscribes from the feed on unmount", async () => {
    pushStatus("S1", 5);
    vi.mocked(getSessionLaps).mockResolvedValue([{ id: 9, lap_time: 216.0, valid: 1, ts: 100 }]);

    const before = eventFeed.status.subscriberCount;
    const { unmount } = render(LapList);
    await screen.findByText("Lap 1");
    expect(eventFeed.status.subscriberCount).toBeGreaterThan(before);

    unmount();
    expect(eventFeed.status.subscriberCount).toBe(before);
    expect(eventFeed.laps.subscriberCount).toBe(0);

    // Frames after unmount do no work.
    const calls = vi.mocked(getSessionLaps).mock.calls.length;
    pushStatus("S1", 6);
    await new Promise((r) => setTimeout(r, 20));
    expect(vi.mocked(getSessionLaps).mock.calls.length).toBe(calls);
  });
  // --- issue #77: out/in/partial laps are listed, greyed, and never the best ----

  /** The rig's shape: out lap, two timed laps, in-lap -- game lap numbers 1..4. */
  const partialRun = [
    { id: 11, lap_time: null, valid: 0, ts: 10, lap_no: 1, partial: "out" as const },
    { id: 12, lap_time: 95.0, valid: 1, ts: 20, lap_no: 2 },
    { id: 13, lap_time: 92.0, valid: 1, ts: 30, lap_no: 3 },
    { id: 14, lap_time: null, valid: 0, ts: 40, lap_no: 4, partial: "in" as const },
  ];

  it("renders a partial lap greyed, with its kind instead of a time, still selectable", async () => {
    pushStatus("S1", 14);
    vi.mocked(getSessionLaps).mockResolvedValue(partialRun);

    const onselect = vi.fn();
    render(LapList, { props: { onselect } });

    const outRow = await screen.findByText("Lap 1");
    expect(screen.getByText("out lap")).toBeTruthy();
    expect(screen.getByText("in lap")).toBeTruthy();
    // Greyed via the shared muted class on the row, not a new colour.
    expect(outRow.closest(".lap")?.classList.contains("partial")).toBe(true);
    // A spin lap's inputs + corner panel are the point of storing it: still clickable.
    await fireEvent.click(outRow);
    expect(onselect).toHaveBeenCalledWith({ lap_id: 11, session_id: "S1" });
  });

  it("numbering runs 1,2,3,4 with no gap and no lap shows a bogus time", async () => {
    pushStatus("S1", 14);
    vi.mocked(getSessionLaps).mockResolvedValue(partialRun);

    render(LapList);
    await screen.findByText("Lap 1");
    for (const n of [1, 2, 3, 4]) expect(screen.getByText(`Lap ${n}`)).toBeTruthy();
    expect(screen.queryByText("0:00.000")).toBeNull();
    expect(screen.queryByText("NaN:NaN")).toBeNull();
  });

  it("a partial lap is never the best lap", async () => {
    pushStatus("S1", 14);
    vi.mocked(getSessionLaps).mockResolvedValue(partialRun);

    render(LapList);
    const best = await screen.findByText("Lap 3");          // 92.0, the only real best
    expect(best.closest(".lap")?.classList.contains("best")).toBe(true);
    expect(screen.getByText("Lap 1").closest(".lap")?.classList.contains("best")).toBe(false);
    // The Best header reads the real lap, not a null.
    expect(document.querySelector(".best-head .t")?.textContent).toBe("1:32.000");
  });

  // --- issue #142: lap numbering restarts per RACE, not just per phase --------

  it("two gap-only race windows: numbering restarts at 1 and a divider names each race", async () => {
    // No non-race lap between the two races -- only race_index tells them apart
    // (the case the old phase-only grouping got wrong).
    pushStatus("S1", 4);
    vi.mocked(getSessionLaps).mockResolvedValue([
      { id: 1, lap_time: 95.0, valid: 1, ts: 100, phase: "race", race_index: 1 },
      { id: 2, lap_time: 94.0, valid: 1, ts: 200, phase: "race", race_index: 1 },
      { id: 3, lap_time: 93.0, valid: 1, ts: 1600, phase: "race", race_index: 2 },
      { id: 4, lap_time: 92.5, valid: 1, ts: 1700, phase: "race", race_index: 2 },
    ]);

    const { container } = render(LapList);

    await screen.findByText(/Race 1 — started/);
    expect(screen.getByText(/Race 2 — started/)).toBeTruthy();
    expect(screen.queryByText("RACE")).toBeNull();
    // Numbering restarts per race: two "Lap 1" rows (one per race).
    expect(screen.getAllByText("Lap 1")).toHaveLength(2);
    expect(container.querySelectorAll(".phase-head")).toHaveLength(2);
  });

  it("a single race renders unchanged: no divider, plain RACE header", async () => {
    pushStatus("S1", 2);
    vi.mocked(getSessionLaps).mockResolvedValue([
      { id: 1, lap_time: 95.0, valid: 1, ts: 100, phase: "race", race_index: 1 },
      { id: 2, lap_time: 94.0, valid: 1, ts: 200, phase: "race", race_index: 1 },
    ]);

    render(LapList);

    await screen.findByText("RACE");
    expect(screen.queryByText(/Race 1 — started/)).toBeNull();
    expect(screen.getAllByText(/Lap [12]/)).toHaveLength(2);
  });

  it("mixes practice, qualifying and two races: groups and numbers correctly", async () => {
    pushStatus("S1", 6);
    vi.mocked(getSessionLaps).mockResolvedValue([
      { id: 1, lap_time: 100.0, valid: 1, ts: 100, phase: "practice", race_index: null },
      { id: 2, lap_time: 99.0, valid: 1, ts: 200, phase: "qualifying", race_index: null },
      { id: 3, lap_time: 95.0, valid: 1, ts: 300, phase: "race", race_index: 1 },
      { id: 4, lap_time: 94.0, valid: 1, ts: 400, phase: "race", race_index: 1 },
      { id: 5, lap_time: 93.0, valid: 1, ts: 1900, phase: "race", race_index: 2 },
      { id: 6, lap_time: 92.5, valid: 1, ts: 2000, phase: "race", race_index: 2 },
    ]);

    const { container } = render(LapList);

    await screen.findByText("PRACTICE");
    expect(screen.getByText("QUALIFYING")).toBeTruthy();
    expect(screen.getByText(/Race 1 — started/)).toBeTruthy();
    expect(screen.getByText(/Race 2 — started/)).toBeTruthy();
    expect(container.querySelectorAll(".phase-head")).toHaveLength(4);
    // Four groups (practice, qualifying, race 1, race 2), each restarting at Lap 1.
    expect(screen.getAllByText("Lap 1")).toHaveLength(4); // practice, qualifying, race 1, race 2
    expect(screen.getAllByText("Lap 2")).toHaveLength(2); // race 1, race 2
  });

  /** Mirrors LapList's own formatTime, to disambiguate rows by displayed time. */
  function fmtTime(seconds: number): string {
    const totalMs = Math.round(seconds * 1000);
    const m = Math.floor(totalMs / 60000);
    const s = ((totalMs - m * 60000) / 1000).toFixed(3);
    return `${m}:${s.padStart(6, "0")}`;
  }

  // The lap number the UI shows must equal what the backend's _lap_numbering would
  // produce for the same rows -- a fixture built from the algorithm's own rules
  // (restart per phase AND per race window; prefer lap_no when the whole run has
  // one), so the two can't silently drift apart (#142).
  it("UI lap numbers match the backend's _lap_numbering rules, built from a fixture", async () => {
    // No lap_no on any race row (legacy capture): numbering falls back to 1..N
    // WITHIN each run, so this only reads right if the run boundary is per race
    // window, not per phase -- the case #142's phase-only grouping got wrong.
    const rows = [
      { id: 1, lap_time: 100.0, valid: 1, ts: 100, phase: "practice", lap_no: null },
      { id: 2, lap_time: 95.0, valid: 1, ts: 300, phase: "race", race_index: 1, lap_no: null },
      { id: 3, lap_time: 94.0, valid: 1, ts: 400, phase: "race", race_index: 1, lap_no: null },
      { id: 4, lap_time: 93.0, valid: 1, ts: 1900, phase: "race", race_index: 2, lap_no: null },
      { id: 5, lap_time: 92.5, valid: 1, ts: 2000, phase: "race", race_index: 2, lap_no: null },
    ];
    // Expected label per lap id, independently derived from _lap_numbering's rule
    // (run = consecutive same phase AND same race window; no lap_no on any race
    // row here, so each run counts 1..N on its own).
    const expected: Record<number, number> = { 1: 1, 2: 1, 3: 2, 4: 1, 5: 2 };

    pushStatus("S1", 5);
    vi.mocked(getSessionLaps).mockResolvedValue(rows);
    render(LapList);

    for (const [id, n] of Object.entries(expected)) {
      const row = rows.find((r) => r.id === Number(id))!;
      const els = await screen.findAllByText(`Lap ${n}`);
      // At least one rendered "Lap n" belongs to a row with this id's lap_time,
      // since ids aren't shown directly -- disambiguate via the row's time text.
      const match = els.some((el) => el.closest(".lap")?.querySelector(".t")?.textContent ===
        fmtTime(row.lap_time));
      expect(match).toBe(true);
    }
  });

  it("a partial lap never becomes the sector-delta reference", async () => {
    // A null lap_time compares as 0 in JS, so an unguarded `<` makes the partial the
    // reference and every real lap reads as slower. Give it sectors to be sure it is
    // considered at all.
    pushStatus("S1", 14);
    vi.mocked(getSessionLaps).mockResolvedValue([
      { id: 21, lap_time: null, valid: 0, ts: 10, lap_no: 1, partial: "in" as const,
        s1: 1.0, s2: 1.0, s3: 1.0 },
      { id: 22, lap_time: 95.0, valid: 1, ts: 20, lap_no: 2, s1: 30.0, s2: 32.0, s3: 33.0 },
    ]);

    render(LapList);
    await screen.findByText("Lap 2");
    // Lap 2 IS the reference, so each of its sectors reads "best", not a huge loss.
    // Both rows carry sectors here, so target lap 2's own toggle.
    await fireEvent.click(screen.getAllByLabelText("Toggle sector detail")[1]);
    expect(screen.getAllByText("best").length).toBe(3);
  });
});
