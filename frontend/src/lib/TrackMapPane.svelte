<script lang="ts">
  import { withRetry, getSessionLaps } from "./api";
  import TrackMap from "./TrackMap.svelte";
  import TrackMapEditor from "./TrackMapEditor.svelte";

  let { sessionId }: { sessionId: string | undefined } = $props();

  const LS_KEY = "trackmap.collapsed";
  let collapsed = $state(readCollapsed());
  let lapId: number | null = $state(null);
  let editing = $state(false);
  let editorMode: "view" | "edit" = $state("view"); // 🔍 opens view; "Generate" opens edit
  let mapNonce = $state(0); // bump to force TrackMap to re-fetch after an edit save

  function readCollapsed(): boolean {
    try {
      return localStorage.getItem(LS_KEY) === "1";
    } catch {
      return false;
    }
  }
  function toggle() {
    collapsed = !collapsed;
    try {
      localStorage.setItem(LS_KEY, collapsed ? "1" : "0");
    } catch {
      /* private mode / blocked storage — collapse still works in-session */
    }
  }

  // The map is canonical per-track: any lap of the selected session resolves it.
  $effect(() => {
    const sid = sessionId;
    if (!sid) {
      lapId = null;
      return;
    }
    withRetry(() => getSessionLaps(sid))
      .then((laps) => {
        if (sid !== sessionId) return;
        lapId = laps[0]?.id ?? null;
      })
      .catch(() => {
        if (sid === sessionId) lapId = null;
      });
  });
</script>

<section class="map-pane" class:collapsed>
  <header>
    <button type="button" class="ttl" aria-expanded={!collapsed}
            aria-label={collapsed ? "Show track map" : "Hide track map"} onclick={toggle}>
      <span class="caret">{collapsed ? "▸" : "▾"}</span>
      <h2>Track map</h2>
    </button>
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
  {#if !collapsed}
    {#if lapId !== null}
      {#key mapNonce}
        <TrackMap {lapId} />
      {/key}
    {:else}
      <p class="empty">No map yet — “Generate map” records the live track and builds one.</p>
    {/if}
  {/if}
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
     side column. Collapsing hides the map body (accordion), header stays visible. */
  .map-pane {
    border-top: 1px solid var(--line);
    background: var(--surface);
    display: flex;
    flex-direction: column;
    min-height: 0;
  }
  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 6px;
    padding: 10px 12px;
  }
  /* Title doubles as the collapse toggle (whole label is the hit target). */
  .ttl {
    display: flex;
    align-items: center;
    gap: 6px;
    background: none;
    border: none;
    padding: 2px;
    cursor: pointer;
    color: inherit;
  }
  .ttl .caret {
    color: var(--faint);
    font-size: 11px;
    width: 12px;
  }
  header h2 {
    margin: 0;
    font-family: var(--display, inherit);
    font-size: 12px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.12em;
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
