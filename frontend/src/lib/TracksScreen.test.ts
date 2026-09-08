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

test("corner-notes list is driven by the map, orphaned notes flagged", async () => {
  vi.spyOn(api, "getTrackNotes").mockResolvedValue({
    overview: "", corners: { "1": "brake late", "9": "stale note" }, reference_times: {}, source: "",
  });
  // A map with corners T1 and T3; note "9" matches no corner -> unmapped.
  vi.spyOn(api, "getTrackMap").mockResolvedValue({
    width: 100, height: 100, path: [[0, 0], [1, 1]], dist: [0, 1], sectors: [], sections: [],
    corners: [
      { number: 1, label: "1", name: "Turn 1", direction: "right", x: 0, y: 0, apex_dist: 0, apexes: [0], start_dist: 0, end_dist: 1 },
      { number: 2, label: "3", name: "Turn 3", direction: "left", x: 1, y: 1, apex_dist: 1, apexes: [1], start_dist: 0, end_dist: 1 },
    ],
  } as unknown as api.TrackMap);
  render(TracksScreen);
  await fireEvent.click(await screen.findByText("Algarve"));
  // Rows come from the map corners (labels 1 and 3), keyed via the textarea aria-label.
  expect(await screen.findByLabelText("corner note 1")).toBeTruthy();
  expect(screen.getByLabelText("corner note 3")).toBeTruthy();
  // Note "9" matches no map corner -> surfaced as unmapped, not dropped.
  expect(screen.getByText(/don't match any corner/i)).toBeTruthy();
  expect(screen.getByLabelText("corner note 9")).toBeTruthy();
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
