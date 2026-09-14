import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup, within, waitFor } from "@testing-library/svelte";
import { getSessions, getSessionsSummary, getSessionChats, type SessionsFilter } from "./api";
import SessionsScreen from "./SessionsScreen.svelte";

function sessList(container: HTMLElement) {
  return container.querySelector(".sess-list") as HTMLElement;
}

vi.mock("./api", async () => {
  const actual = await vi.importActual<typeof import("./api")>("./api");
  return {
    ...actual,
    getSessions: vi.fn(),
    getSessionsSummary: vi.fn(),
    getSessionChats: vi.fn(),
  };
});

const SESSIONS = [
  { session_id: "s1", track: "Le Mans", car: "Corvette Z06", laps: 12, best: 216.512, started: 1_725_000_000, last: 1_725_003_600 },
  { session_id: "s2", track: "Le Mans", car: "Ferrari 296", laps: 8, best: 220.1, started: 1_725_100_000, last: 1_725_103_600 },
  { session_id: "s3", track: "Sebring", car: "Corvette Z06", laps: 5, best: null, started: 1_725_200_000, last: 1_725_203_600 },
];

// A fake backend over a fixture: filters + paginates like the real /sessions,
// and aggregates like /sessions/summary, so the component's server-driven flow
// is exercised end to end.
function fakeBackend(data = SESSIONS) {
  const match = (f: SessionsFilter) =>
    data.filter((s) => (!f.track || s.track === f.track) && (!f.car || s.car === f.car));

  vi.mocked(getSessions).mockImplementation(async (f: SessionsFilter = {}) => {
    const m = match(f);
    const offset = f.offset ?? 0;
    const limit = f.limit ?? 30;
    return {
      rows: m.slice(offset, offset + limit),
      total: m.length,
      tracks: [...new Set(data.map((s) => s.track))].sort(),
      cars: [...new Set(data.map((s) => s.car))],
    };
  });

  vi.mocked(getSessionsSummary).mockImplementation(async (f: SessionsFilter = {}) => {
    const m = match(f);
    const withBest = m.filter((s) => s.best != null) as (typeof m[number] & { best: number })[];
    const bests = withBest.map((s) => s.best);
    let series: { car: string; points: { x: number; value: number }[] }[] = [];
    if (f.track) {
      const byCar: Record<string, { x: number; value: number }[]> = {};
      [...withBest].sort((a, b) => a.started - b.started).forEach((s) => {
        (byCar[s.car] ||= []).push({ x: s.started, value: s.best });
      });
      series = Object.entries(byCar).map(([car, points]) => ({ car, points }));
    }
    return {
      count: m.length,
      cars: new Set(m.map((s) => s.car)).size,
      personal_best: bests.length ? Math.min(...bests) : null,
      consistency: bests.length >= 2 ? (Math.max(...bests) - Math.min(...bests)) / 2 : null,
      improvement: null,
      series,
    };
  });
}

