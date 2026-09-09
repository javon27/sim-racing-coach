import { describe, it, expect, afterEach } from "vitest";
import { render, cleanup } from "@testing-library/svelte";
import InputTraces from "./InputTraces.svelte";
import type { InputTrace, InputTraces as Traces } from "./api";

// jsdom does not lay out or paint SVG, so these assert the geometry the component
// COMPUTES — the `d` attribute it writes — not how it looks. Computed styles are
// worthless here (vitest doesn't process app.css, so every CSS var is empty).

function trace(over: Partial<InputTrace> = {}): InputTrace {
  return {
    lap_id: 7,
    lap_time: 92.5,
    length: 1000,
    dist: [0, 500, 1000],
    throttle: [1, 0, 1],
    brake: [0, 1, 0],
    steering: [-1, 0, 1],
    ...over,
  };
}

// Parse "M0.0 0.0 L500.0 46.0" -> [[x, y], ...]
function points(d: string): number[][] {
  return d
    .trim()
    .split(/(?=[ML])/)
    .map((seg) => seg.replace(/^[ML]/, "").trim().split(/\s+/).map(Number));
}

describe("InputTraces (Pit Wall driver-input graphs)", () => {
  afterEach(() => cleanup());

  it("prompts for a lap when there is nothing to draw", () => {
    const { getByTestId } = render(InputTraces, { props: { traces: null } });
    expect(getByTestId("inputs-empty")).toBeTruthy();
  });

  it("maps lap distance to x across the full plot width", () => {
    const { getByTestId } = render(InputTraces, { props: { traces: { last: trace(), best: null } } });
    const pts = points(getByTestId("trace-throttle-last").getAttribute("d")!);
    expect(pts.map((p) => p[0])).toEqual([0, 500, 1000]); // 0 / 500 / 1000 m of a 1000 m lap
  });

  it("maps a 0..1 pedal onto the row: 1 at the top edge, 0 at the bottom", () => {
    const { getByTestId } = render(InputTraces, { props: { traces: { last: trace(), best: null } } });
    const thr = points(getByTestId("trace-throttle-last").getAttribute("d")!);
    // Throttle is the first row (top 0, height 46): full = y 0, released = y 46.
    expect(thr.map((p) => p[1])).toEqual([0, 46, 0]);
    const brk = points(getByTestId("trace-brake-last").getAttribute("d")!);
    // Brake is the second row (top 46 + 4 gap = 50).
    expect(brk.map((p) => p[1])).toEqual([96, 50, 96]);
  });

  it("centres steering on its own row: full lock either way spans the band", () => {
    const { getByTestId } = render(InputTraces, { props: { traces: { last: trace(), best: null } } });
    const st = points(getByTestId("trace-steering-last").getAttribute("d")!);
    // Third row top = 2 * (46 + 4) = 100. -1 -> bottom (146), 0 -> middle (123), +1 -> top (100).
    expect(st.map((p) => p[1])).toEqual([146, 123, 100]);
  });

  it("aligns a shorter best lap on NORMALIZED progress, not raw metres", () => {
    // The best lap measured 4 m shorter; both still start at the S/F line, so the
    // two traces must span the same x range (a raw-metre x would leave a gap).
    const best = trace({ lap_time: 90.0, length: 996, dist: [0, 498, 996] });
    const { getByTestId } = render(InputTraces, { props: { traces: { last: trace(), best } } });
    const a = points(getByTestId("trace-throttle-last").getAttribute("d")!);
    const b = points(getByTestId("trace-throttle-best").getAttribute("d")!);
    expect(b.map((p) => p[0])).toEqual(a.map((p) => p[0]));
  });

  it("omits the best trace when the server sends none (last lap IS the best)", () => {
    const { queryByTestId, getByTestId } = render(InputTraces, {
      props: { traces: { last: trace(), best: null } as Traces },
    });
    expect(queryByTestId("trace-throttle-best")).toBeNull();
    expect(getByTestId("inputs-last-time").textContent).toBe("1:32.500");
  });
});
