<script lang="ts">
  import { getLapAnalysis, type CornerMetrics } from "./api";

  let { lapId }: { lapId: number | null } = $props();

  let corners: CornerMetrics[] = $state([]);
  let phase: "empty" | "loading" | "ok" | "error" = $state("empty");

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

  function formatDelta(delta: number | null): string {
    if (delta === null) return "—";
    const sign = delta > 0 ? "+" : "";
    return `${sign}${delta.toFixed(2)}s`;
  }

  // brake_point/apex_dist are absolute lap-distances; show the brake point the way
  // the coach does — metres BEFORE the apex — not the raw distance from S/F.
  function brakeBeforeApex(c: CornerMetrics): string {
    const d = c.apex_dist - c.brake_point;
    return d > 0 ? `${d.toFixed(0)}m` : "—";
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
          <th>Min</th>
          <th title="metres before apex">Brake</th>
          <th>Trail</th>
          <th>Δ best</th>
        </tr>
      </thead>
      <tbody>
        {#each corners as c (c.corner_no)}
          <tr>
            <td class="cname">
              <span class="dir">{dirArrow(c.direction)}</span>T{c.corner_no} {c.name}
            </td>
            <td>{Number.isFinite(c.min_spd) ? c.min_spd.toFixed(0) : "—"}</td>
            <td title="metres before apex">{brakeBeforeApex(c)}</td>
            <td>{(c.trailbrake_overlap * 100).toFixed(0)}%</td>
            <td
              class="delta"
              class:up={c.min_spd_delta !== null && c.min_spd_delta > 0}
              class:down={c.min_spd_delta !== null && c.min_spd_delta <= 0}
            >
              {formatDelta(c.min_spd_delta)}
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  {/if}
</div>
