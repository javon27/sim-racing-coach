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

  it("empty state has no fixed --inputs-h height", () => {
    const { container } = render(InputsPanel, { props: { lapId: null } });
    const panel = container.querySelector(".panel") as HTMLElement;
    expect(panel.style.getPropertyValue("--inputs-h")).toBe("");
  });

  it("applies --inputs-h only once a lap's traces are showing", async () => {
    vi.mocked(getLapInputs).mockResolvedValue({ lap: LAP, best: null, corners: [] });
    const { container } = render(InputsPanel, { props: { lapId: 9 } });
    const panel = container.querySelector(".panel") as HTMLElement;
    await waitFor(() => expect(panel.style.getPropertyValue("--inputs-h")).toBe("220px"));
  });

  it("collapse hides the body and flips aria-expanded", async () => {
    vi.mocked(getLapInputs).mockResolvedValue({ lap: LAP, best: null, corners: [] });
    const { container } = render(InputsPanel, { props: { lapId: 9 } });
    await screen.findByText(/inputs/i);

    const toggle = screen.getByRole("button", { name: /collapse|expand/i });
    expect(toggle.getAttribute("aria-expanded")).toBe("true");

    await fireEvent.click(toggle);
    expect(container.querySelector('[data-testid="inputs-panel-body"]')).toBeNull();
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
  });

  it("applies the height prop to --inputs-h (height now lives in App, #95 round 2)", async () => {
    vi.mocked(getLapInputs).mockResolvedValue({ lap: LAP, best: null, corners: [] });
    const { container } = render(InputsPanel, { props: { lapId: 9, height: 340 } });
    const panel = container.querySelector(".panel") as HTMLElement;
    await waitFor(() => expect(panel.style.getPropertyValue("--inputs-h")).toBe("340px"));
  });

  it("no longer renders its own drag handle (the chat bar is the handle now)", async () => {
    vi.mocked(getLapInputs).mockResolvedValue({ lap: LAP, best: null, corners: [] });
    const { container } = render(InputsPanel, { props: { lapId: 9 } });
    await waitFor(() => expect(container.querySelector(".panel")).toBeTruthy());
    expect(container.querySelector(".rail-resizer")).toBeNull();
  });

  it("clicking anywhere on the header row toggles collapse, not just the button", async () => {
    vi.mocked(getLapInputs).mockResolvedValue({ lap: LAP, best: null, corners: [] });
    const { container } = render(InputsPanel, { props: { lapId: 9 } });
    await screen.findByText(/inputs/i);

    const hdr = container.querySelector(".hdr h2") as HTMLElement;
    await fireEvent.click(hdr);

    expect(container.querySelector('[data-testid="inputs-panel-body"]')).toBeNull();
    expect(localStorage.getItem("inputsPanel.collapsed")).toBe("true");
  });

  it("clicking the collapse button toggles exactly once (no double-toggle from the header handler)", async () => {
    vi.mocked(getLapInputs).mockResolvedValue({ lap: LAP, best: null, corners: [] });
    const { container } = render(InputsPanel, { props: { lapId: 9 } });
    await screen.findByText(/inputs/i);

    const toggle = screen.getByRole("button", { name: /collapse|expand/i });
    await fireEvent.click(toggle);

    expect(container.querySelector('[data-testid="inputs-panel-body"]')).toBeNull();
    expect(localStorage.getItem("inputsPanel.collapsed")).toBe("true");
  });
});
