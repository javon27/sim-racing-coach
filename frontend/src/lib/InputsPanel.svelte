<!-- frontend/src/lib/InputsPanel.svelte
     The coaching screen's primary visual (#95): selected lap vs its track/car
     best, driver-input lines + corner markers, full width above the chat.
     Collapsible (remembered in localStorage), expanded by default. Its height
     is owned by App (round 2): the chat bar directly below doubles as the
     drag handle, so App passes `height` down as a prop instead of this panel
     tracking its own. -->
<script lang="ts">
  import InputTraces from "./InputTraces.svelte";
  import { getLapInputs, type LapInputs } from "./api";

  // height: driven by App (lifted so the chat bar below can double as the drag
  // handle, #95 round 2); defaults to the same 220px round 1 used.
  let { lapId, height = 220 }: { lapId: number | null; height?: number } = $props();

  const STORAGE_KEY = "inputsPanel.collapsed";

  function loadCollapsed(): boolean {
    try {
      return localStorage.getItem(STORAGE_KEY) === "true";
    } catch {
      return false;
    }
  }
  function saveCollapsed(v: boolean) {
    try {
      localStorage.setItem(STORAGE_KEY, String(v));
    } catch {
      /* private mode / storage disabled — collapse just won't persist */
    }
  }

  let collapsed = $state(loadCollapsed());
  let data: LapInputs | null = $state(null);
  let phase: "empty" | "loading" | "ok" | "error" = $state("empty");

  function toggle() {
    collapsed = !collapsed;
    saveCollapsed(collapsed);
  }

  $effect(() => {
    const id = lapId;
    if (id === null) {
      phase = "empty";
      data = null;
      return;
    }
    phase = "loading";
    getLapInputs(id)
      .then((res) => {
        if (id !== lapId) return; // stale response, a newer lap was selected meanwhile
        data = res;
        phase = "ok";
      })
      .catch(() => {
        if (id !== lapId) return;
        data = null;
        phase = "error";
      });
  });

  const hasChannels = $derived(!!data?.lap && "dist" in data.lap && (data.lap.dist?.length ?? 0) > 0);
  const showingTraces = $derived((phase as string) === "ok" && hasChannels);
</script>

<div class="panel" style={showingTraces ? `--inputs-h: ${height}px` : undefined}>
  <div class="hdr">
    <h2>Inputs — lap vs best</h2>
    <button
      type="button"
      class="collapse-btn"
      aria-label={collapsed ? "Expand inputs panel" : "Collapse inputs panel"}
      aria-expanded={!collapsed}
      onclick={toggle}
    >{collapsed ? "▸" : "▾"}</button>
  </div>
  {#if !collapsed}
    <div class="body" class:sized={showingTraces} data-testid="inputs-panel-body">
      {#if phase === "empty"}
        <p class="empty">Select a lap to see its inputs.</p>
      {:else if phase === "loading"}
        <p class="empty">Loading…</p>
      {:else if phase === "error"}
        <p class="empty">Couldn't load inputs for this lap.</p>
      {:else if !hasChannels}
        <p class="empty">No telemetry for this lap.</p>
      {:else}
        <InputTraces mode="line" traces={data!.lap} best={data!.best} corners={data!.corners} />
      {/if}
    </div>
  {/if}
</div>

<style>
  .panel {
    display: flex;
    flex-direction: column;
    flex: 0 0 auto;
    border-bottom: 1px solid var(--line);
  }
  .hdr {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 8px 20px;
  }
  .hdr h2 {
    margin: 0;
    font-size: 10px;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--faint);
    font-weight: 700;
  }
  .collapse-btn {
    background: none;
    border: none;
    color: var(--faint);
    cursor: pointer;
    font-size: 12px;
    padding: 2px 6px;
  }
  .body {
    padding: 0 20px 12px;
  }
  .body.sized {
    height: var(--inputs-h, 220px);
  }
  .empty {
    margin: 0;
    padding: 14px 0;
    text-align: center;
    color: var(--faint);
    font-size: 12px;
  }
</style>
