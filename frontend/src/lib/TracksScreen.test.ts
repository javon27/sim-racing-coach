import { render, screen, fireEvent, cleanup } from "@testing-library/svelte";
import { test, expect, vi, afterEach } from "vitest";
import TracksScreen from "./TracksScreen.svelte";
import * as api from "./api";

afterEach(cleanup);

vi.spyOn(api, "listTracks").mockResolvedValue([
  { slug: "algarve_international_circuit", name: "Algarve", has_map: true, has_notes: false, user_edited: false },
]);

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
