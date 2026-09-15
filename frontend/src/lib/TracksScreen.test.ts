import { render, screen, fireEvent, cleanup, within } from "@testing-library/svelte";
import { test, expect, vi, afterEach } from "vitest";
import TracksScreen from "./TracksScreen.svelte";
import * as api from "./api";

afterEach(cleanup);

// What actually goes over the wire. saveNotes is handed the live $state proxy, and that
// proxy answers Object.hasOwn from the RAW TARGET -- so it still claims a key the user
// deleted, while ownKeys (and therefore api.ts's JSON.stringify) correctly drops it.
// Assert on the serialised body, never on the proxy, or the assertion passes for a note
// that was never really removed.
const wire = (o: unknown) => JSON.parse(JSON.stringify(o)) as Record<string, string>;

vi.spyOn(api, "listTracks").mockResolvedValue([
  { slug: "algarve_international_circuit", name: "Algarve", has_map: true, has_notes: false, user_edited: false, captures: 0, live_laps: 0 },
]);
vi.spyOn(api, "getTrackNotes").mockResolvedValue({ overview: "", corners: {}, reference_times: {}, source: "" });
vi.spyOn(api, "getTrackMap").mockResolvedValue({});

// #105: these two run FIRST and in order -- the module-level `cached` var they exercise
// is shared across every test in this file (it lives in TracksScreen.svelte's <script
// module>, loaded once), so the loading-state assertion needs to run before any other
// test's render() has already populated it.
test("shows a loading state instead of 'No tracks yet' while the initial fetch is pending", async () => {
  let resolveList!: (rows: api.TrackRow[]) => void;
  vi.spyOn(api, "listTracks").mockReturnValue(new Promise((r) => { resolveList = r; }));
  render(TracksScreen);
  expect(screen.getByText(/loading tracks/i)).toBeTruthy();
  expect(screen.queryByText(/no tracks yet/i)).toBeNull();
  resolveList([
    { slug: "algarve_international_circuit", name: "Algarve", has_map: true, has_notes: false, user_edited: false, captures: 0, live_laps: 0 },
  ]);
  await screen.findByText("Algarve");
});

test("a re-visit renders the cached rows synchronously before the new fetch resolves", async () => {
  const { unmount } = render(TracksScreen);
  await screen.findByText("Algarve"); // seeded by the previous test's now-cached fetch
  unmount();

  let resolveList!: (rows: api.TrackRow[]) => void;
  vi.spyOn(api, "listTracks").mockReturnValue(new Promise((r) => { resolveList = r; }));
  render(TracksScreen);
  expect(screen.getByText("Algarve")).toBeTruthy(); // from cache, before the new fetch resolves
  resolveList([
    { slug: "algarve_international_circuit", name: "Algarve", has_map: true, has_notes: false, user_edited: false, captures: 0, live_laps: 0 },
  ]);
});

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

test("a track whose map comes back drawn shows it with its corners, and offers Edit, not Add map", async () => {
  // #82: shipped Spa has corners but no stored outline, so GET /tracks/{slug}/map used to
  // return {} and this pane said "no layout" + "Add map". The sidecar now borrows the
  // outline its driven laps draw; the payload is the ordinary map shape, marked shipped.
  const spa = { ...fourCornerMap(), shipped: true, curating: false, track_name: "Circuit de Spa-Francorchamps" };
  vi.spyOn(api, "getTrackMap").mockResolvedValue(spa as unknown as api.TrackMap);
  const { container } = render(TracksScreen);
  await fireEvent.click(await screen.findByText("Algarve"));
  expect(await screen.findByRole("button", { name: "Edit" })).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Add map" })).toBeNull();
  expect(screen.queryByText(/no layout for this track yet/i)).toBeNull();
  const pane = within(container.querySelector(".mappane") as HTMLElement);
  for (const n of [1, 2, 3, 4]) expect(pane.getByText(`Turn ${n}`)).toBeTruthy();
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
  // '07' is 7 to Number() but only '7' to the Python guard's `str(n) in labels` probe,
  // so collapsing corner '07' under '5-9' showed the note as bound here and had it
  // dropped on the next map save. Canonical decimal labels only.
  const map = fourCornerMap();
  (map.corners as unknown as { label: string }[])[2].label = "07";
  vi.spyOn(api, "getTrackNotes").mockResolvedValue({
    overview: "", corners: { "5-9": "the complex" }, reference_times: {}, source: "",
  });
  vi.spyOn(api, "getTrackMap").mockResolvedValue(map);
  render(TracksScreen);
  await fireEvent.click(await screen.findByText("Algarve"));
  expect(await screen.findByLabelText("corner note 07")).toBeTruthy();   // its own row
  expect(screen.getByText(/match no corner/i)).toBeTruthy();   // '5-9' flagged, not bound
});

