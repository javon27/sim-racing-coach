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
    sector: 0,
    laps: 8,
    in_pits: false,
    is_player: false,
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
    elapsed: 100,
    end_time: 0,
    cur_s1: null,
    cur_s2: null,
    best_s1: null,
    best_s2: null,
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

  it("windows around the player in deltas mode", async () => {
    localStorage.setItem("pitwall.tower", "deltas");
    const { container } = render(LiveTower, { props: { snapshot: snap } });
    // player is P3, so with 3-each window the whole 5-car field still shows,
    // but the anchored player row must be present and marked.
    expect(container.querySelector(".trow.you")).not.toBeNull();
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
