import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, waitFor } from "@testing-library/svelte";

// Stub the network + WS deps before importing the component.
vi.mock("./api", () => ({
  getStatus: vi.fn(async () => ({ track: "Le Mans", car: "Corvette", car_model: null,
    on_track: true, activity: "track", has_map: true, lmu_connected: true,
    session_id: "s", lap_id: 42 })),
  getSettings: vi.fn(async () => ({ units: "metric" })),
  withRetry: (fn: () => unknown) => fn(),
  getLapMap: vi.fn(async () => ({})),
}));

import PitWall from "./PitWall.svelte";
import { LiveField } from "./live";

beforeEach(() => {
  vi.spyOn(LiveField.prototype, "start").mockImplementation(function (this: LiveField) {});
  vi.spyOn(LiveField.prototype, "stop").mockImplementation(function (this: LiveField) {});
});
afterEach(() => vi.restoreAllMocks());

describe("PitWall", () => {
  it("shows the waiting state when not live", async () => {
    const { getByText } = render(PitWall);
    await waitFor(() => expect(getByText(/waiting for lmu/i)).toBeTruthy());
  });
});