test("a corner labelled toString or valueOf does not break the screen", async () => {
  // Both names resolve through Object.prototype, so `'toString' in notes.corners` was
  // true without the key existing: the blank-note seed never ran, the row then read
  // Object.prototype.toString -- a function, which `?? ""` keeps, so wordCount() threw
  // "s.trim is not a function" and the whole screen went blank. A corner label is free
  // text in the map editor and `PUT /map` stores both verbatim (its 8-char label cap
  // happens to admit exactly these two), so this is reachable by typing.
  const map = fourCornerMap();
  const labels = map.corners as unknown as { label: string }[];
  labels[0].label = "toString";
  labels[1].label = "valueOf";
  vi.spyOn(api, "getTrackNotes").mockResolvedValue({
    overview: "", corners: {}, reference_times: {}, source: "",
  });
  vi.spyOn(api, "getTrackMap").mockResolvedValue(map);
  render(TracksScreen);
  await fireEvent.click(await screen.findByText("Algarve"));
  for (const key of ["toString", "valueOf"]) {
    const box = (await screen.findByLabelText(`corner note ${key}`)) as HTMLTextAreaElement;
    expect(box.value).toBe("");            // a blank note, never a function's source
  }
  expect(screen.getByLabelText("corner note 3")).toBeTruthy();   // rest of the map intact
  expect(screen.queryByText(/match no corner/i)).toBeNull();

  // ...and the note is still editable and still reaches the save payload under its own
  // key. `Object.hasOwn` on the serialised body, not expect.objectContaining: that
  // matcher walks the prototype chain, so it would report a 'toString' key on any object.
  const saveSpy = vi.spyOn(api, "saveNotes").mockResolvedValue({ ok: true });
  await fireEvent.input(screen.getByLabelText("corner note toString"),
                        { target: { value: "brake here" } });
  await fireEvent.click(screen.getByRole("button", { name: /^save$/i }));
  const sent = wire(saveSpy.mock.lastCall![2].corners);
  expect(Object.hasOwn(sent, "toString")).toBe(true);
  expect(sent["toString"]).toBe("brake here");
});

test("a deleted corner note is gone from the list, the wire body and the object", async () => {
  vi.spyOn(api, "getTrackNotes").mockResolvedValue({
    overview: "", corners: { "1": "brake late", "13": "late apex" },
    reference_times: {}, source: "",
  });
  vi.spyOn(api, "getTrackMap").mockResolvedValue(fourCornerMap());
  const saveSpy = vi.spyOn(api, "saveNotes").mockResolvedValue({ ok: true });
  render(TracksScreen);
  await fireEvent.click(await screen.findByText("Algarve"));
  await fireEvent.click(await screen.findByRole("button", { name: "remove corner note 13" }));
  expect(screen.queryByLabelText("corner note 13")).toBeNull();

  await fireEvent.click(screen.getByRole("button", { name: /^save$/i }));
  const notes = saveSpy.mock.lastCall![2];
  expect(Object.hasOwn(wire(notes.corners), "13")).toBe(false);   // absent on the wire
  expect(wire(notes.corners)["1"]).toBe("brake late");            // the kept note survives
  // And the key is really gone from the OBJECT, not just filtered out of its own keys.
  // An in-place `delete` leaves it on the proxy's target, where Object.hasOwn still finds
  // it -- which is what makes noteOf return undefined and the seed skip re-seeding.
  expect(Object.hasOwn(notes.corners, "13")).toBe(false);
});

