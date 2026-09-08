import { describe, it, expect, afterEach } from "vitest";
import { render, cleanup } from "@testing-library/svelte";
import SectorDeltas from "./SectorDeltas.svelte";
import type { SessionState } from "./live";

function session(over: Partial<SessionState> = {}): SessionState {
  return { live: true, phase: "race", current_lap: 9, max_laps: 24, track_len: 4000,
    elapsed: 100, end_time: 0, cur_s1: null, cur_s2: null, best_s1: null, best_s2: null,
    last_s1: null, last_s2: null, last_lap: null, best_lap: null, ...over };
}

describe("SectorDeltas (MFD timing table)", () => {
  afterEach(() => cleanup());

  it("shows per-sector splits (not cumulative), colored vs best", () => {
    const { getByTestId } = render(SectorDeltas, {
      props: { session: session({ cur_s1: 29.5, best_s1: 29.8, cur_s2: 62.0, best_s2: 61.5 }) },
    });
    expect(getByTestId("cur-s1").textContent).toBe("0:29.500");
    expect(getByTestId("cur-s2").textContent).toBe("0:32.500"); // 62.0 - 29.5 split
    expect(getByTestId("cur-s1").className).toContain("good"); // 29.5 < 29.8
    expect(getByTestId("cur-s2").className).toContain("bad"); // 32.5 > best split 31.7
  });

  it("splits both last and best laps per sector and shows the total lap column", () => {
    const { getByTestId } = render(SectorDeltas, {
      props: { session: session({
        last_lap: 92.5, last_s1: 30.0, last_s2: 62.0,
        best_lap: 91.9, best_s1: 29.8, best_s2: 61.5,
      }) },
    });
    expect(getByTestId("best-s1").textContent).toBe("0:29.800");
    expect(getByTestId("best-s2").textContent).toBe("0:31.700"); // 61.5 - 29.8
    expect(getByTestId("best-s3").textContent).toBe("0:30.400"); // 91.9 - 61.5
    expect(getByTestId("best-total").textContent).toBe("1:31.900");
    expect(getByTestId("last-s1").textContent).toBe("0:30.000");
    expect(getByTestId("last-s2").textContent).toBe("0:32.000"); // 62.0 - 30.0
    expect(getByTestId("last-s3").textContent).toBe("0:30.500"); // 92.5 - 62.0
    expect(getByTestId("last-total").textContent).toBe("1:32.500");
  });

  it("colors last-lap splits vs best (green/red); player best is green without a class best", () => {
    const { getByTestId } = render(SectorDeltas, {
      props: { session: session({
        last_lap: 91.5, last_s1: 29.0, last_s2: 61.0,
        best_lap: 91.9, best_s1: 29.8, best_s2: 61.5,
      }) },
    });
    expect(getByTestId("last-s1").className).toContain("good"); // 29.0 < 29.8
    expect(getByTestId("last-s2").className).toContain("bad"); // 32.0 split > 31.7
    expect(getByTestId("last-total").className).toContain("good"); // 91.5 < 91.9
    // No classBest -> the player's best is a personal best (green), not purple.
    expect(getByTestId("best-s1").className).toContain("good");
    expect(getByTestId("best-s1").className).not.toContain("best");
  });

  it("purple only when the player's best ties the class best; shows the class row", () => {
    const { getByTestId } = render(SectorDeltas, {
      props: {
        session: session({ best_lap: 91.9, best_s1: 29.8, best_s2: 61.5 }),
        // player holds class-best S1 + lap, but someone else has a faster S2 split
        classBest: { s1: 29.8, s2: 31.0, s3: 30.0, lap: 91.9 },
      },
    });
    expect(getByTestId("best-s1").className).toContain("best"); // ties class best -> purple
    expect(getByTestId("best-s2").className).toContain("good"); // 31.7 split > 31.0 -> green
    expect(getByTestId("best-total").className).toContain("best");
    expect(getByTestId("class-s1").textContent).toBe("0:29.800");
    expect(getByTestId("class-total").textContent).toBe("1:31.900");
  });

  it("dashes cells with no data", () => {
    const { getByTestId } = render(SectorDeltas, { props: { session: session() } });
    expect(getByTestId("cur-s1").textContent).toBe("—");
    expect(getByTestId("best-total").textContent).toBe("—");
  });
});
