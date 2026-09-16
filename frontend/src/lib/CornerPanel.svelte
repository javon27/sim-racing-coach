<script lang="ts">
  import { onMount } from "svelte";
  import { getLapAnalysis, getSettings, type CornerMetrics } from "./api";

  let { lapId }: { lapId: number | null } = $props();

  let corners: CornerMetrics[] = $state([]);
  let phase: "empty" | "loading" | "ok" | "error" = $state("empty");
  let units: "metric" | "imperial" = $state("metric");

  // Analysis speeds are km/h; convert for display + label the unit.
  const spdUnit = $derived(units === "imperial" ? "mph" : "km/h");
  function spd(v: number): number {
    return units === "imperial" ? v * 0.621371 : v;
  }

  onMount(async () => {
    try {
      units = (await getSettings()).units;
    } catch {
      units = "metric";
    }
  });

  $effect(() => {
    const id = lapId;
    if (id === null) {
      phase = "empty";
      corners = [];
      return;
    }
    phase = "loading";
    getLapAnalysis(id)
      .then((res) => {
        if (id !== lapId) return; // stale response, a newer lap was selected meanwhile
        corners = res.corners;
        phase = "ok";
      })
      .catch(() => {
        if (id !== lapId) return;
        corners = [];
        phase = "error";
      });
  });

  function dirArrow(direction: string): string {
    return direction === "left" ? "↰" : "↱";
  }

  // min_spd_delta is a SPEED delta (this lap's min corner speed − your best lap's),
  // in km/h. Positive = you carried MORE speed here = faster. Show it in the display
  // unit with a sign, never a "s" (it isn't a time).
  function formatDelta(delta: number | null): string {
    if (delta === null) return "—";
    const v = spd(delta);
    const sign = v > 0 ? "+" : v < 0 ? "−" : "";
    return `${sign}${Math.abs(v).toFixed(1)}`;
  }

  // brake_point/apex_dist are absolute lap-distances; show the brake point the way
  // the coach does — metres BEFORE the apex — not the raw distance from S/F.
  // null means the driver never braked here, and it MUST be checked before the
  // subtraction: `apex_dist - null` is apex_dist in JS, so guarding on the result
  // being positive would print the apex distance as if it were a brake figure.
  // A negative brake_point is not that case — it is a corner braking across the S/F
  // line, and it yields a correct (larger) metres-before-apex.
  function brakeBeforeApex(c: CornerMetrics): string {
    if (c.brake_point === null || c.brake_point === undefined) return "—";
    return `${(c.apex_dist - c.brake_point).toFixed(0)}m`;
  }
</script>

<div class="panel">
  {#if phase === "empty"}
    <p class="empty">Select a lap to see the corner breakdown</p>
  {:else if phase === "loading"}
    <p class="empty">Loading…</p>
  {:else if phase === "error"}
    <p class="empty">No analysis for this lap</p>
  {:else if corners.length === 0}
    <p class="empty">No corners recorded for this lap</p>
  {:else}
    <table>
      <thead>
        <tr>
          <th>Corner</th>
          <th title="Minimum speed through the corner">Min {spdUnit}</th>
          <th title="Where you get on the brakes, in metres before the apex">Brake</th>
          <th title="Trail-braking: % of the corner you're still on the brakes past turn-in">Trail</th>
          <th title="Your min corner speed vs your best lap ({spdUnit}). + / green = you carried more speed (faster); − / red = slower.">Δ best</th>
        </tr>
      </thead>
      <tbody>
        {#each corners as c (c.corner_no)}
          <tr>
            <td class="cname">
              <span class="dir">{dirArrow(c.direction)}</span>T{c.corner_no} {c.name}
            </td>
            <td>{Number.isFinite(c.min_spd) ? spd(c.min_spd).toFixed(0) : "—"}</td>
            <td title="metres before apex">{brakeBeforeApex(c)}</td>
            <!-- A corner the lap never reached comes back all-null (the sidecar maps
                 NaN to null). `null * 100` is 0 in JS, which would print a measured-
                 looking "0%" for a corner with no telemetry at all (#77). -->
            <td
              >{Number.isFinite(c.trailbrake_overlap)
                ? `${(c.trailbrake_overlap * 100).toFixed(0)}%`
                : "—"}</td
            >
            <td
              class="delta"
              class:down={c.min_spd_delta !== null && c.min_spd_delta > 0}
              class:up={c.min_spd_delta !== null && c.min_spd_delta < 0}
            >
              {formatDelta(c.min_spd_delta)}
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  {/if}
</div>