test("a deleted note whose corner reappears on the map re-seeds instead of throwing", async () => {
  // The consequence of that dishonest target, end to end. Delete the orphan note "13",
  // then save a layout that HAS a corner 13: onSaved -> reloadMap refetches only the map,
  // so the notes state (with its stale target key) is carried into a render that now has
  // a row keyed "13". Object.hasOwn said true, the seed skipped it, noteOf returned
  // undefined, and wordCount threw "Cannot read properties of undefined (reading 'trim')"
  // -- the same crash this component was fixed for, reintroduced by the delete path.
  const twoCorners = fourCornerMap();
  twoCorners.corners = twoCorners.corners.slice(0, 2);
  const withThirteen = fourCornerMap();
  const thirteen = { ...withThirteen.corners[2], number: 13, label: "13" };
  withThirteen.corners = [...twoCorners.corners, thirteen];
  vi.spyOn(api, "getTrackNotes").mockResolvedValue({
    overview: "", corners: { "13": "late apex" }, reference_times: {}, source: "",
  });
  vi.spyOn(api, "getTrackMap")
    .mockResolvedValue(withThirteen)        // the post-save reload, and anything after
    .mockResolvedValueOnce(twoCorners)      // this screen's first load
    .mockResolvedValueOnce(twoCorners);     // the editor's own load when it opens
  vi.spyOn(api, "saveTrackMap").mockResolvedValue(withThirteen);

  render(TracksScreen);
  await fireEvent.click(await screen.findByText("Algarve"));
  await fireEvent.click(await screen.findByRole("button", { name: "remove corner note 13" }));
  await fireEvent.click(screen.getByRole("button", { name: /^edit$/i }));
  const dialog = within(await screen.findByRole("dialog"));
  await fireEvent.click(await dialog.findByText("Save"));

  const box = (await screen.findByLabelText("corner note 13")) as HTMLTextAreaElement;
  expect(box.value).toBe("");              // re-seeded blank, not undefined
});

test("removing a reference time leaves the rest addable", async () => {
  // Guards the same conversion on reference_times, which has no visible symptom today.
  vi.spyOn(api, "getTrackNotes").mockResolvedValue({
    overview: "", corners: {}, reference_times: { LMP2: "1:30.000" }, source: "",
  });
  vi.spyOn(api, "getTrackMap").mockResolvedValue({});
  const saveSpy = vi.spyOn(api, "saveNotes").mockResolvedValue({ ok: true });
  render(TracksScreen);
  await fireEvent.click(await screen.findByText("Algarve"));
  await fireEvent.click(await screen.findByRole("button", { name: "remove reference time LMP2" }));
  expect(screen.queryByLabelText("reference time LMP2")).toBeNull();

  await fireEvent.input(screen.getByLabelText("New reference-time class"),
                        { target: { value: "LMP2" } });
  await fireEvent.input(screen.getByLabelText("New reference time"),
                        { target: { value: "1:29.000" } });
  await fireEvent.click(screen.getByRole("button", { name: /^add$/i }));
  expect(((await screen.findByLabelText("reference time LMP2")) as HTMLInputElement).value)
    .toBe("1:29.000");
  await fireEvent.click(screen.getByRole("button", { name: /^save$/i }));
  expect(wire(saveSpy.mock.lastCall![2].reference_times)).toEqual({ LMP2: "1:29.000" });
});

