import { describe, it, expect, beforeEach } from "vitest";
import { render } from "@testing-library/svelte";
import LiveTower from "./LiveTower.svelte";
import type { FieldSnapshot, VehicleState } from "./live";

function car(p: number, over: Partial<VehicleState> = {}): VehicleState {
  return {
    id: p,
    place: p,
    class_position: p,
    driver: `D${p}`,
    car: "Car",
    cls: "GT3",
    lap_dist: 0,
    speed: 50,
    sector: 0,
    laps: 8,
    in_pits: false,
    is_player: false,
    best_lap: null,
    last_lap: null,
    best_s1: null,
    best_s2: null,
    gap_leader: p === 1 ? 0 : p,
    laps_behind_leader: 0,
    gap_next: p === 1 ? 0 : 1.2,
    laps_behind_next: 0,
    ...over,
  };
}

const snap: FieldSnapshot = {
  session: {
    live: true,
    phase: "race",
    current_lap: 9,
    max_laps: 24,
    track_len: 4000,
    elapsed: 100,
    end_time: 0,
    cur_s1: null,
    cur_s2: null,
    best_s1: null,
    best_s2: null,
    last_s1: null,
    last_s2: null,
    last_lap: null,
    best_lap: null,
  },
  cars: [car(1), car(2), car(3, { is_player: true }), car(4), car(5)],
};

beforeEach(() => localStorage.clear());

describe("LiveTower", () => {
  it("lists the full field ordered by place in position mode", () => {
    const { container } = render(LiveTower, { props: { snapshot: snap } });
    const rows = container.querySelectorAll(".trow");
    expect(rows.length).toBe(5);
    expect(container.querySelector(".trow.you")).not.toBeNull();
  });

  it("windows around the player in relative mode (migrating the old 'deltas' key)", async () => {
    localStorage.setItem("pitwall.tower", "deltas");
    const { container } = render(LiveTower, { props: { snapshot: snap } });
    // player is P3, so with 3-each window the whole 5-car field still shows,
    // but the anchored player row must be present and marked.
    expect(container.querySelector(".trow.you")).not.toBeNull();
  });

  it("relative mode signs the gap by track position (ahead negative, behind positive)", () => {
    localStorage.setItem("pitwall.tower", "relative");
    const relSnap: FieldSnapshot = {
      ...snap,
      cars: [
        car(1, { lap_dist: 2100 }), // 100m ahead of player -> negative
        car(3, { is_player: true, lap_dist: 2000 }),
        car(2, { lap_dist: 1900 }), // 100m behind player -> positive
      ],
    };
    const { container } = render(LiveTower, { props: { snapshot: relSnap } });
    const rows = [...container.querySelectorAll(".trow")];
    const gaps = rows.map((r) => r.querySelector(".gap")?.textContent?.trim());
    // Ahead sorts above the player, behind below; player reads "—".
    expect(gaps[0]?.startsWith("-")).toBe(true);
    expect(gaps[1]).toBe("—");
    expect(gaps[2]?.startsWith("+")).toBe(true);
  });

  it("position (class scope): class-position numbers, race order, relative-to-player time", () => {
    localStorage.setItem("pitwall.tower.view", "position");
    localStorage.setItem("pitwall.tower.scope", "class");
    const classSnap: FieldSnapshot = {
      ...snap,
      cars: [
        car(1, { cls: "GT3", class_position: 1, place: 1, lap_dist: 2100, laps: 5, speed: 50 }), // 100m ahead
        car(2, { cls: "GT3", class_position: 2, place: 2, is_player: true, lap_dist: 2000, laps: 5, speed: 50 }),
        car(3, { cls: "GT3", class_position: 3, place: 3, lap_dist: 1900, laps: 5, speed: 50 }), // 100m behind
      ],
    };
    const { container } = render(LiveTower, { props: { snapshot: classSnap } });
    const rows = [...container.querySelectorAll(".trow")];
    const pos = rows.map((r) => r.querySelector(".pos")?.textContent?.trim());
    const gaps = rows.map((r) => r.querySelector(".gap")?.textContent?.trim());
    expect(pos).toEqual(["1", "2", "3"]); // class positions, race order
    expect(gaps[0]).toBe("-2.0"); // ahead of player
    expect(gaps[1]).toBe("—"); // the player
    expect(gaps[2]).toBe("+2.0"); // behind player
  });

  it("shows each car's fastest lap, then flashes the last lap after it crosses the line", async () => {
    const mk = (lastLap: number): FieldSnapshot => ({
      ...snap,
      cars: [car(1, { cls: "GT3", best_lap: 92.0, last_lap: lastLap })],
    });
    const { rerender, container } = render(LiveTower, { props: { snapshot: mk(93.0) } });
    const lap = () => container.querySelector(".lap")?.textContent?.trim();
    expect(lap()).toBe("1:32.000"); // fastest (no line-cross yet)
    await rerender({ snapshot: mk(91.5) }); // last_lap changed -> crossed the line
    expect(lap()).toBe("1:31.500"); // flashing the just-completed lap
  });

  it("renders nothing useful when snapshot is null", () => {
    const { container } = render(LiveTower, { props: { snapshot: null } });
    expect(container.querySelectorAll(".trow").length).toBe(0);
  });

  it("places unclassified cars (place <= 0) LAST in position mode", () => {
    // Create a snapshot with an unclassified car (place: 0) plus classified cars
    const unclassifiedCar = car(0, { driver: "Unclassified", is_player: false });
    const snapWithUnclassified: FieldSnapshot = {
      ...snap,
      cars: [car(1), unclassifiedCar, car(2), car(3, { is_player: true })],
    };

    const { container } = render(LiveTower, { props: { snapshot: snapWithUnclassified } });
    const rows = container.querySelectorAll(".trow");
    expect(rows.length).toBe(4);

    // Get position values from the first and last .trow elements
    const firstPos = rows[0].querySelector(".pos")?.textContent;
    const lastPos = rows[rows.length - 1].querySelector(".pos")?.textContent;

    // First row should be P1 (place: 1)
    expect(firstPos).toBe("1");
    // Last row should be the unclassified car (place: 0)
    expect(lastPos).toBe("0");
  });
});
