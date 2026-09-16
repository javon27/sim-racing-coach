<!-- frontend/src/lib/PitWall.svelte -->
<script lang="ts">
  import { onMount, onDestroy } from "svelte";
  import TrackMap from "./TrackMap.svelte";
  import LiveTower from "./LiveTower.svelte";
  import SectorDeltas from "./SectorDeltas.svelte";
  import InputTraces from "./InputTraces.svelte";
  import { liveField } from "./live";
  import { eventFeed } from "./events";
  import { getInputTraces, type InputTraces as InputTraceData,
           type TrackMap as TrackMapData } from "./api";

  // Shared app-lifetime feed (started in App); we only subscribe — never start/stop
  // it — so it keeps streaming while other views are open.
  let snapshot = $state(liveField.snapshot);
  let trackMap: TrackMapData | null = $state(null);
  let traces: InputTraceData | null = $state(null);
  let tracedLap: number | null | undefined; // undefined = not fetched yet

  const unsub = liveField.subscribe((v) => { snapshot = v.snapshot; });

  // #106: the map was a 2 s getCurrentMap() poll; it now arrives on the shared
  // /events feed. Still only swap when the track actually changes — handing
  // TrackMap a new object resets the user's zoom/pan.
  $effect(() => eventFeed.map.subscribe((m) => {
    const next = m && "path" in m && m.path.length > 0 ? m : null;
    if ((next?.track_name ?? null) !== (trackMap?.track_name ?? null)) trackMap = next;
  }));

  async function loadTraces() {
    try { traces = await getInputTraces(); } catch { /* keep last */ }
  }

  // The input graphs only change when a lap COMPLETES, so refetch on the lap
  // counter rather than on the 2 s map timer — the traces are ~40 KB and redrawing
  // them every 2 s would be pure churn.
  // ponytail: the capture can commit a moment after the counter ticks, so a trace
  // may be one lap behind until the next lap. Poll /status's lap_id if that ever
  // shows at the rig.
  $effect(() => {
    const lap = snapshot?.session.current_lap ?? null;
    if (lap === tracedLap) return;
    tracedLap = lap;
    loadTraces();
  });

  onDestroy(unsub);

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
    <div class="inputs">
      <h2>Driver inputs</h2>
      <InputTraces {traces} />
    </div>
  </div>
  <LiveTower snapshot={snapshot ?? null} />
</div>

<style>
  /* Left: lap/sector strip (sized to content) over the map. Right: live timing. */
  .pit { height: 100%; min-height: 0; display: grid; grid-template-columns: 1fr 380px; }
  .trackview { min-height: 0; display: grid; grid-template-rows: auto 1fr auto; }
  .laplist { padding: 12px 20px; display: flex; flex-direction: column; gap: 10px; }
  .tv-h { display: flex; align-items: center; gap: 12px; }
  .live-dot { font-size: 10px; letter-spacing: .1em; font-weight: 700; color: var(--faint); }
  .live-dot.on { color: var(--bad); }
  .lapc { margin-left: auto; font-size: 12px; color: var(--muted); font-variant-numeric: tabular-nums; }
  .map { min-height: 0; }
  .inputs { padding: 8px 20px 12px; border-top: 1px solid var(--line); }
  .inputs h2 {
    margin: 0 0 6px; font-size: 10px; letter-spacing: .1em; text-transform: uppercase;
    color: var(--faint); font-weight: 700;
  }
  .waiting { display: grid; place-items: center; color: var(--faint); font-size: 13px; text-align: center; }
  @media (max-width: 820px) { .pit { grid-template-columns: 1fr; } }
</style>