test("a reference-time class named toString is actually stored", async () => {
  // The SAME prototype hole, on the wider input: the class box is free text with no cap
  // at all (a corner label is narrowed to {toString, valueOf} by PUT /map's 8-char cap).
  // `notes.reference_times[cls] = v` was silently discarded by the $state proxy for any
  // prototype-shadowed name -- you typed a class and a time, clicked Add, nothing
  // happened. 'toString' stands in for constructor/hasOwnProperty/__proto__/valueOf/
  // toLocaleString, which all failed the same way.
  vi.spyOn(api, "getTrackNotes").mockResolvedValue({
    overview: "", corners: {}, reference_times: {}, source: "",
  });
  const saveSpy = vi.spyOn(api, "saveNotes").mockResolvedValue({ ok: true });
  render(TracksScreen);
  await fireEvent.click(await screen.findByText("Algarve"));
  await fireEvent.input(screen.getByLabelText("New reference-time class"),
                        { target: { value: "toString" } });
  await fireEvent.input(screen.getByLabelText("New reference time"),
                        { target: { value: "1:32.500" } });
  await fireEvent.click(screen.getByRole("button", { name: /^add$/i }));

  const row = (await screen.findByLabelText("reference time toString")) as HTMLInputElement;
  expect(row.value).toBe("1:32.500");                 // the row renders at all
  await fireEvent.click(screen.getByRole("button", { name: /^save$/i }));
  // lastCall, not calls[0]: vi.spyOn returns the mock already installed by an earlier
  // test in this file, calls and all, so calls[0] reads someone else's payload.
  const sent = saveSpy.mock.lastCall![2].reference_times;
  expect(Object.hasOwn(sent, "toString")).toBe(true);  // an own property, not the method
  expect(sent["toString"]).toBe("1:32.500");
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
    null,                                     // the mocked load carried no rev
  );
});

test("save sends back the rev the notes were loaded with, then the rev the save returned", async () => {
  vi.spyOn(api, "getTrackNotes").mockResolvedValue({
    overview: "", corners: {}, reference_times: {}, source: "", rev: "rev-loaded",
  });
  vi.spyOn(api, "getTrackMap").mockResolvedValue({});
  const saveSpy = vi.spyOn(api, "saveNotes")
    .mockResolvedValue({ ok: true, rev: "rev-saved", dropped_notes: {} });
  saveSpy.mockClear();                        // the spy is shared with earlier tests
  render(TracksScreen);
  await fireEvent.click(await screen.findByText("Algarve"));
  await fireEvent.click(screen.getByRole("button", { name: /^save$/i }));
  await vi.waitFor(() => expect(saveSpy).toHaveBeenCalledTimes(1));
  expect(saveSpy.mock.calls[0][3]).toBe("rev-loaded");
  await screen.findByText("Saved.");
  await fireEvent.click(screen.getByRole("button", { name: /^save$/i }));
  await vi.waitFor(() => expect(saveSpy).toHaveBeenCalledTimes(2));
  expect(saveSpy.mock.calls[1][3]).toBe("rev-saved");
});

test("a stale notes save keeps the driver's edits and offers an explicit reload", async () => {
  // The server refuses a save made from notes a map save has since re-keyed (409). The
  // screen must say so WITHOUT replacing what the driver typed -- reloading discards it,
  // so it is a button, never automatic -- and the reload must really refetch.
  vi.spyOn(api, "getTrackMap").mockResolvedValue({});
  vi.spyOn(api, "getTrackNotes")
    .mockResolvedValueOnce({ overview: "as loaded", corners: {}, reference_times: {}, source: "", rev: "r1" })
    .mockResolvedValueOnce({ overview: "as re-keyed on disk", corners: {}, reference_times: {}, source: "", rev: "r2" });
  vi.spyOn(api, "saveNotes").mockRejectedValue(new Error(
    "API request failed: PUT /tracks/algarve_international_circuit/notes -> 409: these notes changed since they were loaded"));
  render(TracksScreen);
  await fireEvent.click(await screen.findByText("Algarve"));
  const overview = (await screen.findByDisplayValue("as loaded")) as HTMLTextAreaElement;
  await fireEvent.input(overview, { target: { value: "my unsaved edit" } });
  await fireEvent.click(screen.getByRole("button", { name: /^save$/i }));

  expect(await screen.findByText(/changed since you opened them/i)).toBeTruthy();
  expect((screen.getByLabelText(/overview/i) as HTMLTextAreaElement).value).toBe("my unsaved edit");

  await fireEvent.click(screen.getByRole("button", { name: /reload notes/i }));
  expect(await screen.findByDisplayValue("as re-keyed on disk")).toBeTruthy();
  expect(screen.queryByRole("button", { name: /reload notes/i })).toBeNull();
  expect(screen.queryByText(/changed since you opened them/i)).toBeNull();
});

