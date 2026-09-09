import { render, screen, fireEvent, cleanup } from "@testing-library/svelte";
import { test, expect, vi, afterEach } from "vitest";
import TracksScreen from "./TracksScreen.svelte";
import * as api from "./api";

afterEach(cleanup);

vi.spyOn(api, "listTracks").mockResolvedValue([
  { slug: "algarve_international_circuit", name: "Algarve", has_map: true, has_notes: false, user_edited: false },
]);
vi.spyOn(api, "getTrackNotes").mockResolvedValue({ overview: "", corners: {}, reference_times: {}, source: "" });
vi.spyOn(api, "getTrackMap").mockResolvedValue({});

test("lists tracks with coverage badges", async () => {
  render(TracksScreen);
  expect(await screen.findByText("Algarve")).toBeTruthy();
  expect(screen.getByText(/no notes/i)).toBeTruthy(); // has_notes:false badge
});

test("generate draft fills the notes editor", async () => {
  vi.spyOn(api, "draftNotes").mockResolvedValue({
    draft: { overview: "rhythm track", corners: { "1": "brake late" }, reference_times: {}, source: "draft" },
  });
  render(TracksScreen);
  await fireEvent.click(await screen.findByText("Algarve"));
  await fireEvent.click(screen.getByRole("button", { name: /generate draft/i }));
  expect(await screen.findByDisplayValue(/rhythm track/)).toBeTruthy();
});

test("generate draft preserves saved reference times", async () => {
  vi.spyOn(api, "getTrackNotes").mockResolvedValue({
    overview: "long straight into a heavy braking chicane",
    corners: { "1": "brake late" },
    reference_times: { GT3: "1:32.500" },
    source: "driver notes",
  });
  vi.spyOn(api, "draftNotes").mockResolvedValue({
    draft: { overview: "rhythm track", corners: { "1": "brake late" }, reference_times: {}, source: "draft" },
  });
  render(TracksScreen);
  await fireEvent.click(await screen.findByText("Algarve"));
  await fireEvent.click(screen.getByRole("button", { name: /generate draft/i }));
  await screen.findByDisplayValue(/rhythm track/);
  expect((screen.getByLabelText("reference time GT3") as HTMLInputElement).value).toBe("1:32.500");
});

test("selecting a track pre-fills the editor from its existing notes", async () => {
  vi.spyOn(api, "getTrackNotes").mockResolvedValue({
    overview: "long straight into a heavy braking chicane",
    corners: { "1": "brake late" },
    reference_times: { GT3: "1:32.500" },
    source: "driver notes",
  });
  render(TracksScreen);
  await fireEvent.click(await screen.findByText("Algarve"));
  expect(await screen.findByDisplayValue(/long straight into a heavy braking chicane/)).toBeTruthy();
});

test("shows the map pane empty state when the track has no layout", async () => {
  render(TracksScreen);
  await fireEvent.click(await screen.findByText("Algarve"));
  expect(await screen.findByText(/no layout for this track yet/i)).toBeTruthy();
});

function fourCornerMap() {
  const mk = (n: number) => ({
    number: n, label: String(n), name: `Turn ${n}`, direction: "right",
    x: n, y: n, apex_dist: n, apexes: [n], start_dist: 0, end_dist: 10,
  });
  return {
    width: 100, height: 100, path: [[0, 0], [1, 1]], dist: [0, 1], sectors: [], sections: [],
    corners: [mk(1), mk(2), mk(3), mk(4)],
  } as unknown as api.TrackMap;
}

test("corner-notes list is driven by the map, true orphans flagged", async () => {
  vi.spyOn(api, "getTrackNotes").mockResolvedValue({
    overview: "", corners: { "1": "brake late", "9": "stale note" }, reference_times: {}, source: "",
  });
  vi.spyOn(api, "getTrackMap").mockResolvedValue(fourCornerMap());
  render(TracksScreen);
  await fireEvent.click(await screen.findByText("Algarve"));
  expect(await screen.findByLabelText("corner note 1")).toBeTruthy();
  expect(screen.getByLabelText("corner note 4")).toBeTruthy();
  // "9" matches no corner and no range -> surfaced as orphan, not dropped.
  expect(screen.getByText(/match no corner/i)).toBeTruthy();
  expect(screen.getByLabelText("corner note 9")).toBeTruthy();
});

