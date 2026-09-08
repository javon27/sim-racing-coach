<!-- frontend/src/lib/PitWall.svelte -->
<script lang="ts">
  import { onMount, onDestroy } from "svelte";
  import TrackMap from "./TrackMap.svelte";
  import LiveTower from "./LiveTower.svelte";
  import SectorDeltas from "./SectorDeltas.svelte";
  import { liveField } from "./live";
  import { getCurrentMap, type TrackMap as TrackMapData } from "./api";

  // Shared app-lifetime feed (started in App); we only subscribe — never start/stop
  // it — so it keeps streaming while other views are open.
  let snapshot = $state(liveField.snapshot);
  let trackMap: TrackMapData | null = $state(null);
  let mapTimer: ReturnType<typeof setInterval> | undefined;

  const unsub = liveField.subscribe((v) => { snapshot = v.snapshot; });

  async function pollMap() {
    try {
      const m = await getCurrentMap();
      const next = "path" in m && m.path.length > 0 ? m : null;
      // Only swap when the track actually changes. Re-assigning an equal map every
      // poll hands TrackMap a new object, which resets the user's zoom/pan.
      if ((next?.track_name ?? null) !== (trackMap?.track_name ?? null)) trackMap = next;
    } catch { /* keep last */ }
  }

  onMount(() => {
    pollMap();
    mapTimer = setInterval(pollMap, 2000);
  });
  onDestroy(() => {
    unsub();
    if (mapTimer) clearInterval(mapTimer);
  });

  const cars = $derived(
    (snapshot?.cars ?? []).map((c) => ({
      id: c.id, lapDist: c.lap_dist, cls: c.cls, isPlayer: c.is_player, pos: c.class_position,
    })),
  );
  const isLive = $derived(snapshot?.session.live ?? false);
  // Session-class best per-sector SPLITS + lap (fastest by anyone in the player's
  // class) — the purple reference shown alongside the player's own times.
  const classBest = $derived.by(() => {
    const cars = snapshot?.cars ?? [];
    const me = cars.find((c) => c.is_player);
    if (!me) return null;
    const min = (a: number | null, b: number | null | undefined) =>
      b != null && b > 0 ? (a == null ? b : Math.min(a, b)) : a;
    let s1: number | null = null, s2: number | null = null, s3: number | null = null, lap: number | null = null;
    for (const c of cars) {
      if (c.cls !== me.cls) continue;
      s1 = min(s1, c.best_s1);
      s2 = min(s2, c.best_s1 != null && c.best_s2 != null ? c.best_s2 - c.best_s1 : null);
      s3 = min(s3, c.best_s2 != null && c.best_lap != null ? c.best_lap - c.best_s2 : null);
      lap = min(lap, c.best_lap);
    }
    return { s1, s2, s3, lap };
  });
  const lapLabel = $derived.by(() => {
    const s = snapshot?.session;
    if (!s?.current_lap) return "";
    return s.max_laps ? `Lap ${s.current_lap} / ${s.max_laps}` : `Lap ${s.current_lap}`;
  });

</script>

<div class="pit">
  <div class="trackview">
    <div class="laplist">
      <div class="tv-h">
        <span class="live-dot" class:on={isLive}>● LIVE</span>
        <span class="lapc">{lapLabel}</span>
      </div>
      {#if isLive}
        <SectorDeltas session={snapshot?.session ?? null} {classBest} />
      {/if}
    </div>
    {#if isLive}
      <div class="map"><TrackMap mapData={trackMap} {cars} fill /></div>
    {:else}
      <div class="waiting">Waiting for LMU — get on track to see the field.</div>
    {/if}
  </div>
  <LiveTower snapshot={snapshot ?? null} />
</div>

<style>
  /* Left: lap/sector strip (sized to content) over the map. Right: live timing. */
  .pit { height: 100%; min-height: 0; display: grid; grid-template-columns: 1fr 380px; }
  .trackview { min-height: 0; display: grid; grid-template-rows: auto 1fr; }
  .laplist { padding: 12px 20px; display: flex; flex-direction: column; gap: 10px; }
  .tv-h { display: flex; align-items: center; gap: 12px; }
  .live-dot { font-size: 10px; letter-spacing: .1em; font-weight: 700; color: var(--faint); }
  .live-dot.on { color: var(--bad); }
  .lapc { margin-left: auto; font-size: 12px; color: var(--muted); font-variant-numeric: tabular-nums; }
  .map { min-height: 0; }
  .waiting { display: grid; place-items: center; color: var(--faint); font-size: 13px; text-align: center; }
  @media (max-width: 820px) { .pit { grid-template-columns: 1fr; } }
</style>
