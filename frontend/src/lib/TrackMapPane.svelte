<script lang="ts">
  import { withRetry, getSessionLaps, type TrackMap as TrackMapData } from "./api";
  import { eventFeed } from "./events";
  import { liveField } from "./live";
  import TrackMap from "./TrackMap.svelte";

  let { sessionId, expanded = false, onexpand }: {
    sessionId: string | undefined;
    // #151: App owns the rail width; this pane only asks for it to change.
    expanded?: boolean;
    onexpand?: () => void;
  } = $props();

  let lapId: number | null = $state(null);
  let trackName: string | null = $state(null);

  // A selected/past session: the map is canonical per-track; any lap of it resolves it.
  $effect(() => {
    const sid = sessionId;
    if (!sid) {
      lapId = null;
      trackName = null;
      return;
    }
    withRetry(() => getSessionLaps(sid))
      .then((laps) => {
        if (sid !== sessionId) return;
        lapId = laps[0]?.id ?? null;
        trackName = laps[0]?.track ?? null;
      })
      .catch(() => {
        if (sid === sessionId) {
          lapId = null;
          trackName = null;
        }
      });
  });

  // No session selected yet (fresh live session, zero completed laps): resolve the
  // map from LIVE track detection instead of waiting for a lap.
  //
  // #106: this was a 2 s getStatus() poll plus a getCurrentMap() fetch guarded by a
  // track/car key (#98, so a ~130 KB map wasn't re-pulled every tick). The sidecar
  // now owns both: status streams, and a map frame is sent only when the track/car
  // it belongs to changes — so the zoom/pan-preserving "same object" property is
  // kept by the feed, not by a local key.
  let feedStatus = $state(eventFeed.status.value);
  let feedMap = $state(eventFeed.map.value);
  $effect(() => eventFeed.status.subscribe((v) => (feedStatus = v)));
  $effect(() => eventFeed.map.subscribe((v) => (feedMap = v)));

  // #151: on track the rail map is LIVE. The shared /live feed is started once in
  // App; we only subscribe. Its `live` flag gates the mode because the same frame
  // carries the player position and the server-resolved next corner.
  let live = $state(liveField.snapshot);
  $effect(() => liveField.subscribe((v) => { live = v.snapshot; }));
  const onTrack = $derived(live?.live === true);
  const player = $derived(onTrack ? (live?.player ?? null) : null);
  // ONLY the player's car: the field is the game HUD's job, not the rail's.
  const liveCars = $derived(
    player ? [{ id: 0, lapDist: player.lap_dist, cls: player.cls, isPlayer: true }] : [],
  );
  const nextCorner = $derived(onTrack ? (live?.next_corner ?? null) : null);
  const DIR: Record<string, string> = { left: "L", right: "R" };

  const liveTrack = $derived(feedStatus?.track ?? null);
  const liveMap = $derived.by<TrackMapData | null>(() => {
    const m = feedMap;
    return m && "path" in m && m.path.length > 0 ? m : null;
  });

  const displayTrack = $derived(onTrack || !sessionId ? liveTrack : trackName);
</script>

<section class="map-pane">
  <header>
    <div class="ttl-text">
      <h2>Track map</h2>
      {#if displayTrack}<span class="tname">{displayTrack}</span>{/if}
    </div>
    <div class="acts">
      <!-- #151: expand WIDENS the rail (pushing the chat narrower) — replaces the
           🔍 overlay, which covered the chat. -->
      <button type="button" class="icon" aria-pressed={expanded}
              aria-label={expanded ? "Collapse track map" : "Expand track map"}
              title={expanded ? "Collapse map" : "Expand map"}
              onclick={() => onexpand?.()}>
        {#if expanded}
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 14h6v6M20 10h-6V4M14 10l7-7M3 21l7-7"/></svg>
        {:else}
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 3h6v6M9 21H3v-6M21 3l-7 7M3 21l7-7"/></svg>
        {/if}
      </button>
    </div>
  </header>
  <div class="map-body">
    {#if onTrack}
      <!-- #151: live — the live track's canonical map with only the player's car. -->
      <TrackMap mapData={liveMap} cars={liveCars} fill={expanded} />
    {:else if lapId !== null}
      <TrackMap {lapId} fill={expanded} />
    {:else if !sessionId && liveTrack}
      <!-- Live session, no completed lap yet: the track is detected, so draw its
           canonical map directly (mapData bypasses the lapId fetch) instead of
           waiting on a lap. TrackMap's own "none" phase (mapData === null) covers
           "track detected, no map built yet" with its own build-me hint. -->
      <TrackMap mapData={liveMap} fill={expanded} />
    {:else}
      <p class="empty">No map yet — build one in Track view (Tracks → Edit).</p>
    {/if}
  </div>
  {#if nextCorner}
    <p class="next-corner">Next: T{nextCorner.label}{nextCorner.name ? ` ${nextCorner.name}` : ""} ({DIR[nextCorner.direction] ?? nextCorner.direction})</p>
  {/if}
</section>

<style>
  /* Stacked under the corner/setup pane in the left rail — a full-width block, not a
     side column. The map body's height is set by the drag handle above it
     (--map-h on the rail); drag it small to tuck the map away. */
  .map-pane {
    border-top: 1px solid var(--line);
    background: var(--surface);
    display: flex;
    flex-direction: column;
    min-height: 0;
  }
  .map-body {
    height: var(--map-h, 200px);
    overflow: auto;
  }
  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 6px;
    padding: 10px 12px;
  }
  .ttl-text {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 1px;
    min-width: 0;
  }
  header h2 {
    margin: 0;
    font-family: var(--display, inherit);
    font-size: 12px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.12em;
  }
  .tname {
    font-family: var(--body);
    font-size: 12px;
    font-weight: 600;
    color: var(--text);
    text-transform: none;
    letter-spacing: 0;
    max-width: 220px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .acts {
    display: flex;
    gap: 6px;
    align-items: center;
  }
  .acts button {
    font: inherit;
    font-size: 12px;
    background: var(--surface-2);
    color: var(--text);
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 6px 12px;
    cursor: pointer;
  }
  .icon {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    padding: 6px !important;
    line-height: 0;
    color: var(--muted);
    background: none !important;
    border: none !important;
  }
  .icon:hover {
    color: var(--text);
  }
  .next-corner {
    margin: 0;
    padding: 6px 12px 10px;
    font-family: var(--mono, monospace);
    font-size: 13px;
    font-weight: 700;
    color: var(--accent);
  }
  .empty {
    color: var(--faint);
    font-size: 13px;
    text-align: center;
    padding: 16px 12px;
  }
</style>