test("a save after the notes failed to load says so, not that they changed", async () => {
  // A failed GET leaves no rev, so the server answers 428. Telling the driver the notes
  // "changed since you opened them" would be false -- they never loaded. Edits still stay.
  vi.spyOn(api, "getTrackMap").mockResolvedValue({});
  vi.spyOn(api, "getTrackNotes").mockRejectedValueOnce(new Error("API request failed: GET /tracks/x/notes -> 500"));
  const saveSpy = vi.spyOn(api, "saveNotes").mockRejectedValue(new Error(
    "API request failed: PUT /tracks/algarve_international_circuit/notes -> 428: notes rev required"));
  saveSpy.mockClear();
  render(TracksScreen);
  await fireEvent.click(await screen.findByText("Algarve"));
  const overview = (await screen.findByLabelText(/overview/i)) as HTMLTextAreaElement;
  await fireEvent.input(overview, { target: { value: "typed after a failed load" } });
  await fireEvent.click(screen.getByRole("button", { name: /^save$/i }));

  expect(await screen.findByText(/notes didn't load/i)).toBeTruthy();
  expect(screen.queryByText(/changed since you opened them/i)).toBeNull();
  expect(saveSpy.mock.calls[0][3]).toBeNull();                      // really sent with no rev
  expect((screen.getByLabelText(/overview/i) as HTMLTextAreaElement).value).toBe("typed after a failed load");
  expect(screen.getByRole("button", { name: /reload notes/i })).toBeTruthy();
});

// --- driven-but-unmapped tracks (#69) + visible capture coverage (#66) -----------------
// These override the module-level listTracks spy, so they live LAST in the file: the
// override persists (afterEach only cleans the DOM), and an earlier placement would hand
// every following test a track list it never asked for.

test("a driven-but-unmapped track shows its lap count and a build action", async () => {
  vi.spyOn(api, "listTracks").mockResolvedValue([
    {
      slug: "silverstone_national_circuit", name: "Silverstone National Circuit",
      has_map: false, has_notes: false, user_edited: false, captures: 27, live_laps: 27,
    },
  ]);
  render(TracksScreen);
  const row = (await screen.findByText("Silverstone National Circuit")).closest("li")!;
  expect(within(row).getByText("no map")).toBeTruthy();
  expect(within(row).getByText("27 laps")).toBeTruthy();   // #66: 3 clouds != 48 clouds
  // The count is ON the action too -- the driver has to see what backs the map (#41).
  const build = within(row).getByRole("button", { name: "Build map from 27 laps" });
  await fireEvent.click(build);
  // And it routes through the map editor's EXISTING rebuild-from-saved-capture path
  // (POST /mapcapture/rebuild -> /mapcapture/save), not a second track-file writer.
  expect(await screen.findByRole("button", { name: /rebuild from saved capture/i })).toBeTruthy();
});

test("a mapped track renders as before, with no build action", async () => {
  vi.spyOn(api, "listTracks").mockResolvedValue([
    {
      slug: "algarve_international_circuit", name: "Algarve", has_map: true,
      has_notes: false, user_edited: false, captures: 35, live_laps: 35,
    },
  ]);
  render(TracksScreen);
  const row = (await screen.findByText("Algarve")).closest("li")!;
  expect(within(row).getByText("map")).toBeTruthy();
  expect(within(row).getByText("no notes")).toBeTruthy();
  expect(within(row).getByText("shipped")).toBeTruthy();
  expect(within(row).getByText("35 laps")).toBeTruthy();
  expect(within(row).queryByRole("button", { name: /build map/i })).toBeNull();
});

test("a track with laps but no clouds gets no build action", async () => {
  // Spa: 991 result-log laps, zero point clouds. The server already refuses to count
  // those, so the row arrives with captures 0 -- and must not offer a build that
  // would 422 on a capture that does not exist.
  vi.spyOn(api, "listTracks").mockResolvedValue([
    {
      slug: "circuit_de_spa_francorchamps", name: "Spa", has_map: false,
      has_notes: false, user_edited: false, captures: 0, live_laps: 0,
    },
  ]);
  render(TracksScreen);
  const row = (await screen.findByText("Spa")).closest("li")!;
  expect(within(row).getByText("no map")).toBeTruthy();
  expect(within(row).queryByText(/laps?$/)).toBeNull();
  expect(within(row).queryByRole("button", { name: /build map/i })).toBeNull();
});

test("live laps that carry no position data explain the row and offer no build", async () => {
  // Sebring: 15 live laps, every one recorded before position capture existed, so none
  // can ever be rebuilt from. The row must SAY that -- hiding it would leave the driver
  // thinking the track is covered when it needs re-driving (#66) -- and must not offer a
  // build that walks straight back into the 422 this feature exists to remove.
  vi.spyOn(api, "listTracks").mockResolvedValue([
    {
      slug: "sebring_international_raceway", name: "Sebring", has_map: false,
      has_notes: false, user_edited: false, captures: 0, live_laps: 15,
    },
  ]);
  render(TracksScreen);
  const row = (await screen.findByText("Sebring")).closest("li")!;
  expect(within(row).getByText("15 laps, none with position data")).toBeTruthy();
  expect(within(row).queryByRole("button", { name: /build map/i })).toBeNull();
});

test("a buildable track counts usable laps, not rows, and carries no elevation warning", async () => {
  // The badge is the build's own count (17 usable of 27 rows). And no elevation-lap
  // warning at any count: width/banking are now gated server-side on the cloud's real
  // lateral spread, and the old "< 2 elevation laps" warning fired where they were
  // closest while staying silent at the 3-4 laps a driver really brings.
  vi.spyOn(api, "listTracks").mockResolvedValue([
    {
      slug: "silverstone_national_circuit", name: "Silverstone", has_map: false,
      has_notes: false, user_edited: false, captures: 17, live_laps: 27,
    },
  ]);
  render(TracksScreen);
  const row = (await screen.findByText("Silverstone")).closest("li")!;
  expect(within(row).getByText("17 laps")).toBeTruthy();   // usable, not the 27 rows
  expect(within(row).getByRole("button", { name: "Build map from 17 laps" })).toBeTruthy();
  expect(within(row).queryByText(/elevation/i)).toBeNull();
});

test("an unreadable saved capture is named, not blamed on laps that have position data", async () => {
  // #84: a corrupt captures/<slug>.npz arrives as captures 0 beside 17 good live laps. The
  // badge used to read "17 laps, none with position data" -- false. It must name the
  // capture instead, and still offer no build (the server refuses to fall back).
  vi.spyOn(api, "listTracks").mockResolvedValue([
    {
      slug: "fuji_speedway", name: "Fuji", has_map: false, has_notes: false,
      user_edited: false, captures: 0, live_laps: 17, capture_unreadable: true,
    },
  ]);
  render(TracksScreen);
  const row = (await screen.findByText("Fuji")).closest("li")!;
  expect(within(row).getByText("saved capture unreadable, 17 laps driven")).toBeTruthy();
  expect(within(row).queryByText(/position data/)).toBeNull();
  expect(within(row).queryByRole("button", { name: /build map/i })).toBeNull();
});

test("an unreadable saved capture with no live laps says only that", async () => {
  // Nothing driven to count: exactly the capture state, never ", 0 laps driven".
  vi.spyOn(api, "listTracks").mockResolvedValue([
    {
      slug: "fuji_speedway", name: "Fuji", has_map: true, has_notes: false,
      user_edited: false, captures: 0, live_laps: 0, capture_unreadable: true,
    },
  ]);
  render(TracksScreen);
  const row = (await screen.findByText("Fuji")).closest("li")!;
  // findByText, not getByText: the previous test's now-cached Fuji row (#105) renders
  // synchronously first, so this must wait for the fresh fetch to replace it.
  expect(await within(row).findByText("saved capture unreadable")).toBeTruthy();   // exact text
});