describe("SessionsScreen", () => {
  beforeEach(() => {
    vi.mocked(getSessions).mockReset();
    vi.mocked(getSessionsSummary).mockReset();
    vi.mocked(getSessionChats).mockReset();
    vi.mocked(getSessionChats).mockResolvedValue([]);
    fakeBackend();
  });

  afterEach(cleanup);

  it("renders session rows and filter dropdowns from the API payload", async () => {
    const { container } = render(SessionsScreen);
    await screen.findByRole("combobox", { name: /track/i });
    const list = within(sessList(container));

    expect((await list.findAllByText("Le Mans")).length).toBe(2);
    expect(list.getByText("Sebring")).toBeTruthy();
    expect(list.getAllByText("Corvette Z06").length).toBe(2);
    expect(screen.getByRole("combobox", { name: /track/i })).toBeTruthy();
    expect(screen.getByRole("combobox", { name: /car/i })).toBeTruthy();
  });

  it("narrows the session list when the track filter changes", async () => {
    const { container } = render(SessionsScreen);
    await within(sessList(container)).findByText("Sebring");

    const trackSelect = screen.getByRole("combobox", { name: /track/i }) as HTMLSelectElement;
    await fireEvent.change(trackSelect, { target: { value: "Sebring" } });

    const list = within(sessList(container));
    await waitFor(() => expect(list.queryByText("Le Mans")).toBeNull());
    expect(list.getByText("Sebring")).toBeTruthy();
    expect(getSessions).toHaveBeenCalledWith(expect.objectContaining({ track: "Sebring" }));
  });

  it("fires onselectSession with the session id and its linked chat id when a row is clicked", async () => {
    vi.mocked(getSessionChats).mockResolvedValue([
      { chat_id: "chatA", started: 1, last: 5, message_count: 3, tracks: "Le Mans" },
    ]);
    const onselectSession = vi.fn();

    const { container } = render(SessionsScreen, { props: { onselectSession } });
    await within(sessList(container)).findByText("Sebring");

    const row = container.querySelectorAll(".srow")[0] as HTMLElement;
    await fireEvent.click(row);

    await waitFor(() => expect(onselectSession).toHaveBeenCalledWith("s1", "chatA"));
    expect(getSessionChats).toHaveBeenCalledWith("s1");
  });

  it("ignores a stale resolve from a superseded click (#98)", async () => {
    // Two rapid clicks (s1 then s2); s1's getSessionChats resolves LAST. Without a
    // click token, s1's late resolve would fire onselectSession again and steal
    // focus back to the wrong session.
    let resolveFirst!: (v: { chat_id: string; started: number; last: number; message_count: number; tracks: string }[]) => void;
    const firstPromise = new Promise<
      { chat_id: string; started: number; last: number; message_count: number; tracks: string }[]
    >((res) => { resolveFirst = res; });
    vi.mocked(getSessionChats).mockImplementation(async (sid: string) =>
      sid === "s1" ? firstPromise
        : [{ chat_id: "chatB", started: 1, last: 5, message_count: 1, tracks: "Le Mans" }],
    );
    const onselectSession = vi.fn();

    const { container } = render(SessionsScreen, { props: { onselectSession } });
    await within(sessList(container)).findByText("Sebring");

    const rows = container.querySelectorAll(".srow");
    await fireEvent.click(rows[0] as HTMLElement); // s1 -- pending
    await fireEvent.click(rows[1] as HTMLElement); // s2 -- resolves first

    await waitFor(() => expect(onselectSession).toHaveBeenCalledWith("s2", "chatB"));

    resolveFirst([{ chat_id: "chatA", started: 1, last: 5, message_count: 1, tracks: "Le Mans" }]);
    await new Promise((r) => setTimeout(r, 10));

    expect(onselectSession).toHaveBeenCalledTimes(1);
  });

  it("passes chatId undefined when a session has no linked chat", async () => {
    vi.mocked(getSessionChats).mockResolvedValue([]);
    const onselectSession = vi.fn();

    const { container } = render(SessionsScreen, { props: { onselectSession } });
    await within(sessList(container)).findByText("Sebring");

    await fireEvent.click(container.querySelectorAll(".srow")[0] as HTMLElement);

    await waitFor(() => expect(onselectSession).toHaveBeenCalledWith("s1", undefined));
  });

  it("shows an empty state when there are no sessions", async () => {
    fakeBackend([]);

    render(SessionsScreen);

    expect(await screen.findByText(/no sessions yet/i)).toBeTruthy();
  });

  it("shows a select-a-track placeholder in the chart before a track is chosen", async () => {
    const { container } = render(SessionsScreen);
    await within(sessList(container)).findByText("Sebring");

    expect(screen.getByText(/select a track/i)).toBeTruthy();
    expect(document.querySelector("svg")).toBeNull();
  });

  it("renders the trend chart once a specific track is chosen", async () => {
    const { container } = render(SessionsScreen);
    await within(sessList(container)).findByText("Sebring");

    const trackSelect = screen.getByRole("combobox", { name: /track/i }) as HTMLSelectElement;
    await fireEvent.change(trackSelect, { target: { value: "Le Mans" } });

    await waitFor(() => expect(document.querySelector("svg")).toBeTruthy());
  });

  it("formats best lap times as m:ss.mmm", async () => {
    const { container } = render(SessionsScreen);

    expect(await within(sessList(container)).findByText("3:36.512")).toBeTruthy();
  });
});
