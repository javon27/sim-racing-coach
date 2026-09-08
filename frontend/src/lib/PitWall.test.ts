import { describe, it, expect, vi, afterEach } from "vitest";
import { render, waitFor, cleanup } from "@testing-library/svelte";

// Stub the network dep before importing the component.
vi.mock("./api", () => ({
  getCurrentMap: vi.fn(async () => ({})),
}));

import PitWall from "./PitWall.svelte";
import { liveField } from "./live";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("PitWall", () => {
  it("shows the waiting state when not live", async () => {
    const { getByText } = render(PitWall);
    await waitFor(() => expect(getByText(/waiting for lmu/i)).toBeTruthy());
  });

  it("does NOT stop the shared live field on destroy (it streams app-wide)", async () => {
    const stop = vi.spyOn(liveField, "stop");
    const { unmount } = render(PitWall);
    unmount();
    expect(stop).not.toHaveBeenCalled();
  });
});
