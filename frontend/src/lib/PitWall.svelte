<!-- frontend/src/lib/PitWall.svelte -->
<script lang="ts">
  import { onMount, onDestroy } from "svelte";
  import SessionBar from "./SessionBar.svelte";
  import TrackMap from "./TrackMap.svelte";
  import LiveTower from "./LiveTower.svelte";
  import SectorDeltas from "./SectorDeltas.svelte";
  import { LiveField } from "./live";
  import { getStatus } from "./api";

  const live = new LiveField();
  let snapshot = $state(live.snapshot);
  let lapId: number | null = $state(null);
  let statusTimer: ReturnType<typeof setInterval> | undefined;

  const unsub = live.subscribe((v) => { snapshot = v.snapshot; });

  async function pollStatus() {
    try { lapId = (await getStatus()).lap_id ?? null; } catch { /* keep last */ }
  }

  onMount(() => {
    live.start();
    pollStatus();
    statusTimer = setInterval(pollStatus, 2000);
  });
  onDestroy(() => {
    unsub();
    live.stop();
    if (statusTimer) clearInterval(statusTimer);
  });

  const cars = $derived(
    (snapshot?.cars ?? []).map((c) => ({ id: c.id, lapDist: c.lap_dist, cls: c.cls, isPlayer: c.is_player })),
  );
  const isLive = $derived(snapshot?.session.live ?? false);
  const lapLabel = $derived.by(() => {
    const s = snapshot?.session;
    if (!s?.current_lap) return "";
    return s.max_laps ? `Lap ${s.current_lap} / ${s.max_laps}` : `Lap ${s.current_lap}`;
  });
</script>

<div class="pitwall">
  <SessionBar />
  <div class="pit">
    <div class="trackview">
      <div class="tv-h">
        <span class="live-dot" class:on={isLive}>● LIVE</span>
        <span class="lapc">{lapLabel}</span>
      </div>
      {#if isLive}
        <SectorDeltas session={snapshot?.session ?? null} />
        <div class="map"><TrackMap {lapId} {cars} /></div>
      {:else}
        <div class="waiting">Waiting for LMU — get on track to see the field.</div>
      {/if}
    </div>
    <LiveTower snapshot={snapshot ?? null} />
  </div>
</div>

<style>
  .pitwall { height: 100%; display: grid; grid-template-rows: auto 1fr; min-height: 0; }
  .pit { display: grid; grid-template-columns: 1fr 380px; min-height: 0; }
  .trackview { display: flex; flex-direction: column; gap: 10px; padding: 16px 20px; min-height: 0; }
  .tv-h { display: flex; align-items: center; gap: 12px; }
  .live-dot { font-size: 10px; letter-spacing: .1em; font-weight: 700; color: var(--faint); }
  .live-dot.on { color: var(--bad); }
  .lapc { margin-left: auto; font-size: 12px; color: var(--muted); font-variant-numeric: tabular-nums; }
  .map { flex: 1; display: grid; place-items: center; min-height: 0; }
  .waiting { flex: 1; display: grid; place-items: center; color: var(--faint); font-size: 13px; text-align: center; }
  @media (max-width: 820px) { .pit { grid-template-columns: 1fr; } }
</style>
