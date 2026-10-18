<!-- frontend/src/lib/InputsPanel.svelte
     The coaching screen's primary visual (#95): selected lap vs its track/car
     best, driver-input lines + corner markers, full width above the chat.
     Collapsible (remembered in localStorage), expanded by default. It sizes to
     the traces it draws -- no fixed height, no drag handle. -->
<script lang="ts">
  import InputTraces from "./InputTraces.svelte";
  import { getLapInputs, type LapInputs } from "./api";

  let { lapId }: { lapId: number | null } = $props();

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

  // Whole header row toggles too; the button click already handles it and
  // bubbles up here, so ignore clicks that originated on the button itself
  // (otherwise it would toggle twice and cancel out).
  function onHdrClick(e: MouseEvent) {
    if ((e.target as HTMLElement).closest(".collapse-btn")) return;
    toggle();
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

<div class="panel">
  <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions
       (mouse-only convenience; the real button below already covers keyboard/a11y) -->
  <div class="hdr" onclick={onHdrClick}>
    <h2>Inputs — lap vs best</h2>
    {#if showingTraces}
      <ul class="trace-legend" aria-hidden="true">
        <li><span class="sw selected"></span>selected lap</li>
        <li><span class="sw best"></span>best</li>
      </ul>
    {/if}
    <button
      type="button"
      class="collapse-btn"
      class:collapsed
      aria-label={collapsed ? "Expand inputs panel" : "Collapse inputs panel"}
      aria-expanded={!collapsed}
      onclick={toggle}
    >
      <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor"
           stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <polyline points="6 9 12 15 18 9" />
      </svg>
    </button>
  </div>
  {#if !collapsed}
    <div class="body" data-testid="inputs-panel-body">
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
    gap: 10px;
    padding: 8px 20px;
    cursor: pointer;
  }
  .hdr h2 {
    margin: 0;
    font-size: 10px;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--faint);
    font-weight: 700;
  }
  .trace-legend {
    display: flex;
    gap: 10px;
    margin: 0;
    padding: 0;
    list-style: none;
    font-size: 10px;
    font-family: var(--mono);
    color: var(--faint);
  }
  .trace-legend li {
    display: flex;
    align-items: center;
    gap: 5px;
  }
  .trace-legend .sw {
    width: 14px;
    height: 2px;
    border-radius: 1px;
  }
  .trace-legend .sw.selected {
    background: var(--accent);
  }
  .trace-legend .sw.best {
    border-top: 2px dashed var(--trace-best);
    height: 0;
  }
  .collapse-btn {
    display: grid;
    place-items: center;
    width: 24px;
    height: 24px;
    margin-left: auto;
    background: none;
    border: 1px solid transparent;
    border-radius: 6px;
    color: var(--faint);
    cursor: pointer;
    padding: 0;
  }
  .collapse-btn:hover {
    color: var(--text);
    border-color: var(--line);
  }
  .collapse-btn svg {
    transition: transform 0.15s;
  }
  .collapse-btn.collapsed svg {
    transform: rotate(-90deg);
  }
  .body {
    padding: 0 20px 12px;
  }
  .empty {
    margin: 0;
    padding: 14px 0;
    text-align: center;
    color: var(--faint);
    font-size: 12px;
  }
</style>
