<script lang="ts">
  import { getLapMap, type TrackMap } from "./api";

  let { lapId }: { lapId: number | null } = $props();

  let map: TrackMap | null = $state(null);
  let phase: "empty" | "loading" | "none" = $state("empty");

  $effect(() => {
    const id = lapId;
    if (id === null) {
      phase = "empty";
      map = null;
      return;
    }
    phase = "loading";
    getLapMap(id)
      .then((res) => {
        if (id !== lapId) return; // stale response, a newer lap was selected meanwhile
        if ("path" in res && res.path.length > 0) {
          map = res;
          phase = "loading"; // rendered via map presence below
        } else {
          map = null;
          phase = "none";
        }
      })
      .catch(() => {
        if (id !== lapId) return;
        map = null;
        phase = "none";
      });
  });

  const points = $derived(map ? map.path.map(([x, y]) => `${x},${y}`).join(" ") : "");
</script>

<div class="panel">
  {#if lapId === null}
    <p class="empty">Select a lap to see the track map</p>
  {:else if map}
    <svg viewBox="0 0 {map.width} {map.height}" style="width:100%;height:auto">
      <polyline {points} fill="none" stroke="var(--line, var(--muted))" stroke-width="1.5" />
      {#each map.corners as c (c.number)}
        <circle cx={c.x} cy={c.y} r="2" fill="var(--accent)" />
        <text x={c.x + 4} y={c.y - 4} font-size="8" fill="var(--accent)">
          T{c.number}<title>{c.name}</title>
        </text>
      {/each}
    </svg>
  {:else if phase === "none"}
    <p class="empty">No track map for this lap yet (position not captured)</p>
  {:else}
    <p class="empty">Loading…</p>
  {/if}
</div>
