import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup, within } from "@testing-library/svelte";
import { getSessions } from "./api";
import SessionsScreen from "./SessionsScreen.svelte";

function sessList(container: HTMLElement) {
  return container.querySelector(".sess-list") as HTMLElement;
}

vi.mock("./api", async () => {
  const actual = await vi.importActual<typeof import("./api")>("./api");
  return { ...actual, getSessions: vi.fn() };
});

const SESSIONS = [
  { session_id: "s1", track: "Le Mans", car: "Corvette Z06", laps: 12, best: 216.512, started: 1_725_000_000, last: 1_725_003_600 },
  { session_id: "s2", track: "Le Mans", car: "Ferrari 296", laps: 8, best: 220.1, started: 1_725_100_000, last: 1_725_103_600 },
  { session_id: "s3", track: "Sebring", car: "Corvette Z06", laps: 5, best: null, started: 1_725_200_000, last: 1_725_203_600 },
];

describe("SessionsScreen", () => {
  beforeEach(() => {
    vi.mocked(getSessions).mockReset();
  });

  afterEach(cleanup);

  it("renders session rows and filter dropdowns from the API payload", async () => {
    vi.mocked(getSessions).mockResolvedValue(SESSIONS);

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
    vi.mocked(getSessions).mockResolvedValue(SESSIONS);

    const { container } = render(SessionsScreen);
    await within(sessList(container)).findByText("Sebring");

    const trackSelect = screen.getByRole("combobox", { name: /track/i }) as HTMLSelectElement;
    await fireEvent.change(trackSelect, { target: { value: "Sebring" } });

    const list = within(sessList(container));
    expect(list.queryByText("Le Mans")).toBeNull();
    expect(list.getByText("Sebring")).toBeTruthy();
  });

  it("fires onselectSession with the session id when a row is clicked", async () => {
    vi.mocked(getSessions).mockResolvedValue(SESSIONS);
    const onselectSession = vi.fn();

    const { container } = render(SessionsScreen, { props: { onselectSession } });
    await within(sessList(container)).findByText("Sebring");

    const row = container.querySelectorAll(".srow")[0] as HTMLElement;
    await fireEvent.click(row);

    expect(onselectSession).toHaveBeenCalledWith("s1");
  });

  it("shows an empty state when there are no sessions", async () => {
    vi.mocked(getSessions).mockResolvedValue([]);

    render(SessionsScreen);

    expect(await screen.findByText(/no sessions yet/i)).toBeTruthy();
  });

  it("shows a select-a-track placeholder in the chart before a track is chosen", async () => {
    vi.mocked(getSessions).mockResolvedValue(SESSIONS);

    const { container } = render(SessionsScreen);
    await within(sessList(container)).findByText("Sebring");

    expect(screen.getByText(/select a track/i)).toBeTruthy();
    expect(document.querySelector("svg")).toBeNull();
  });

  it("renders the trend chart once a specific track is chosen", async () => {
    vi.mocked(getSessions).mockResolvedValue(SESSIONS);

    const { container } = render(SessionsScreen);
    await within(sessList(container)).findByText("Sebring");

    const trackSelect = screen.getByRole("combobox", { name: /track/i }) as HTMLSelectElement;
    await fireEvent.change(trackSelect, { target: { value: "Le Mans" } });

    expect(document.querySelector("svg")).toBeTruthy();
  });

  it("formats best lap times as m:ss.mmm", async () => {
    vi.mocked(getSessions).mockResolvedValue(SESSIONS);

    const { container } = render(SessionsScreen);

    expect(await within(sessList(container)).findByText("3:36.512")).toBeTruthy();
  });
});
