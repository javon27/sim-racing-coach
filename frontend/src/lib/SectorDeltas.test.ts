import { describe, it, expect, afterEach } from "vitest";
import { render, cleanup } from "@testing-library/svelte";
import SectorDeltas from "./SectorDeltas.svelte";
import type { SessionState } from "./live";

function session(over: Partial<SessionState> = {}): SessionState {
  return { live: true, phase: "race", current_lap: 9, max_laps: 24, elapsed: 100,
    end_time: 0, cur_s1: null, cur_s2: null, best_s1: null, best_s2: null,
    last_lap: null, best_lap: null, ...over };
}

describe("SectorDeltas", () => {
  afterEach(() => cleanup());

  it("shows a green S1 when the current sector beats best", () => {
    const { getByTestId } = render(SectorDeltas, {
      props: { session: session({ cur_s1: 29.5, best_s1: 29.8 }) },
    });
    const s1 = getByTestId("sec-s1");
    expect(s1.textContent).toContain("-0.3");
    expect(s1.className).toContain("good");
  });

  it("shows a red total when the last lap is slower than best", () => {
    const { getByTestId } = render(SectorDeltas, {
      props: { session: session({ last_lap: 92.5, best_lap: 91.9 }) },
    });
    const tot = getByTestId("sec-total");
    expect(tot.textContent).toContain("+0.6");
    expect(tot.className).toContain("bad");
  });

  it("dashes cells with no data", () => {
    const { getByTestId } = render(SectorDeltas, { props: { session: session() } });
    expect(getByTestId("sec-s1").textContent).toContain("—");
  });
});
