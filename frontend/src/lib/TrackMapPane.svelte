<script lang="ts">
  import { withRetry, getSessionLaps } from "./api";
  import TrackMap from "./TrackMap.svelte";
  import TrackMapEditor from "./TrackMapEditor.svelte";

  let { sessionId }: { sessionId: string | undefined } = $props();

  const LS_KEY = "trackmap.collapsed";
  let collapsed = $state(readCollapsed());
  let lapId: number | null = $state(null);
  let editing = $state(false);
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
  {#if collapsed}
    <button type="button" class="strip" aria-label="Show track map" onclick={toggle}><span class="cap">‹</span></button>
  {:else}
    <header>
      <h2>Track map</h2>
      <div class="acts">
        {#if lapId !== null}
          <button type="button" onclick={() => (editing = true)}>Edit map</button>
        {/if}
        <button type="button" class="collapse" aria-label="Hide track map" onclick={toggle}>›</button>
      </div>
    </header>
    {#if lapId !== null}
      {#key mapNonce}
        <TrackMap {lapId} />
      {/key}
      <TrackMapEditor
        {lapId}
        open={editing}
        onClose={() => (editing = false)}
        onSaved={() => (mapNonce += 1)}
      />
    {:else}
      <p class="empty">Select a session to see its track map</p>
    {/if}
  {/if}
</section>

<style>
  .map-pane {
    width: 360px;
    border-left: 1px solid var(--line);
    background: var(--surface);
    display: flex;
    flex-direction: column;
    min-height: 0;
    overflow: auto;
  }
  .map-pane.collapsed {
    width: 20px;
    overflow: hidden;
  }
  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 14px 16px 8px;
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
  .strip {
    width: 100%;
    height: 100%;
    display: flex;
    align-items: center;
    justify-content: center;
    border: none;
    background: transparent;
    cursor: pointer;
    padding: 0;
  }
  /* A prominent caret tab on the thin collapsed strip — easy to spot and click. */
  .strip .cap {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 18px;
    height: 48px;
    border-radius: 6px;
    background: var(--surface2, rgba(127, 127, 127, 0.14));
    color: var(--muted);
    font-size: 18px;
    font-weight: 700;
    line-height: 1;
  }
  .strip:hover .cap {
    background: var(--accent-soft, rgba(127, 127, 127, 0.26));
    color: var(--text);
  }
  .empty {
    color: var(--faint);
    font-size: 13px;
    text-align: center;
    padding: 24px 12px;
  }
</style>
