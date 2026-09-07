<script lang="ts">
  import { withRetry, getSessionLaps } from "./api";
  import TrackMap from "./TrackMap.svelte";
  import TrackMapEditor from "./TrackMapEditor.svelte";

  let { sessionId }: { sessionId: string | undefined } = $props();

  let lapId: number | null = $state(null);
  let trackName: string | null = $state(null);
  let editing = $state(false);
  let editorMode: "view" | "edit" = $state("view"); // 🔍 opens view; "Generate" opens edit
  let mapNonce = $state(0); // bump to force TrackMap to re-fetch after an edit save

  // The map is canonical per-track: any lap of the selected session resolves it.
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
</script>

<section class="map-pane">
  <header>
    <div class="ttl-text">
      <h2>Track map</h2>
      {#if trackName}<span class="tname">{trackName}</span>{/if}
    </div>
    <div class="acts">
      {#if lapId !== null}
        <!-- Magnifier: opens the map enlarged in a read-only VIEW modal (a pencil in
             the modal reveals the edit tools). -->
        <button type="button" class="icon" aria-label="Enlarge track map" title="View map"
                onclick={() => { editorMode = "view"; editing = true; }}>
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg>
        </button>
      {:else}
        <!-- No stored lap: open the capture flow directly (uses the LIVE track). -->
        <button type="button" onclick={() => { editorMode = "edit"; editing = true; }}>Generate map</button>
      {/if}
    </div>
  </header>
  <div class="map-body">
    {#if lapId !== null}
      {#key mapNonce}
        <TrackMap {lapId} />
      {/key}
    {:else}
      <p class="empty">No map yet — “Generate map” records the live track and builds one.</p>
    {/if}
  </div>
  <TrackMapEditor
    {lapId}
    open={editing}
    startMode={editorMode}
    onClose={() => (editing = false)}
    onSaved={() => (mapNonce += 1)}
  />
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
  .empty {
    color: var(--faint);
    font-size: 13px;
    text-align: center;
    padding: 16px 12px;
  }
</style>
