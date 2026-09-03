<script lang="ts">
  import { onMount } from "svelte";
  import { getSessions, getSessionLaps, type LapRow } from "./api";

  let { onselect }: { onselect?: (detail: { lap_id: number }) => void } = $props();

  let laps: LapRow[] = $state([]);
  let bestId: number | null = $state(null);

  onMount(async () => {
    try {
      const sessions = await getSessions();
      if (sessions.length === 0) return;
      const latest = [...sessions].sort((a, b) => b.last - a.last)[0];
      laps = await getSessionLaps(latest.session_id);
      bestId = laps.reduce<LapRow | null>(
        (best, lap) => (best === null || lap.lap_time < best.lap_time ? lap : best),
        null,
      )?.id ?? null;
    } catch {
      laps = [];
    }
  });

  function formatTime(seconds: number): string {
    const m = Math.floor(seconds / 60);
    const s = (seconds - m * 60).toFixed(3);
    return `${m}:${s.padStart(6, "0")}`;
  }

  function select(lapId: number) {
    onselect?.({ lap_id: lapId });
  }
</script>

<aside class="rail">
  <div class="rail-head">
    <h2>This session</h2>
    <span>{laps.length} laps</span>
  </div>
  <div class="laps">
    {#if laps.length === 0}
      <p class="empty">No laps yet</p>
    {:else}
      {#each laps as lap (lap.id)}
        <button type="button" class="lap" class:best={lap.id === bestId} onclick={() => select(lap.id)}>
          <span class="n">Lap {lap.id}</span>
          <span class="t">{formatTime(lap.lap_time)}</span>
          <span class="flag">{lap.valid ? "" : "✗ inv"}</span>
        </button>
      {/each}
    {/if}
  </div>
</aside>
