import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup, waitFor } from "@testing-library/svelte";
import { getLapInputs } from "./api";
import InputsPanel from "./InputsPanel.svelte";

vi.mock("./api", async () => {
  const actual = await vi.importActual<typeof import("./api")>("./api");
  return { ...actual, getLapInputs: vi.fn() };
});

const flat = (v: number[]) => ({ lo: [...v], hi: [...v], mean: [...v] });
const LAP = {
  lap_id: 9, lap_time: 216.0, length: 1000, dist: [0, 500, 1000],
  throttle: flat([1, 0, 1]), brake: flat([0, 1, 0]), steering: flat([0, 0, 0]),
};

describe("InputsPanel", () => {
  beforeEach(() => {
    vi.mocked(getLapInputs).mockReset();
    localStorage.clear();
  });

  afterEach(cleanup);

  it("shows a select-a-lap message when lapId is null", () => {
    render(InputsPanel, { props: { lapId: null } });
    expect(screen.getByText(/select a lap/i)).toBeTruthy();
    expect(getLapInputs).not.toHaveBeenCalled();
  });

  it("fetches inputs for the given lap id", async () => {
    vi.mocked(getLapInputs).mockResolvedValue({ lap: LAP, best: null, corners: [], length: 1000 });
    render(InputsPanel, { props: { lapId: 9 } });
    await waitFor(() => expect(getLapInputs).toHaveBeenCalledWith(9));
  });

  it("shows an empty message when the lap has no telemetry channels", async () => {
    vi.mocked(getLapInputs).mockResolvedValue({ lap: {}, best: null, corners: [] });
    render(InputsPanel, { props: { lapId: 9 } });
    expect(await screen.findByText(/no telemetry/i)).toBeTruthy();
  });

  it("shows a one-line error message instead of throwing when the fetch rejects", async () => {
    vi.mocked(getLapInputs).mockRejectedValue(new Error("API request failed: GET /laps/9/inputs -> 404"));
    render(InputsPanel, { props: { lapId: 9 } });
    expect(await screen.findByText(/couldn.t load/i)).toBeTruthy();
  });

  it("toggles collapsed state and persists it to localStorage", async () => {
    vi.mocked(getLapInputs).mockResolvedValue({ lap: LAP, best: null, corners: [] });
    render(InputsPanel, { props: { lapId: 9 } });
    await screen.findByText(/inputs/i);

    const toggle = screen.getByRole("button", { name: /collapse|expand/i });
    await fireEvent.click(toggle);

    expect(localStorage.getItem("inputsPanel.collapsed")).toBe("true");

    await fireEvent.click(toggle);
    expect(localStorage.getItem("inputsPanel.collapsed")).toBe("false");
  });

  it("starts collapsed when localStorage says so", async () => {
    localStorage.setItem("inputsPanel.collapsed", "true");
    vi.mocked(getLapInputs).mockResolvedValue({ lap: LAP, best: null, corners: [] });
    const { container } = render(InputsPanel, { props: { lapId: 9 } });
    await screen.findByText(/inputs/i);
    expect(container.querySelector('[data-testid="inputs-panel-body"]')).toBeNull();
  });
});