test("a hyphen-range note collapses its covered corners into one row", async () => {
  vi.spyOn(api, "getTrackNotes").mockResolvedValue({
    overview: "", corners: { "1": "a", "2-3": "double apex", "4": "d" }, reference_times: {}, source: "",
  });
  vi.spyOn(api, "getTrackMap").mockResolvedValue(fourCornerMap());
  render(TracksScreen);
  await fireEvent.click(await screen.findByText("Algarve"));
  // Corners 2 and 3 collapse under the "2-3" range row; no separate 2/3 rows.
  expect(await screen.findByLabelText("corner note 2-3")).toBeTruthy();
  expect(screen.queryByLabelText("corner note 2")).toBeNull();
  expect(screen.queryByLabelText("corner note 3")).toBeNull();
  expect(screen.getByLabelText("corner note 1")).toBeTruthy();
  expect(screen.getByLabelText("corner note 4")).toBeTruthy();
  // The range is valid -> no orphan warning.
  expect(screen.queryByText(/match no corner/i)).toBeNull();
});

test("a range this screen cannot bind is flagged, not collapsed", async () => {
  // The grammar here must match `note_key_binds` in simcoach/track_lookup.py, which is
  // the guard deciding which per-corner notes survive a map save. Anything this screen
  // collapses under a range row but that guard drops is authored prose deleted with no
  // warning, so the strictness is the point: a 4-digit endpoint ('1-1000'), a span over
  // the cap ('1-101') and a descending key ('3-1') are all NOT ranges, and so show as
  // orphans rather than silently swallowing corners 1-4.
  vi.spyOn(api, "getTrackNotes").mockResolvedValue({
    overview: "", corners: { "1-1000": "a", "1-101": "b", "3-1": "c" },
    reference_times: {}, source: "",
  });
  vi.spyOn(api, "getTrackMap").mockResolvedValue(fourCornerMap());
  render(TracksScreen);
  await fireEvent.click(await screen.findByText("Algarve"));
  for (const n of [1, 2, 3, 4]) {
    expect(await screen.findByLabelText(`corner note ${n}`)).toBeTruthy();   // own row
  }
  expect(screen.getByText(/match no corner/i)).toBeTruthy();
});

test("a label Number() would coerce does not collapse under a range", async () => {
  // '0007' is 7 to Number(), but the Python guard bounds a corner number to 3 digits, so
  // collapsing corner '0007' under '5-9' showed the note as bound here and had it
  // dropped on the next map save. Same for ' 7 ', '7.0', '+7', '0x7', '1e1' and ''.
  const map = fourCornerMap();
  (map.corners as unknown as { label: string }[])[2].label = "0007";
  vi.spyOn(api, "getTrackNotes").mockResolvedValue({
    overview: "", corners: { "5-9": "the complex" }, reference_times: {}, source: "",
  });
  vi.spyOn(api, "getTrackMap").mockResolvedValue(map);
  render(TracksScreen);
  await fireEvent.click(await screen.findByText("Algarve"));
  expect(await screen.findByLabelText("corner note 0007")).toBeTruthy();   // its own row
  expect(screen.getByText(/match no corner/i)).toBeTruthy();   // '5-9' flagged, not bound
});

test("save sends the edited notes for the selected track", async () => {
  const saveSpy = vi.spyOn(api, "saveNotes").mockResolvedValue({ ok: true });
  render(TracksScreen);
  await fireEvent.click(await screen.findByText("Algarve"));

  const overview = screen.getByLabelText(/overview/i) as HTMLTextAreaElement;
  await fireEvent.input(overview, { target: { value: "fast flowing circuit" } });
  await fireEvent.click(screen.getByRole("button", { name: /^save$/i }));

  expect(saveSpy).toHaveBeenCalledWith(
    "algarve_international_circuit",
    "Algarve",
    expect.objectContaining({ overview: "fast flowing circuit" }),
  );
});
